"""Docker-backed worker lifecycle and in-memory conversation buffers."""

from __future__ import annotations

import asyncio
import json
import queue
import socket
import struct
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import docker
from docker.errors import DockerException, NotFound

from sanctuary.config import Mount, Settings, WorkerProfile
from sanctuary.protocol import decode_message, encode_message


RESERVED_DOCKER_OPTIONS = {
    "image",
    "command",
    "name",
    "working_dir",
    "volumes",
    "stdin_open",
    "tty",
    "detach",
}


class DockerChannel:
    """Bidirectional line protocol over a Docker attach socket."""

    def __init__(self, container: Any):
        self.container = container
        self.response = container.attach_socket(
            params={"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1}
        )
        self.socket: socket.socket = getattr(self.response, "_sock", self.response)
        self.write_lock = threading.Lock()
        self.lines: queue.Queue[bytes | None] = queue.Queue()
        self.diagnostics = bytearray()
        self.closed = False
        self.reader_thread = threading.Thread(target=self._read_frames, daemon=True)
        self.reader_thread.start()

    def _recv_exactly(self, size: int) -> bytes | None:
        data = bytearray()
        while len(data) < size and not self.closed:
            try:
                part = self.socket.recv(size - len(data))
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return None
            if not part:
                return None
            data.extend(part)
        return bytes(data) if len(data) == size else None

    def _read_frames(self) -> None:
        pending = bytearray()
        try:
            while not self.closed:
                header = self._recv_exactly(8)
                if header is None:
                    break
                stream_id = header[0]
                payload_size = struct.unpack(">I", header[4:8])[0]
                payload = self._recv_exactly(payload_size)
                if payload is None:
                    break
                if stream_id == 2:
                    self.diagnostics.extend(payload)
                    if len(self.diagnostics) > 32_768:
                        del self.diagnostics[:-32_768]
                    continue
                if stream_id != 1:
                    continue
                pending.extend(payload)
                while True:
                    newline = pending.find(b"\n")
                    if newline < 0:
                        break
                    self.lines.put(bytes(pending[:newline]))
                    del pending[: newline + 1]
        finally:
            if pending:
                self.lines.put(bytes(pending))
            self.lines.put(None)

    async def send(self, message: dict[str, Any]) -> None:
        payload = encode_message(message)

        def write() -> None:
            with self.write_lock:
                self.socket.sendall(payload)

        await asyncio.to_thread(write)

    async def read_line(self) -> bytes | None:
        return await asyncio.to_thread(self.lines.get)

    def close(self) -> None:
        self.closed = True
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.socket.close()
        except OSError:
            pass


@dataclass
class Worker:
    id: str
    profile: str
    container: Any
    channel: DockerChannel
    status: str = "starting"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    exit_code: int | None = None
    error: str | None = None
    output: str = ""
    poll_cursor: int = 0
    ready: asyncio.Future[None] | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    stopping: bool = False
    monitor_task: asyncio.Task[None] | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "profile": self.profile,
            "status": self.status,
            "created_at": self.created_at,
            "exit_code": self.exit_code,
            "error": self.error,
        }


