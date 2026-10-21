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
from canvas_grader_mcp.guidelines import GuidelineStore, LateTier, tier_penalty
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
    "attempt": 2,
    "body": "<p>See attached.</p>",
    "attachments": [
        {"id": 7, "filename": "fizz.py", "display_name": "fizz.py", "url": "https://files.test/7?verifier=x", "size": 40},
        {"id": 8, "filename": "report.docx", "display_name": "report.docx", "url": "https://files.test/8?verifier=y", "size": 1000},
    ],
    "submission_comments": [],
}
SUB_ALICE["submission_history"] = [
    {"attempt": 1, "submitted_at": "2026-09-30T12:00:00Z", "late": False, "seconds_late": 0, "body": "<p>First try</p>", "attachments": []},
    {k: v for k, v in SUB_ALICE.items() if k != "submission_history"},
]
SUB_CAROL = {
    "user_id": 102, "user": {"sortable_name": "Poe, Carol"}, "workflow_state": "submitted",
    "submitted_at": "2026-09-30T12:00:00Z", "late": False, "seconds_late": 0, "attempt": 1,
    "attachments": [{"id": 9, "filename": "fizz.py", "display_name": "fizz.py", "url": "https://files.test/7?verifier=z", "size": 40}],
}
SUB_BOB = {"user_id": 101, "user": {"sortable_name": "Roe, Bob"}, "workflow_state": "unsubmitted", "submitted_at": None, "missing": True}


def make_png(color: str = "red", size: tuple[int, int] = (200, 120)) -> bytes:
    from PIL import Image as PILImage

    buf = io.BytesIO()
    PILImage.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def make_pdf_with_image() -> bytes:
    from PIL import Image as PILImage

    buf = io.BytesIO()
    PILImage.new("RGB", (300, 400), "blue").save(buf, "PDF")
    return buf.getvalue()


def make_docx() -> bytes:
    import docx

    doc = docx.Document()
    doc.add_paragraph("My report on fizzbuzz.")
    doc.add_picture(io.BytesIO(make_png("red")))
    doc.add_picture(io.BytesIO(make_png("red")))  # duplicate screenshot, should be returned once
    doc.add_picture(io.BytesIO(make_png("green", (20, 20))))  # icon-sized, should be skipped
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
        if api == "/courses/1/custom_gradebook_columns":
            if method == "POST":
                self.notes_col = {"id": 5, "title": "Notes", "teacher_notes": True}
                return httpx.Response(200, json=self.notes_col)
            return httpx.Response(200, json=[self.notes_col] if getattr(self, "notes_col", None) else [])
        if api == "/courses/1/custom_gradebook_columns/5/data":
            return httpx.Response(200, json=[{"user_id": u, "content": t} for u, t in getattr(self, "notes", {}).items()])
        if api.startswith("/courses/1/custom_gradebook_columns/5/data/") and method == "PUT":
            self.notes = getattr(self, "notes", {})
            self.notes[int(api.rsplit("/", 1)[1])] = parse_qs(request.content.decode())["column_data[content]"][0]
            return httpx.Response(200, json={})
        if api == "/courses/1/assignments/10":
            return httpx.Response(200, json=self.assignment)
        if api == "/courses/1/assignments/10/submissions":
            if url.params.get("page") == "2":
                return httpx.Response(200, json=[SUB_BOB, SUB_CAROL])
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
    monkeypatch.setattr(server, "guideline_store", GuidelineStore(tmp_path / "guidelines.json"))
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
    assert server.list_submissions(1, 10, "all")["count"] == 3
    pending = server.list_submissions(1, 10)["submissions"]
    assert [s["student"] for s in pending] == ["Doe, Alice", "Poe, Carol"]
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
    assert fake.puts == [{"submission[posted_grade]": "9", "comment[text_comment]": "Add unit tests for 15.", "comment[attempt]": "2"}]
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


def test_configure_rejects_blank_token(tmp_path, monkeypatch):
    monkeypatch.setenv("CANVAS_GRADER_CONFIG", str(tmp_path / "c.json"))
    monkeypatch.delenv("CANVAS_API_TOKEN", raising=False)
    monkeypatch.setattr("builtins.input", lambda _: "")
    with pytest.raises(SystemExit, match="No access token"):
        server.configure(["--url", "canvas.test"])
    assert not (tmp_path / "c.json").exists()


