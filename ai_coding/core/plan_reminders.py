"""The plan reminder (M2 T3) — one text, two pure decisions.

The plan posture used to be announced by two unrelated things: a static
``<plan-mode>`` section in the system prompt, and a one-shot planning
instruction in the transcript.  Neither survived a compaction, and neither
was withdrawn when the operator left plan mode — so the model could read
"you are in plan mode" long after the operator left it, and could forget it
right after the reminder was compacted away.

This module is the third shape, and it has three parts:

* **two texts** — one for entering, one for leaving.  The leaving one is an
  explicit withdrawal, not the absence of a new instruction: a model that
  read "you are in plan mode" earlier needs to be told it stopped.
* **which text a switch produces** (:func:`reminder_for`) — a switch between
  two ordinary agents is not a posture change and produces nothing.
* **what is missing** (:func:`reconcile`) — the per-request check that puts a
  eaten reminder back and withdraws a stale one.

All three are pure functions over messages, so they can be tested without a
loop, a model or a file.  OpenCode does the same thing in
``packages/core/src/plugin/plan.ts`` (``switchReminder`` / ``lastReminder``);
the text differs because we have no plan directory yet (that is T5).
"""

from __future__ import annotations

from collections.abc import Sequence

from ai_coding.domain.agents import PLAN_DIR, is_plan_agent
from ai_coding.domain.message import ChatMessage

__all__ = [
    "PLAN_ENTER_REMINDER",
    "PLAN_LEAVE_REMINDER",
    "last_reminder",
    "reconcile",
    "reminder_for",
]

#: What the model is told when the session enters plan mode.  It names the
#: posture and the two exits, and it names **no tools** — the tool surface
#: says which tools are there, and a text that lists tools is a text that
#: can contradict the surface.
PLAN_ENTER_REMINDER = (
    "<system-reminder>You are in Plan mode. Discuss the plan with the user "
    "directly in the conversation; explore only as much as you need to plan, "
    "and do not implement the changes yourself. Do not create or update plan "
    "files unless the user explicitly asks you to; when they do, write them "
    f"only under {PLAN_DIR}/. Every step you submit must carry the command "
    "that proves it. To submit a plan, call present_plan; if the request needs "
    "no implementation, close it with finish_research instead. You remain in "
    "Plan mode until the user switches agents. "
    "Only present_plan's approval starts execution; a conversational yes does not."
    "</system-reminder>"
)

#: What the model is told when the session leaves plan mode.  It has to be
#: explicit: "the restrictions no longer apply" is a different statement
#: from "here is no new instruction", and only the first one undoes the
#: first one.
PLAN_LEAVE_REMINDER = (
    "<system-reminder>You are NO LONGER in Plan mode. The Plan mode "
    "restrictions no longer apply, and any Plan mode instruction from "
    "earlier in this conversation is no longer active."
    "</system-reminder>"
)


def reminder_for(previous: str, target: str) -> str | None:
    """The reminder a switch from ``previous`` to ``target`` produces.

    ``None`` means the switch is not a posture change: two ordinary agents
    are two ways of working, not two postures, and a reminder about them
    would be noise the model has to read past.
    """
    was = is_plan_agent(previous)
    now = is_plan_agent(target)
    if was == now:
        return None
    return PLAN_ENTER_REMINDER if now else PLAN_LEAVE_REMINDER


def last_reminder(messages: Sequence[ChatMessage]) -> str | None:
    """The last reminder in the history, or ``None``.

    Scans for the two texts by identity, so an ordinary user message that
    merely *says* "you are in plan mode" is not mistaken for one.
    """
    found: str | None = None
    for message in messages:
        if message.role != "user":
            continue
        content = message.content or ""
        if content in (PLAN_ENTER_REMINDER, PLAN_LEAVE_REMINDER):
            found = content
    return found


def reconcile(messages: Sequence[ChatMessage], *, is_plan: bool) -> str | None:
    """The reminder this request needs, or ``None`` when it needs none.

    Two cases, and they are the two halves of one rule — *the history must
    agree with the posture*:

    * the session is in plan mode but the history does not say so (a
      compaction ate it) ⇒ put it back;
    * the session is not in plan mode but the history still says so (the
      withdrawal was compacted away) ⇒ withdraw it.
    """
    current = last_reminder(messages)
    if is_plan:
        return None if current == PLAN_ENTER_REMINDER else PLAN_ENTER_REMINDER
    if current == PLAN_ENTER_REMINDER:
        return PLAN_LEAVE_REMINDER
    return None
