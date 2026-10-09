---
name: context-debug
description: Debug context compression and instruction loss issues in ai-coding sessions
version: 1
triggers:
  keywords: ["压缩", "compaction", "指令丢失", "instruction loss", "上下文", "context", "串台", "drift"]
  tools: ["bash", "read"]
  task_types: ["debugging", "context_analysis"]
  failure_patterns: ["instruction lost", "compacted", "context overflow", "串台", "drifted"]
---

## When to Use

- Session history was compacted and the model lost track of current instructions
- Model drifted to an old topic after compaction
- Context overflow errors occurred
- User reports the model "forgot" what it was doing

## Steps

1. **Identify the compaction event**: Check `ContextCompacted` events in the trace
2. **Find the instruction gap**: Compare what the model was doing before vs after compaction
3. **Check trace events**: Look for `ContextAssembled` events with large `before_chars` → `after_chars` drops
4. **Verify current instruction**: The `<task-state>` block in the latest messages should match the user's actual goal
5. **Report findings**: Summarize what was lost and what the current instruction should be

## Verification

- The model's current instruction matches the user's actual goal
- No large gaps in the conversation history without explanation
- Compaction events are accompanied by working-memory injection
