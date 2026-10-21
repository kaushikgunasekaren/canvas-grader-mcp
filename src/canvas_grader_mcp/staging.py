"""Local store of proposed grades that have not been posted to Canvas yet."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .config import data_dir


def _key(course_id: int, assignment_id: int, user_id: int) -> str:
    return f"{course_id}:{assignment_id}:{user_id}"


class StagingStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_dir() / "staged.json"

    def _load(self) -> dict[str, dict]:
        if not self.path.is_file():
            return {}
        return json.loads(self.path.read_text())

    def _save(self, entries: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".staged-")
        with os.fdopen(fd, "w") as fh:
            json.dump(entries, fh, indent=2)
        os.chmod(tmp, 0o600)  # contains student names and feedback
        os.replace(tmp, self.path)

    def put(self, entry: dict) -> dict:
        entries = self._load()
        entry = {**entry, "staged_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        entries[_key(entry["course_id"], entry["assignment_id"], entry["user_id"])] = entry
        self._save(entries)
        return entry

    def list(self, course_id: int | None = None, assignment_id: int | None = None) -> list[dict]:
        return [
            e for e in self._load().values()
            if (course_id is None or e["course_id"] == course_id)
            and (assignment_id is None or e["assignment_id"] == assignment_id)
        ]

    def remove(self, course_id: int, assignment_id: int, user_ids: list[int] | None = None) -> int:
        entries = self._load()
        doomed = [
            k for k, e in entries.items()
            if e["course_id"] == course_id and e["assignment_id"] == assignment_id
            and (user_ids is None or e["user_id"] in user_ids)
        ]
        for k in doomed:
            del entries[k]
        self._save(entries)
        return len(doomed)