def test_submission_images(fake):
    from mcp.server.mcpserver import Image

    sub = server.get_submission(1, 10, 100)
    assert "get_submission_images" in sub["content"]["visual_content"]
    out = server.get_submission_images(1, 10, 100)
    assert isinstance(out[0], str) and "1 image(s)" in out[0]
    assert len(out) == 2 and isinstance(out[1], Image)


def test_pdf_pages_with_images_are_rendered(tmp_path):
    from canvas_grader_mcp.extract import extract_file
    from canvas_grader_mcp.images import collect

    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(make_pdf_with_image())
    info = extract_file(pdf)
    assert info["pages_with_images"] == [1] and "scanned" in info["note"]
    found, total = collect([pdf], max_images=5)
    assert total == 1 and found[0][0] == "scan.pdf page 1"


def test_html_list_items_with_paragraphs():
    assert html_to_text("<ul><li><p>Screenshots</p></li><li><p>Video</p></li></ul>") == "- Screenshots\n\n- Video"


def test_zip_submission(tmp_path):
    import zipfile

    from canvas_grader_mcp.extract import extract_file
    from canvas_grader_mcp.images import collect

    z = tmp_path / "lab.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Lab/report.pdf", make_pdf_with_image())
        zf.writestr("Lab/notes.txt", "jump height 20mm")
        zf.writestr("Lab/video.mp4", b"\x00" * 100)
        zf.writestr("../evil.txt", "nope")
        zf.writestr("__MACOSX/Lab/._notes.txt", "junk")
    info = extract_file(z)
    kinds = {m["filename"]: m["kind"] for m in info["members"]}
    assert kinds == {"Lab/report.pdf": "pdf", "Lab/notes.txt": "text", "Lab/video.mp4": "media"}
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "lab.zip.d" / "Lab" / "video.mp4").exists()
    found, _ = collect([z], 5)
    assert [label for label, _ in found] == ["report.pdf page 1"]


def make_pdf_with_link(url: str) -> bytes:
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Annots [4 0 R] >>",
        f"<< /Type /Annot /Subtype /Link /Rect [10 10 100 40] /A << /S /URI /URI ({url}) >> >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def test_pdf_hyperlinks_are_extracted(tmp_path):
    from canvas_grader_mcp.extract import extract_file

    pdf = tmp_path / "video.pdf"
    pdf.write_bytes(make_pdf_with_link("https://youtu.be/abc123"))
    assert "https://youtu.be/abc123" in extract_file(pdf)["text"]


def test_earlier_attempt(fake):
    latest = server.get_submission(1, 10, 100)
    assert [a["attempt"] for a in latest["attempts"]] == [1, 2]
    first = server.get_submission(1, 10, 100, attempt=1)
    assert first["attempt"] == 1 and first["content"]["text"] == "First try" and "files" not in first["content"]
    with pytest.raises(ToolError, match="No attempt 7"):
        server.get_submission(1, 10, 100, attempt=7)


LAB_TIERS = [LateTier(under_days=1, percent=0), LateTier(under_days=7, percent=5, min_points=1), LateTier(under_days=None, percent=10, min_points=3)]


@pytest.mark.parametrize("days,points,expected", [
    (0.02, 100, 0), (0.9, 100, 0), (1.5, 100, 5), (6.5, 100, 5), (8, 100, 10),
    (3, 10, 1), (8, 10, 3), (3, 50, 3), (3, 20, 1),
])
def test_late_tiers(days, points, expected):
    assert tier_penalty(int(days * 86400), points, LAB_TIERS)["points"] == expected


def test_guidelines_flow_into_assignment_and_lateness(fake):
    server.set_grading_guidelines(1, guidelines="Be lenient.", late_penalty_tiers=LAB_TIERS)
    assert server.get_assignment(1, 10)["grading_guidelines"]["guidelines"] == "Be lenient."
    late = server.get_submission(1, 10, 100)["lateness"]  # Alice: ~2 days late, but course 1 has an automatic policy
    assert late["automatic_deduction"]["points"] == 2.0 and "suggested_penalty" not in late
    manual = server.lateness.analyze({"late": True, "seconds_late": 2 * 86400}, {"points_possible": 10}, None, tiers=LAB_TIERS)
    assert manual["suggested_penalty"]["points"] == 1 and "late tiers" in manual["note"]


