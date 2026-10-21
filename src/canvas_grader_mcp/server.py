"""MCP server for grading Canvas submissions (the API behind SpeedGrader)."""

from __future__ import annotations

import logging
import shutil
import sys
import webbrowser
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from . import lateness
from .canvas import CanvasClient
from .config import ConfigError, Settings, cache_dir
from .extract import extract_file, html_to_text, truncate
from .staging import StagingStore

GRADER_ROLES = {"teacher", "ta", "designer"}

# httpx logs every request (including URLs with student/course ids) at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = MCPServer(
    "canvas-grader",
    instructions=(
        "Grades Canvas LMS submissions for courses where the user is a teacher or TA. "
        "Workflow: list_grading_courses -> list_assignments -> get_assignment (read the description, rubric and "
        "late policy) -> list_submissions -> get_submission for each student -> stage_grade -> show the user the "
        "staged grades -> post_staged_grades with confirm=true only after the user approves. "
        "Base every point deduction on the assignment description or rubric and say in the comment exactly what "
        "is missing or wrong and how to fix it. Keep comments constructive and addressed to the student. "
        "If a course has an automatic late policy, stage the raw score: Canvas deducts late points itself. "
        "Otherwise apply only the late penalty the description/syllabus states, and mention it in the comment. "
        "Never post grades or comments without the user's explicit go-ahead."
    ),
)

_client: CanvasClient | None = None
_grader_ok: dict[int, bool] = {}
staging = StagingStore()


def client() -> CanvasClient:
    global _client
    if _client is None:
        _client = CanvasClient(Settings.load())
    return _client


def require_grader(course_id: int) -> None:
    if course_id not in _grader_ok:
        perms = client().permissions(course_id, ["manage_grades", "view_all_grades"])
        _grader_ok[course_id] = bool(perms.get("manage_grades"))
    if not _grader_ok[course_id]:
        raise ToolError(f"You don't have grading permission (manage_grades) in course {course_id}.")


def writable(assignment: dict) -> None:
    if assignment.get("moderated_grading"):
        raise ToolError("This is a moderated assignment; provisional grades aren't supported. Grade it in SpeedGrader.")
    if assignment.get("anonymize_students") or assignment.get("anonymous_grading"):
        raise ToolError("This assignment uses anonymous grading, which isn't supported yet. Grade it in SpeedGrader.")
    if assignment.get("grading_type") == "not_graded":
        raise ToolError("This assignment is not graded. You can still leave comments with post_comment.")


def is_group(assignment: dict) -> bool:
    return bool(assignment.get("group_category_id")) and not assignment.get("grade_group_students_individually")


def visibility_note(assignment: dict) -> str:
    if assignment.get("post_manually"):
        return "Grades are hidden from students until you post them (manual posting policy) in the Gradebook/SpeedGrader."
    return "WARNING: this assignment posts grades automatically, so students will see grades and comments immediately."


def student_name(sub: dict) -> str:
    user = sub.get("user") or {}
    return user.get("sortable_name") or user.get("name") or f"user {sub.get('user_id')}"


# --- discovery -----------------------------------------------------------------


@mcp.tool()
def whoami() -> dict:
    """The Canvas account and instance this server is connected to."""
    me = client().me()
    return {"id": me.get("id"), "name": me.get("name"), "login": me.get("login_id"), "canvas": client().base_url}


@mcp.tool()
def list_grading_courses(include_concluded: bool = False) -> list[dict]:
    """Courses where you are a teacher/TA (or similar) and have permission to grade."""
    out = []
    for course in client().courses(include_concluded):
        roles = {e.get("type") for e in course.get("enrollments") or []}
        if not roles & GRADER_ROLES:
            continue
        try:
            require_grader(course["id"])
        except ToolError:
            continue
        out.append({
            "course_id": course["id"],
            "name": course.get("name"),
            "code": course.get("course_code"),
            "term": (course.get("term") or {}).get("name"),
            "roles": sorted(r for r in roles if r),
            "students": course.get("total_students"),
        })
    return out


