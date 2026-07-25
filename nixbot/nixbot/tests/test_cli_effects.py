"""`nbo effects`: local effect commands wired into the nbo CLI."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from nixbot_cli import effects
from nixbot_cli import main as cli
from nixbot_effects import EffectError


async def test_flake_ref_without_fragment_errors() -> None:
    # A flake ref without "#<effect>" must exit non-zero so CI callers
    # see the failure instead of a silent no-op.
    args = MagicMock()
    args.secrets = None
    args.git_token_file = None
    args.task_token_file = None
    args.debug = True
    args.rev = None
    args.branch = None
    args.repo = None
    args.path = Path()
    args.effect = "git+file:///some/repo"

    with pytest.raises(SystemExit, match="1"):
        await effects.run_command(args)


def test_effect_error_reported_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A failed effect already logged its own diagnostics. nbo must exit
    # 1 with a one-line message, not dump a Python traceback.
    msg = "bwrap failed with exit code 1"

    async def boom(_options: object, _effect: str) -> bool:
        raise EffectError(msg)

    monkeypatch.setattr(effects, "run_effect", boom)
    with pytest.raises(SystemExit) as exc:
        cli.main(["effects", "run", "deploy"])
    assert exc.value.code == 1
    assert capsys.readouterr().err == "nbo: bwrap failed with exit code 1\n"