def test_stage_with_breakdown(fake):
    from canvas_grader_mcp.breakdown import Adjustment, Category

    cats = [Category(name="Program", earned=15, max=20, reason="no forever loop"), Category(name="Video", earned=10, max=10)]
    r = server.stage_grade(1, 10, 100, rationale="r", comment="Nice work.", breakdown=cats,
                           late_penalty=Adjustment(points=1, reason="2 days late"), extra_credit=[Adjustment(points=3, reason="Python")])
    assert r["grade"] == "27"
    assert r["comment"].splitlines() == [
        "Nice work.", "", "Score breakdown:", "- Program: 15/20 (no forever loop)", "- Video: 10/10",
        "- Late penalty: -1 (2 days late)", "- Extra credit: +3 (Python)", "Total: 27/30"]
    with pytest.raises(ToolError, match="doesn't match"):
        server.stage_grade(1, 10, 100, rationale="r", grade=5, breakdown=cats)
    with pytest.raises(ToolError, match="more than the category maximum"):
        server.stage_grade(1, 10, 100, rationale="r", breakdown=[Category(name="Video", earned=11, max=10)])


def test_find_shared_files(fake):
    groups = server.find_shared_files(1, 10)
    assert [sorted(s["student"] for s in g["students"]) for g in groups] == [["Doe, Alice", "Poe, Carol"]]


def test_sb3_projects_are_listed(tmp_path):
    import zipfile

    from canvas_grader_mcp.extract import extract_file

    blocks = {
        "a": {"opcode": "event_whenflagclicked", "next": "b", "topLevel": True},
        "b": {"opcode": "control_forever", "next": None, "inputs": {"SUBSTACK": [2, "c"]}},
        "c": {"opcode": "motion_movesteps", "next": "d", "inputs": {"STEPS": [1, [4, "10"]]}},
        "d": {"opcode": "looks_seteffectto", "next": None, "fields": {"EFFECT": ["COLOR", None]}},
    }
    sb3 = tmp_path / "lab.sb3"
    with zipfile.ZipFile(sb3, "w") as zf:
        zf.writestr("project.json", json.dumps({"targets": [{"name": "Sprite1", "blocks": blocks}]}))
    assert extract_file(sb3)["text"].splitlines() == [
        "--- Sprite1 script 1 ---", "event_whenflagclicked", "control_forever", "  do:",
        "    motion_movesteps STEPS=10", "    looks_seteffectto EFFECT=COLOR"]


def test_qr_codes_are_decoded(tmp_path):
    import zxingcpp
    from PIL import Image as PILImage

    from canvas_grader_mcp.images import qr_codes

    code = zxingcpp.create_barcode("https://youtu.be/demo", zxingcpp.BarcodeFormat.QRCode).to_image(scale=6)
    page = PILImage.new("L", (600, 400), 255)
    page.paste(PILImage.frombuffer("L", (code.shape[1], code.shape[0]), bytes(code)), (50, 50))
    page.save(tmp_path / "video.png")
    assert [c["text"] for c in qr_codes([tmp_path / "video.png"])] == ["https://youtu.be/demo"]


