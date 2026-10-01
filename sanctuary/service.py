"""Local HTTP API used by the MCP frontend."""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request

from sanctuary.config import Settings, load_settings
from sanctuary.orchestrator import Orchestrator


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.orchestrator = Orchestrator(settings)
        try:
            yield
        finally:
            await app.state.orchestrator.close()

    app = FastAPI(title="Sanctuary service", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"service": "sanctuary", "status": "ok"}

    @app.get("/workers")
    async def list_workers(request: Request) -> list[dict[str, Any]]:
        return request.app.state.orchestrator.list_workers()

    @app.post("/workers")
    async def start_worker(body: dict[str, Any], request: Request) -> dict[str, Any]:
        try:
            return await request.app.state.orchestrator.start_worker(
                str(body.get("profile", "")),
                str(body.get("prompt", "")),
                mounts=body.get("mounts"),
                docker_options=body.get("docker_options"),
            )
        except (ValueError, RuntimeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/workers/{worker_id}/messages")
    async def send_message(worker_id: str, body: dict[str, Any], request: Request) -> dict[str, bool]:
        try:
            await request.app.state.orchestrator.send_message(
                worker_id,
                str(body.get("text", "")),
                interrupt=bool(body.get("interrupt", False)),
            )
            return {"accepted": True}
        except ValueError as error:
            raise HTTPException(status_code=404 if "Unknown worker" in str(error) else 400, detail=str(error)) from error

    @app.get("/workers/{worker_id}/poll")
    async def poll_worker(
        worker_id: str,
        request: Request,
        full: bool = False,
        timeout: float = 0,
    ) -> dict[str, str]:
        try:
            text = await request.app.state.orchestrator.poll_worker(
                worker_id, full=full, timeout=timeout
            )
            return {"text": text}
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.delete("/workers/{worker_id}")
    async def stop_worker(worker_id: str, request: Request) -> dict[str, bool]:
        try:
            await request.app.state.orchestrator.stop_worker(worker_id)
            return {"stopped": True}
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Sanctuary local orchestration service")
    parser.add_argument("--config", help="Path to sanctuary TOML configuration")
    args = parser.parse_args()
    settings = load_settings(args.config)
    uvicorn.run(create_app(settings), host=settings.service_host, port=settings.service_port)


if __name__ == "__main__":
    main()
