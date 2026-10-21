"""Turn per-category scores into a total and a student-facing score breakdown."""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field


class Category(BaseModel):
    name: str = Field(description="Grading category from the assignment, e.g. 'Loop Demonstrations'")
    earned: float = Field(ge=0)
    max: float = Field(gt=0)
    reason: str | None = Field(None, description="Why points were lost (shown to the student); omit when full marks")


class Adjustment(BaseModel):
    points: float = Field(gt=0)
    reason: str


def build(intro: str | None, categories: list[Category], late: Adjustment | None = None,
          extra_credit: list[Adjustment] | None = None, note: str | None = None) -> tuple[float, str]:
    """(total, comment). Late penalty is subtracted, extra credit added."""
    lines = [intro, ""] if intro else []
    lines.append("Score breakdown:")
    total = 0.0
    for cat in categories:
        if cat.earned > cat.max:
            raise ToolError(f"{cat.name}: earned {cat.earned:g} is more than the category maximum {cat.max:g}")
        total += cat.earned
        why = f" ({cat.reason})" if cat.reason and cat.earned < cat.max else ""
        lines.append(f"- {cat.name}: {cat.earned:g}/{cat.max:g}{why}")
    if late:
        total -= late.points
        lines.append(f"- Late penalty: -{late.points:g} ({late.reason})")
    for extra in extra_credit or []:
        total += extra.points
        lines.append(f"- Extra credit: +{extra.points:g} ({extra.reason})")
    possible = sum(c.max for c in categories)
    total = max(total, 0.0)
    lines.append(f"Total: {total:g}/{possible:g}")
    if note:
        lines += ["", note]
    return total, "\n".join(lines)