class Orchestrator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.docker = docker.from_env()
        self.workers: dict[str, Worker] = {}
        self.lock = asyncio.Lock()
        self.closed = False

    def _profile(self, name: str) -> WorkerProfile:
        try:
            return self.settings.profiles[name]
        except KeyError as error:
            raise ValueError(f"Unknown worker profile: {name}") from error

    @staticmethod
    def _volumes(mounts: tuple[Mount, ...] | list[Mount]) -> dict[str, dict[str, str]]:
        volumes: dict[str, dict[str, str]] = {}
        for mount in mounts:
            volumes[mount.source] = mount.docker_volume()
        return volumes

    async def start_worker(
        self,
        profile_name: str,
        prompt: str,
        *,
        mounts: list[dict[str, Any]] | None = None,
        docker_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not prompt.strip():
            raise ValueError("Initial prompt is required")
        profile = self._profile(profile_name)
        async with self.lock:
            if self.closed:
                raise RuntimeError("Orchestrator is shutting down")
            active_count = sum(worker.status in {"starting", "running", "waiting"} for worker in self.workers.values())
            if active_count >= self.settings.max_workers:
                raise ValueError(f"Worker limit reached ({self.settings.max_workers})")

            worker_id = uuid.uuid4().hex[:12]
            worker_mounts = list(profile.mounts)
            if mounts:
                worker_mounts.extend(Mount(**mount) for mount in mounts)
            volumes = self._volumes(worker_mounts)
            options = {**profile.docker_options, **(docker_options or {})}
            reserved = RESERVED_DOCKER_OPTIONS.intersection(options)
            if reserved:
                raise ValueError(f"Docker options cannot override reserved fields: {', '.join(sorted(reserved))}")
            docker_kwargs = {
                "image": profile.image,
                "name": f"sanctuary-{worker_id}",
                "stdin_open": True,
                "tty": False,
                "detach": True,
                "volumes": volumes,
                **options,
            }
            if profile.working_dir:
                docker_kwargs["working_dir"] = profile.working_dir

            container = None
            channel = None
            try:
                container = await asyncio.to_thread(self.docker.containers.create, **docker_kwargs)
                await asyncio.to_thread(container.start)
                channel = DockerChannel(container)
            except (DockerException, OSError, RuntimeError) as error:
                if channel:
                    channel.close()
                if container:
                    try:
                        await asyncio.to_thread(container.remove, force=True)
                    except (DockerException, NotFound):
                        pass
                raise RuntimeError(f"Could not start worker container: {error}") from error

            ready = asyncio.get_running_loop().create_future()
            worker = Worker(
                id=worker_id,
                profile=profile_name,
                container=container,
                channel=channel,
                ready=ready,
            )
            self.workers[worker_id] = worker
            worker.monitor_task = asyncio.create_task(self._monitor(worker))
            await channel.send(
                {
                    "type": "start",
                    "worker_id": worker_id,
                    "prompt": prompt,
                    "working_dir": profile.working_dir,
                    "runtime": profile.runtime,
                }
            )

        try:
            await asyncio.wait_for(ready, timeout=120)
        except TimeoutError as error:
            await self._mark_failed(worker, "Worker runtime did not become ready within 120 seconds")
            raise RuntimeError(worker.error) from error
        return worker.snapshot()

    async def _monitor(self, worker: Worker) -> None:
        wait_task = asyncio.create_task(asyncio.to_thread(worker.container.wait))
        line_task = asyncio.create_task(worker.channel.read_line())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {wait_task, line_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if line_task in done:
                    line = line_task.result()
                    if line is None:
                        break
                    line_task = asyncio.create_task(worker.channel.read_line())
                    try:
                        message = decode_message(line)
                    except (ValueError, json.JSONDecodeError) as error:
                        worker.error = f"Invalid worker protocol output: {error}"
                        continue
                    await self._handle_worker_message(worker, message)
                if wait_task in done:
                    result = wait_task.result()
                    worker.exit_code = result.get("StatusCode") if isinstance(result, dict) else None
                    break
            if wait_task.done() and not worker.stopping:
                # Docker's wait response can race the final attach frames. Drain
                # briefly so the last text deltas and stderr reach the registry.
                for _ in range(100):
                    if not line_task.done():
                        try:
                            await asyncio.wait_for(asyncio.shield(line_task), timeout=0.05)
                        except TimeoutError:
                            break
                    line = line_task.result()
                    if line is None:
                        break
                    line_task = asyncio.create_task(worker.channel.read_line())
                    try:
                        message = decode_message(line)
                    except (ValueError, json.JSONDecodeError) as error:
                        worker.error = f"Invalid worker protocol output: {error}"
                        continue
                    await self._handle_worker_message(worker, message)
            if not wait_task.done():
                try:
                    result = await asyncio.wait_for(wait_task, timeout=10)
                    worker.exit_code = result.get("StatusCode") if isinstance(result, dict) else None
                except TimeoutError:
                    await asyncio.to_thread(worker.container.kill)
                    result = await asyncio.to_thread(worker.container.wait)
                    worker.exit_code = result.get("StatusCode") if isinstance(result, dict) else None
            if worker.stopping:
                return
            await self._mark_failed(
                worker,
                f"Worker container exited unexpectedly (code {worker.exit_code})",
            )
        except (DockerException, OSError, RuntimeError) as error:
            if not worker.stopping:
                await self._mark_failed(worker, f"Worker monitor failed: {error}")
        finally:
            worker.channel.close()

    async def _handle_worker_message(self, worker: Worker, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        if message_type == "ready":
            worker.status = "running"
            if worker.ready and not worker.ready.done():
                worker.ready.set_result(None)
            worker.changed.set()
        elif message_type == "text":
            text = message.get("text")
            if isinstance(text, str):
                worker.output += text
                worker.changed.set()
        elif message_type == "status":
            status = message.get("value")
            if status in {"running", "waiting"}:
                worker.status = status
            worker.changed.set()
        elif message_type == "error":
            worker.error = str(message.get("message", "Worker runtime error"))
            if message.get("fatal") and worker.ready and not worker.ready.done():
                worker.ready.set_exception(RuntimeError(worker.error))
            worker.changed.set()

    async def _mark_failed(self, worker: Worker, reason: str) -> None:
        worker.status = "failed"
        previous_error = worker.error
        if previous_error and previous_error not in reason:
            reason = f"{reason}\nPrevious worker error: {previous_error}"
        diagnostics = worker.channel.diagnostics.decode("utf-8", errors="replace").strip()
        if diagnostics and diagnostics not in reason:
            reason = f"{reason}\n{diagnostics}"
        worker.error = reason
        if worker.ready and not worker.ready.done():
            worker.ready.set_exception(RuntimeError(reason))
        worker.changed.set()
        try:
            await asyncio.to_thread(worker.container.remove, force=True)
        except (DockerException, NotFound):
            pass

    async def send_message(self, worker_id: str, text: str, *, interrupt: bool = False) -> None:
        if not text.strip():
            raise ValueError("Message text is required")
        worker = self._get_worker(worker_id)
        if worker.status not in {"starting", "running", "waiting"}:
            raise ValueError(f"Worker {worker_id} is {worker.status}")
        await worker.channel.send({"type": "message", "text": text, "interrupt": interrupt})

    async def poll_worker(self, worker_id: str, *, full: bool = False, timeout: float = 0) -> str:
        worker = self._get_worker(worker_id)
        timeout = max(0.0, min(float(timeout), 300.0))
        if not full and worker.poll_cursor >= len(worker.output) and timeout:
            worker.changed.clear()
            if worker.poll_cursor >= len(worker.output) and worker.status not in {"failed", "stopped"}:
                try:
                    await asyncio.wait_for(worker.changed.wait(), timeout=timeout)
                except TimeoutError:
                    pass
        if full:
            return worker.output
        text = worker.output[worker.poll_cursor :]
        worker.poll_cursor = len(worker.output)
        return text

    def list_workers(self) -> list[dict[str, Any]]:
        return [
            worker.snapshot()
            for worker in self.workers.values()
            if worker.status in {"starting", "running", "waiting", "failed"}
        ]

    async def stop_worker(self, worker_id: str) -> None:
        worker = self._get_worker(worker_id)
        worker.stopping = True
        worker.status = "stopped"
        worker.changed.set()
        try:
            await asyncio.to_thread(worker.container.kill)
        except (DockerException, NotFound):
            pass
        try:
            await asyncio.to_thread(worker.container.remove, force=True)
        except (DockerException, NotFound):
            pass
        worker.channel.close()
        self.workers.pop(worker_id, None)

    def _get_worker(self, worker_id: str) -> Worker:
        try:
            return self.workers[worker_id]
        except KeyError as error:
            raise ValueError(f"Unknown worker: {worker_id}") from error

    async def close(self) -> None:
        self.closed = True
        workers = list(self.workers.values())
        for worker in workers:
            worker.stopping = True
            worker.status = "stopped"
            worker.channel.close()
            try:
                await asyncio.to_thread(worker.container.kill)
            except (DockerException, NotFound):
                pass
            try:
                await asyncio.to_thread(worker.container.remove, force=True)
            except (DockerException, NotFound):
                pass
        self.workers.clear()
