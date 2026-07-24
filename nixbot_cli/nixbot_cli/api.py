"""Synchronous httpx client for the nixbot /api surface.

Standalone on purpose: no imports from the server package, so the CLI
installs without the service. Responses are decoded JSON matching the
response models in the server's OpenAPI spec and /llms.txt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Iterator


class ApiError(Exception):
    """Non-2xx response from the nixbot API."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class RepoRef:
    """forge/owner/name triple identifying a repository."""

    forge: str
    owner: str
    name: str

    @classmethod
    def parse(cls, slug: str) -> RepoRef:
        parts = slug.strip("/").split("/")
        if len(parts) != 3 or not all(parts):
            msg = f"expected forge/owner/name, got {slug!r}"
            raise ValueError(msg)
        return cls(*parts)

    def __str__(self) -> str:
        return f"{self.forge}/{self.owner}/{self.name}"


class NixbotClient:
    """Thin wrapper over the /api endpoints."""

    def __init__(
        self,
        url: str = "",
        token: str | None = None,
        *,
        http: httpx.Client | None = None,
    ) -> None:
        self.http = http or httpx.Client(base_url=url, timeout=30)
        if token:
            self.http.headers["Authorization"] = f"Bearer {token}"

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> NixbotClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        query = {k: v for k, v in (params or {}).items() if v is not None}
        response = self.http.request(method, path, params=query)
        if response.is_error:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise ApiError(response.status_code, str(detail))
        return response

    def _json(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        return self._request(method, path, params).json()

    # --- repos ---------------------------------------------------------

    def repos(self) -> list[dict]:
        return self._json("GET", "/api/repos")

    def repo(self, repo: RepoRef) -> dict:
        return self._json("GET", f"/api/repos/{repo}")

    def set_enabled(self, repo: RepoRef, *, enabled: bool) -> dict:
        action = "enable" if enabled else "disable"
        return self._json("POST", f"/api/repos/{repo}/{action}")

    def queue(self) -> list[dict]:
        return self._json("GET", "/api/queue")

    # --- builds --------------------------------------------------------

    def builds(  # noqa: PLR0913
        self,
        repo: RepoRef,
        *,
        status: str | None = None,
        branch: str | None = None,
        pr_number: int | None = None,
        commit: str | None = None,
        page: int | None = None,
    ) -> dict:
        """One page of builds: {items, page, has_next}."""
        params = {
            "status": status,
            "branch": branch,
            "pr_number": pr_number,
            "commit": commit,
            "page": page,
        }
        return self._json("GET", f"/api/repos/{repo}/builds", params)

    def build(self, repo: RepoRef, number: int) -> dict:
        """Build detail: {build, attributes}."""
        return self._json("GET", f"/api/repos/{repo}/builds/{number}")

    def failures(self, repo: RepoRef, number: int, *, tail: int | None = None) -> dict:
        """Why a build failed: failed attributes with log tails."""
        params = {"tail": tail}
        return self._json("GET", f"/api/repos/{repo}/builds/{number}/failures", params)

    def attr_history(self, repo: RepoRef, attr: str) -> list[dict]:
        return self._json("GET", f"/api/repos/{repo}/attrs/{attr}")

    def restart_build(self, repo: RepoRef, number: int) -> dict:
        return self._json("POST", f"/api/repos/{repo}/builds/{number}/restart")

    def cancel_build(self, repo: RepoRef, number: int) -> dict:
        return self._json("POST", f"/api/repos/{repo}/builds/{number}/cancel")

    def restart_attr(self, repo: RepoRef, number: int, attr: str) -> dict:
        return self._json(
            "POST", f"/api/repos/{repo}/builds/{number}/attrs/{attr}/restart"
        )

    def cancel_attr(self, repo: RepoRef, number: int, attr: str) -> dict:
        return self._json(
            "POST", f"/api/repos/{repo}/builds/{number}/attrs/{attr}/cancel"
        )

    def restart_effects(self, repo: RepoRef, number: int) -> dict:
        return self._json("POST", f"/api/repos/{repo}/builds/{number}/effects/restart")

    # --- logs ----------------------------------------------------------

    def log_toc(self, repo: RepoRef, number: int, attr: str) -> dict:
        """Table of contents of one attribute's structured log."""
        return self._json("GET", f"/api/repos/{repo}/builds/{number}/logs/{attr}")

    def log_text(  # noqa: PLR0913
        self,
        repo: RepoRef,
        number: int,
        attr: str,
        *,
        tail: int | None = None,
        drv: str | None = None,
        ansi: bool = False,
    ) -> str:
        """Plain-text log. drv selects one derivation by store path or
        name substring."""
        params = {"tail": tail, "drv": drv, "ansi": "1" if ansi else None}
        return self._request(
            "GET", f"/api/repos/{repo}/builds/{number}/logs/{attr}/text", params
        ).text

    # --- streams -------------------------------------------------------

    def log_stream(
        self, repo: RepoRef, number: int, attr: str
    ) -> Iterator[tuple[str, Any]]:
        """SSE events of a running attribute's structured log as
        (event, payload) pairs. The stream ends with ("done", {})."""
        url = f"/api/repos/{repo}/builds/{number}/logs/{attr}/stream"
        with self.http.stream("GET", url, timeout=None) as response:
            if response.is_error:
                response.read()
                raise ApiError(response.status_code, response.text)
            yield from _iter_sse(response)


def _iter_sse(response: httpx.Response) -> Iterator[tuple[str, Any]]:
    """Parse an SSE body into (event, decoded JSON data) pairs."""
    event = "message"
    data: list[str] = []
    for line in response.iter_lines():
        if not line:
            if data:
                yield event, json.loads("\n".join(data))
            event, data = "message", []
        elif line.startswith("event:"):
            event = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data.append(line.removeprefix("data:").strip())
