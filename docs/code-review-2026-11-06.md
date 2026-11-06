# 代码评审（2026-11-06）

范围：运行时主链路（回合循环 / 工具面 / 持久化 / 审批与沙箱边界），只做代码层评审，
不评价产品形态。每条都给出**可复现证据**与**修复方案**，按严重度排序。

---

## 0. 结论摘要

| # | 问题 | 严重度 | 直接影响 |
|---|---|---|---|
| 1 | 后台任务写入回执与内存会话「丢更新」 | P1 | 撤销/证据账本丢失，下一次保存把磁盘上的回执覆盖掉 |
| 2 | write/edit 覆盖非原子 + 检查与写之间的 TOCTOU | P1 | 崩溃留下半截文件；并发写入静默丢失 |
| 3 | 只读 bash 判定把「写文件」命令当成 look | P2 | 停滞阶梯失明（`2>f`、`git branch -D`、`env rm` 等） |
| 4 | sessions 目录里一个合法 JSON 非对象就崩 `/resume` | P2 | `list_sessions`/`resolve`/`load` 抛 AttributeError |
| 5 | 计划文件的 stale-read 键空间不一致 | P2 | 符号链接工作区下，刚读过的计划文件被误判 never_read |
| 6 | skill review 的 task 无强引用，且游标会跳过消息 | P2 | 后台评审可能被 GC 掉；评审期间的新消息永远不会被评审 |
| 7 | `load("")` / `resolve("")` 与注释契约相反 | P3 | 空 id 会匹配唯一会话；前缀为空的候选列表返回全部 |
| 8 | 原子写的临时文件名是固定的 | P3 | 两个进程写同一会话/标记时互相搬走对方的 tmp |
| 9 | `asyncio.wait` 取消与完成同 tick 时取消优先 | P3 | 已成功的工具结果被当成 cancelled 丢弃 |

CI 已恢复全绿（见 §1）。每一项的修复方案都在 §2，建议按 P1 → P2 → P3 分批，
每批配一条**先失败后通过**的单测。

---

## 1. CI 失败与修复记录（已推送 `ecb1922`）

- 失败任务：`unit (3.10)` 与 `unit (3.13)`，同一个测试
  `tests/test_trace_recorder.py::test_a_continuation_batch_gets_its_own_span_namespace`。
- 失败断言：`lines[4]["span"] == "b2-step1"`，实际 `"step1"`。
- 根因：`7291ff2` 给 `TraceRecorder._describe` 加 `nested` 区分时，把非嵌套分支也写成了
  `f"step{event.index}"`；续跑批次的 `ModelStepFinished` 因此丢掉前缀。
- 修复：nested 用 `step{i}`（子循环自己的命名空间），非嵌套回到
  `self._span("step", i)` / `self._span("i", i)`。
- 验证：本地 `pytest -q -m "not integration and not live"`、`ruff check .`、`mypy ai_coding`
  全通过；GitHub Actions 9 个 job 全绿。

---

## 2. 实质性问题

### 2.1 P1 后台任务回执与内存会话丢更新

**证据**

