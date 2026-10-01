"""Discover a local service or embed one in the MCP server process."""

from __future__ import annotations

import time
import threading

import httpx
import uvicorn

from sanctuary.config import Settings
from sanctuary.service import create_app


class EmbeddedService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.owns_service = False

    def _is_sanctuary_running(self) -> bool:
        try:
            response = httpx.get(f"{self.settings.service_url}/health", timeout=0.4)
            return response.is_success and response.json().get("service") == "sanctuary"
        except (httpx.HTTPError, ValueError):
            return False

    def ensure_running(self, timeout: float = 10.0) -> None:
        if self._is_sanctuary_running():
            return
        config = uvicorn.Config(
            create_app(self.settings),
            host=self.settings.service_host,
            port=self.settings.service_port,
            log_level="warning",
            access_log=False,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="sanctuary-service", daemon=True)
        self.thread.start()
        self.owns_service = True

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._is_sanctuary_running():
                return
            if not self.thread.is_alive():
                if self._is_sanctuary_running():
                    self.owns_service = False
                    return
                raise RuntimeError("Could not start Sanctuary service; check its configured host and port")
            time.sleep(0.1)
        self.close()
        raise RuntimeError("Timed out while starting Sanctuary service")

    def close(self) -> None:
        if self.owns_service and self.server:
            self.server.should_exit = True
            if self.thread:
                self.thread.join(timeout=10)
        self.owns_service = False
