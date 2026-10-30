"""Wave grouping for the plan executor (r24/C).

A wave is the unit of *continuity*: items in one wave share a session, so what
one of them learns the next one can still see.  Cutting a wave is therefore the
only place where the plan pays a handoff cost — this module decides where those
cuts go.

The rules are deliberately soft (grouping is a hint, never a correctness
constraint):

* only open items form waves, in task-list order (topological for fresh lists);
* a wave is capped at ``size`` (default 4);
* a heavy proof command (test/build suite) closes its wave, so integration
  verification does not run once per wave;
* a cut is shifted one item earlier when the item after it declares one of the
  same ``files`` as the item before it — the pair that edits one file should not
  be split across a context reset.

Nothing here mutates the task or looks at execution history, so the whole module
is a pure function over ``TaskState`` and is tested as one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ai_coding.domain.task import TaskState, TodoItem

#: Proof commands that are expensive enough to close a wave: they are the
#: wave's integration check, so running one in the middle would pay the suite
#: cost twice for the same information.
_HEAVY_VERIFY = re.compile(
    r"(^|\s)(pytest|tox|nox|make|ctest|"
    r"npm\s+(run\s+)?test|yarn\s+(run\s+)?test|pnpm\s+(run\s+)?test|"
    r"go\s+test|cargo\s+test|mvn\s|gradle\s|dotnet\s+test)",
    re.IGNORECASE,
)

#: How many items one wave holds when the caller says nothing.
DEFAULT_WAVE_SIZE = 4


@dataclass(frozen=True)
class Wave:
    """One run of continuous work: a slice of the open list, in list order."""

    index: int
    items: tuple[TodoItem, ...]

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.items)

    @property
    def files(self) -> tuple[str, ...]:
        seen: list[str] = []
        for item in self.items:
            for path in item.files:
                if path not in seen:
                    seen.append(path)
        return tuple(seen)


def is_heavy_verify(command: str) -> bool:
    """Does this proof command cost enough to end a wave?"""
    return bool(command and _HEAVY_VERIFY.search(command))


def group_waves(task: TaskState, *, size: int = DEFAULT_WAVE_SIZE) -> list[Wave]:
    """Slice the open items into waves (see the module docstring for the rules)."""
    open_items = [item for item in task.items if not item.is_terminal()]
    if not open_items:
        return []
    cap = max(1, int(size or DEFAULT_WAVE_SIZE))
    waves: list[Wave] = []
    current: list[TodoItem] = []

    def close() -> None:
        nonlocal current
        if current:
            waves.append(Wave(index=len(waves), items=tuple(current)))
            current = []

    for item in open_items:
        if len(current) >= cap:
            # The cut would separate two items that edit the same file: move the
            # boundary one item left so the pair stays together (when possible).
            if current and len(current) > 1 and _shares_file(current[-1], item):
                carried = current.pop()
                close()
                current.append(carried)
            else:
                close()
        current.append(item)
        if is_heavy_verify(item.verify):
            close()
    close()
    return waves


def ready_in_wave(task: TaskState, wave: Wave) -> TodoItem | None:
    """The first item of ``wave`` that can run now (dependencies satisfied)."""
    for item in wave.items:
        current = task.get(item.id)
        if current is None or current.is_terminal():
            continue
        if not task.blockers(current.id).any:
            return current
    return None


def wave_is_open(task: TaskState, wave: Wave) -> bool:
    """True while some item of the wave is still pending (not terminal)."""
    for item in wave.items:
        current = task.get(item.id)
        if current is not None and not current.is_terminal():
            return True
    return False


def _shares_file(left: TodoItem, right: TodoItem) -> bool:
    return bool(set(left.files) & set(right.files))


__all__ = [
    "DEFAULT_WAVE_SIZE",
    "Wave",
    "group_waves",
    "is_heavy_verify",
    "ready_in_wave",
    "wave_is_open",
]