@mcp.tool()
def list_assignments(course_id: int, only_needing_grading: bool = False) -> list[dict]:
    """Assignments in a course, with how many submissions still need grading."""
    require_grader(course_id)
    rows = []
    for a in client().assignments(course_id):
        if only_needing_grading and not a.get("needs_grading_count"):
            continue
        rows.append({
            "assignment_id": a["id"],
            "name": a.get("name"),
            "due_at": a.get("due_at"),
            "has_multiple_due_dates": bool(a.get("has_overrides")),
            "points_possible": a.get("points_possible"),
            "grading_type": a.get("grading_type"),
            "needs_grading_count": a.get("needs_grading_count"),
            "submission_types": a.get("submission_types"),
            "has_rubric": bool(a.get("rubric")),
            "published": a.get("published"),
            "group_assignment": is_group(a),
            "anonymous": bool(a.get("anonymize_students")),
            "moderated": bool(a.get("moderated_grading")),
        })
    return rows


@mcp.tool()
def get_assignment(course_id: int, assignment_id: int) -> dict:
    """Everything needed to grade an assignment: description, rubric, points, due dates and late policy."""
    require_grader(course_id)
    c = client()
    a = c.assignment(course_id, assignment_id)
    rubric = [
        {
            "criterion_id": crit.get("id"),
            "description": crit.get("description"),
            "details": crit.get("long_description"),
            "points": crit.get("points"),
            "ratings": [
                {"rating_id": r.get("id"), "points": r.get("points"), "description": r.get("description"), "details": r.get("long_description")}
                for r in crit.get("ratings") or []
            ],
        }
        for crit in a.get("rubric") or []
    ]
    return {
        "assignment_id": a["id"],
        "name": a.get("name"),
        "description": truncate(html_to_text(a.get("description"))),
        "points_possible": a.get("points_possible"),
        "grading_type": a.get("grading_type"),
        "submission_types": a.get("submission_types"),
        "allowed_extensions": a.get("allowed_extensions"),
        "due_at": a.get("due_at"),
        "lock_at": a.get("lock_at"),
        "all_dates": a.get("all_dates"),
        "overrides": [
            {"title": o.get("title"), "due_at": o.get("due_at"), "student_ids": o.get("student_ids"), "section_id": o.get("course_section_id")}
            for o in a.get("overrides") or []
        ],
        "late_policy": lateness.describe_policy(c.late_policy(course_id)),
        "rubric": rubric,
        "rubric_used_for_grading": bool(a.get("use_rubric_for_grading")),
        "rubric_points_possible": (a.get("rubric_settings") or {}).get("points_possible"),
        "group_assignment": is_group(a),
        "anonymous": bool(a.get("anonymize_students")),
        "moderated": bool(a.get("moderated_grading")),
        "visibility": visibility_note(a),
        "speedgrader_url": c.speedgrader_url(course_id, assignment_id),
    }


Filter = Literal["needs_grading", "all", "submitted", "graded", "late", "missing", "unsubmitted"]


def needs_grading(sub: dict) -> bool:
    if not sub.get("submitted_at") or sub.get("excused"):
        return False
    return sub.get("workflow_state") in ("submitted", "pending_review") or (
        sub.get("workflow_state") == "graded" and sub.get("grade_matches_current_submission") is False
    )


