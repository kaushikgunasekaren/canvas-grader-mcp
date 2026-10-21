"""Thin Canvas REST API client: auth, pagination, rate limits, downloads."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterator

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from .config import Settings

Params = list[tuple[str, Any]] | dict[str, Any] | None


class CanvasError(ToolError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CanvasClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = settings.base_url
        self._http = httpx.Client(
            base_url=f"{settings.base_url}/api/v1",
            headers={"Authorization": f"Bearer {settings.token}", "Accept": "application/json"},
            timeout=httpx.Timeout(30.0),
            transport=transport,
            follow_redirects=False,
        )
        # Downloads redirect to a separate file host; httpx drops the auth header on
        # cross-origin redirects, and Canvas file URLs carry their own verifier.
        self._token = settings.token
        self._host = httpx.URL(settings.base_url).host
        self._files = httpx.Client(
            timeout=httpx.Timeout(120.0),
            transport=transport,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._http.close()
        self._files.close()

    # --- core ---------------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(4):
            resp = self._http.request(method, url, **kwargs)
            rate_limited = resp.status_code == 429 or (
                resp.status_code == 403 and "rate limit" in resp.text.lower()
            )
            if rate_limited and attempt < 3:
                time.sleep(2**attempt)
                continue
            break
        if resp.status_code == 401:
            raise CanvasError("Canvas rejected the access token (401). It may be expired or revoked; run `canvas-grader-mcp configure`.", 401)
        if resp.status_code == 403:
            raise CanvasError(f"Canvas says you are not allowed to do that (403): {_error_text(resp)}", 403)
        if resp.status_code == 404:
            raise CanvasError(f"Not found (404): {method} {url}", 404)
        if resp.is_error:
            raise CanvasError(f"Canvas error {resp.status_code} on {method} {url}: {_error_text(resp)}", resp.status_code)
        return resp

    def get(self, path: str, params: Params = None) -> Any:
        return self._request("GET", path, params=params).json()

    def get_all(self, path: str, params: Params = None, limit: int | None = None) -> list[Any]:
        return list(self.iter_pages(path, params, limit))

    def iter_pages(self, path: str, params: Params = None, limit: int | None = None) -> Iterator[Any]:
        items = list(params.items()) if isinstance(params, dict) else list(params or [])
        items.append(("per_page", 100))
        resp = self._request("GET", path, params=items)
        count = 0
        while True:
            for item in resp.json():
                yield item
                count += 1
                if limit is not None and count >= limit:
                    return
            nxt = resp.links.get("next", {}).get("url")
            if not nxt:
                return
            resp = self._request("GET", nxt)

    def put(self, path: str, data: dict[str, Any]) -> Any:
        return self._request("PUT", path, data=data).json()

    def download(self, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Only ever send the token to the Canvas host itself.
        headers = {"Authorization": f"Bearer {self._token}"} if httpx.URL(url).host == self._host else {}
        with self._files.stream("GET", url, headers=headers) as resp:
            if resp.is_error:
                raise CanvasError(f"Download failed ({resp.status_code}) for {dest.name}")
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
        return dest

    # --- endpoints ----------------------------------------------------------

    def me(self) -> dict:
        return self.get("/users/self")

    def courses(self, include_concluded: bool = False) -> list[dict]:
        states = ["available", "completed"] if include_concluded else ["available"]
        params = [("include[]", "term"), ("include[]", "total_students")] + [("state[]", s) for s in states]
        return self.get_all("/courses", params)

    def permissions(self, course_id: int, names: list[str]) -> dict[str, bool]:
        return self.get(f"/courses/{course_id}/permissions", [("permissions[]", n) for n in names])

    def course(self, course_id: int) -> dict:
        return self.get(f"/courses/{course_id}", [("include[]", "term")])

    def late_policy(self, course_id: int) -> dict | None:
        try:
            return self.get(f"/courses/{course_id}/late_policy").get("late_policy")
        except CanvasError as exc:
            if exc.status == 404:
                return None
            raise

    def assignments(self, course_id: int) -> list[dict]:
        params: list[tuple[str, Any]] = [("include[]", "all_dates"), ("order_by", "due_at")]
        return self.get_all(f"/courses/{course_id}/assignments", params)

    def assignment(self, course_id: int, assignment_id: int) -> dict:
        params = [("include[]", "overrides"), ("include[]", "all_dates"), ("include[]", "can_edit")]
        return self.get(f"/courses/{course_id}/assignments/{assignment_id}", params)

    def submissions(self, course_id: int, assignment_id: int) -> list[dict]:
        params = [("include[]", "user"), ("include[]", "rubric_assessment")]
        return self.get_all(f"/courses/{course_id}/assignments/{assignment_id}/submissions", params)

    def submission(self, course_id: int, assignment_id: int, user_id: int) -> dict:
        params = [
            ("include[]", "user"),
            ("include[]", "submission_comments"),
            ("include[]", "rubric_assessment"),
            ("include[]", "submission_history"),
        ]
        return self.get(f"/courses/{course_id}/assignments/{assignment_id}/submissions/{user_id}", params)

    def update_submission(self, course_id: int, assignment_id: int, user_id: int, data: dict[str, Any]) -> dict:
        return self.put(f"/courses/{course_id}/assignments/{assignment_id}/submissions/{user_id}", data)

    # Gradebook "Notes": a course-wide column only teachers and TAs can see.
    def notes_column(self, course_id: int, create: bool = False) -> dict | None:
        cols = self.get_all(f"/courses/{course_id}/custom_gradebook_columns", [("include_hidden", "true")])
        col = next((c for c in cols if c.get("teacher_notes")), None)
        if col is None and create:
            col = self._request("POST", f"/courses/{course_id}/custom_gradebook_columns",
                                data={"column[title]": "Notes", "column[teacher_notes]": "true"}).json()
        return col

    def notes(self, course_id: int, column_id: int) -> dict[int, str]:
        rows = self.get_all(f"/courses/{course_id}/custom_gradebook_columns/{column_id}/data", [("include_hidden", "true")])
        return {r["user_id"]: r.get("content") or "" for r in rows}

    def set_note(self, course_id: int, column_id: int, user_id: int, content: str) -> dict:
        return self.put(f"/courses/{course_id}/custom_gradebook_columns/{column_id}/data/{user_id}",
                        {"column_data[content]": content})

    def speedgrader_url(self, course_id: int, assignment_id: int, user_id: int | None = None) -> str:
        url = f"{self.base_url}/courses/{course_id}/gradebook/speed_grader?assignment_id={assignment_id}"
        return f"{url}&student_id={user_id}" if user_id else url


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(body, dict) and "errors" in body:
        errors = body["errors"]
        if isinstance(errors, list):
            return "; ".join(e.get("message", str(e)) if isinstance(e, dict) else str(e) for e in errors)
        return str(errors)[:300]
    return str(body)[:300]
