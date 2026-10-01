"""Configuration loading for Sanctuary."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path.home() / ".sanctuary" / "config.toml"


@dataclass(frozen=True)
class Mount:
    source: str
    target: str
    read_only: bool = True

    def docker_volume(self) -> dict[str, str]:
        return {
            "bind": self.target,
            "mode": "ro" if self.read_only else "rw",
        }


@dataclass(frozen=True)
class WorkerProfile:
    name: str
    image: str
    working_dir: str | None = None
    mounts: tuple[Mount, ...] = ()
    docker_options: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Settings:
    config_path: Path
    service_host: str = "127.0.0.1"
    service_port: int = 43187
    max_workers: int = 10
    profiles: dict[str, WorkerProfile] = field(default_factory=dict)

    @property
    def service_url(self) -> str:
        return f"http://{self.service_host}:{self.service_port}"


def expand_path(value: str) -> str:
    """Expand common shell and Windows environment references in host paths."""
    expanded = os.path.expandvars(os.path.expanduser(value))
    if "%USERPROFILE%" in expanded:
        expanded = expanded.replace("%USERPROFILE%", os.environ.get("USERPROFILE", ""))
    return expanded


def _parse_mount(raw: dict[str, Any]) -> Mount:
    if "source" not in raw or "target" not in raw:
        raise ValueError("Each mount needs source and target fields")
    return Mount(
        source=expand_path(str(raw["source"])),
        target=str(raw["target"]),
        read_only=bool(raw.get("read_only", True)),
    )


def load_settings(path: str | Path | None = None) -> Settings:
    config_path = Path(path or os.environ.get("SANCTUARY_CONFIG", DEFAULT_CONFIG_PATH)).expanduser()
    if not config_path.exists():
        raise FileNotFoundError(
            f"Sanctuary config not found at {config_path}. "
            "Copy config.example.toml and set SANCTUARY_CONFIG to its path."
        )

    with config_path.open("rb") as config_file:
        raw = tomllib.load(config_file)

    service = raw.get("service", {})
    max_workers = int(service.get("max_workers", 10))
    if max_workers < 1:
        raise ValueError("service.max_workers must be at least 1")

    profiles: dict[str, WorkerProfile] = {}
    for name, profile in raw.get("profiles", {}).items():
        image = profile.get("image")
        if not image:
            raise ValueError(f"Profile {name!r} must define an image")
        mounts = tuple(_parse_mount(mount) for mount in profile.get("mounts", []))
        docker_options = dict(profile.get("docker", {}))
        runtime = dict(profile.get("runtime", {}))
        profiles[name] = WorkerProfile(
            name=name,
            image=str(image),
            working_dir=profile.get("working_dir"),
            mounts=mounts,
            docker_options=docker_options,
            runtime=runtime,
        )

    return Settings(
        config_path=config_path,
        service_host=str(service.get("host", "127.0.0.1")),
        service_port=int(service.get("port", 43187)),
        max_workers=max_workers,
        profiles=profiles,
    )