@mcp.tool()
def list_submissions(course_id: int, assignment_id: int, filter: Filter = "needs_grading") -> dict:
    """Students' submissions for an assignment with status, lateness and current score."""
    require_grader(course_id)
    c = client()
    a = c.assignment(course_id, assignment_id)
    policy = c.late_policy(course_id)
    staged = {e["user_id"] for e in staging.list(course_id, assignment_id)}
    checks = {
        "all": lambda s: True,
        "needs_grading": needs_grading,
        "submitted": lambda s: bool(s.get("submitted_at")),
        "graded": lambda s: s.get("workflow_state") == "graded",
        "late": lambda s: bool(s.get("late")),
        "missing": lambda s: bool(s.get("missing")),
        "unsubmitted": lambda s: not s.get("submitted_at"),
    }
    rows = []
    for s in c.submissions(course_id, assignment_id):
        if not checks[filter](s):
            continue
        late = lateness.analyze(s, a, policy)
        rows.append({
            "user_id": s["user_id"],
            "student": student_name(s),
            "state": s.get("workflow_state"),
            "submitted_at": s.get("submitted_at"),
            "attempt": s.get("attempt"),
            "due_at": late["due_at"],
            "late": late["late"],
            "days_late": late["days_late"],
            "missing": late["missing"],
            "excused": late["excused"],
            "score": s.get("score"),
            "grade": s.get("grade"),
            "staged": s["user_id"] in staged,
            "speedgrader_url": c.speedgrader_url(course_id, assignment_id, s["user_id"]),
        })
    return {"assignment": a.get("name"), "filter": filter, "count": len(rows), "submissions": rows}


@mcp.tool()
def get_submission(course_id: int, assignment_id: int, user_id: int, download_files: bool = True) -> dict:
    """A student's submitted work as text (text entry, URL, PDF/DOCX/code/notebook files), lateness, previous comments and grade."""
    require_grader(course_id)
    c = client()
    a = c.assignment(course_id, assignment_id)
    s = c.submission(course_id, assignment_id, user_id)
    content: dict[str, Any] = {"type": s.get("submission_type")}
    if s.get("body"):
        content["text"] = truncate(html_to_text(s["body"]))
    if s.get("url"):
        content["url"] = s["url"]
    files = []
    for att in s.get("attachments") or []:
        entry: dict[str, Any] = {"filename": att.get("display_name") or att.get("filename"), "size": att.get("size"), "content_type": att.get("content-type")}
        if download_files and att.get("url"):
            dest = cache_dir() / str(course_id) / str(assignment_id) / str(user_id) / f"{att['id']}-{att.get('filename') or 'file'}"
            if not dest.exists():
                c.download(att["url"], dest)
            entry["local_path"] = str(dest)
            entry.update(extract_file(dest))
        files.append(entry)
    if files:
        content["files"] = files
    if s.get("media_comment"):
        content["media"] = "Audio/video submission; review it in SpeedGrader."
    if s.get("submission_type") in ("online_quiz", "discussion_topic", "external_tool", "student_annotation"):
        content["note"] = f"{s['submission_type']} submissions are best reviewed in SpeedGrader."
    if not s.get("submitted_at"):
        content["note"] = "Nothing submitted."

    return {
        "student": student_name(s),
        "user_id": user_id,
        "attempt": s.get("attempt"),
        "attempts_total": len(s.get("submission_history") or []),
        "workflow_state": s.get("workflow_state"),
        "lateness": lateness.analyze(s, a, c.late_policy(course_id)),
        "content": content,
        "current_grade": {"score": s.get("score"), "grade": s.get("grade"), "entered_score": s.get("entered_score")},
        "rubric_assessment": s.get("rubric_assessment"),
        "previous_comments": [
            {"author": cm.get("author_name"), "at": cm.get("created_at"), "text": cm.get("comment")}
            for cm in s.get("submission_comments") or []
        ],
        "points_possible": a.get("points_possible"),
        "speedgrader_url": c.speedgrader_url(course_id, assignment_id, user_id),
    }


# --- staging & posting ---------------------------------------------------------


class RubricScore(BaseModel):
    points: float
    comments: str | None = None
    rating_id: str | None = None


