# Sanctuary — v1 Plan

## Goal

Build a local agent orchestration system that can run agents in isolated Docker containers and expose them through an MCP server. The initial prototype targets Codex on a local Docker engine, while keeping the worker protocol open to other harnesses, frontends, and Linux containers.

Workers are independent long-lived agents. The system does not assume they are subagents of the MCP host.

## Agreed v1 decisions

- Implement the MCP frontend, orchestrator service, and container worker runtime in Python.
- Keep the MCP frontend separate from the orchestrator service in the architecture. Initially, the MCP process checks for a local service and starts an embedded service if one is not already available.
- Use the local machine's Docker engine initially. A remote Docker engine is a later option.
- Use prebuilt, fixed images selected through named profiles in MCP settings. Image building and image customization tooling are out of scope for v1.
- Put a small Python worker runtime in each image. It starts and supervises the selected harness. The first runtime adapter targets Codex app-server; Claude can follow later.
- Communicate between the orchestrator and worker runtime using newline-delimited JSON over the container process's stdin/stdout. Reserve stdout for protocol messages and send diagnostics to stderr. Keep the protocol independent of Docker so a local or remote process can implement it later.
- Use the Codex app-server over its documented newline-delimited JSON stdio protocol. The runtime translates its events into the common worker protocol and exposes agent text to the orchestrator.
- Require an initial prompt when starting a worker. The start result includes a worker ID; the initial response is retrieved with poll.
- Support one conversation per worker and plain-text messages only.
- Keep workers alive until explicitly stopped or until the orchestrator service exits. A stop command terminates the container.
- Configure a maximum of 10 active workers by default; make the limit configurable in MCP settings.
- Allow network access by default. Image/profile and start configuration can specify mounts and other Docker settings. The image defines the default working directory and runtime setup.
- Initially, allow configured mounts of harness credential folders such as `~/.codex` or `~/.claude`. Treat these as sensitive; make read-only mounts the default where practical.
- New messages are queued by default while the agent is working. A send parameter can request interruption/steering instead.
- Poll returns plain agent text only, returns immediately by default, and supports a timeout parameter. By default it returns text since the worker's last poll; a parameter can request the full available conversation text.
- Track the poll position internally per worker in v1. This means clients polling the same worker advance a shared cursor and can affect what another client sees. Explicit or per-client cursors can be considered later.
- List active and failed workers. If a worker exits unexpectedly, mark it failed and retain its output and exit details while the service is alive; let the host decide whether to start a replacement. A manually stopped worker is removed from the list.
- Keep state and output in memory in v1. When the MCP/service process exits, stop its workers and clear in-memory state.

## Architecture

```text
MCP host(s)
    │ MCP tools
    ▼
Python MCP frontend
    │ local service API (transport to choose/validate)
    ▼
Python orchestrator service
    ├── worker registry, limits, lifecycle, text buffers, poll positions
    └── local Docker engine
            │ container stdin/stdout: newline-delimited JSON
            ▼
       Python worker runtime
            │ child-process stdin/stdout: newline-delimited JSON
            ▼
       Codex app-server
```

The orchestrator owns Docker lifecycle and worker state. The runtime owns the harness process and adapts harness-specific behavior to a small, stable worker protocol. MCP tools are a frontend over the service, rather than the place where Docker and harness details live.

## Initial MCP tool shape

- `start_worker(profile, prompt, ...)` — validate the profile and capacity, start a container, send the required initial prompt, and return the worker ID and status.
- `send_message(worker_id, text, interrupt=false)` — queue text by default; optionally interrupt/steer the current turn.
- `poll_worker(worker_id, full=false, timeout=0)` — return new text since the shared worker poll position, or the full available text; no wait by default.
- `list_workers()` — show active and failed workers with status and failure details where relevant.
- `stop_worker(worker_id)` — terminate the worker container and remove the manually stopped worker from the list.

Exact JSON schemas and error behavior will be designed during implementation.

## Work plan

1. [x] Define the versioned newline-delimited JSON worker protocol for startup, messages, text output, status, and errors.
2. [x] Implement the Python worker runtime and first Codex app-server adapter, including text deltas, queued messages, interruption, and turn failures.
3. [x] Add bootstrap Dockerfiles for the fixed worker images:
   - Linux image: install Python and Codex CLI, include the worker runtime, and launch it as the container entrypoint.
   - Windows image: install Python and Codex CLI, include the worker runtime, and launch it as the container entrypoint.
   - [x] Build the Linux image with the local Linux Docker engine.
   - [x] Build the Windows image with the local Windows Docker engine using Hyper-V isolation.
   - [x] Run the Windows worker image and confirm a live Codex app-server response.
4. [x] Implement the Python orchestrator: local Docker integration, profile loading, worker limit, start/send/poll/list/stop, failure capture, and service-exit cleanup.
5. [x] Implement the Python MCP frontend and embedded service discovery/startup behavior.
6. [x] Add example configuration and local setup documentation.
7. [ ] Exercise the end-to-end Codex flow on Docker: start, poll text, send a follow-up, list, stop, and inspect an unexpected exit.

## Deliberately deferred

- Claude and other harness adapters.
- Building or customizing images through the product.
- Remote Docker engines and remote worker hosts.
- Durable state, restart recovery, and persistent output storage.
- Per-client poll cursors and event streaming.
- File attachments and automatic workspace copying.
- A broad settings UI, auth service, or multi-user permissions layer.
- Final choice of local MCP-to-orchestrator transport, beyond the requirement that it stay local and support an embedded service startup path.

## Risks and points to validate

- Windows containers have host/version and runtime constraints that differ from Linux containers. The LTSC 2022 image built and completed a Codex turn on the local Windows 26H2 host with Hyper-V isolation; process isolation did not work on this host.
- A manual worker `stop` command reports `stopping` but the container remained alive during the Windows smoke test. The orchestrator's Docker stop/removal calls also hung for this container, so worker shutdown and cleanup need investigation before relying on the Windows lifecycle.
- Docker attach behavior must reliably support bidirectional line-oriented stdin/stdout for the lifetime of a worker. If the selected Docker SDK path makes this fragile, preserve the worker protocol and change only the transport implementation.
- Mounting user credential directories gives the container access to sensitive auth/configuration data. Keep exact host paths explicit in profiles; Codex's local thread state currently makes a writable Codex home useful, so choose the mount mode deliberately.
- Codex app-server protocol and CLI availability can evolve. Keep that integration isolated in the Codex adapter and pin or document the image's Codex version.
- Since v1 state is in memory, service exit intentionally loses conversation buffers and worker records after terminating containers.
