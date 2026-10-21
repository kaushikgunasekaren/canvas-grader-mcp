"""Per-course grading guidelines and manual late-penalty tiers, stored locally.

Canvas only knows automatic late policies. Many courses instead apply a manual
scale ("under a day: nothing, under a week: 5%..."); graders can record that
here once and every assignment in the course will report the suggested penalty.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from .config import data_dir


class LateTier(BaseModel):
    under_days: float | None = Field(description="Applies when the submission is less than this many days late; null = any lateness beyond the previous tiers")
    percent: float = Field(ge=0, le=100, description="Penalty as a percent of points possible")
    min_points: float = Field(0, ge=0, description="Minimum penalty in points when percent rounds lower")


def tier_penalty(seconds_late: int, points_possible: float, tiers: list[LateTier]) -> dict | None:
    """The first tier the lateness falls under, with its penalty in points."""
    if seconds_late <= 0:
        return None
    days = seconds_late / 86400
    for tier in tiers:
        if tier.under_days is None or days < tier.under_days:
            points = 0.0 if tier.percent == 0 else max(tier.min_points, math.floor(points_possible * tier.percent / 100 + 0.5))
            limit = "any" if tier.under_days is None else f"under {tier.under_days:g} day(s)"
            return {"points": points, "tier": f"{limit} late: {tier.percent:g}% (min {tier.min_points:g} pts)", "days_late": round(days, 2)}
    return None


class GuidelineStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_dir() / "guidelines.json"

    def _load(self) -> dict[str, dict]:
        return json.loads(self.path.read_text()) if self.path.is_file() else {}

    def get(self, course_id: int) -> dict:
        return self._load().get(str(course_id), {})

    def late_tiers(self, course_id: int) -> list[LateTier]:
        return [LateTier(**t) for t in self.get(course_id).get("late_tiers") or []]

    def set(self, course_id: int, guidelines: str | None, late_tiers: list[LateTier] | None) -> dict:
        entries = self._load()
        entry = entries.get(str(course_id), {})
        if guidelines is not None:
            entry["guidelines"] = guidelines
        if late_tiers is not None:
            ordered = sorted(late_tiers, key=lambda t: math.inf if t.under_days is None else t.under_days)
            entry["late_tiers"] = [t.model_dump() for t in ordered]
        entries[str(course_id)] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".guidelines-")
        with os.fdopen(fd, "w") as fh:
            json.dump(entries, fh, indent=2)
        os.replace(tmp, self.path)
        return entry