def normalize_grade(a: dict, grade: str | float | None) -> tuple[str | None, list[str]]:
    if grade is None:
        return None, []
    warnings: list[str] = []
    gtype = a.get("grading_type")
    text = str(grade).strip()
    if gtype == "pass_fail":
        lowered = text.lower()
        if lowered not in ("complete", "incomplete", "pass", "fail"):
            raise ToolError("Pass/fail assignment: grade must be complete, incomplete, pass or fail.")
        return lowered, warnings
    if gtype == "percent" and not text.endswith("%"):
        try:
            float(text)
        except ValueError:
            raise ToolError(f"Percent assignment: expected a number or 'NN%', got {text!r}") from None
        text += "%"
    if gtype == "points":
        try:
            value = float(text)
        except ValueError:
            raise ToolError(f"Points assignment: expected a number, got {text!r}") from None
        if value < 0:
            raise ToolError("Score can't be negative.")
        possible = a.get("points_possible") or 0
        if value > possible:
            warnings.append(f"Score {value:g} is above points possible ({possible:g}) - extra credit?")
    return text, warnings


def rubric_payload(a: dict, rubric: dict[str, RubricScore]) -> tuple[dict[str, Any], float]:
    criteria = {c["id"]: c for c in a.get("rubric") or []}
    if not criteria:
        raise ToolError("This assignment has no rubric.")
    payload: dict[str, Any] = {}
    total = 0.0
    for crit_id, score in rubric.items():
        crit = criteria.get(crit_id)
        if crit is None:
            raise ToolError(f"Unknown rubric criterion {crit_id!r}. Valid ids: {list(criteria)}")
        if score.points < 0 or score.points > (crit.get("points") or 0):
            raise ToolError(f"Criterion {crit.get('description')!r}: points must be 0-{crit.get('points')}")
        if score.rating_id and score.rating_id not in {r.get("id") for r in crit.get("ratings") or []}:
            raise ToolError(f"Unknown rating_id {score.rating_id!r} for criterion {crit_id!r}")
        payload[f"rubric_assessment[{crit_id}][points]"] = score.points
        if score.rating_id:
            payload[f"rubric_assessment[{crit_id}][rating_id]"] = score.rating_id
        if score.comments:
            payload[f"rubric_assessment[{crit_id}][comments]"] = score.comments
        total += score.points
    return payload, total


@mcp.tool()
def stage_grade(
    course_id: int,
    assignment_id: int,
    user_id: int,
    rationale: Annotated[str, Field(description="Private note for the grader: why this grade (not shown to the student)")],
    grade: Annotated[str | float | None, Field(description="Raw score/grade before any automatic late deduction, e.g. 8.5, '85%', 'A-', 'complete'")] = None,
    comment: Annotated[str | None, Field(description="Feedback the student will see: what is missing/wrong and how to improve")] = None,
    rubric: Annotated[dict[str, RubricScore] | None, Field(description="criterion_id -> {points, comments?, rating_id?}")] = None,
    excuse: bool = False,
    group_comment: Annotated[bool | None, Field(description="Group assignments: send the comment to every member (default yes)")] = None,
) -> dict:
    """Save a proposed grade/comment locally (nothing is sent to Canvas). Review with list_staged, send with post_staged_grades."""
    require_grader(course_id)
    c = client()
    a = c.assignment(course_id, assignment_id)
    writable(a)
    if grade is None and not rubric and not comment and not excuse:
        raise ToolError("Nothing to stage: give a grade, rubric scores, a comment, or excuse=true.")
    posted, warnings = normalize_grade(a, grade)
    rubric_fields: dict[str, Any] = {}
    if rubric:
        rubric_fields, rubric_total = rubric_payload(a, rubric)
        if a.get("use_rubric_for_grading"):
            if posted is not None and a.get("grading_type") == "points" and abs(float(posted) - rubric_total) > 1e-6:
                warnings.append(f"Rubric is used for grading, so Canvas will set the score to the rubric total ({rubric_total:g}), not {posted}.")
            posted = None  # Canvas derives the score from the rubric
    s = c.submission(course_id, assignment_id, user_id)
    raw = float(posted) if posted is not None and a.get("grading_type") == "points" else None
    late = lateness.analyze(s, a, c.late_policy(course_id), raw_score=raw)

    entry = staging.put({
        "course_id": course_id,
        "assignment_id": assignment_id,
        "user_id": user_id,
        "student": student_name(s),
        "assignment": a.get("name"),
        "attempt": s.get("attempt"),
        "posted_grade": posted,
        "excuse": excuse,
        "comment": comment,
        "group_comment": is_group(a) if group_comment is None else group_comment,
        "rubric_fields": rubric_fields,
        "rationale": rationale,
        "lateness": late,
        "warnings": warnings,
    })
    deduction = (late.get("automatic_deduction") or {}).get("points")
    return {
        "staged": True,
        "student": entry["student"],
        "grade": posted if not rubric_fields or posted else "from rubric",
        "canvas_will_deduct_for_lateness": deduction,
        "comment": comment,
        "warnings": warnings,
        "visibility_after_posting": visibility_note(a),
    }


