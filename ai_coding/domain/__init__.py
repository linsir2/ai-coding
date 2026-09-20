"""Data-contract layer: pure, side-effect-free data models shared across the app.

Invariants (inherited from the original Java project's semantics):
- ``ChatMessage`` carries all four roles (user/assistant/tool/system).
- A ``tool`` message MUST carry ``tool_call_id``; an ``assistant`` message that
  requests tools MUST carry ``tool_calls``.
- ``ToolCallRef.arguments`` is the raw JSON *string* produced by the model;
  ``ToolCall.params`` is its parsed mapping. A tool call and its tool result are
  paired by ``id`` == ``tool_call_id``.
"""
