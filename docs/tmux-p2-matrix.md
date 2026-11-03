# P1 / P2-A/B/C tmux 真机矩阵

> 2026-11-03，HEAD `ec0522a`。隔离 workspace：`/tmp/aic-matrix/ws`；模型
> `qwen3.7-flash`；`max_execution_slots=1`、`max_concurrent_subagents=2`、
> `process_sandbox.mode=enforce`、`mcp.enabled=false`。
> 证据目录：`.ai-coding/tasks/*.jsonl`、`.ai-coding/agents/meta/*.json`、
> `transcripts/subagents/*.json`、`.ai-coding/notices/*.jsonl`、
> `.ai-coding/traces/*/turn-*.jsonl`。

## P0 用例

| ID | 改造 | 操作 | 预期 | 结果 |
|---|---|---|---|---|
| P0-01 | P1 plan⇒readonly | `/plan`；要求 write | 状态 `Plan·只读`，写被拒 | PASS（状态与命令面一致） |
| P0-02 | P1 `/readonly off` | `/plan` → `/readonly off` → `/plan off` | 拒绝关闭只读；`/plan off` 回 Build | PASS |
| P0-03 | P1 readonly spawn | `/readonly on`；spawn readonly=true/false | 只读 spawn 可跑；可写拒绝 | 设计完成，未单独跑 |
| P0-04 | P2-A packet v3 | `spawn_agent(name=pkt, task=读 f.txt 后写 pkt_out.txt)` | ledger `v=3`、`changed_paths`、`commit`、`base_commit`；`/tasks` 显示分支未合并 | PASS，但发现后台 base 未进 ledger，已修 |
| P0-05 | P2-B NoticeStore | spawn 慢任务，完成后不立刻发下一轮 | `notices/<session>.jsonl` 出现一行；下一轮注入后删除 | 设计完成，未单独跑 |
| P0-06 | P2-B conflicts | 两个可写子改同一文件 | 后完成者 `conflicts` 非空 | 设计完成，未跑 |
| P0-07 | P2-C1 slots=1 | `max_execution_slots=1`；spawn 可写子 | 父回合完成、不死锁、槽释放 | PASS（P0-04 在 slots=1 下完成） |
| P0-08 | P2-C2 recover | spawn 长任务后 kill 宿主，重启 | journal `recovered`；`needs_reconciliation`；不重跑 | 设计完成，未跑 |
| P0-09 | P2-C2 stop/fence | spawn 长任务，`/tasks stop` | journal `stopped`；晚到结果被 fence 丢弃 | 设计完成，未跑 |
| P0-10 | P2-C3 merge | spawn 可写子；`merge_agent(verify_cmd=false)` 再 `merge_agent` | 第一次 `verify_failed` 主区不变；第二次 `merged`，源分支保留，临时区清理 | PASS；发现 UI 分支 `?`/“未知原因”，已修 |

## 本轮真机发现并修复

1. **后台 `base_commit` 只进 meta，不进 ledger/result**：`_with_handle_branch` 只补
   branch；后台 `_refresh_ledger` 写的是 `base_commit=None`。已改为同时补 base，
   并加 `test_background_row_and_meta_carry_base_commit`。
2. **merge 失败卡片分支显示 `?`**：`MergeQueue._finish` 的失败 outcome 没有
   branch/base；已加默认字段。
3. **前端把 `verify_failed` 显示为“未知原因”**：`toolmeta.ts` 缺 merge 状态标签；
   已补 `verify_failed`/`verify_unavailable`/`conflict`/`dirty` 等，并给
   `merge_agent` 加“合并分支”动词。
4. **plan 工具幻觉**：`propose_plan`/`proposed_plan` 是模型把文本块当工具；
   已统一 `PLAN_TEXT_RULE`，`_unknown` 给可操作提示；`ask_user` 不再 deferred，
   `missing_question` 有中文标签。
5. **Ctrl+C**：有选区复制、无选区退出；新增 `app.copy-or-exit`，前端 11 条
   选区测试通过。headless tmux 只自动验证了“无选区退出”；拖选复制由单测覆盖。

## 未跑/后续

- P0-03/05/06/08/09 的完整矩阵、P2-C3 冲突/ignored 碰撞、重启恢复。
- 真实鼠标拖选复制需要在交互终端验证；tmux 无鼠标注入时不作为自动化判据。
