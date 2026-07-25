"""Effect dependency graph: validation and ASCII rendering for
`nixbot-effects graph`, so a bad DAG is diagnosable before pushing."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import EffectError


class EffectGraphError(EffectError):
    pass


@dataclass(frozen=True)
class EffectMeta:
    """Scheduling metadata an effect declares in the flake."""

    after: tuple[str, ...] = ()
    lock: str | None = None


def validate_deps(meta: dict[str, EffectMeta]) -> None:
    """Raise on unknown dependencies or cycles."""
    deps = {name: info.after for name, info in meta.items()}
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(name: str, chain: list[str]) -> None:
        if name in done:
            return
        if name in visiting:
            cycle = " -> ".join([*chain[chain.index(name) :], name])
            msg = f"dependency cycle between effects: {cycle}"
            raise EffectGraphError(msg)
        visiting.add(name)
        for dep in deps[name]:
            if dep not in deps:
                msg = f"effect '{name}' depends on unknown effect '{dep}'"
                raise EffectGraphError(msg)
            visit(dep, [*chain, name])
        done.add(name)

    for name in deps:
        visit(name, [])


def render_tree(meta: dict[str, EffectMeta]) -> str:
    """ASCII tree of the effect DAG.

    Effects with multiple parents appear under each parent (a tree
    cannot show diamonds faithfully).
    """
    validate_deps(meta)
    children: dict[str, list[str]] = {name: [] for name in meta}
    for name, info in meta.items():
        for dep in info.after:
            children[dep].append(name)

    def label(name: str) -> str:
        lock = meta[name].lock
        return f"{name} [lock: {lock}]" if lock else name

    lines: list[str] = []

    def walk(name: str, prefix: str) -> None:
        kids = sorted(children[name])
        for i, kid in enumerate(kids):
            last = i == len(kids) - 1
            lines.append(f"{prefix}{'└── ' if last else '├── '}{label(kid)}")
            walk(kid, prefix + ("    " if last else "│   "))

    for root in sorted(name for name, info in meta.items() if not info.after):
        lines.append(label(root))
        walk(root, "")
    return "\n".join(lines)
