"""nbo commands (nixbot_cli.main) against the real web app."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
import zstandard
from nixbot_cli import main as cli
from nixbot_cli.api import NixbotClient, RepoRef
from nixbot_cli.config import Settings

from nixbot.api_tokens import ApiTokenStore
from nixbot.auth import AuthzConfig, User
from nixbot.executor import attribute_log_path, container_path
from nixbot.logstore import LogContainerWriter
from nixbot.web.control_routes import create_control_api_router

from .support import (
    WebHarness,
    db_pool,
    git,
    init_upstream,
    insert_build,
    insert_project,
    web_harness,
)
from .test_cli_api import make_client
from .test_control import FakeBackend

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import asyncpg
    from fastapi import FastAPI

REPO = RepoRef("github", "acme", "widget")
ROOT = User(provider="github", username="root")
COMMIT = "c0ffee1234c0ffee1234c0ffee1234c0ffee1234"
BACKEND = FakeBackend()


@pytest.fixture(scope="module")
async def postgres_dsn(postgres_dsn: str) -> str:
    async with db_pool(postgres_dsn) as pool:
        project_id = await insert_project(pool, forge_repo_id="cli-cmd")
        failed = await insert_build(
            pool, project_id, number=1, status="failed", commit_sha=COMMIT
        )
        await insert_build(
            pool, project_id, number=2, status="building", branch="pr", started=True
        )
        await pool.execute(
            """
            INSERT INTO build_attributes (build_id, attr, system, status, error, drv_path)
            VALUES
              ($1, 'good', 'x86_64-linux', 'succeeded', NULL, '/nix/store/aaa-good.drv'),
              ($1, 'bad1', 'x86_64-linux', 'failed', 'builder failed',
               '/nix/store/bbb-hello-1.0.drv')
            """,
            failed,
        )
    return postgres_dsn


@pytest.fixture(scope="module")
def harness(postgres_dsn: str) -> Iterator[WebHarness]:
    def configure(app: FastAPI, pool: asyncpg.Pool) -> None:
        ctx = app.state.web_context
        ctx.authz = AuthzConfig(admins=["github:root"])
        ctx.token_store = ApiTokenStore(pool)
        app.include_router(
            create_control_api_router(
                ctx, BACKEND, AuthzConfig(admins=["github:root"]), "http://test"
            )
        )

    with web_harness(postgres_dsn, configure=configure) as h:
        yield h


@pytest.fixture(scope="module")
def api(harness: WebHarness) -> NixbotClient:
    store = harness.ctx.token_store
    assert store is not None
    return make_client(harness, token=harness.run(store.create(ROOT, "cli")))


def run_cli(client: NixbotClient, *argv: str) -> int:
    """Parse argv like nbo and run the selected command."""
    args = cli.build_parser().parse_args(argv)
    return int(args.func(client, args))


def test_repo_list_and_toggle(
    api: NixbotClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(api, "repo", "list") == 0
    assert "github/acme/widget" in capsys.readouterr().out

    assert run_cli(api, "repo", "disable", "acme/widget") == 0
    assert "disabled" in capsys.readouterr().out
    assert run_cli(api, "repo", "enable", "github/acme/widget") == 0
    assert api.repo(REPO)["enabled"] is True
    capsys.readouterr()


def test_build_list_and_view(
    api: NixbotClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(api, "build", "list", "-R", "github/acme/widget") == 0
    out = capsys.readouterr().out
    assert "1" in out
    assert "✗ failed" in out

    assert (
        run_cli(
            api,
            "build",
            "list",
            "-R",
            "acme/widget",
            "--status",
            "building",
            "--json",
            "number,status",
        )
        == 0
    )
    assert '"number": 2' in capsys.readouterr().out

    assert run_cli(api, "build", "view", "1", "-R", "acme/widget") == 0
    out = capsys.readouterr().out
    assert "build #1" in out
    assert "1 failed" in out
    assert "bad1" in out


def test_build_restart_cancel(
    api: NixbotClient, capsys: pytest.CaptureFixture[str]
) -> None:
    BACKEND.attr_restarts.clear()
    assert (
        run_cli(api, "build", "restart", "1", "-R", "acme/widget", "--attr", "bad1")
        == 0
    )
    assert [a for _, a in BACKEND.attr_restarts] == ["bad1"]
    assert run_cli(api, "build", "restart", "1", "-R", "acme/widget", "--effects") == 0
    assert run_cli(api, "build", "cancel", "2", "-R", "acme/widget") == 0
    assert "cancelling build #2" in capsys.readouterr().out
    assert len(BACKEND.cancelled) == 1


def test_log_summary_attr_and_drv(
    harness: WebHarness,
    api: NixbotClient,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    async def seed() -> None:
        harness.ctx.state_dir = tmp_path
        build_id = await harness.ctx.pool.fetchval(
            "SELECT id FROM builds WHERE number = 1"
        )
        log_file = attribute_log_path(tmp_path, build_id, "bad1")
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_bytes(
            zstandard.ZstdCompressor().compress(b"CC main.o\nerror: boom\n")
        )
        w = LogContainerWriter()
        w.register("/nix/store/bbb-hello-1.0.drv", "hello-1.0")
        w.line("/nix/store/bbb-hello-1.0.drv", "CC main.o")
        w.line("/nix/store/bbb-hello-1.0.drv", "error: boom")
        w.status("/nix/store/bbb-hello-1.0.drv", "failed")
        container_path(log_file).write_bytes(w.finalize())

    harness.run(seed())

    # Whole build: failure summary, exit reflects the build result.
    assert run_cli(api, "log", "1", "-R", "acme/widget") == 1
    out = capsys.readouterr().out
    assert "── bad1 ──" in out
    assert "error: boom" in out

    # Attribute substring with --tail limiting the output.
    assert run_cli(api, "log", "1", "bad", "-R", "acme/widget", "--tail", "1") == 1
    assert capsys.readouterr().out == "error: boom\n"

    # A .drv store path scopes to that derivation.
    assert (
        run_cli(
            api,
            "log",
            "1",
            "/nix/store/bbb-hello-1.0.drv",
            "-R",
            "acme/widget",
            "--tail",
            "1",
        )
        == 1
    )
    assert capsys.readouterr().out == "error: boom\n"

    with pytest.raises(cli.UsageError, match="no attribute"):
        run_cli(api, "log", "1", "nope", "-R", "acme/widget")


def test_repo_and_build_inference(
    harness: WebHarness,
    api: NixbotClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without -R and a number, the repo comes from the git remote and
    the build from the HEAD commit."""
    checkout = init_upstream(tmp_path / "repo")
    git(checkout, "remote", "add", "origin", "git@github.com:acme/widget.git")
    head = git(checkout, "rev-parse", "HEAD")
    monkeypatch.chdir(checkout)

    assert cli.resolve_repo(api, None) == REPO
    with pytest.raises(cli.UsageError, match="no build for commit"):
        cli.resolve_build(api, REPO, None)
    harness.run(
        harness.ctx.pool.execute(
            "UPDATE builds SET commit_sha = $1 WHERE number = 2", head
        )
    )
    assert cli.resolve_build(api, REPO, None) == 2