@mcp.tool()
def list_staged(course_id: int | None = None, assignment_id: int | None = None) -> list[dict]:
    """Grades and comments staged locally but not yet posted to Canvas."""
    return staging.list(course_id, assignment_id)


@mcp.tool()
def discard_staged(course_id: int, assignment_id: int, user_ids: list[int] | None = None) -> str:
    """Delete staged grades (all for the assignment, or only the given students). Does not touch Canvas."""
    removed = staging.remove(course_id, assignment_id, user_ids)
    return f"Discarded {removed} staged grade(s)."


def submission_payload(entry: dict) -> dict[str, Any]:
    data: dict[str, Any] = dict(entry.get("rubric_fields") or {})
    if entry.get("excuse"):
        data["submission[excuse]"] = "true"
    elif entry.get("posted_grade") is not None:
        data["submission[posted_grade]"] = entry["posted_grade"]
    if entry.get("comment"):
        data["comment[text_comment]"] = entry["comment"]
        if entry.get("attempt"):
            data["comment[attempt]"] = entry["attempt"]
        if entry.get("group_comment"):
            data["comment[group_comment]"] = "true"
    return data


@mcp.tool()
def post_staged_grades(
    course_id: int,
    assignment_id: int,
    user_ids: Annotated[list[int] | None, Field(description="Only these students; default all staged for the assignment")] = None,
    confirm: Annotated[bool, Field(description="false = preview only. Set true only after the user has approved the staged grades.")] = False,
) -> dict:
    """Send staged grades/comments to Canvas (they appear in SpeedGrader). Without confirm=true, only previews."""
    require_grader(course_id)
    c = client()
    a = c.assignment(course_id, assignment_id)
    writable(a)
    entries = [e for e in staging.list(course_id, assignment_id) if user_ids is None or e["user_id"] in user_ids]
    if not entries:
        raise ToolError("No staged grades for that assignment (or those students).")
    preview = [
        {"student": e["student"], "grade": e.get("posted_grade") or ("excused" if e.get("excuse") else "rubric" if e.get("rubric_fields") else None),
         "comment": e.get("comment"), "late": e["lateness"].get("late"), "warnings": e.get("warnings")}
        for e in entries
    ]
    if not confirm:
        return {"preview": preview, "count": len(entries), "visibility": visibility_note(a),
                "next": "Show this to the user. If they approve, call again with confirm=true."}

    posted, failed = [], []
    for e in entries:
        try:
            result = c.update_submission(course_id, assignment_id, e["user_id"], submission_payload(e))
        except Exception as exc:
            failed.append({"student": e["student"], "error": str(exc)})
            continue
        staging.remove(course_id, assignment_id, [e["user_id"]])
        posted.append({
            "student": e["student"],
            "score": result.get("score"),
            "grade": result.get("grade"),
            "points_deducted": result.get("points_deducted"),
            "speedgrader_url": c.speedgrader_url(course_id, assignment_id, e["user_id"]),
        })
    return {"posted": posted, "failed": failed, "visibility": visibility_note(a)}


