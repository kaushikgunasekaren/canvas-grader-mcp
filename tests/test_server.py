"""End-to-end tool tests against a fake Canvas API (httpx.MockTransport)."""

from __future__ import annotations

import io
import json
import stat
from urllib.parse import parse_qs

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from canvas_grader_mcp import lateness, server
from canvas_grader_mcp.canvas import CanvasClient
from canvas_grader_mcp.config import Settings, normalize_base_url
from canvas_grader_mcp.extract import html_to_text
from canvas_grader_mcp.staging import StagingStore

BASE = "https://canvas.test"

ASSIGNMENT = {
    "id": 10,
    "name": "Lab 1",
    "description": "<p>Write <b>fizzbuzz</b>.</p><ul><li>Handle 15</li><li>Add tests</li></ul>",
    "points_possible": 10,
    "grading_type": "points",
    "due_at": "2026-10-01T06:59:59Z",
    "submission_types": ["online_upload", "online_text_entry"],
    "post_manually": True,
    "rubric": [
        {"id": "c1", "description": "Correctness", "points": 6, "ratings": [{"id": "r1", "points": 6, "description": "Full"}]},
        {"id": "c2", "description": "Tests", "points": 4, "ratings": [{"id": "r2", "points": 4, "description": "Full"}]},
    ],
    "use_rubric_for_grading": False,
}

SUB_ALICE = {
    "user_id": 100,
    "user": {"sortable_name": "Doe, Alice"},
    "workflow_state": "submitted",
    "submitted_at": "2026-10-02T12:00:00Z",
    "cached_due_date": "2026-10-01T06:59:59Z",
    "late": True,
    "seconds_late": 2 * 86400 - 100,
    "attempt": 1,
    "body": "<p>See attached.</p>",
    "attachments": [
        {"id": 7, "filename": "fizz.py", "display_name": "fizz.py", "url": "https://files.test/7?verifier=x", "size": 40},
        {"id": 8, "filename": "report.docx", "display_name": "report.docx", "url": "https://files.test/8?verifier=y", "size": 1000},
    ],
    "submission_comments": [],
}
SUB_BOB = {"user_id": 101, "user": {"sortable_name": "Roe, Bob"}, "workflow_state": "unsubmitted", "submitted_at": None, "missing": True}


