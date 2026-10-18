# hotel 四连败修复：本轮未做事项（P0 已落地，记账备查）

P0 落地：`tools/glob_tool.py` 绝对路径相对化 + `workspace_escape` 结构化错误；
`app/commands.py` `/todo` 双发 `TaskUpdated` + `render_display()`；
`core/request_assembly.py` `_environment_block` 工作区外警告（复用
`core/plan_runner.py:104 outside_workspace_paths`）。

以下按拍板“只做 P0”砍掉或推迟，记录根因判断、风险与未来入口。
触发条件满足时再立项，不要顺手做。

## D1. guard 对失败调用计数（放大器，非根因）

* 位置：`core/guard.py:192 DuplicateCallGuard(threshold=2)`、
  `core/guard.py:236 FailedRepeatGuard(threshold=2)` + `TurnView.strict_repeats`
  武装（`core/agent_loop.py:1496`）。
* 本轮判断：路径错本是 `tool_error`，P0 把错误文案改成可行动的之后，
  模型不再同参重试，guard 不触发。改计数口径会削弱真循环攻击防护。
* 未来入口：若 P0 上线后 trace 仍出现 `error=guard_denied,data.guard=
  duplicate_call|failed_repeat` 且前序是 `tool_error`，再议：
  `after()` 对 `success=False` 的 `tool_error/workspace_escape` 不计入
  `_failures`，补决策表测试（`tests/test_guard_failed_repeat.py` 同风格）。

## D2. Thought 英文与回答脱节

* 位置：`ai/prompt.py:16 BASE_INSTRUCTIONS`（只约束回答语言）、
  `ai/openai_engine.py:382 reasoning_content` 直通、`server/bridge.py:102`
  `reasoning` 透传。
* 本轮判断：与四连败无因果；改 prompt 影响全量请求前缀缓存
 （`docs/r3` 实测 prefix 92%），需单独评估。
* 未来入口：`METHODS_SECTION` 加“思考与回答同语言”一句 + 前端 Thought
  默认折叠；先测缓存命中变化。

## D3. 预算观感（10 步硬顶 + fatigue 30 收尾）

* 位置：`core/turn_policy.py:71 max_iterations=10`、
  `core/interventions.py:69 FATIGUE_THRESHOLD=30.0`、
  `TurnPolicyConfig.max_auto_continuations=3`。
* 本轮判断：失败吃步数是 P0 修复的对象（失败归零则步数够用）；
  放宽步数/改续跑机制是架构选择，另立项。
* 未来入口：dsh 式 `needsContinuation` 机制位；动 `turn_policy.py`
  决策表（`tests/test_turn_policy.py`）。

## D4. 跨仓库 `-w` 一键切换（TUI/IPC 契约改动）

* 位置：`server/handlers.py:session.create`、`server/world.py`、
  前端 `session.ts`。
* 本轮判断：P0 文案已指引 `-w` 重启；真一键切需改 IPC 契约，
  超最小范围。
* 未来入口：`session.create` 加 `workspace` 参数 + `files.find`
  复用；前后端同步改。