@mcp.tool()
def post_comment(
    course_id: int,
    assignment_id: int,
    user_id: int,
    text: str,
    confirm: Annotated[bool, Field(description="false = preview only; true only after the user approves the text")] = False,
    group_comment: bool | None = None,
) -> dict:
    """Post a feedback comment on a student's submission without changing the grade."""
    require_grader(course_id)
    c = client()
    a = c.assignment(course_id, assignment_id)
    s = c.submission(course_id, assignment_id, user_id)
    group = is_group(a) if group_comment is None else group_comment
    if not confirm:
        return {"preview": {"student": student_name(s), "comment": text, "to_whole_group": group},
                "visibility": visibility_note(a), "next": "Call again with confirm=true once the user approves."}
    data: dict[str, Any] = {"comment[text_comment]": text}
    if s.get("attempt"):
        data["comment[attempt]"] = s["attempt"]
    if group:
        data["comment[group_comment]"] = "true"
    c.update_submission(course_id, assignment_id, user_id, data)
    return {"posted": True, "student": student_name(s), "speedgrader_url": c.speedgrader_url(course_id, assignment_id, user_id)}


@mcp.tool()
def open_in_speedgrader(course_id: int, assignment_id: int, user_id: int | None = None) -> str:
    """Open SpeedGrader in the user's browser (optionally on a specific student)."""
    url = client().speedgrader_url(course_id, assignment_id, user_id)
    webbrowser.open(url)
    return f"Opened {url}"


@mcp.tool()
def clear_downloaded_files() -> str:
    """Delete locally cached copies of student submission files."""
    path = cache_dir()
    if path.exists():
        shutil.rmtree(path)
    return f"Removed {path}"


# --- entry point ---------------------------------------------------------------


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def configure(args: list[str]) -> None:
    import argparse
    import os

    from .config import normalize_base_url

    parser = argparse.ArgumentParser(prog="canvas-grader-mcp configure")
    parser.add_argument("--url", help="Canvas URL, e.g. https://canvas.school.edu (or CANVAS_BASE_URL)")
    parser.add_argument("--token", help="Canvas access token (or CANVAS_API_TOKEN)")
    opts = parser.parse_args(args)

    print("Canvas grader setup. Your token is stored only on this computer.")
    base = opts.url or os.environ.get("CANVAS_BASE_URL") or _ask("Canvas URL (e.g. https://canvas.school.edu): ")
    if not base:
        sys.exit("No Canvas URL given. Pass --url https://canvas.school.edu")
    token = opts.token or os.environ.get("CANVAS_API_TOKEN")
    if not token:
        print("Create a token in Canvas: Account > Settings > Approved Integrations > + New Access Token")
        token = _ask("Access token: ")
    token = token.strip()
    if not token:
        sys.exit(
            "No access token given. If you can't type at the prompt (e.g. running via `!` in Claude Code), "
            "pass it directly: canvas-grader-mcp configure --url <url> --token <token>"
        )

    settings = Settings(normalize_base_url(base), token)
    probe = CanvasClient(settings)
    try:
        me = probe.me()
    except Exception as exc:
        sys.exit(f"Could not sign in to {settings.base_url}: {exc}")
    finally:
        probe.close()
    print(f"Signed in as {me.get('name')}. Saved to {settings.save()}")


def check() -> None:
    try:
        me = whoami()
        courses = list_grading_courses()
    except (ToolError, ConfigError) as exc:
        sys.exit(str(exc))
    print(f"Connected to {me['canvas']} as {me['name']}")
    print(f"Courses you can grade: {len(courses)}")
    for course in courses:
        print(f"  {course['course_id']}: {course['name']} ({', '.join(course['roles'])})")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command == "configure":
        configure(sys.argv[2:])
    elif command == "check":
        check()
    elif command in ("serve", "stdio"):
        mcp.run()
    else:
        sys.exit("usage: canvas-grader-mcp [serve | configure [--url URL --token TOKEN] | check]")


if __name__ == "__main__":
    main()
