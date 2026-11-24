"""Where credentials and local data live.

Credentials come from CANVAS_BASE_URL / CANVAS_API_TOKEN, or from a config file
written by ``canvas-grader-mcp configure`` (readable only by the current user).
Nothing is sent anywhere except the Canvas instance you configure.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

APP = "canvas-grader-mcp"


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default) / APP


def config_file() -> Path:
    if override := os.environ.get("CANVAS_GRADER_CONFIG"):
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / APP / "config.json"
    return _xdg("XDG_CONFIG_HOME", ".config") / "config.json"


def data_dir() -> Path:
    """Staged (not yet posted) grades."""
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / APP
    return _xdg("XDG_DATA_HOME", ".local/share")


def cache_dir() -> Path:
    """Downloaded submission files."""
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / APP / "cache"
    return _xdg("XDG_CACHE_HOME", ".cache")


def private_dir(path: Path) -> Path:
    """Create path (and any missing parents) readable only by the current user.

    Student submissions and grades are education records, so nothing this server
    writes should be readable by other accounts on the machine.
    """
    missing = [p for p in (path, *path.parents) if not p.exists()]
    for p in reversed(missing):
        p.mkdir(mode=0o700)
        os.chmod(p, 0o700)  # mkdir's mode is masked by the umask
    return path


def private_file(path: Path) -> int:
    """Open path for writing with owner-only permissions; returns a file descriptor."""
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(path, 0o600)
    return fd


def write_private(path: Path, data: bytes) -> Path:
    with os.fdopen(private_file(path), "wb") as fh:
        fh.write(data)
    return path


class ConfigError(ToolError):
    pass


@dataclass
class Settings:
    base_url: str
    token: str

    @classmethod
    def load(cls) -> "Settings":
        base_url = os.environ.get("CANVAS_BASE_URL", "")
        token = os.environ.get("CANVAS_API_TOKEN", "")
        path = config_file()
        if (not base_url or not token) and path.is_file():
            stored = json.loads(path.read_text())
            base_url = base_url or stored.get("base_url", "")
            token = token or stored.get("token", "")
        if not base_url or not token:
            raise ConfigError(
                "Canvas is not configured. Run `canvas-grader-mcp configure`, or set "
                "CANVAS_BASE_URL (e.g. https://canvas.school.edu) and CANVAS_API_TOKEN."
            )
        return cls(normalize_base_url(base_url), token.strip())

    def save(self) -> Path:
        path = config_file()
        with os.fdopen(private_file(path), "w") as fh:
            json.dump({"base_url": self.base_url, "token": self.token}, fh, indent=2)
        return path


def normalize_base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith(("https://", "http://")):
        url = "https://" + url
    return url.removesuffix("/api/v1")