def test_auth_status(
    api: NixbotClient,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NIXBOT_URL", "http://test")
    monkeypatch.setenv("NIXBOT_TOKEN", "bnix_secret")
    assert run_cli(api, "auth", "status") == 0
    out = capsys.readouterr().out
    assert "server: http://test" in out
    assert "bnix_sec" in out
    assert "server is reachable" in out


def test_settings_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NIXBOT_URL", raising=False)
    monkeypatch.delenv("NIXBOT_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="no server configured"):
        Settings.load()
    hosts = tmp_path / "nixbot" / "hosts.toml"
    hosts.parent.mkdir(parents=True)
    hosts.write_text('["https://ci.example.org"]\ntoken = "bnix_abc"\n')
    assert Settings.load() == Settings("https://ci.example.org", "bnix_abc")
    monkeypatch.setenv("NIXBOT_TOKEN", "bnix_env")
    assert Settings.load().token == "bnix_env"  # noqa: S105 (test credential)


def test_settings_token_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token can come from a secret manager command (pass, rbw, ...)."""
    monkeypatch.delenv("NIXBOT_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    hosts = tmp_path / "nixbot" / "hosts.toml"
    hosts.parent.mkdir(parents=True)
    hosts.write_text(
        '["https://ci.example.org"]\ntoken_command = "echo bnix_from_pass"\n'
    )
    assert Settings.load().token == "bnix_from_pass"  # noqa: S105 (test credential)

    hosts.write_text('["https://ci.example.org"]\ntoken_command = "false"\n')
    with pytest.raises(ValueError, match="token_command"):
        Settings.load()


def test_log_follow_finished_attr_falls_back_to_stored_log(
    api: NixbotClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """A finished attribute has no live stream. --follow prints the log."""
    assert run_cli(api, "log", "1", "bad1", "--follow", "-R", "acme/widget") == 1
    out = capsys.readouterr().out
    assert "CC main.o" in out
    assert "error: boom" in out


def test_log_follow_renders_live_stream(capsys: pytest.CaptureFixture[str]) -> None:
    """The structured SSE stream (as emitted by /logs/{attr}/stream)
    becomes derivation headers, phase separators and raw lines."""
    body = (
        'event: state\ndata: [{"idx":0,"drv":"/nix/store/aaa-hello-1.0.drv",'
        '"name":"hello-1.0","status":"running","n":3}]\n\n'
        ": keepalive\n\n"
        'event: drv\ndata: {"t":"drv","idx":1,"drv":"/nix/store/bbb-dep.drv","name":"dep-2.0"}\n\n'
        'event: phase\ndata: {"t":"phase","idx":0,"phase":"buildPhase","line":3}\n\n'
        'event: line\ndata: {"t":"line","idx":0,"from":4,"text":"CC main.o"}\n\n'
        'event: drv-done\ndata: {"t":"status","idx":0,"status":"failed"}\n\n'
        "event: done\ndata: {}\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/logs/broken/stream")
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    client = NixbotClient(
        http=httpx.Client(
            base_url="http://test", transport=httpx.MockTransport(handler)
        )
    )
    cli.follow_attr(client, REPO, 5, "broken", tail=None)
    out = capsys.readouterr().out
    assert "── dep-2.0 ──" in out
    assert "── hello-1.0: buildPhase ──" in out
    assert "CC main.o" in out
    assert "hello-1.0: ✗ failed" in out


def test_build_watch_reports_progress_and_failures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """watch prints one verdict per finished attribute as events arrive
    and ends with the failure summary once the build is terminal."""
    build = {"id": 9, "number": 5, "status": "building"}
    attrs = [
        {"attr": "good", "status": "succeeded", "cached": True},
        {"attr": "bad", "status": "building", "cached": False},
    ]
    hint = 'data: {"build_id":9,"attr":"bad","status":"failed"}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/builds/5"):
            return httpx.Response(200, json={"build": build, "attributes": attrs})
        if path == "/api/events":
            assert request.url.params["build"] == "9"
            # After the hint the build finishes with one failed attribute.
            attrs[1] = {"attr": "bad", "status": "failed", "cached": False}
            build["status"] = "failed"
            return httpx.Response(
                200, content=hint, headers={"content-type": "text/event-stream"}
            )
        if path.endswith("/failures"):
            return httpx.Response(
                200,
                json={
                    "status": "failed",
                    "error": None,
                    "failures": [
                        {
                            "attr": "bad",
                            "status": "failed",
                            "error": "builder failed",
                            "log_tail": "error: boom",
                        }
                    ],
                },
            )
        raise AssertionError(path)

    client = NixbotClient(
        http=httpx.Client(
            base_url="http://test", transport=httpx.MockTransport(handler)
        )
    )
    assert cli.watch_build(client, REPO, 5) == 1
    out = capsys.readouterr().out
    assert "✓ cached good" in out
    assert "✗ failed bad" in out
    assert "build #5: 2 finished, 1 failed" in out
    assert "error: boom" in out
