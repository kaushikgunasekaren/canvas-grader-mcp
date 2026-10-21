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
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"base_url": self.base_url, "token": self.token}, fh, indent=2)
        os.chmod(path, 0o600)
        return path


def normalize_base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith(("https://", "http://")):
        url = "https://" + url
    return url.removesuffix("/api/v1")
