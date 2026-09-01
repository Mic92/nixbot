"""Handle on a claimed effect item so a restart can cancel it."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class RunningEffect:
    """Registered for the full lifetime of a claimed effect item, so an
    effects restart can always free the item's dedup key. `settled` is
    set once the item released its row."""

    task: asyncio.Task[None]
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    restart: bool = False

    def cancel(self) -> None:
        self.restart = True
        self.task.cancel()
