# 历史调研归档说明

这四份是 2026-10-06 那次编排层对标的**原始 subagent 报告**，从 `~/.local/share/opencode/opencode.db` 的会话记录里恢复出来的。

它们此前只存在于对话中——`docs/orchestration-explore/` 当时是个空目录，所以那份结论在 session 之间彻底丢失，10-07 不得不重做一遍一遍调研。重做的结果在 `../2026-10-07-gap-report.md`，本目录保存的是**第一次**的原始记录，两者结论互相印证。

| 文件 | 行数 | 会话 ID | 覆盖对象 |
|---|---|---|---|
| `opencode.md` | 1102 | `ses_ef0d3fea2ffeVe4oya1x6Ld8XM` | OpenCode 编排层（TypeScript） |
| `genericagent.md` | 978 | `ses_ef0c3eb9bffe8334uox2bssUQ7` | GenericAgent 编排层 |
| `hermes.md` | 937 | `ses_ef0bd477bffePNTUhqvpYlZ7XL` | Hermes 编排层（同语言参考） |
| `nexus_ai-coding.md` | 1643 | `ses_ef0b2aac4ffeR7EjRswyuf62Q6` | 本项目编排层现状 |

## 恢复方法

```bash
cp ~/.local/share/opencode/opencode.db /tmp/oc_probe.db
sqlite3 /tmp/oc_probe.db "SELECT id,title FROM session WHERE directory LIKE '%ai-coding%';"
# 每个 subagent 会话的最后一个长 text part 就是它的最终报告
```

会话记录存在 SQLite 的 `part` 表里，`data` 字段是 JSON，含 `text` 键。subagent 的最终报告是时间最晚、长度最大的那个 text part。

## 为什么要留档

调研结论是这个项目里程碑的唯一依据。它丢了两次（对话结束时丢、session 之间丢），代价是重做一遍 22 万字符的源码阅读。所以：**任何只存在于对话里的调研，都视为未完成。**