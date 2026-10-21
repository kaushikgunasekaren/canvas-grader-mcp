# canvas-grader-mcp

An [MCP](https://modelcontextprotocol.io) server that helps **teachers and TAs** grade [Canvas LMS](https://www.instructure.com/canvas) submissions with an AI assistant (Claude Code, Claude Desktop, Cursor, VS Code, …).

It reads each assignment's description, rubric, due dates and late policy, pulls in every student's submitted work as text, and lets the assistant propose a grade and written feedback about what's missing. Proposed grades are **staged on your computer first**; nothing reaches Canvas until you review it and approve posting. Posted grades and comments appear in SpeedGrader like any other.

> [!IMPORTANT]
> **Read [Privacy and responsible use](#privacy-and-responsible-use) before using this with real student work.** The server runs locally, but the student work it reads is sent to whichever AI model your MCP client uses.

## What it can do

- **Find your grading work**: every course where you're a teacher/TA with grading permission, assignments with how many submissions need grading.
- **Understand the assignment**: description (as clean text), points, grading type, rubric criteria and ratings, per-student due dates (overrides and extensions), the course's automatic late policy.
- **Read submissions**: text entries, URLs, and uploaded files: PDF, Word (.docx), Jupyter notebooks, Scratch projects (.sb3), zip archives, source code and other text files; publicly shared Google Docs/Slides/Sheets the student linked instead of uploading; links hidden behind text in PDFs; QR codes in screenshots; and files uploaded without (or with the wrong) extension, detected from their contents.
- **Check videos and links**: every link in a submission is classified (video, photo, document, not public), so a photo album isn't mistaken for the required video, and frames can be pulled from submitted videos, e.g. when a student recorded their code instead of taking screenshots. Screenshots and photos inside PDFs and Word files (and uploaded images) are passed to the AI as images, so program screenshots get graded too. Files are downloaded to a local cache only.
- **Account for due dates**: how late each submission was. If the course has an automatic late policy, it shows the deduction Canvas will apply (so you enter the raw score); if not, it flags that any penalty from the syllabus must be applied by the grader.
- **Your grading rules, per course**: save guidelines (how lenient to be, what earns extra credit, what counts as evidence) and a manual late-penalty scale for each course. They're stored only on your computer and shown to the assistant with every assignment in that course.
- **Score breakdowns**: stage a grade as per-category scores plus late penalty and extra credit; the tool totals it and writes the breakdown into the student's comment.
- **Consistency checks**: find students who submitted identical files, and read any earlier attempt when a student re-uploaded the wrong file.
- **Stage → review → post**: grades, rubric scores and comments are staged locally, previewed, then posted only with explicit confirmation. Comments explain to the student what was missing.
- **SpeedGrader links** for every submission, or open SpeedGrader directly in your browser.

## Requirements

- A Canvas account with **teacher, TA, or another role with "Grade submissions"** permission in the courses you want to grade.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (installs Python 3.10+ automatically). Works on Linux, macOS and Windows.

## Setup

### 1. Create a Canvas access token

In Canvas: **Account → Settings → Approved Integrations → + New Access Token**. Give it a purpose (e.g. "grading assistant") and an expiry date. Copy the token.

> Some schools disable personal access tokens. If you don't see the button, ask your Canvas administrator.

### 2. Save your credentials (stored only on your computer)

```bash
uvx --from git+https://github.com/kaushikgunasekaren/canvas-grader-mcp canvas-grader-mcp configure
```

This asks for your Canvas URL and token (or pass them as `configure --url <url> --token <token>` where you can't type at a prompt), checks that they work, and saves them to `~/.config/canvas-grader-mcp/config.json` (readable only by you; `%APPDATA%\canvas-grader-mcp` on Windows). Alternatively set the `CANVAS_BASE_URL` and `CANVAS_API_TOKEN` environment variables in your MCP client config.

Check it can see your courses:

```bash
uvx --from git+https://github.com/kaushikgunasekaren/canvas-grader-mcp canvas-grader-mcp check
```

### 3. Add it to your MCP client

**Claude Code**

```bash
claude mcp add canvas-grader --scope user -- uvx --from git+https://github.com/kaushikgunasekaren/canvas-grader-mcp canvas-grader-mcp
```

**Claude Desktop, Cursor and other JSON-configured clients**

```json
{
  "mcpServers": {
    "canvas-grader": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/kaushikgunasekaren/canvas-grader-mcp", "canvas-grader-mcp"]
    }
  }
}
```

**VS Code** (`.vscode/mcp.json`): the same, under `"servers"` instead of `"mcpServers"`.

## Using it

Example prompts:

- "Which of my courses have assignments that need grading?"
- "Grade the ungraded submissions for Lab 3 in CSE 110 using the rubric. Stage them, don't post yet."
- "Show me what you staged for Lab 3, with your reasoning."
- "Change Alice's score to 8 and soften the comment, then post all of them."
- "Leave a comment on Bob's Lab 3 submission saying his tests are missing."

The intended workflow is:

1. `get_assignment`: the assistant reads the description, rubric and late policy.
2. `get_submission` per student: reads their work and lateness, plus `get_submission_images` for screenshots/photos.
3. `stage_grade`: proposes a grade, rubric scores and a student-facing comment, plus a private rationale for you.
4. `list_staged` / `post_staged_grades` (preview): **you review**.
5. `post_staged_grades` with `confirm=true`: grades and comments go to Canvas.

### Per-course grading guidelines

Every course is graded differently, so rules live with the course, not in the tool. Tell the assistant once, for example:

> "For course 12345, save these grading guidelines: be lenient on short answers, give +1 for each optional challenge completed, and require a screenshot of working code. Late penalty: nothing under 1 day late, 5% under a week, 10% after that (at least 3 points)."

That calls `set_grading_guidelines`, which stores the rules in `~/.local/share/canvas-grader-mcp/guidelines.json` (owner-only). From then on, `get_assignment` includes them, and every late submission gets a `suggested_penalty` from your tiers. Different courses can have completely different rules.

If the assignment posts grades automatically, students see them immediately once posted; the tool warns you. With a manual posting policy they stay hidden until you post them in the Gradebook or SpeedGrader.

## Tools

| Tool | Writes to Canvas? | What it does |
|---|---|---|
| `whoami` | no | Account and Canvas instance in use |
| `list_grading_courses` | no | Courses where you can grade |
| `list_assignments` | no | Assignments, due dates, needs-grading counts |
| `get_assignment` | no | Description, rubric, points, dates, late policy, posting policy |
| `list_submissions` | no | Students' submission status, lateness, scores (filter: needs_grading, late, missing, …) |
| `get_submission` | no | A student's work as text, lateness, previous comments |
| `get_submission_images` | no | Screenshots, photos and scanned pages from the submitted files, as images the AI can look at (`detail="high"` for small text, `video_frames=N` for frames from submitted videos) |
| `find_shared_files` | no | Groups of students who submitted byte-identical files |
| `set_grading_guidelines` / `get_grading_guidelines` | no (local) | Your per-course grading rules and late-penalty tiers |
| `stage_grade` | no | Save a proposed grade/rubric/comment locally, optionally from a per-category breakdown |
| `list_staged` / `discard_staged` | no | Review or drop staged grades |
| `post_staged_grades` | **yes** (with `confirm=true`) | Send staged grades and comments to Canvas |
| `post_comment` | **yes** (with `confirm=true`) | Comment on a submission without grading |
| `get_gradebook_notes` / `add_gradebook_note` | notes only (with `confirm=true`) | Staff-only notes in the Gradebook Notes column (never visible to students) |
| `open_in_speedgrader` | no | Open SpeedGrader in your browser |
| `clear_downloaded_files` | no | Delete cached copies of student files |

## Privacy and responsible use

- **Where data goes.** This server only talks to your Canvas instance. But everything a tool returns, including student names, submissions and feedback, is passed to the AI model behind your MCP client (e.g. Anthropic for Claude). Student work is an education record under **FERPA** (US) and similar laws elsewhere. **Check your institution's policy** on sharing student work with AI services, and use an account/plan your school has approved where required.
- **Local data.** Downloaded files are cached in `~/.cache/canvas-grader-mcp` and staged grades in `~/.local/share/canvas-grader-mcp/staged.json` (owner-only permissions). Run `clear_downloaded_files` when done.
- **You are the grader.** AI-suggested grades can be wrong, especially for scanned work, images, or anything that needs judgment the description doesn't spell out. Review every staged grade before posting. Consider telling students that AI assists your grading if your institution requires disclosure.
- **Token safety.** Your token can do anything your Canvas account can. Give it an expiry date, never commit it, and revoke it in Canvas settings if it leaks. The server only sends it to your Canvas host.

## Limitations

- **Anonymous and moderated** assignments are read-only (grade them in SpeedGrader).
- Classic quizzes, discussions, annotations, LTI tools, and audio/video submissions aren't converted to text; the tool points you to SpeedGrader.
- Videos aren't watched end to end: the tool confirms a link or file is a video, reports its length, and can show sample frames. Only Google Drive/Photos, YouTube and common QR/short-link hosts are checked; other links are listed as web pages.
- Account admins without a course enrollment aren't listed by `list_grading_courses`.

## Development

```bash
git clone https://github.com/kaushikgunasekaren/canvas-grader-mcp.git
cd canvas-grader-mcp
uv sync
uv run pytest            # runs against a fake Canvas API, no account needed
```

## License

MIT. Not affiliated with Instructure.
