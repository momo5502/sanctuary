# Sanctuary

Orchestrate AI agents in isolated containers.

Sanctuary has a Python MCP frontend, a local orchestration service, and a small runtime inside each worker container. The first worker adapter uses Codex app-server. The MCP server starts an embedded local service when it cannot find one already running.

## Current state

This is an early prototype. The service API, MCP tools, worker runtime, and Linux/Windows bootstrap Dockerfiles are in place. Both images build locally, and the Windows worker has completed a live Codex app-server turn. The full MCP/orchestrator lifecycle still needs end-to-end exercise.

## Setup

Install Sanctuary in a Python 3.11+ environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

Copy `config.example.toml` to `~/.sanctuary/config.toml` and edit its profiles and host paths. Configure the MCP client to launch `sanctuary-mcp` (or `python -m sanctuary.mcp_server`) with `SANCTUARY_CONFIG` set to that file.

The orchestrator uses the local Docker engine. The selected profile must point at an image available to that engine. The default active worker limit is 10.

## Build the worker images

From the repository root, with Docker using Linux containers:

```powershell
docker build -f images/linux/Dockerfile -t sanctuary/codex-linux:dev .
```

For a Windows worker, switch Docker to Windows containers and use a host that supports the `ltsc2022` base image:

```powershell
docker build --isolation=hyperv -f images/windows/Dockerfile -t sanctuary/codex-windows:dev .
```

The Windows image is large and tied to Windows container host compatibility. Linux and Windows containers may require switching the Docker engine mode; one local engine may not run both OS types at the same time.

Both images install Codex CLI through its npm package. `CODEX_VERSION` is a build argument and defaults to `latest`; the Windows image also uses Node.js 22.22.3. The runtime expects the selected profile to mount the Codex home directory at the matching container user's `~/.codex` location. The example makes this mount writable because Codex stores local thread state there; use a read-only mount only if the mounted contents and Codex behavior you need permit it.

## MCP tools

- `start_worker(profile, prompt, mounts?, docker_options?)` starts a long-lived worker. The prompt is required.
- `send_message(worker_id, text, interrupt=false)` queues plain text by default; `interrupt=true` asks Codex to interrupt the current turn before receiving the new message.
- `poll_worker(worker_id, full=false, timeout=0)` returns text only and returns immediately by default. Poll position is shared per worker. Full history does not advance that position.
- `list_workers()` shows active and failed workers.
- `stop_worker(worker_id)` terminates the container and removes its record.

The container runtime and orchestrator exchange versioned newline-delimited JSON over container stdin/stdout. Agent text is emitted as it arrives. Diagnostics stay on stderr.

## Configuration notes

Profiles live in the Sanctuary TOML file. A profile selects a prebuilt image, working directory, mounts, Docker options, and runtime settings. Per-worker `mounts` and `docker_options` can be passed to `start_worker` as well. Review host mount source paths and access modes before starting a worker.

The service keeps worker state and output in memory. It removes failed containers after recording their exit status and retains failed worker output until the service exits. Exiting the service stops all its containers and clears state.
