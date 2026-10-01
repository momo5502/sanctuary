"""MCP tools for creating and managing Sanctuary workers."""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from sanctuary.config import load_settings
from sanctuary.service_manager import EmbeddedService


mcp = FastMCP("Sanctuary")
_service_url = "http://127.0.0.1:43187"


async def _request(method: str, path: str, **kwargs: Any) -> Any:
    async with httpx.AsyncClient(base_url=_service_url, timeout=kwargs.pop("timeout", 30.0)) as client:
        response = await client.request(method, path, **kwargs)
    if response.is_error:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise RuntimeError(f"Sanctuary service error ({response.status_code}): {detail}")
    return response.json()


@mcp.tool()
async def start_worker(
    profile: str,
    prompt: str,
    mounts: list[dict[str, Any]] | None = None,
    docker_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a containerized worker from a configured profile. The initial prompt is required."""
    body = {"profile": profile, "prompt": prompt}
    if mounts is not None:
        body["mounts"] = mounts
    if docker_options is not None:
        body["docker_options"] = docker_options
    return await _request("POST", "/workers", json=body, timeout=130.0)


@mcp.tool()
async def send_message(worker_id: str, text: str, interrupt: bool = False) -> dict[str, bool]:
    """Send plain text to a worker. Messages are queued unless interrupt is true."""
    return await _request(
        "POST",
        f"/workers/{worker_id}/messages",
        json={"text": text, "interrupt": interrupt},
    )


@mcp.tool()
async def poll_worker(worker_id: str, full: bool = False, timeout: float = 0) -> str:
    """Get new agent text, or all available text with full=true. Waits immediately by default."""
    response = await _request(
        "GET",
        f"/workers/{worker_id}/poll",
        params={"full": full, "timeout": timeout},
        timeout=max(30.0, min(timeout, 300.0) + 5.0),
    )
    return response["text"]


@mcp.tool()
async def list_workers() -> list[dict[str, Any]]:
    """List active and failed workers."""
    return await _request("GET", "/workers")


@mcp.tool()
async def stop_worker(worker_id: str) -> dict[str, bool]:
    """Terminate a worker container and remove its record from the worker list."""
    return await _request("DELETE", f"/workers/{worker_id}")


def main() -> None:
    global _service_url
    settings = load_settings()
    _service_url = settings.service_url
    service = EmbeddedService(settings)
    service.ensure_running()
    try:
        mcp.run(transport="stdio")
    finally:
        service.close()


if __name__ == "__main__":
    main()
