"""Adapter for the Codex CLI app-server JSONL interface."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from sanctuary import __version__


class CodexAppServer:
    def __init__(self, *, cwd: str, model: str | None = None, approval_policy: str = "never", sandbox: str = "danger-full-access"):
        self.cwd = cwd
        self.model = model
        self.approval_policy = approval_policy
        self.sandbox = sandbox
        self.process: asyncio.subprocess.Process | None = None
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.next_id = 1
        self.thread_id: str | None = None
        self.active_turn_id: str | None = None
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            "codex",
            "app-server",
            "--listen",
            "stdio://",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )
        self._reader_task = asyncio.create_task(self._read_protocol())
        self._stderr_task = asyncio.create_task(self._copy_stderr())
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "sanctuary",
                    "title": "Sanctuary",
                    "version": __version__,
                }
            },
        )
        await self.notify("initialized", {})

        thread_params: dict[str, Any] = {
            "cwd": self.cwd,
            "approvalPolicy": self.approval_policy,
            "sandbox": self.sandbox,
        }
        if self.model:
            thread_params["model"] = self.model
        response = await self.request("thread/start", thread_params)
        thread = response.get("thread", response)
        self.thread_id = thread.get("id")
        if not self.thread_id:
            raise RuntimeError("Codex app-server thread/start did not return a thread ID")

    async def begin_turn(self, text: str) -> None:
        if not self.thread_id:
            raise RuntimeError("Codex thread has not started")
        response = await self.request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": text}],
            },
        )
        turn = response.get("turn", response)
        self.active_turn_id = turn.get("id")

    async def interrupt(self) -> None:
        if not self.thread_id or not self.active_turn_id:
            return
        await self.request(
            "turn/interrupt",
            {"threadId": self.thread_id, "turnId": self.active_turn_id},
        )

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            response = await asyncio.wait_for(future, timeout=60)
        finally:
            self.pending.pop(request_id, None)
        if "error" in response:
            error = response["error"]
            raise RuntimeError(f"Codex app-server {method} failed: {error}")
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise RuntimeError(f"Codex app-server {method} returned an invalid response")
        return result

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write(self, message: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise RuntimeError("Codex app-server is not running")
        payload = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            self.process.stdin.write(payload)
            await self.process.stdin.drain()

    async def _read_protocol(self) -> None:
        assert self.process and self.process.stdout
        while line := await self.process.stdout.readline():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                print(f"Ignored non-JSON Codex app-server output: {line!r}", file=sys.stderr)
                continue
            request_id = message.get("id")
            if isinstance(request_id, int) and request_id in self.pending:
                future = self.pending[request_id]
                if not future.done():
                    future.set_result(message)
            else:
                await self.events.put(message)
        exit_code = await self.process.wait()
        error = {"method": "sanctuary/process-exit", "params": {"exitCode": exit_code}}
        await self.events.put(error)
        for future in tuple(self.pending.values()):
            if not future.done():
                future.set_exception(RuntimeError("Codex app-server closed its output"))

    async def _copy_stderr(self) -> None:
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            sys.stderr.buffer.write(line)
            sys.stderr.buffer.flush()

    async def close(self) -> None:
        if not self.process or self.process.returncode is not None:
            return
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=5)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
