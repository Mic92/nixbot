"""Post-build steps: generic commands run after each successful
attribute build (cachix/niks3 uploads and operator-defined steps), and
build-finished steps, run once per build with all its attributes.

Ported from PostBuildStep.to_buildstep: instead of buildbot's
Interpolate, the service substitutes the placeholder forms actually used
by the NixOS modules: `%(prop:NAME)s` (see `build_props` and
`build_finished_props` for the available properties) and
`%(secret:NAME)s` (files under $CREDENTIALS_DIRECTORY). `warn_only`
steps log failures without failing the attribute.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nixbot_effects.proc import ProcessGroup

from .config import PostBuildStep
from .gcroots import safe_attr_filename

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .config import BuildFinishedStep, Interpolate
    from .events import ChangeEvent
    from .models import NixEvalJobSuccess

logger = logging.getLogger(__name__)

# Hard cap on one post-build step: a hung cachix/upload step must not
# block the attribute forever.
POST_BUILD_STEP_TIMEOUT = 60 * 60

_PLACEHOLDER_RE = re.compile(r"%\((prop|secret):([^)]+)\)s")


class InterpolationError(Exception):
    pass


def build_props(event: ChangeEvent, job: NixEvalJobSuccess) -> dict[str, str]:
    """Properties available to %(prop:...)s in post-build steps."""
    return {
        "attr": job.attr,
        # Matches the executor's out-link name (percent-encoded attr);
        # upload steps must reference this, not "result-%(prop:attr)s".
        "out_link": f"result-{safe_attr_filename(job.attr)}",
        "out_path": job.outputs.get("out") or "",
        "drv_path": job.drv_path,
        "system": job.system,
        "project": event.repo.name,
        "branch": event.branch,
        "revision": event.commit_sha,
        "pr_number": str(event.pr_number or ""),
        "default_branch": event.repo.default_branch,
    }


def build_finished_props(
    event: ChangeEvent, status: str, build_url: str
) -> dict[str, str]:
    """Properties available to %(prop:...)s in build-finished steps."""
    return {
        "project": event.repo.name,
        "branch": event.branch,
        "revision": event.commit_sha,
        "pr_number": str(event.pr_number or ""),
        "default_branch": event.repo.default_branch,
        "status": status,
        "build_url": build_url,
    }


def read_secret(name: str) -> str:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory is None:
        msg = f"secret {name!r} requested but $CREDENTIALS_DIRECTORY is not set"
        raise InterpolationError(msg)
    path = Path(directory) / name
    try:
        return path.read_text().rstrip()
    except OSError as e:
        msg = f"failed to read secret {name!r}: {e}"
        raise InterpolationError(msg) from e


def interpolate(value: str | Interpolate, props: Mapping[str, str]) -> str:
    """Substitute %(prop:...)s and %(secret:...)s placeholders."""
    if isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        kind, name = match.group(1), match.group(2)
        if kind == "prop":
            if name not in props:
                msg = f"unknown build property {name!r} in post-build step"
                raise InterpolationError(msg)
            return props[name]
        return read_secret(name)

    return _PLACEHOLDER_RE.sub(replace, value.value)


@dataclass
class PostBuildStepResult:
    name: str
    success: bool
    warn_only: bool
    output: str

    @property
    def failed(self) -> bool:
        """Failure that should fail the attribute (warn-only excluded)."""
        return not self.success and not self.warn_only


async def run_post_build_step(
    step: PostBuildStep,
    props: Mapping[str, str],
    cwd: Path,
    step_timeout: float = POST_BUILD_STEP_TIMEOUT,
    stdin: bytes | None = None,
) -> PostBuildStepResult:
    try:
        command = [interpolate(arg, props) for arg in step.command]
        # Inherit the service environment (HOME, XDG_*, ...): tools like
        # cachix and ssh-based uploads need it, and the NixOS module
        # deliberately sets HOME for the service.
        env = {
            **os.environ,
            **{
                key: interpolate(value, props)
                for key, value in step.environment.items()
            },
        }
    except InterpolationError as e:
        return PostBuildStepResult(
            name=step.name, success=False, warn_only=step.warn_only, output=str(e)
        )

    try:
        group = await ProcessGroup.start(
            command,
            cwd=cwd,
            env=env,
            stdin=None if stdin is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as e:
        # Missing/unexecutable step binary: a post-build failure, not
        # an internal error.
        return PostBuildStepResult(
            name=step.name,
            success=False,
            warn_only=step.warn_only,
            output=f"failed to start post-build step {command[0]!r}: {e}",
        )

    try:
        stdout, _ = await asyncio.wait_for(
            group.proc.communicate(stdin), timeout=step_timeout
        )
    except TimeoutError:
        await group.reap()
        return PostBuildStepResult(
            name=step.name,
            success=False,
            warn_only=step.warn_only,
            output=f"post-build step timed out after {step_timeout}s",
        )
    except asyncio.CancelledError:
        # Build cancelled: do not leave the step's process tree running.
        await group.reap()
        raise
    output = stdout.decode(errors="replace")
    success = group.proc.returncode == 0
    if not success:
        logger.log(
            logging.WARNING if step.warn_only else logging.ERROR,
            "post-build step failed",
            extra={"step": step.name, "returncode": group.proc.returncode},
        )
    return PostBuildStepResult(
        name=step.name, success=success, warn_only=step.warn_only, output=output
    )


async def run_post_build_steps(
    steps: list[PostBuildStep],
    props: Mapping[str, str],
    cwd: Path,
) -> list[PostBuildStepResult]:
    """Run all steps in order. A hard (non-warn-only) failure fails the
    attribute but does not stop the sequence, matching buildbot's
    flunkOnFailure (not haltOnFailure) behavior."""
    results = []
    for step in steps:
        result = await run_post_build_step(step, props, cwd)
        results.append(result)
    return results


async def run_build_finished_steps(
    steps: list[BuildFinishedStep],
    props: Mapping[str, str],
    attributes: list[dict[str, Any]],
    cwd: Path,
) -> list[PostBuildStepResult]:
    """Run all steps in order, each fed the props and `attributes` as
    one JSON object on stdin."""
    payload = json.dumps({**props, "attributes": attributes}).encode()
    results = []
    for step in steps:
        # The build has already settled, so a failure can only warn
        result = await run_post_build_step(
            PostBuildStep(
                name=step.name,
                environment=step.environment,
                command=step.command,
                warn_only=True,
            ),
            props,
            cwd,
            stdin=payload,
        )
        results.append(result)
    return results
