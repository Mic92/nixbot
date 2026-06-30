"""Tests for CLI flake ref support."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nixbot_effects import NixbotEffectsError
from nixbot_effects.cli import _key_value, main, run_command


def test_flake_ref_without_fragment_errors() -> None:
    # A flake ref without "#<effect>" must exit non-zero so CI callers
    # see the failure instead of a silent no-op.
    args = MagicMock()
    args.secrets = Path("/tmp/secrets.json")  # noqa: S108
    args.debug = True
    args.rev = None
    args.branch = None
    args.repo = None
    args.path = Path()
    args.effect = "git+file:///some/repo"

    with pytest.raises(SystemExit, match="1"):
        run_command(args)


def test_main_reports_effects_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A failed effect already logged its own diagnostics; main must exit
    # 1 with a one-line message, not dump a Python traceback into the
    # run log.
    msg = "command failed with exit code 1"

    def boom(_args: argparse.Namespace) -> None:
        raise NixbotEffectsError(msg)

    ns = argparse.Namespace(func=boom)
    monkeypatch.setattr("nixbot_effects.cli.parse_args", lambda: ns)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert capsys.readouterr().err == "error: command failed with exit code 1\n"


def test_extra_nix_option_requires_key_value() -> None:
    assert _key_value("max-jobs=1") == ("max-jobs", "1")
    with pytest.raises(argparse.ArgumentTypeError):
        _key_value("max-jobs")
