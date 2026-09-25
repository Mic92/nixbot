"""Build store token file."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nixbot.store_token import store_token_file
from nixbot.workload_identity import EffectIdentity

if TYPE_CHECKING:
    from pathlib import Path


class FakeIssuer:
    token_ttl = 1

    def __init__(self) -> None:
        self.minted: list[str] = []

    def mint(self, identity: EffectIdentity, audience: str) -> FakeToken:
        self.minted.append(audience)
        return FakeToken(f"{identity.effect}:{audience}:{len(self.minted)}")


class FakeToken:
    def __init__(self, token: str) -> None:
        self.token = token


IDENTITY = EffectIdentity(
    forge="gitea", owner="clan", repo="clan-core", event="push", effect="build"
)


def test_token_file_is_written_and_removed() -> None:
    issuer = FakeIssuer()

    async def run() -> Path:
        async with store_token_file(issuer, IDENTITY, "nix-farm") as path:  # type: ignore[arg-type]
            assert path.read_text() == "build:nix-farm:1"
            return path

    path = asyncio.run(run())
    assert not path.exists()


def test_token_file_is_refreshed_before_expiry() -> None:
    issuer = FakeIssuer()

    async def run() -> str:
        async with store_token_file(issuer, IDENTITY, "nix-farm") as path:  # type: ignore[arg-type]
            await asyncio.sleep(1.5)
            return path.read_text()

    assert asyncio.run(run()) == "build:nix-farm:2"
