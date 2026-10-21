# canvas-grader-mcp

An [MCP](https://modelcontextprotocol.io) server that helps **teachers and TAs** grade [Canvas LMS](https://www.instructure.com/canvas) submissions with an AI assistant (Claude Code, Claude Desktop, Cursor, VS Code, …).

It reads each assignment's description, rubric, due dates and late policy, pulls in every student's submitted work as text, and lets the assistant propose a grade and written feedback about what's missing. Proposed grades are **staged on your computer first**; nothing reaches Canvas until you review it and approve posting. Posted grades and comments appear in SpeedGrader like any other.

> [!IMPORTANT]
> **Read [Privacy and responsible use](#privacy-and-responsible-use) before using this with real student work.** The server runs locally, but the student work it reads is sent to whichever AI model your MCP client uses.

## What it can do

- **Find your grading work**: every course where you're a teacher/TA with grading permission, assignments with how many submissions need grading.
- **Understand the assignment**: description (as clean text), points, grading type, rubric criteria and ratings, per-student due dates (overrides and extensions), the course's automatic late policy.
- **Read submissions**: text entries, URLs, and uploaded files: PDF, Word (.docx), Jupyter notebooks, source code and other text files. Files are downloaded to a local cache only.
- **Account for due dates**: how late each submission was. If the course has an automatic late policy, it shows the deduction Canvas will apply (so you enter the raw score); if not, it flags that any penalty from the syllabus must be applied by the grader.
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

This asks for your Canvas URL and token, checks that they work, and saves them to `~/.config/canvas-grader-mcp/config.json` (readable only by you; `%APPDATA%\canvas-grader-mcp` on Windows). Alternatively set the `CANVAS_BASE_URL` and `CANVAS_API_TOKEN` environment variables in your MCP client config.

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
2. `get_submission` per student: reads their work and lateness.
3. `stage_grade`: proposes a grade, rubric scores and a student-facing comment, plus a private rationale for you.
4. `list_staged` / `post_staged_grades` (preview): **you review**.
5. `post_staged_grades` with `confirm=true`: grades and comments go to Canvas.

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
| `stage_grade` | no | Save a proposed grade/rubric/comment locally |
| `list_staged` / `discard_staged` | no | Review or drop staged grades |
| `post_staged_grades` | **yes** (with `confirm=true`) | Send staged grades and comments to Canvas |
| `post_comment` | **yes** (with `confirm=true`) | Comment on a submission without grading |
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
- Scanned PDFs and images have no extractable text.
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
