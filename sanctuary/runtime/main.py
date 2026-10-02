"""Container entrypoint: translate worker JSONL commands to a harness adapter."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from typing import Any

from sanctuary.protocol import decode_message, encode_message
from sanctuary.runtime.codex import CodexAppServer


def emit(message: dict[str, Any]) -> None:
    sys.stdout.buffer.write(encode_message(message))
    sys.stdout.buffer.flush()


async def read_host_line() -> bytes:
    return await asyncio.to_thread(sys.stdin.buffer.readline)


async def run_worker(first_message: dict[str, Any]) -> int:
    if first_message.get("type") != "start":
        emit({"type": "error", "fatal": True, "message": "First worker message must be start"})
        return 2

    prompt = first_message.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        emit({"type": "error", "fatal": True, "message": "Initial prompt is required"})
        return 2

    runtime_config = first_message.get("runtime", {})
    auth_file = runtime_config.pop("auth_file", None)
    harness = runtime_config.get("harness", os.environ.get("SANCTUARY_HARNESS", "codex"))
    if harness != "codex":
        emit({"type": "error", "fatal": True, "message": f"Unsupported harness: {harness}"})
        return 2

    app_server = CodexAppServer(
        cwd=str(first_message.get("working_dir") or os.environ.get("SANCTUARY_WORKDIR") or os.getcwd()),
        model=runtime_config.get("model"),
        reasoning_effort=runtime_config.get("reasoning_effort"),
        approval_policy=str(runtime_config.get("approval_policy", "never")),
        sandbox=str(runtime_config.get("sandbox", "danger-full-access")),
        bypass_hook_trust=bool(runtime_config.get("bypass_hook_trust", False)),
    )
    queued_messages: list[tuple[str, bool]] = []
    active = False
    pending_text: list[str] = []
    pending_text_chars = 0

    def flush_text() -> None:
        nonlocal pending_text_chars
        if pending_text:
            emit({"type": "text", "text": "".join(pending_text)})
            pending_text.clear()
            pending_text_chars = 0

    try:
        if auth_file is not None:
            if not isinstance(auth_file, dict) or not isinstance(auth_file.get("target"), str):
                raise ValueError("Invalid configured auth file")
            target = auth_file["target"]
            encoded = auth_file.get("content_base64")
            if not isinstance(encoded, str):
                raise ValueError("Invalid configured auth file content")
            contents = base64.b64decode(encoded, validate=True)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as destination:
                destination.write(contents)
            if os.name != "nt":
                os.chmod(target, 0o600)
        await app_server.start()
        emit({"type": "ready", "worker_id": first_message.get("worker_id")})
        await app_server.begin_turn(prompt)
        active = True
        emit({"type": "status", "value": "running"})

        host_task = asyncio.create_task(read_host_line())
        event_task = asyncio.create_task(app_server.events.get())
        while True:
            completed, _ = await asyncio.wait(
                {host_task, event_task},
                timeout=0.05,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not completed:
                flush_text()
                continue
            if host_task in completed:
                host_line = host_task.result()
                if not host_line:
                    return 0
                host_task = asyncio.create_task(read_host_line())
                try:
                    command = decode_message(host_line)
                except (ValueError, json.JSONDecodeError) as error:
                    emit({"type": "error", "fatal": False, "message": str(error)})
                    continue
                command_type = command.get("type")
                if command_type == "stop":
                    emit({"type": "status", "value": "stopping"})
                    return 0
                if command_type == "message":
                    text = command.get("text")
                    if not isinstance(text, str):
                        emit({"type": "error", "fatal": False, "message": "Message text must be a string"})
                        continue
                    interrupt = bool(command.get("interrupt", False))
                    if active:
                        queued_messages.append((text, interrupt))
                        if interrupt:
                            try:
                                await app_server.interrupt()
                            except RuntimeError as error:
                                # The turn may finish between receiving and interrupting.
                                print(f"Codex interrupt was no longer needed: {error}", file=sys.stderr)
                    else:
                        await app_server.begin_turn(text)
                        active = True
                        emit({"type": "status", "value": "running"})
                else:
                    emit({"type": "error", "fatal": False, "message": f"Unknown command: {command_type}"})

            if event_task in completed:
                event = event_task.result()
                event_task = asyncio.create_task(app_server.events.get())
                method = event.get("method")
                params = event.get("params", {})
                if method == "item/agentMessage/delta":
                    delta = params.get("delta")
                    if isinstance(delta, str) and delta:
                        pending_text.append(delta)
                        pending_text_chars += len(delta)
                        if pending_text_chars >= 512:
                            flush_text()
                elif method == "turn/completed":
                    flush_text()
                    turn = params.get("turn", {})
                    turn_status = turn.get("status", params.get("status", "completed"))
                    app_server.active_turn_id = None
                    active = False
                    emit({"type": "turn_complete", "status": turn_status})
                    if turn_status not in ("completed", "interrupted"):
                        emit({"type": "error", "fatal": False, "message": f"Codex turn ended with status {turn_status}"})
                    if queued_messages:
                        next_message, _ = queued_messages.pop(0)
                        await app_server.begin_turn(next_message)
                        active = True
                        emit({"type": "status", "value": "running"})
                    else:
                        emit({"type": "status", "value": "waiting"})
                elif method == "sanctuary/process-exit":
                    raise RuntimeError(f"Codex app-server exited with code {params.get('exitCode')}")
                elif method in ("error", "codex/event/error"):
                    emit({"type": "error", "fatal": False, "message": str(params)})

    except Exception as error:
        print(f"Sanctuary worker failed: {error}", file=sys.stderr)
        emit({"type": "error", "fatal": True, "message": str(error)})
        return 1
    finally:
        flush_text()
        await app_server.close()


async def async_main() -> int:
    line = await read_host_line()
    if not line:
        return 0
    try:
        first_message = decode_message(line)
    except (ValueError, json.JSONDecodeError) as error:
        emit({"type": "error", "fatal": True, "message": str(error)})
        return 2
    return await run_worker(first_message)


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
