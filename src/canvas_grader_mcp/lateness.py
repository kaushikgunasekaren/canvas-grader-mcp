"""Due dates and late penalties.

Canvas computes each student's effective due date (including overrides and
extensions) and how late they were. When the course has an automatic late
policy, Canvas also deducts points itself when a grade is entered, so graders
should enter the raw score. Without one, the deduction is up to the grader
(usually per the assignment description or syllabus).
"""

from __future__ import annotations

import math
from datetime import datetime

INTERVAL_SECONDS = {"hour": 3600, "day": 86400}


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def describe_policy(policy: dict | None) -> dict:
    if not policy:
        return {"automatic": False, "summary": "No automatic late policy in this course."}
    parts = []
    late_on = bool(policy.get("late_submission_deduction_enabled"))
    if late_on:
        parts.append(f"-{policy.get('late_submission_deduction')}% per {policy.get('late_submission_interval', 'day')} late")
        if policy.get("late_submission_minimum_percent_enabled"):
            parts.append(f"never below {policy.get('late_submission_minimum_percent')}%")
    if policy.get("missing_submission_deduction_enabled"):
        parts.append(f"missing submissions get {100 - float(policy.get('missing_submission_deduction') or 0):g}%")
    return {
        "automatic": late_on,
        "summary": "Automatic late policy: " + ", ".join(parts) if parts else "Late policy exists but no deductions are enabled.",
        "raw": policy,
    }


def analyze(submission: dict, assignment: dict, policy: dict | None, raw_score: float | None = None,
            tiers: list | None = None) -> dict:
    """Lateness facts for one submission, plus the automatic deduction Canvas would apply to raw_score."""
    due = submission.get("cached_due_date") or assignment.get("due_at")
    submitted = submission.get("submitted_at")
    seconds_late = int(submission.get("seconds_late") or 0)
    if not seconds_late and due and submitted:
        seconds_late = max(0, int((_parse(submitted) - _parse(due)).total_seconds()))
    status = submission.get("late_policy_status")
    late = bool(submission.get("late")) or (seconds_late > 0 and status not in ("none", "extended"))
    info: dict = {
        "due_at": due,
        "submitted_at": submitted,
        "late": late,
        "missing": bool(submission.get("missing")),
        "excused": bool(submission.get("excused")),
        "seconds_late": seconds_late if late else 0,
        "days_late": math.ceil(seconds_late / 86400) if late else 0,
        "late_policy_status": status,
        "points_deducted_by_canvas": submission.get("points_deducted"),
    }
    if late:
        info["hours_late"] = round(seconds_late / 3600, 1)

    points_possible = assignment.get("points_possible") or 0
    if late and policy and policy.get("late_submission_deduction_enabled") and points_possible:
        interval = INTERVAL_SECONDS.get(policy.get("late_submission_interval") or "day", 86400)
        intervals = math.ceil(seconds_late / interval)
        percent = min(100.0, intervals * float(policy.get("late_submission_deduction") or 0))
        deduction = points_possible * percent / 100
        if raw_score is not None:
            if policy.get("late_submission_minimum_percent_enabled"):
                floor = points_possible * float(policy.get("late_submission_minimum_percent") or 0) / 100
                deduction = min(deduction, max(0.0, raw_score - floor))
            deduction = min(deduction, max(0.0, raw_score))
        info["automatic_deduction"] = {
            "intervals_late": intervals,
            "percent": percent,
            "points": round(deduction, 2),
            "note": "Canvas applies this automatically; enter the raw score. This is an estimate, Canvas's value is authoritative.",
        }
    elif late:
        info["automatic_deduction"] = None
        if tiers:
            from .guidelines import tier_penalty

            info["suggested_penalty"] = tier_penalty(seconds_late, points_possible, tiers)
            info["note"] = (
                "No automatic Canvas late policy; apply suggested_penalty from this course's late tiers yourself "
                "(unless the student had an extension) and say so in the comment."
            )
        else:
            info["note"] = (
                "No automatic late policy: Canvas will not deduct anything. Apply any late penalty stated in the "
                "assignment description/syllabus yourself and say so in the comment."
            )
    return info
