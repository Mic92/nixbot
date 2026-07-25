from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .proc import LogWrite


@dataclass
class EffectsOptions:
    """Everything one effect run needs, passed by value.

    The CLI fills these from its file-based flags, the daemon directly.
    """

    # Repository / revision the effects are evaluated for.
    path: Path = field(default_factory=Path.cwd)
    repo: str = ""
    rev: str | None = None
    branch: str | None = None
    url: str | None = None
    tag: str | None = None
    locked_url: str | None = None
    default_branch: str | None = None
    # Credentials and secrets, as values.
    secrets: dict[str, Any] | None = None
    git_token: str | None = None
    task_token: str | None = None
    # Hercules state API + project metadata.
    api_base_url: str | None = None
    project_id: str | None = None
    project_path: str | None = None
    # Sandbox configuration.
    mountables_file: Path | None = None
    extra_nix_options: list[tuple[str, str]] = field(default_factory=list)
    extra_sandbox_paths: list[Path] = field(default_factory=list)
    # Runner-prepared pushable clone, mounted at the effect's
    # __nixbot_effect_checkout path.
    effect_checkout: Path | None = None
    # Line-wise sink for all child output (nix eval, bwrap, the effect).
    log: LogWrite | None = None
    debug: bool = False