def test_linked_google_docs(fake, monkeypatch, tmp_path):
    from canvas_grader_mcp import linked

    url = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcd/edit?usp=sharing"
    assert linked.google_links(f"see {url} and {url}") == [("document", "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcd", url.split("/edit")[0])]

    def fake_fetch(kind, doc_id, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(make_pdf_with_link("https://youtu.be/inside"))
        return dest, None

    monkeypatch.setattr(linked, "fetch", fake_fetch)
    files = server.linked_documents(1, 10, 100, f"my work: {url}")
    assert files[0]["kind"] == "pdf" and "youtu.be/inside" in files[0]["text"] and "live Google link" in files[0]["note"]
    monkeypatch.setattr(linked, "fetch", lambda *a: (None, "not publicly shared"))
    assert "not publicly shared" in server.linked_documents(1, 10, 100, url)[0]["note"]


def test_gradebook_notes(fake):
    preview = server.add_gradebook_note(1, 100, "missing video", label="Lab 1")
    assert preview["creates_notes_column"] and not getattr(fake, "notes_col", None)  # preview writes nothing
    server.add_gradebook_note(1, 100, "missing video", label="Lab 1", confirm=True)
    server.add_gradebook_note(1, 100, "late", label="Lab 2", confirm=True)
    assert server.get_gradebook_notes(1, 100)["notes"][100] == "[Lab 1] missing video\n[Lab 2] late"
    assert server.add_gradebook_note(1, 100, "late", label="Lab 2", confirm=True)["unchanged"]


def make_video(path, seconds=2):
    import subprocess

    from canvas_grader_mcp.media import ffmpeg_exe

    subprocess.run([ffmpeg_exe(), "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10:duration={seconds}",
                    "-pix_fmt", "yuv420p", "-f", "mp4", str(path)], check=True)


def test_extensionless_files_are_detected(tmp_path):
    from canvas_grader_mcp.extract import extract_file
    from canvas_grader_mcp.images import collect

    video = tmp_path / "Lab 1.6"  # what Canvas gives for an upload named without an extension
    make_video(video)
    info = extract_file(video)
    assert info["kind"] == "media" and info["detected_type"] == ".mp4" and info["duration_seconds"] == 2.0
    photo = tmp_path / "Lab 1.6 code"
    photo.write_bytes(make_png("blue", (300, 200)))
    assert extract_file(photo)["kind"] == "image"
    found, _ = collect([photo, video], max_images=10)
    assert [label for label, _ in found] == ["Lab 1.6 code"]  # videos only when frames are requested
    found, _ = collect([video], max_images=10, frames_per_video=3)
    assert [label.split(" at ")[0] for label, _ in found] == ["Lab 1.6"] * 3


def test_sniff_office_and_projects(tmp_path):
    import zipfile

    from canvas_grader_mcp.media import sniff

    for name, member, expected in (("a", "word/document.xml", ".docx"), ("b", "project.json", ".sb3"), ("c", "x.txt", ".zip")):
        with zipfile.ZipFile(tmp_path / name, "w") as zf:
            zf.writestr(member, "{}")
        assert sniff(tmp_path / name) == expected
    (tmp_path / "d").write_bytes(make_pdf_with_image())
    assert sniff(tmp_path / "d") == ".pdf"


def test_classify_links():
    from canvas_grader_mcp.media import classify_link, find_urls

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "drive.google.com/file/d/VIDEO" in url:
            return httpx.Response(200, text='<meta property="og:title" content="Lab.mp4">"video/mp4"')
        if "drive.google.com/file/d/PRIVATE" in url:
            return httpx.Response(302, headers={"Location": "https://accounts.google.com/signin"})
        if "accounts.google.com" in url:
            return httpx.Response(200, text="sign in")
        if url.startswith("https://photos.app.goo.gl/"):
            return httpx.Response(302, headers={"Location": "https://photos.google.com/share/ABC"})
        if "photos.google.com/share/ABC" in url:
            return httpx.Response(200, text='<meta property="og:title" content="New video">"video/mp4"')
        raise AssertionError(f"unexpected fetch {url}")

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    kinds = {u: classify_link(u, http)["kind"] for u in [
        "https://drive.google.com/file/d/VIDEO/view", "https://drive.google.com/file/d/PRIVATE/view",
        "https://photos.app.goo.gl/xyz", "https://youtu.be/abc", "https://example.com/page"]}
    assert kinds == {"https://drive.google.com/file/d/VIDEO/view": "video", "https://drive.google.com/file/d/PRIVATE/view": "not_public",
                     "https://photos.app.goo.gl/xyz": "video", "https://youtu.be/abc": "video", "https://example.com/page": "webpage"}
    assert find_urls("see https://youtu.be/abc. and (https://example.com/x)") == ["https://youtu.be/abc", "https://example.com/x"]


LI_TEXT_POST = (
    '<meta property="og:title" content="#robotics | Sam Student"><meta property="og:type" content="article">'
    '<p class="attributed-text-segment-list__content text-color-text">Learned about automata in Professor X&#39;s class.<br>'
    'Design, prototype, test.<br><a href="https://www.linkedin.com/feed/hashtag/robotics">#Robotics</a></p>'
    '<img data-delayed-url="https://media.licdn.com/dms/image/v2/ABC/feedshare-shrink_800/0/1?e=1&amp;v=beta">'
    '"datePublished":"2026-09-09T04:44:51Z"'
)
LI_VIDEO_POST = '<meta property="og:type" content="video.other"><meta property="og:description" content="Our conveyor lab #Dobot"><div class="video-js x"></div>'
LI_ARTICLE = (
    '<meta property="og:type" content="article">"articleBody":"Conveyor and AI vision labs.\\nThanks team! #Dobot",'
    '"author":{"@type":"Person","name":"Pat Student"},'
    '\\"https://media.licdn.com/dms/image/v2/IMG1/feedshare-\\",\\"imageRenditions\\":[{\\"width\\":160,\\"height\\":200,\\"suffixUrl\\":\\"shrink_160/a?e=1\\u0026t=x\\"},{\\"width\\":800,\\"height\\":900,\\"suffixUrl\\":\\"shrink_800/a?e=1\\u0026t=x\\"}],'
    '\\"https://media.licdn.com/dms/image/v2/IMG2/feedshare-\\",\\"imageRenditions\\":[{\\"width\\":800,\\"height\\":600,\\"suffixUrl\\":\\"shrink_800/b?e=1\\"}]'
)


@pytest.fixture
def web(monkeypatch):
    from canvas_grader_mcp import webpage

    pages = {
        "https://www.linkedin.com/posts/text": (200, LI_TEXT_POST),
        "https://www.linkedin.com/posts/video": (200, LI_VIDEO_POST),
        "https://www.linkedin.com/posts/article": (200, LI_ARTICLE),
        "https://example.edu/portfolio": (200, "<title>Portfolio</title><main><h1>My robot</h1><p>It sorts cubes.</p><video src='a.mp4'></video></main>"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://lnkd.in/p/short":
            return httpx.Response(302, headers={"Location": "https://www.linkedin.com/posts/text"})
        if url == "https://lnkd.in/p/private":
            return httpx.Response(302, headers={"Location": "https://www.linkedin.com/authwall?trk=x"})
        if "media.licdn.com" in url:
            return httpx.Response(200, content=make_png("red", (300, 200)), headers={"content-type": "image/png"})
        status, body = pages.get(url, (404, "not found"))
        return httpx.Response(status, text=body)

    client = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "test"})
    monkeypatch.setattr(webpage, "_check_public", lambda url: None)
    real_fetch, real_download = webpage.fetch, webpage.download_images
    monkeypatch.setattr(webpage, "fetch", lambda url, http=None: real_fetch(url, client))
    monkeypatch.setattr(webpage, "download_images", lambda urls, limit, http=None: real_download(urls, limit, client))
    return webpage


def test_linkedin_text_post(web):
    page = web.read("https://lnkd.in/p/short")
    assert page["final_url"] == "https://www.linkedin.com/posts/text" and page["site"] == "linkedin"
    assert page["text"] == "Learned about automata in Professor X's class.\nDesign, prototype, test.\n#Robotics"
    assert page["hashtags"] == ["#Robotics"] and len(page["images"]) == 1 and not page["has_video"]
    assert page["author"] == "Sam Student" and page["published"].startswith("2026-09-09")


def test_linkedin_video_and_article_posts(web):
    video = web.read("https://www.linkedin.com/posts/video")
    assert video["has_video"] and video["text"] == "Our conveyor lab #Dobot"
    assert web._linkedin('<meta property="og:type" content="article"><video data-sources="[]"></video>')["has_video"]
    article = web.read("https://www.linkedin.com/posts/article")
    assert article["author"] == "Pat Student" and "Thanks team!" in article["text"]
    assert [u.split("/feedshare-")[1] for u in article["images"]] == ["shrink_800/a?e=1&t=x", "shrink_800/b?e=1"]


def test_private_and_generic_pages(web):
    assert "isn't public" in web.read("https://lnkd.in/p/private")["note"]
    site = web.read("https://example.edu/portfolio")
    assert "It sorts cubes." in site["text"] and site["has_video"]
    assert "HTTP 404" in web.read("https://example.edu/missing")["note"]


def test_view_webpage_returns_photos(web):
    from mcp.server.mcpserver import Image

    out = server.view_webpage("https://www.linkedin.com/posts/article")
    assert out[0]["photos"] == 2 and sum(isinstance(o, Image) for o in out[1:]) == 2


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/admin", "http://localhost/", "file:///etc/passwd", "http://10.0.0.5/"])
def test_local_addresses_are_refused(url):
    from canvas_grader_mcp import webpage

    with pytest.raises(webpage.PageError):
        webpage.read(url)
