"""Nix evaluation result models, ported from nixbot.models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class CacheStatus(StrEnum):
    cached = "cached"
    local = "local"
    not_built = "notBuilt"


class NixEvalJobError(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    error: str
    attr: str
    attr_path: list[str] = Field(validation_alias="attrPath")


class NixEvalJobSuccess(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    attr: str
    attr_path: list[str] = Field(validation_alias="attrPath")
    cache_status: CacheStatus | None = Field(
        default=None, validation_alias="cacheStatus"
    )
    needed_builds: list[str] = Field(validation_alias="neededBuilds")
    needed_substitutes: list[str] = Field(validation_alias="neededSubstitutes")
    drv_path: str = Field(validation_alias="drvPath")
    name: str
    # nix-eval-jobs emits null output paths for impure and some
    # content-addressed derivations: it cannot know their store paths
    # without actually building them. Accept None so the whole eval step
    # does not crash. Downstream consumers already treat a missing
    # out path as "not statically known".
    outputs: dict[str, str | None]
    system: str
    # hercules-ci build modifiers exported by the eval's --apply function
    # (buildDependenciesOnly, ignoreFailure, requireFailure).
    extra_value: dict[str, Any] | None = Field(
        default=None, validation_alias="extraValue"
    )

    def _flag(self, name: str) -> bool:
        return bool((self.extra_value or {}).get(name))

    @property
    def build_dependencies_only(self) -> bool:
        """Build only the dependencies, not the derivation itself
        (buildDependenciesOnly or a noBuildPhase shell)."""
        return self._flag("buildDependenciesOnly")

    @property
    def ignore_failure(self) -> bool:
        """Build, but exclude a failure from the aggregate status."""
        return self._flag("ignoreFailure")

    @property
    def require_failure(self) -> bool:
        """The build is expected to fail; success fails the attribute."""
        return self._flag("requireFailure")


NixEvalJob = NixEvalJobError | NixEvalJobSuccess
NixEvalJobModel: TypeAdapter[NixEvalJob] = TypeAdapter(NixEvalJob)