def make_docx() -> bytes:
    import docx

    doc = docx.Document()
    doc.add_paragraph("My report on fizzbuzz.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


class FakeCanvas:
    def __init__(self) -> None:
        self.puts: list[dict] = []
        self.assignment = dict(ASSIGNMENT)
        self.docx = make_docx()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, path, method = request.url, request.url.path, request.method
        if url.host == "files.test":
            assert "authorization" not in request.headers  # token never leaves the Canvas host
            return httpx.Response(200, content=b"for i in range(1, 16): print(i)\n" if path == "/7" else self.docx)
        assert request.headers["Authorization"] == "Bearer tok"
        api = path.removeprefix("/api/v1")
        if api == "/users/self":
            return httpx.Response(200, json={"id": 1, "name": "Prof", "login_id": "prof"})
        if api == "/courses":
            return httpx.Response(200, json=[
                {"id": 1, "name": "CSE 110", "course_code": "CSE110", "enrollments": [{"type": "ta"}], "total_students": 2},
                {"id": 2, "name": "History", "enrollments": [{"type": "student"}]},
                {"id": 3, "name": "Observer-ish", "enrollments": [{"type": "designer"}]},
            ])
        if api.endswith("/permissions"):
            return httpx.Response(200, json={"manage_grades": api.startswith("/courses/1/"), "view_all_grades": True})
        if api == "/courses/1/late_policy":
            return httpx.Response(200, json={"late_policy": {
                "late_submission_deduction_enabled": True, "late_submission_deduction": 10,
                "late_submission_interval": "day", "late_submission_minimum_percent_enabled": False}})
        if api == "/courses/1/assignments/10":
            return httpx.Response(200, json=self.assignment)
        if api == "/courses/1/assignments/10/submissions":
            if url.params.get("page") == "2":
                return httpx.Response(200, json=[SUB_BOB])
            return httpx.Response(200, json=[SUB_ALICE], headers={"Link": f'<{BASE}/api/v1/courses/1/assignments/10/submissions?page=2>; rel="next"'})
        if api == "/courses/1/assignments/10/submissions/100":
            if method == "PUT":
                form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
                self.puts.append(form)
                return httpx.Response(200, json={"score": 7.0, "grade": "7", "points_deducted": 2.0})
            return httpx.Response(200, json=SUB_ALICE)
        return httpx.Response(404, json={"errors": [{"message": "not found"}]})


@pytest.fixture
def fake(tmp_path, monkeypatch):
    canvas = FakeCanvas()
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(server, "_client", CanvasClient(Settings(BASE, "tok"), transport=httpx.MockTransport(canvas)))
    monkeypatch.setattr(server, "_grader_ok", {})
    monkeypatch.setattr(server, "staging", StagingStore(tmp_path / "staged.json"))
    return canvas


def test_only_courses_with_grading_permission(fake):
    courses = server.list_grading_courses()
    assert [c["course_id"] for c in courses] == [1]
    with pytest.raises(ToolError, match="grading permission"):
        server.list_assignments(3)


def test_assignment_details(fake):
    a = server.get_assignment(1, 10)
    assert "fizzbuzz" in a["description"] and "- Handle 15" in a["description"]
    assert a["late_policy"]["automatic"] is True
    assert [c["criterion_id"] for c in a["rubric"]] == ["c1", "c2"]
    assert "hidden from students" in a["visibility"]


def test_list_submissions_paginates_and_filters(fake):
    assert server.list_submissions(1, 10, "all")["count"] == 2
    pending = server.list_submissions(1, 10)["submissions"]
    assert [s["student"] for s in pending] == ["Doe, Alice"]
    assert pending[0]["days_late"] == 2
    assert server.list_submissions(1, 10, "missing")["submissions"][0]["student"] == "Roe, Bob"


def test_get_submission_extracts_files(fake):
    sub = server.get_submission(1, 10, 100)
    files = {f["filename"]: f for f in sub["content"]["files"]}
    assert "range(1, 16)" in files["fizz.py"]["text"]
    assert "My report on fizzbuzz." in files["report.docx"]["text"]
    assert sub["content"]["text"] == "See attached."
    assert sub["lateness"]["automatic_deduction"]["percent"] == 20


def test_stage_preview_and_post(fake):
    staged = server.stage_grade(1, 10, 100, rationale="missing tests", grade=9, comment="Add unit tests for 15.")
    assert staged["canvas_will_deduct_for_lateness"] == 2.0  # 2 days x 10% of 10 points possible
    preview = server.post_staged_grades(1, 10)
    assert preview["count"] == 1 and not fake.puts  # preview never writes
    result = server.post_staged_grades(1, 10, confirm=True)
    assert result["posted"][0]["points_deducted"] == 2.0
    assert fake.puts == [{"submission[posted_grade]": "9", "comment[text_comment]": "Add unit tests for 15.", "comment[attempt]": "1"}]
    assert server.list_staged() == []


def test_rubric_staging(fake):
    from canvas_grader_mcp.server import RubricScore

    server.stage_grade(1, 10, 100, rationale="r", rubric={"c1": RubricScore(points=6, rating_id="r1"), "c2": RubricScore(points=2, comments="few tests")})
    server.post_staged_grades(1, 10, confirm=True)
    assert fake.puts[0]["rubric_assessment[c2][comments]"] == "few tests"
    with pytest.raises(ToolError, match="points must be"):
        server.stage_grade(1, 10, 100, rationale="r", rubric={"c2": RubricScore(points=5)})
    with pytest.raises(ToolError, match="Unknown rubric criterion"):
        server.stage_grade(1, 10, 100, rationale="r", rubric={"zz": RubricScore(points=1)})


def test_grade_validation(fake):
    assert server.stage_grade(1, 10, 100, rationale="r", grade=12)["warnings"]
    with pytest.raises(ToolError, match="expected a number"):
        server.stage_grade(1, 10, 100, rationale="r", grade="great")
    fake.assignment["moderated_grading"] = True
    with pytest.raises(ToolError, match="moderated"):
        server.stage_grade(1, 10, 100, rationale="r", grade=5)


def test_post_comment_requires_confirm(fake):
    assert "preview" in server.post_comment(1, 10, 100, "Nice work")
    assert not fake.puts
    server.post_comment(1, 10, 100, "Nice work", confirm=True)
    assert fake.puts[0]["comment[text_comment]"] == "Nice work"


def test_late_minimum_percent():
    policy = {"late_submission_deduction_enabled": True, "late_submission_deduction": 50, "late_submission_interval": "day",
              "late_submission_minimum_percent_enabled": True, "late_submission_minimum_percent": 60}
    sub = {"late": True, "seconds_late": 3 * 86400, "submitted_at": "x"}
    info = lateness.analyze(sub, {"points_possible": 10}, policy, raw_score=9)
    assert info["automatic_deduction"]["points"] == 3.0  # floored at 6/10


def test_no_policy_explains_manual_penalty():
    info = lateness.analyze({"late": True, "seconds_late": 3600}, {"points_possible": 10}, None)
    assert info["automatic_deduction"] is None and "yourself" in info["note"]


def test_html_to_text():
    assert html_to_text('<h2>Title</h2><p>See <a href="http://x">this</a></p><script>x()</script>') == "## Title\n\nSee this (http://x)"


def test_config_file_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("CANVAS_GRADER_CONFIG", str(tmp_path / "c.json"))
    monkeypatch.delenv("CANVAS_BASE_URL", raising=False)
    monkeypatch.delenv("CANVAS_API_TOKEN", raising=False)
    path = Settings(normalize_base_url("canvas.school.edu/api/v1/"), "tok").save()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert Settings.load().base_url == "https://canvas.school.edu"
    assert json.loads(path.read_text())["token"] == "tok"