- [agent_loop.py:2336-2347](../ai_coding/core/agent_loop.py#L2336-L2347)：只有
  「当前会话相同 **且** turn_lock 已被持有」才写内存；其余情况走
  `self.sessions.mutate(session_id, apply)`，只写磁盘。
- [turn_close.py:203-215](../ai_coding/core/turn_close.py#L203-L215)：回合结束/checkpoint
  保存的是**内存里的整个 SessionData**。
- [session_service.py:344-360](../ai_coding/service/session_service.py#L344-L360)：
  `mutate` 从磁盘读、改、写，完全不碰调用方手里的那个对象。

后台 bash 作业在**回合之间**完成时，回执写进磁盘；应用/循环仍持有旧的
`state.session`，下一回合的保存会把磁盘上的回执覆盖掉。丢失的是
`effects`（文件写/删回执），后果是撤销证据与 stale-read 账本失效，而且
没有任何日志提示。

**修复方案（二选一，推荐 A）**

A. 在回调里同时对齐内存：

```python
active = self._active_session
if active is not None and active.session_id == session_id:
    apply(active)                 # 同一事件循环内，安全
    self._late_effects_dirty = True
    if not self._turn_lock.locked():
        self.sessions.mutate(session_id, apply)   # 也落盘
    return
self.sessions.mutate(session_id, apply)
```

`record_effect` 按 path 覆盖、seq 取 max（[effects.py:48-85](../ai_coding/domain/effects.py#L48-L85)），
所以对内存和磁盘各应用一次是幂等的。

B. 让 `SessionService.save` 变成「带版本合并」：保存时先读盘、按 message id/effect seq
合并再写。改动面大，且需要所有调用方配合，不推荐现在做。

**验收测试**：构造一个会话，先 `save`，再 `mutate` 加一条 effect，然后用**旧对象**再
`save`；断言 `load()` 后 effect 仍在（当前实现会失败）。

---

### 2.2 P1 write/edit 非原子写与 TOCTOU

**证据**

- [write_tool.py:179-189](../ai_coding/tools/write_tool.py#L179-L189)：`target.write_text`
  就地截断；写到一半崩溃 = 半截文件。stale 检查（165 行）与真正写入之间没有任何
  重新校验。
- [edit_tool.py:192-226](../ai_coding/tools/edit_tool.py#L192-L226)：`check` → `read_text`
  → `replace` → `write_text`，全程不在同一个 fd/版本上；期间外部写入会被静默覆盖。

这两个工具是模型改代码的唯一入口，长任务里同时存在用户、子代理和 bash 作业，
TOCTOU 不是理论问题。

**修复方案**

1. 覆盖写改为「同目录临时文件 + fsync + `os.replace`」：

```python
tmp = tempfile.NamedTemporaryFile(
    "wb", dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", delete=False
)
try:
    tmp.write(content.encode("utf-8"))
    tmp.flush(); os.fsync(tmp.fileno())
finally:
    tmp.close()
os.replace(tmp.name, target)
```

2. edit 改为单 fd 流程：`open_inside` 拿 fd → `os.fstat` 记 pre → 从 fd 读全文 →
   apply → 写临时文件 → `os.replace` 前按 path 再 `os.stat` 一次，与 pre 的
   `(mtime_ns, size, ino)` 比对；不一致返回 `stale_read`，临时文件删除，原文件保持。
3. `append` 保持 `O_APPEND`（单次 `os.write` 是原子追加），不要走临时文件。
4. `_write_plan_file` 已经是 fd 流程，补上同样的 replace 前二次校验即可。

**验收测试**：`pytest` 用 monkeypatch 在 `write_text`/`os.replace` 前后注入一次外部
修改，断言返回 `stale_read` 且目标文件内容等于外部写入（没有被覆盖）。

---

### 2.3 P2 只读 bash 判定把写命令当成 look

**证据**：[turn_books.py:133-174](../ai_coding/core/turn_books.py#L133-L174)。
实测（当前 HEAD）：

```
True   ls 2>err.log          # fd 重定向被负向后顾排除
True   ls &>err.log          # &> 同样被排除
True   git branch -D feat    # _GIT_READ_SUBCOMMANDS 含 branch，不看参数
True   git branch new
True   env FOO=1 rm -rf x    # env/command 是程序启动器
True   command rm -rf x
True   sort -o out.txt in.txt
True   find . -fprint out.txt
True   date -s 2020-01-01
```

`_is_look` 用它决定「这一步只是查看」，而停滞阶梯依赖这个分类；一条删除分支或
重定向写文件的命令被算作 look，正是 R4-1 想消灭的「核验循环失明」的另一种形态。

**修复方案**

1. 重定正则改为 `re.compile(r">>?\s*([^\s&|;]+)")`（`2>&1` 的目标以 `&` 开头，
   本来就不会匹配；现有 target 白名单保留）。
2. 把「纯读程序」与「可携带写参数的程序」分开：
   - `sort` 拒绝 `-o` / `--output`；
   - `find` 拒绝 `-fprint` / `-fprint0` / `-fprintf` / `-fls`（并入现有
     `_FIND_MUTATING_FLAGS`）；
   - `date` 拒绝 `-s` / `--set`；
   - `git branch` 只允许无参数或只读旗标（`-a/-r/-v/--list/--show-current`），
     `git log`/`git diff` 拒绝 `--output`；
   - `env`、`command` 从白名单移除（`printenv` 已覆盖读环境；`command -v` 可单独放行）。
3. 判定保持「拿不准就是 action」，不要为了少几次 look 放宽。

**验收测试**：上表每条一个 case；另加 `cat /dev/null 2>&1` 仍是 look 的负例。

---

### 2.4 P2 合法 JSON 非对象会让会话扫描崩溃

**证据**：[session_service.py:408-427](../ai_coding/service/session_service.py#L408-L427)
直接 `data.get("messages")`，只捕获 `ValueError/OSError`；合法 JSON 数组/字符串会抛
`AttributeError`。[load:362-366](../ai_coding/service/session_service.py#L362-L366) 同理。

复现：

```
$ mkdir sessions; echo '["hello"]' > sessions/notasession.json
>>> service.list_sessions()
AttributeError: 'list' object has no attribute 'get'
```

后果是 `/resume`、`resolve`、`mutate` 全部不可用，一个无关文件就能让会话列表瘫掉。

**修复方案**

- `list_sessions` 解析后先 `if not isinstance(data, dict): continue`；
- `load` 先判 `isinstance(data, dict)`，否则当作 miss 返回 `None`；
- `_read_pending_*` 已有类似判断，保持；
- 补测试：合法数组、合法字符串、合法数字三种文件各放一个，列表仍返回其余会话。

---

### 2.5 P2 计划文件的 stale-read 键空间不一致

**证据**

- 读：`self._sandbox.resolve(path_str)`（全程 `Path.resolve()`）后
  `tracker.record(target)` → 键是解析后的绝对路径
  （[read_tool.py:111-269](../ai_coding/tools/read_tool.py#L111-L269)）。
- 计划写：[write_tool.py:250-282](../ai_coding/tools/write_tool.py#L250-L282) 用
  `opened.path = root_path.joinpath(*parts)`，**不 resolve**，并特意用
  `check_stat(opened.path)` / `record_stat(opened.path)`。

当工作区路径本身是符号链接（macOS `/tmp`、用户软链目录）时，两个键不相等：
读过的计划文件在下一次覆盖写时被判 `NEVER_READ`，模型会收到「必须先读」的
错误指令，而它刚刚读过。

**修复方案**：键统一为 `target`（`sandbox.resolve` 已经解析过，且已做过
`relative_to_root` 词法检查）；fd 仍用 `open_inside` 保证写入边界。这样既保留
「按 fd 写」的安全属性，也让键与 `read` 一致。`edit_tool._edit_plan_file` 同样处理。

**验收测试**：用 `tmp_path` 的软链作为 workspace，读计划文件后 plan 模式覆盖写，
断言成功而不是 `must_read_first`。

---

### 2.6 P2 skill review 的后台任务

**证据**：[agent_loop.py:1996-2025](../ai_coding/core/agent_loop.py#L1996-L2025)

```python
asyncio.create_task(self._run_skill_review(session), name=...)
...
delta = session.messages[cursor:]
result = await run_skill_review(messages=delta, ...)
if result.created:
    session.skill_review_cursor = len(session.messages)
```

两个问题：

1. 任务没有强引用。asyncio 只保留弱引用，事件循环在极端情况下可以在任务完成前
   把它回收（CPython 文档明确要求保存引用）。同文件里的 harvest 已经用
   `self._harvest_task` 做了正确示范（[agent_loop.py:1960](../ai_coding/core/agent_loop.py#L1960)）。
2. 游标在 `await` **之后**取 `len(session.messages)`。评审期间新追加的消息既没被
   这次评审看到，又会被游标跳过，永远不会再评审。若评审失败，游标不前进，
   下一次会把同一段重放（可接受）。

**修复方案**

```python
start = len(session.messages)
delta = session.messages[cursor:start]
...
self._skill_review_task = asyncio.create_task(...)   # 链式，像 harvest
...
if result.created:
    session.skill_review_cursor = start
```

并在 `aclose()` / 会话切换时等待或取消该任务（与 `flush_harvest` 同款）。

**验收测试**：在 `run_skill_review` 里 await 一个事件，期间向 `session.messages`
追加一条；放行后断言 `skill_review_cursor == start`，且那条消息留待下一次评审。

---

### 2.7 P3 `load("")` / `resolve("")` 与注释契约相反

**证据**：[session_service.py:383-406](../ai_coding/service/session_service.py#L383-L406)
和 [_check_id:37-46](../ai_coding/service/session_service.py#L37-L46) 的注释都写着
「空串保持 `load("") is None` 的行为」，但 `resolve("")` 会匹配所有 id
（`startswith("")` 恒真），只有一个会话时 `load("")` 直接返回它。已实测复现。

**修复方案**：`resolve` 开头 `if not prefix: return []`；`load` 开头
`if not session_id: return None`。同时给 `resolve` 加上「空串」测试。

---

### 2.8 P3 原子写的临时文件名固定

**证据**：[session_service.py:163-165](../ai_coding/service/session_service.py#L163-L165)、
[188-209](../ai_coding/service/session_service.py#L188-L209)、[229-247](../ai_coding/service/session_service.py#L229-L247)、
[283-292](../ai_coding/service/session_service.py#L283-L292) 全部使用
`path.with_suffix(".tmp")`；[save:341-342](../ai_coding/service/session_service.py#L341-L342) 同样。

同一进程内由 RLock 串行化，但 CLI 与服务端两个进程共享 sessions 目录时，两个写者
会抢同一个临时文件名：A 写 tmp → B 写 tmp → A `os.replace` 把 B 正在写的文件搬走，
B 随后 replace 失败（`OSError` 被吞掉）→ 一次更新静默丢失。

**修复方案**：统一走 `tempfile.NamedTemporaryFile(dir=target.parent, delete=False,
prefix=f".{target.name}.", suffix=".tmp")` + `fsync` + `os.replace`。顺带给 `save`
补 `fsync`（当前只有 replace，没有落盘保证）。

---

### 2.9 P3 deadline 等待的完成/取消竞争

**证据**：[deadline.py:171-180](../ai_coding/infra/deadline.py#L171-L180)：`asyncio.wait`
返回后先判 `watcher in done`，再判 `task in done`。两者同 tick 完成时（旧取消事件
已经 set，工具恰好成功），结果被丢弃并返回 `RunCancelled`。

**修复方案**：先判 `task in done` 并 `return task.result()`，再判 watcher。语义是
「已经完成的工作优先于迟到的取消」。补一条两者同时完成的单测。

---

## 3. 修复顺序建议

1. **批次 A（P1，先做）**：2.1 内存/磁盘对齐 + 2.2 write/edit 原子写与二次校验。
   这两项都直接落在长任务高频路径上，且各有一条可写的回归测试。
2. **批次 B（P2）**：2.3 分类器收紧（只读程序参数表）、2.4 会话 JSON 类型防御、
   2.5 键空间统一。
3. **批次 C（P2/P3）**：2.6 skill review 任务链、2.7/2.8 会话服务小修、2.9 deadline 顺序。

每批独立提交、独立跑 `pytest -m "not integration and not live"` + `ruff` + `mypy`，
推上去看 CI 再进下一批；不要在长任务复跑前把多批混在一次提交里。

---

## 4. 复跑与观测口径（供 14 轮压力任务使用）

- 指标沿用前几轮：每轮耗时/工具数/门禁、`read_repeats`、`duplicate_call` 拒绝数、
  `ContextCompacted` 次数、`turn-*.jsonl` 的 span 是否仍重复、`todo_write` 失败码
  （`unknown_id` / `completion_unverified` / `override_unavailable`）。
- 日志按 `grep -c` 统计：`ERROR|Traceback|approval_timeout|tool_timeout|
  content_too_large|stale_read|must_read_first`。
- 本轮新加观测点：`late_effect` 回执是否在磁盘会话里出现（2.1 的现场证据）。

---

## 5. 14 轮压力任务实测（2026-11-06）

配置 `/tmp/aic-r2/config-fullstack.yaml`（qwen3.7-flash、`context_window=131072`、
`max_context_tokens=0`、`max_tool_iterations=30`、`max_auto_continuations=4`、
`process_sandbox.mode=warn`——本机没有 bwrap，warn 是唯一诚实的降级）。
全新工作区 `/tmp/aic-r2/ws-fullstack6`，`--rounds 14`；任务在第 3 轮就
`TASK COMPLETE`（14 是上限，不是配额），总 728.5s。

| 轮次 | 耗时 | 工具 | 迭代 | 门禁 |
|---|---|---|---|---|
| 1 | 359.5s | 25 | 29 | `be_pytest=1`（pydantic wheel），前端未开始 |
| 2 | 121.0s | 21 | 25 | `be_pytest=0`、`fe_build=0`，仍是脚手架 |
| 3 | 211.4s | 30 | 30 | 全绿（4 组件、非脚手架、README） |

总计 76 次工具、74 次模型步、**0 次压缩**、日志 0 条 `ERROR/Traceback`。
对比 R4（4 轮 1959s / 275 工具 / 17 次压缩）：耗时下降约 63%，工具调用下降 72%，
压缩归零。

### 5.1 日志里发现、并已修复的两个问题（`e4d0776`）

**F1（P1）`todo_write.verify` 在所有非 plan 会话都是「未接线」**

证据：`turn-1.jsonl` 的 7 次 todo_write 失败中，6 次来自同一个条目：
`completion_unverified` → `completion_blocked`（两次）→ `override_unavailable`；
`data.reason` 明确写着 `verify 未接线，无法证明完成`。模型随后只能把条目
标成 `blocked`，第 3 轮结束时还要在交付总结里解释「task-gate 显示的是系统残留
旧条目」。

根因：`verify_item` 只在 `session.plan_verify_items` 为真时接线
（旧 [agent_loop.py:2474](../ai_coding/core/agent_loop.py#L2474)），而
`todo_write` 的 schema 与门禁文案在所有会话都宣传 `verify`。模型照做，
命令永远不执行，条目永远证明不了完成，第二次失败后被永久 `blocked`。

修复：无条件接线（`update_plan` 侧仍由 `plan_verify_items` 自己决定是否
接受 verify 字段，所以没有给 plan 模式偷偷开自动验证）。
回归测试：`test_todo_completion_runs_the_verify_command_outside_plan_mode`。

**F2（P2）advisory 重复守卫把「编辑后重跑构建」当成重复**

证据：`turn-3.jsonl` 第 24 步 `edit TaskList.tsx` 成功，第 25 步执行与第 19 步
完全相同的 `npm run build ... | tail -20`，被 `guard_denied`
（`duplicate_call`）；模型改写成 `tail -25` 后第 27 步才通过，白花 2 个迭代。

根因：[guard.py:329-332](../ai_coding/core/guard.py#L329-L332) 的 advisory 分支在
`view.mutates_world` 时直接按计数拒绝，没有走严格分支里已有的
`_mutated_after_read` 逃生门；而 `BashTool` 的 `mutates_world` 恒为 True，
所以每次 bash 都命中这条提前拒绝。

修复：advisory 分支对 bash 先查 `_mutated_after_read`，与严格分支共用同一语义。
回归测试：`test_an_advisory_bash_may_repeat_after_a_mutation`。

### 5.2 日志里发现、本轮未改的观测/体验问题

**F3（P2）nudge 消耗的迭代在 trace 里不可见**

`turn-1` 的 `IterationStarted` 序列是 `i1…i20, i22, i23, i24, i26, i28, i29`，
缺 21/25/27。它们在 [agent_loop.py:1115-1210](../ai_coding/core/agent_loop.py#L1115-L1210)
的 NUDGE 分支里：`index += 1` 后直接 `continue`，既不 emit
`IterationStarted`，也不 `books.note_iteration`。因此：
(a) 迭代预算被算进去，但 trace 看不到这几步；
(b) 回合报告里的 iterations 与 span 数量对不上，post-mortem 无法关联。
建议：NUDGE 分支 emit 一个 `NudgeInjected(index, gate, level, reason)`（或至少
`IterationStarted` 带 `nudged=true`），并让 `TurnBooks.iterations` 记录它。

**F4（P2）`SearchStalled` 的 reason 与计数自相矛盾**

`turn-2` 第 9 步的事件：`level=1, calls=5, streak=0, repeated=0, novel=2,
unconverged=false, reason="look-only streak"`。它实际是
[interventions.py:339-344](../ai_coding/core/interventions.py#L339-L344) 的
`repeat_streak >= 1` 分支触发，但事件里没有 `repeat_streak` 字段，reason 也
统一写成 look-only。建议给事件加 `repeat_streak`/`trigger`，并让 reason 反映
真正的触发源。

**F5（P3）没有 goal 时 `update_goal` 仍出现在工具面**

`turn-3` 第 28 步 `update_goal` 返回 `no_goal`（「先 start_goal 立案」）。
虽然报错文案是清楚的，但它白花一次调用并进入 ledger 的未知项。
建议在 `_surface_tools` 里按「当前有无运行中的 goal」过滤
`update_goal`（`start_goal` 保留）。

**F6（P3）同一文件一轮内仍可重复读 5 次**

`turn-3` 对 `frontend/src/components/TaskList.tsx` 读了 5 次（每次编辑后一次）。
按现有设计（advisory + 小读豁免）这是允许的，且本轮只花 30 步就完成；
但如果后面做 14 轮长任务，建议把「同一文件在同一批/相邻两步内内容未变」
纳入去重，至少把它算进 `search_streak`。

### 5.3 结论

本轮压力任务在 3 轮内完成，所有交付门槛（后端 8 个 pytest、前端 build、
4 个组件、非脚手架、README）都过了；日志与 trace 没有 ERROR/Traceback。
真正影响效率的两个问题（F1 永久 blocked、F2 白花 2 步）已修复并通过回归测试，
F3/F4 是 trace 可观测性的确定性缺陷，建议下一批处理；F5/F6 是低频优化。

---

## 6. 修复后的三次真机复跑（ws7 / ws8 / ws9）

4 次运行使用同一个任务与配置，模型输出有随机性，结果只能做趋势对比：

| 运行 | 代码状态 | 轮次 | 总耗时 | 工具调用 | 压缩 | todo_write 失败 |
|---|---|---|---|---|---|---|
| ws6 | R4 修复后 | 3 | 728.5s | 76 | 0 | 7 |
| ws7 | + F1/F2 | 4 | 772.7s | 143 | 0 | 3 |
| ws8 | + F1/F2/F7 | 6 | 1226.4s | 156 | 0 | 8 |
| ws9 | + F8（当前 HEAD） | 3 | 614.8s | 90 | 0 | 2 |

ws9 是修复完成后的最快一次，也是 `todo_write` 失败最少的一次：
只剩 1 次 `unknown_id`（模型猜错 id，工具已列出真实 id 后重试成功）与
1 次 `completion_unverified`（第一次没带 files/verify，第二次补上即成功），
**没有任何 `completion_blocked` 级联**。

### 6.1 F1/F2 在真机上的验证（ws7）

- **F1 生效**：`turn-2` 的最后一个 todo_update 携带
  `verify="... pytest ... && npm run build ... && ls README.md"`，返回
  `True/None`，条目从 blocked 恢复为 completed。修复前同一调用得到
  `verify 未接线`，条目永久卡死。
- **F2 生效**：ws7 的 `turn-3` 里 `edit -> npm run build` 不再被
  `duplicate_call` 拒绝；剩余的 `guard_denied` 都是**真正**的重复写/重复
  `ls`（两次之间没有任何写操作），属于守卫该拦的。
- 这两个修复没有把其他计数推高：两次复跑仍是 0 次压缩、0 条 ERROR/Traceback。

### 6.2 F7（已在 `d152095` 修复）：空输出误触 look-only 阶梯

ws7 的 `turn-1` 第 4 步 `SearchStalled` 的计数是
`calls=0, streak=0, novel=0, repeated=0`，却报 `level=1 /
reason="look-only streak"`。触发点是 `repeat_streak=1`，而它来自第 2、3 步
两条 `bash` 的空输出（`mkdir`/`npm create` 本来就静默）。

修复：`TurnBooks.note_outcomes` 只在输出 `strip()` 非空时才累计
`repeat_streak`/写入 `seen_success_outputs`。回归测试
`test_silent_actions_do_not_build_a_repeat_streak`。
ws8 全轮 0 条零调用 `SearchStalled`，只剩真实的
`calls=6/streak=5`、`calls=13/streak=9` 两条。

### 6.3 F8（已在 `acfa292` 修复）：schema 把 evidence 说成「证明」

ws8 的 8 次 todo_write 失败全是同一个模式：模型把路径/测试结果写进
`evidence`，门禁要求 `files`/`verify`，于是
`completion_unverified` → `completion_blocked`（第 2 次起永久 blocked）→
`override_unavailable`。三次运行合计 18 次失败调用里，绝大多数是这一条。

根因不在门禁，而在工具面：`description` 原文是
"mark an item completed only with evidence (the file, command or finding
that proves it)"，`evidence` 字段写的是 "What proves the item is done"。
模型照着 schema 做，然后就撞墙。

修复：description 与 `evidence` 字段都明确写出「完成需要 `files` 或
`verify`；`evidence` 只是给人看的说明，单独不满足完成」。
回归测试 `test_the_schema_names_files_or_verify_as_the_completion_proof`。
**ws9（当前 HEAD）复验**：`todo_write` 失败从 ws8 的 8 次降到 2 次，
`completion_blocked` 级联消失；第一次 `completion_unverified` 之后，
模型在下一个 `update` 里就补上了 `files`/`verify` 并成功。

### 6.4 仍未解决

- **F3/F4**（nudge 与 `SearchStalled` 的 trace 语义）未改。
- **F5/F6** 未改。
- 工具失败缺少稳定 `error` 码：`edit` 的 `old_string not found`、
  `file not found`、`not unique` 都返回 `success=False, error=None`，
  trace/ledger 只能读文案，无法按码统计（ws8 turn-5 三次）。建议补
  `old_string_not_found` / `file_not_found` / `old_string_not_unique`。
- 同一文件一轮内重复读 19 次（ws8 turn-5 的 `App.tsx`）：参数
  `offset/limit` 不同时指纹不同，守卫不拦；模型在 `sed -i` 与重读之间
  反复横跳。若下一轮想压这个，需要按「文件版本未变 + 本轮已读过该区间」
  合并判定，而不是只看 `(tool, params)`。


