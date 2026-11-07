# B5（子代理 / 后台）调研方案

- 批次：B5 子代理 / 后台（W1）
- 项目根：`/workspace/ai-coding`（源码根 `ai_coding/`）
- 覆盖：SUB-01 ~ SUB-05
- 状态：只读调研完成，未改任何代码
- 说明：文中行号均以当前工作树（基线 commit `fd5a929` 之后）为准，已逐条打开核对。

---

## 0. 前置专项验证（任务明确要求）

### (a) `world.aclose` 当前关闭顺序，插入 shutdown 是否会对已关 loop 调用

现状（`ai_coding/server/world.py:56-83`）：

1. `56-66`：取 `self.registry.scheduler`，调用 `scheduler.aclose()`（best-effort，异常吞掉只记 debug）。
2. `67-76`：依次 `self.loop.aclose()`（主 `AgentLoop`）、`self.mcp.aclose()`。
3. `78-83`：同步关闭 `sandbox_backend`。

调用点：`ai_coding/server/launcher.py:171-174`，`await world.aclose()` 位于 **async 函数体的 finally 里**，事件循环仍然存活。

结论：

- 现有顺序已经把 scheduler 放在主 loop 之前关闭，**在这一层插入 `Scheduler.shutdown()` 是安全的**：此时 asyncio 事件循环没有被关闭，`await task.cancel()/gather()/runner.close()` 都合法。
- 子代理的 `handle.loop` 是 `AgentLoop` 对象（`core/subagent.py:175`），不是 asyncio loop；关闭它不依赖事件循环是否还开着。
- 唯一的“已关 loop 调用”风险来自把 shutdown 放到 `asyncio.run()` 返回之后（例如 atexit / `__del__`）。本方案不这样做，也不新增 atexit。
- 二次调用安全：`shutdown` 只对 `_tasks` 快照与 `runner.close` 幂等调用；`runner.close` 在 handle 不存在时返回 `False`（`core/subagent.py:1127-1129`），重复调用无副作用。
- **建议不修改 `server/world.py`**：让 `Scheduler.aclose()` 在开头 `await self.shutdown()`，这样现有 `world.aclose → scheduler.aclose` 的调用即满足“关 loop 前调用”，同时不越出本批文件白名单（`server/world.py` 未在 B5 白名单内，见 §6）。

### (b) `_idem_put` 现有签名与调用点；提前登记是否需要改状态机

签名（`ai_coding/core/scheduler.py:1062-1069`）：

```python
def _idem_put(self, key: str, digest: str, **payload: Any) -> None:
    if not key:
        return
    entry = self._idem.setdefault(key, {})
    entry.update(payload)
    entry["digest"] = digest
    if len(self._idem) > 200:
        self._idem.pop(next(iter(self._idem)))
```

调用点：

| 位置 | 用途 | 写入的 payload |
|---|---|---|
| `scheduler.py:270-272` | `spawn()` 成功启动后台 | `request_id`, `run_id` |
| `scheduler.py:618` | `send()` 命令去重 | `status` |
| `scheduler.py:929-933` | `_settle()` 有 result 时 | `request_id`, `run_id`, `result` |
| `scheduler.py:1096-1099` | fanout 缓存 | `request_id`, `packets` |

读取点只有两个，且都只判断 digest 与“有没有 result / run_id”：

- `_idem_lookup`（`1036-1048`）：digest 不一致才抛 `IdempotencyConflict`；否则原样返回 dict。
- `_idem_get`（`1050-1060`）：同上，供 `send()` 用。

判定“在途”的语义已经存在：

- `run()`（`153-159`）：`cached` 存在但 `cached.get("result") is None` → 抛 `IdempotencyConflict("... already in flight")`。
- `spawn()`（`231-239`）：`cached` 存在但 `cached.get("run_id")` 为空 → 同样抛“already in flight”。

结论：

- **不需要改状态机**，也不需要改 `_idem_put` 签名。提前写入 `status="running"` 只是填充一个当前无人读取的展示字段，真正的拦截来自“没有 result”。
- 需要新增一个删除入口（当前没有）：`_idem_forget(key)`，在 `run()` 的创建失败 / 取消 / 异常路径里弹出，避免失败后同一 key 永久“在途”而无法重试。
- `_idem` 字典有 200 条上限（`1068-1069`），提前登记会让一次失败（若忘记删除）挤掉一条旧记录；删除动作可以消除这个副作用。
- 只对 `run()` 做提前登记：`run()` 在 `_idem_lookup` 与 `create_task` 之间没有 `await`，但 `create_task` 之后 `await task` 让出控制权，第二个协程可在此期间完成一次“未命中的 lookup”。`spawn()` 从 lookup 到 `_idem_put` 全程无 `await`（`try_reserve`、`spawn_background` 均为同步），单事件循环下不会交错，不必改。

### (c) `ai/prompt.py` 是否已有 `<agents>` 或类似块（避免重复注入）

已通读 `ai_coding/ai/prompt.py:1-199`，并全仓 grep `<agents>` / `Available (sub-)agents` / `agent table`：

- `PromptAssembler.build`（`126-191`）现有 section 只有：base → `<system-reminder>Project instructions:` → `<system-reminder>Agent instructions:` → `<methods>` → `<plan-mode>` → `<skills>`。
- **没有** `<agents>`，也没有向模型列出 agent 清单的任何文本。
- `<system-reminder>Agent instructions:`（`148-152`）是“当前 agent 自己的 body”（`core/agent_loop.py:2280-2285`），与“可委派目标清单”不是同一件事，不会重复注入。
- 唯一接近的“清单”在工具描述里：`tools/subagent_tool.py:227-245`、`tools/agents_tool.py:156-162` 只说“从 agent table 选”，不给内容。
- 尾部的 `request_assembly.py:558-593` 只有 `environment / memory / read_ledger / mcp_catalog / plan_mode / plan_record / task / recommended_skills / referenced_files / error_recovery / anchor / hook / digest / budget`，**没有 agents 块**。

结论：新增 `<agents>` 不会重复；但必须保持 `build()` 的默认参数为空串，否则会破坏 `tests/test_prompt.py:89-93` 的字节级断言。

---

## 1. SUB-01 产物路径指向已删除的 worktree

### 现状（证据）

- `ai_coding/tools/subagent_tool.py:87-89`：

  ```python
  if result.artifacts:
      lines.append("产物:")
      lines.extend(f"  - {artifact.path}" for artifact in result.artifacts[:10])
  ```

- `ai_coding/core/worktree.py:224-234`：`temporary()` 在 `finally` 中 `await self.remove(str(wt.path))`，目录必删；`remove` 还会 `shutil.rmtree` 兜底（`163-171`）。
- 同步隔离路径正是用 `temporary()`：`core/subagent.py:547-571`（在块内 `_run_in`，出块即删）。
- 产物 path 是**相对子沙箱 workspace** 的路径：`tools/write_tool.py:352-355`、`tools/edit_tool.py:374-377` 均用 `target.relative_to(self._sandbox.workspace).as_posix()`；子沙箱 workspace 就是 worktree（`tools/builder.py:134-147` → `build_tool_registry(workdir or main_ws, ...)`）。
- 领域对象自己也承认这个缺陷：`domain/subagent.py:307-311` 的注释写明“packet 曾宣告一个返回时就被删掉的 worktree 里的 artifacts 路径”。
- `_render` 的文档字符串已经声明 `workdir` 是“gone by the time the parent reads this”（`tools/subagent_tool.py:51-59`），但 87-89 行仍把 artifacts 当活文件打印。

### 根因

`SubagentResult.artifacts` 与 `worktree_branch/commit` 是同一份事实的两种载体，但 `_render` 只对后者做了“工作区已删除”的说明（`90-93`），对产物列表没有做任何限定。父模型看到的 `  - src/foo.py` 既可能是“worktree 里的相对路径”，也会被 `read` 工具按**主 workspace** 解析，从而读到同名但未修改的旧文件——这是比“路径不存在”更坏的静默错误。

### 候选方案与取舍

| 方案 | 做法 | 兼容性 | 回归面 | 测试成本 | 风险 |
|---|---|---|---|---|---|
| A 不做 | — | 最高 | 0 | 0 | 缺陷保留：模型可能读错文件 |
| B 直接删除产物行 | 不再打印 artifacts | 中（信息丢失） | `tests/test_tool_subagent.py:141-154` 断言 `"parser.py" in output` 会失败 | 低（需改旧测试，属“修正错误期望”） | 丢失有用的改动清单 |
| C 改写文案并标注生命周期（推荐） | 有 branch → 标注“隔离工作区已删除、路径相对该分支”；提交失败 → 标注文件已丢失；无 branch → 原样 | 高（无 branch 的输出逐字节不变） | 小 | 低 | 文案变长 |
| D 把产物复制回主 workspace / 自动 merge | 落盘到主树 | 低 | 大（越权写、与合并队列冲突） | 高 | 破坏隔离边界，明确不做 |

推荐 **C**。

### 推荐方案（含签名）

在 `ai_coding/tools/subagent_tool.py` 新增私有辅助函数，并让 `_render` 调用：

```python
def _artifact_lines(result: SubagentResult) -> list[str]:
    """产物清单：隔离运行的产物目录已被删除，必须说明它现在在哪里。"""
```

行为：

- `result.artifacts` 为空 → 返回 `[]`（不打印空标题）。
- `result.worktree_branch` 为真且无 `commit_error`：
  `["产物（隔离工作区已删除，路径相对分支 {branch}）:", "  - {path}", ...]`
- `result.worktree_branch` 为真且有 `commit_error`：
  `["产物（隔离工作区已删除，且提交失败：{commit_error}，以下文件已丢失）:", ...]`
- 否则：`["产物:", ...]`（与现状一致，保证非隔离路径输出不变）。
- 数量上限仍为 10（沿用 `result.artifacts[:10]`）。

改动点：`_render` 的 `87-89` 替换为 `lines.extend(_artifact_lines(result))`；`__all__` 不变。

错误路径：

- `artifact.path` 为空：`ToolArtifact.path` 是必填字段；仍加 `if not path: continue` 防御，不打印空行。
- 分支存在但 commit 为 `None`：仍按“已提交到分支”措辞不合适，改为 `（路径相对分支 {branch}）`，不声称已提交。
- 超长路径：不截断（保持 `path` 可复制）。

### 测试计划

文件：`tests/test_tool_subagent.py`（白名单写作 `tests/test_subagent_tool.py`，实际文件名见 §6）

| 用例 | 断言要点 | 修复前失败 |
|---|---|---|
| `test_render_marks_isolated_artifacts_as_gone` | `_render` 输出含 `ai_sub_x`、`工作区已删除`，且不只给裸路径 | 是（现在只有 `产物:`） |
| `test_render_marks_uncommitted_artifacts_as_lost` | `commit_error` 时输出含 `已丢失` 与错误文本 | 是 |
| `test_render_keeps_plain_artifact_list_without_branch` | 无 `worktree_branch` 时输出含 `产物:` 与 `parser.py`（现有行为护栏） | 否（回归护栏） |

现有 `test_subagent_tool_passes_artifacts`（`141-154`，无 branch）保持不变。

### 风险与回退

- 风险：文案变长，父模型仍需理解“路径相对分支”。缓解：与已有的 `工作区分支: ...（未合并）` 行相邻输出，语义一致。
- 风险：`data["workdir"]` 仍指向已删目录（`tools/subagent_tool.py:131`）。本项不改 domain/`data`（改动会碰未在白名单的 `domain/subagent.py`，且测试 `tests/test_subagent_runner.py:431` 断言 `result.workdir == "wt/path"`）；在文档中标注为诊断字段。
- 回退：单文件、纯文案，回退即还原 `_render` 三行。

---

## 2. SUB-02 停机不关 runner / 子 loop / worktree；空闲 handle 不剪枝；`Scheduler.stop` 裸 create_task

### 现状（证据）

1. **Scheduler 不关 runner**
   - `core/scheduler.py:1164-1174` `aclose()` 只做三件事：置 `_closed=True`、取消 `_reaper`、`await self._merge_queue.aclose()`。没有取消 `self._tasks`（`122`）中的 turn/fanout 任务，也没有调用 `runner.close`。
   - `core/subagent.py:1120-1185` `close()` 本来就会“取消 turn task → 关子 loop → 提交并删除 worktree → 从 `_handles`/`_tasks` 移除”，但停机路径从不调用它。
   - `server/world.py:56-66` 只调 `scheduler.aclose()`；子 loop 与 worktree 因此泄漏。
2. **空闲 handle 不剪枝**
   - `core/subagent.py:1319-1335` `_prune_background()` 只按 `_MAX_BACKGROUND_ROWS`（`102`）裁剪“**不在 `_handles` 里**的终态行”（`1330`）。有 handle 的行永不删，`_handles`（`342`）也就只增不减：会话、`AgentLoop`、持久 worktree 全部常驻。
3. **`Scheduler.stop` 裸 create_task**
   - `core/scheduler.py:664-673`：`loop.create_task(stop_run(run_id))`，返回的 task 既不保存也不设 done callback；停机时无法取消/等待它，异常也无人回读（`Task exception was never retrieved`）。

### 根因

- 生命周期所有权分裂：`Scheduler` 拥有“任务身份”，`SubAgentRunner` 拥有“子 loop/worktree/handle”，`AgentLoop` 拥有主 loop。停机只清了第一层的 reaper 和 merge 队列，没有沿所有权链把子资源带下去。
- `_prune_background` 的剪枝条件是“行没有 handle”，但 handle 的释放只发生在显式 `close_agent`/`stop_run`，没有任何时间维度，长期进程必然无界增长。
- `stop` 是同步命令入口（`649-651` 注释），作者为了不阻塞命令处理器临时 `create_task`，却没有接回 `_tasks` 的既有约定（`run()`/`fanout()` 都保存引用并在 finally 清理，`165-170`、`317-324`、`342-344`）。

### 候选方案与取舍

| 方案 | 做法 | 兼容性 | 回归面 | 测试成本 | 风险 |
|---|---|---|---|---|---|
| A 不做 | — | 最高 | 0 | 0 | 停机泄漏、长期进程内存/worktree 累积 |
| B 只在 `world.aclose` 取消任务 | 不关 handle | 中 | 小 | 低 | 子 loop/worktree 仍泄漏 |
| C `Scheduler.shutdown()` + `aclose` 内嵌 + `_prune_background` TTL + `stop` 入 `_tasks`（推荐） | 见下 | 高（新增能力，不改已有默认值） | 中（subagent.py / scheduler.py） | 中 | 关闭耗时有上界问题（见风险） |
| D runner 注册 atexit 自行清理 | 跨 loop 等待 | 低 | 大 | 高 | atexit 无法 await，易二次 close |

推荐 **C**。

### 推荐方案（含签名）

#### 2.1 `Scheduler.shutdown()`（`core/scheduler.py`）

```python
#: 单个子代理关闭的上界；close() 内部可能跑 git（WorktreeManager 默认 60s）
_SHUTDOWN_CLOSE_SECONDS = 20.0

class Scheduler:
    async def shutdown(self) -> None:
        """取消在途 turn/fanout（保留引用后取消），再逐个关闭子代理。"""

    async def aclose(self) -> None:
        await self.shutdown()          # 新增：放在最前
        self._closed = True
        ...                             # 其余不变（reaper / merge_queue）
```

实现要点：

1. `tasks = list(self._tasks.items())` —— **先物化再取消**，因为 `run()/fanout()` 的 `finally` 会 `pop`（`183`、`344`），迭代中修改会跳过元素。
2. `for _, task in tasks: task.cancel()`；`await asyncio.gather(*(t for _, t in tasks), return_exceptions=True)`；`self._tasks.clear()`。
3. 逐个关闭子代理：
   ```python
   close = getattr(self._runner, "close", None)
   if callable(close):
       for run_id in self._live_run_ids():
           try:
               await asyncio.wait_for(close(run_id), timeout=_SHUTDOWN_CLOSE_SECONDS)
           except Exception:
               pass  # 与 aclose 的既有 best-effort 风格一致
   ```
4. `_live_run_ids()` 私有辅助：`self.task_list()`（`1182-1184`）的 `run_id` ∪ `self._request_runs.values()`，去重保序。`task_list()` 里的行就是 addressable handle 的投影（`subagent.py:1319-1335` 保证“有 handle 的行不被裁”），所以能覆盖全部 handle。
5. `shutdown` 自身幂等：`_tasks` 已空、`close` 对已消失的 handle 返回 `False`。

`aclose` 内嵌而不是改 `world.py` 的理由见 §0(a)：既满足“关 loop 前调用”，又不越白名单。

#### 2.2 `Scheduler.stop` 的 task 引用（`core/scheduler.py:664-673`）

```python
key = f"stop:{run_id}"
task = loop.create_task(stop_run(run_id), name=key)
self._tasks[key] = task
task.add_done_callback(lambda _t: self._tasks.pop(key, None))
```

- key 用 `stop:` 前缀，避免与 `run()` 的 `request.id` 冲突（`170`）。
- 保留既有分支：无运行 loop 或无 `stop_run` 时仍走同步 `self._runner.cancel(run_id)`（`672-673`）。
- `stop()` 的同步语义与 `bool` 返回值不变。

#### 2.3 空闲 handle TTL（`core/subagent.py:1319-1335`）

```python
#: 空闲 handle 的保留时长（30 分钟）。不新增配置键：config/models.py 不在本批白名单，
#: 加了而读不到会变成死配置。
_MAX_IDLE_HANDLE_SECONDS = 1800.0
```

`_prune_background()` 在原有“行裁剪”之前追加一段“handle 退休”：

```python
def _prune_background(self) -> None:
    self._retire_idle_handles()
    ...  # 原有 terminal 行裁剪保持不变

def _retire_idle_handles(self) -> None:
    ...
def _retire_done(self, run_id: str, _task: asyncio.Task[None]) -> None:
    self._tasks.pop(f"retire:{run_id}", None)
```

判定与动作：

- 只看 `handle.status in ("idle", "interrupted")`；`running/interrupting/stopped/closed` 不动。
- 空闲起点取投影行的时间：`row.ended` 优先，否则 `row.started`（`domain/subagent.py:603-604`）；无行则跳过（保守）。
- `now - idle_since < _MAX_IDLE_HANDLE_SECONDS` → 跳过。
- `f"retire:{run_id}" in self._tasks` → 已在退休中，跳过（防止同一 handle 被调度两次）。
- 调度：`task = asyncio.get_running_loop().create_task(self.close(run_id), name=f"subagent-retire:{run_id}")`，存入 `self._tasks[f"retire:{run_id}"]`，`add_done_callback(functools.partial(self._retire_done, run_id))`。
- 无运行 loop（`RuntimeError`）→ 直接 `break`，不退休（该函数两个调用点 `675`、`1220` 都在 async 栈内，正常情况下不会触发）。
- 不重复释放 slot：`close()` 内部已 `_release_slot`（`1131`），退休路径不再调用。
- `_prune_background` 仍在 `spawn_background:675` 与 `_restore_row:1220` 调用，签名不变。

为什么用行时间而不是给 `AgentHandle` 加字段：`handle.status` 有 4 处赋 idle（`886`、`943`、`1111`、`1398`），逐处补 `idle_since` 容易漏；行的 `ended/started` 已经由 `_finish_background:1505-1519` / `_restore_row:1203-1232` 统一维护。

### 测试计划

`tests/test_scheduler.py`：

| 用例 | 断言要点 | 修复前失败 |
|---|---|---|
| `test_shutdown_closes_every_live_child` | `runner.rows` 两条终态行 → `await scheduler.shutdown()` 后 `runner.closed == [rid1, rid2]` | 是（closed 为空） |
| `test_shutdown_cancels_an_in_flight_turn` | 用一个永不返回的 `_Runner.run` 起 `scheduler.run(req)` 任务，`shutdown()` 后该任务以 `CancelledError` 结束，`scheduler._tasks == {}` | 是 |
| `test_stop_tracks_its_stop_task` | `scheduler.stop(rid)` 后存在 `stop:<run_id>` 键，`await asyncio.sleep(0)` 后被 done callback 清掉，且 `runner.stop_run` 已执行 | 是（无键） |
| `test_shutdown_is_idempotent` | 连续两次 `shutdown()` 不抛异常 | 是（方法不存在） |

`tests/test_subagent_handles.py`（该文件已有 `_runner(tmp_path)` 与 `_settle`，见 §6 白名单偏差）：

| 用例 | 断言要点 | 修复前失败 |
|---|---|---|
| `test_an_idle_handle_past_its_ttl_is_retired` | monkeypatch `_MAX_IDLE_HANDLE_SECONDS = 0.0`；spawn→settle→`_prune_background()`；`run_id not in runner._handles`，行变 `cancelled`，`runner._tasks` 无 `retire:` 残留 | 是 |
| `test_a_recent_idle_handle_is_kept` | 默认 TTL 下 `run_id in runner._handles`（护栏） | 否 |
| `test_prune_never_drops_a_row_that_still_has_a_handle`（已有 `159-170`） | 必须继续通过 | 否（回归护栏） |

`tests/test_subagent_runner.py`：

| 用例 | 断言要点 |
|---|---|
| `test_close_releases_the_child_loop_and_handle` | 已有 `close()` 路径的护栏（若尚无此用例则补） |

### 风险与回退

- 风险：`close()` 可能因 git 提交触发最坏 60s/子代理。用 `_SHUTDOWN_CLOSE_SECONDS = 20.0` 包一层；代价是超大 worktree 的最后一次 safety-net commit 可能被切掉——但这些改动在每轮结束时已由 `_commit_turn` 提交（`core/subagent.py:1387-1390`、`561-563`），close 的 commit 只是兜底。
- 风险：退休 idle handle 会删除其持久 worktree（`close()` 的行为）。因已有 `commit_if_dirty`（`1159-1161`）先提交，不丢写；但“可 resume 的 worktree 目录”会消失，resume 会重建（`resume:1114`）。
- 风险：`shutdown()` 在 `aclose()` 里执行，会让 `aclose` 的耗时从“毫秒级”变成“至多 N×20s”。测试/CLI 退出可接受；如评审要求更快，可改为并发 `gather`，但会并发跑 git。
- 回退：三处独立改动（scheduler shutdown、stop 引用、prune TTL），可分别回退。

---

## 3. SUB-03 同 idempotency_key 并发可双跑

### 现状（证据）

`core/scheduler.py:144-186`（`run()`）：

```python
cached = self._idem_lookup(request)            # 153
if cached is not None:
    if cached.get("result") is not None: return cached["result"]
    raise IdempotencyConflict(... already in flight)   # 157-159
record = self._begin(request, kind="child")    # 160（同步）
...
task = asyncio.create_task(self._execute_child(...))  # 165-168
record.task = task
self._tasks[request.id] = task
try:
    result = await task                        # 172 ← 第一个让出点
...
self._settle(record, result=result)            # 184
```

`_settle` 只在**有 result** 时写 `_idem`（`929-933`）；`run()` 在 153 与 930 之间没有任何 `_idem_put`。

`core/scheduler.py:222-281`（`spawn()`）：

```python
cached = self._idem_lookup(request)            # 231
...
record = self._begin(...)                      # 240
slot = self.slots.try_reserve(request.id)      # 241（同步）
run_id = self._runner.spawn_background(...)    # 250（同步）
...
self._idem_put(record.key, record.digest, request_id=..., run_id=...)  # 270-272
```

### 根因

`_idem` 是“结果缓存”，不是“在途锁”。`run()` 把登记推迟到 `_settle`，于是“lookup 未命中 → create_task → await”之间形成一个竞态窗口：第二个协程在这段时间进入，看到的仍是空缓存，于是再建一个 task。`157-159` 的 “already in flight” 分支当前**不可达**，因为没有任何代码写过“无 result 的条目”。

`spawn()` 虽然也在 settle 前才登记，但整段无 `await`（`try_reserve` / `spawn_background` 都是同步函数），单线程事件循环下不会被切入，因此真正的双跑窗口只在 `run()`（以及 `run()` 与 `spawn()` 混用同一 key 时）。

### 候选方案与取舍

| 方案 | 做法 | 兼容性 | 回归面 | 测试成本 | 风险 |
|---|---|---|---|---|---|
| A 不做 | — | 高 | 0 | 0 | 同一 `call_id` 并发双跑（工具层每个 call 一次，但重试/并发工具批可触发） |
| B `run()` 在 create_task 前 `_idem_put(status="running")`，失败删除（推荐） | 复用既有 in-flight 语义 | 高（只在 `run()` 内部） | 小 | 低 | 第二个调用从“双跑”变为“抛 `IdempotencyConflict`” |
| C 用 `asyncio.Lock` 串行化 lookup+create | 不改缓存 | 中（锁需按 key 或全局） | 中 | 中 | 只保护单 loop；`spawn` 仍读不到在途状态 |
| D 存 `asyncio.Future`，第二个调用 await 同一结果 | 语义最好 | 低 | 大 | 高 | 死锁/取消传播复杂，超出本批 |

推荐 **B**。

### 推荐方案（含签名）

```python
def _idem_forget(self, key: str) -> None:
    if key:
        self._idem.pop(key, None)
```

`run()` 的改动（`144-186`）：

```python
key = request.idempotency_key
...
record = self._begin(request, kind="child")
started = time.monotonic()
if observer is None and callable(emit):
    observer = EmitObserver(emit)
token = current_task_id.set(request.id)
# 新增：抢占式登记，堵住 lookup→await 的窗口；_settle 会用 result 覆盖它
self._idem_put(key, request.payload_digest, request_id=request.id, status="running")
# 注意：create_task 单独包一层 try/except，与下面 await task 的 try 分开，
# 避免 current_task_id.reset 被执行两次。
try:
    task = asyncio.create_task(...)
except BaseException:
    self._idem_forget(key)
    current_task_id.reset(token)
    raise
...
except asyncio.CancelledError:
    ...
    self._idem_forget(key)
    raise
except Exception as exc:
    ...
    self._idem_forget(key)
    raise
finally:
    current_task_id.reset(token)
    self._tasks.pop(request.id, None)
```

错误路径：

- 第二个并发调用：`_idem_lookup` 命中但无 `result` → `IdempotencyConflict`；工具层 `except Exception` 返回 `success=False, error="subagent_error"`（`tools/subagent_tool.py:402-407`），不会双跑。
- `create_task` 抛错 / 被取消 / 抛异常：删除登记，同一 key 可重试。
- 成功：`_settle` 写入 `result`（`929-933`），后续同 key 直接返回缓存结果（语义不变）。
- 返回 `SubagentResult(status="error")` 而非抛异常：仍保留缓存（现状），本项不动——“完成的失败包”与“没跑起来”是两种语义，删除会改变既有行为。
- `key == ""`：`_idem_put` 立即返回（`1063-1064`），`_idem_forget` 也是 no-op，不与既有“空 key 不去重”测试冲突（`tests/test_scheduler.py:540-548`）。

### 测试计划

`tests/test_scheduler.py`：

| 用例 | 断言要点 | 修复前失败 |
|---|---|---|
| `test_concurrent_runs_with_the_same_key_start_only_once` | 用 `asyncio.Event` 卡住 `_Runner.run`；`gather(run(req), run(req), return_exceptions=True)`；`len(runner.run_calls) == 1`，另一个是 `IdempotencyConflict` | 是（run_calls == 2） |
| `test_a_failed_run_releases_the_key_for_retry` | `_Runner.run` 抛 `RuntimeError`；第二次同 key 能再次进入 runner | 是（修复后若不删除则第二次抛 in-flight） |
| `test_a_cancelled_run_releases_the_key_for_retry` | 取消外层任务后再次 `run` 能执行 | 是 |
| `test_a_completed_run_still_returns_the_cached_result` | 顺序两次同 key，第二次不调用 runner（护栏，现有 `517-536` 只覆盖 spawn） | 否 |

### 风险与回退

- 风险：并发第二个调用从“静默双跑”变为“显式失败”，属行为变化；但在 P2 缺陷下这是期望行为，且错误码可读。
- 风险：忘记删除会导致 key 永久占用。测试覆盖失败/取消两条路径；`create_task` 失败也加了 `BaseException` 兜底。
- 回退：`scheduler.py` 内 5 行 + 一个 3 行辅助函数。

---

## 4. SUB-04 自定义 agent 对模型不可见

### 现状（证据）

- `agents/registry.py:223-255` `load_agents()`：内置 4 个（`234-236`）→ 逐个根目录读取 `*.md`、解析失败整文件跳过（`244-254`）→ `AgentCatalog(specs=..., skipped=...)`。自定义 agent 确实进入了 catalog。
- `domain/agents.py:147-151` `subagents()` 按 id 排序返回 kind==`subagent` 的全部；`153-...` `visible()` 再去掉 hidden。
- `core/agent_loop.py:434` `self.agents = agents`；`2280-2285` `_agent_body` 只取**当前 session 的 agent** 的 body；`2272` 只取当前 agent 的规则。**没有任何地方把可委派清单交给模型。**
- `core/agent_loop.py:3494-3552` `_build_instructions()` 组装系统提示：`project_text`、`catalog`（仅 skills，`3501-3521`）、`mode`、`agent_instructions`、`tools`；传给 `PromptAssembler.build`（`3533-3552`）。
- `ai/prompt.py:126-191` 的 section 列表见 §0(c)，无 agents。
- `core/request_assembly.py:558-593` 的 tail 块里 `catalog` 只来自 `self._catalog_builder()`（MCP，`560`），也没有 agents。

后果：模型只能从 `subAgent`/`spawn_agent` 的参数描述里知道“有个 agent table，默认 general”（`tools/subagent_tool.py:264-270`、`tools/agents_tool.py:156-162`），无法知道项目里自定义的 `reviewer`/`planner` 等 id，因此不会去选它们。

### 根因

agent 清单在启动时加载（`server/world.py:165-172`），但系统提示的组装只消费“当前 agent 自身”，没有消费“可委派 agent 集合”。缺一个与 `<skills>` 同级的静态参考段落。

### 候选方案与取舍

| 方案 | 做法 | 兼容性 | 回归面 | 测试成本 | 风险 |
|---|---|---|---|---|---|
| A 不做 | — | 高 | 0 | 0 | 自定义 agent 形同不存在 |
| B `ai/prompt.py` 增 `<agents>` 静态段（推荐） | `build(agents=...)` | 高（默认 None → 输出逐字节不变） | 小（prompt.py + 1 处调用） | 低 | 每请求多 ≤600 字符 |
| C 放进 `request_assembly` 的 volatile tail | 每次迭代重建 | 中 | 中（tail 预算、canonical 变化） | 中 | 与 skills 的静态先例不一致，重复计费更高 |
| D 扩写 `subAgent` 工具 description | 动态拼清单 | 低 | 大（两个工具、schema 每步都付费） | 中 | 工具描述膨胀，且 `spawn_agent` 需同步 |

推荐 **B**。

### 推荐方案（含签名）

`ai_coding/ai/prompt.py`：

```python
from collections.abc import Iterable
from ai_coding.domain.agents import AgentSpec, PLAN_TEXT_RULE

#: 单条 agent 行的上限（含 "- " 前缀）。
_AGENT_LINE_CHARS = 120
#: 整个清单的字符预算；超出即截断，不再追加条目。
_AGENTS_TOTAL_CHARS = 600


def render_agents_block(specs: Iterable[AgentSpec]) -> str:
    """把可委派 agent 渲染成 `<agents>` 段；空集合返回 ""。"""


class PromptAssembler:
    def build(
        self,
        project_instructions: str = "",
        skills_catalog: str = "",
        agent_instructions: str = "",
        mode: str = "normal",
        tools: tuple[str, ...] | None = None,
        #: SUB-04：可委派目标。None/空 → 不输出该段（维持字节级兼容）。
        agents: Iterable[AgentSpec] | None = None,
    ) -> str:
```

渲染规则：

- 跳过 `spec.hidden` 与空 `spec.id`。
- 行格式：`- {id} — {description or name}`；按 `_AGENT_LINE_CHARS` 截断，截断时补 `…`。
- 累计超过 `_AGENTS_TOTAL_CHARS` 的行直接丢弃（不输出“还有 N 个”以省预算；数量上限本身就有界）。
- 段落文本：

  ```
  <agents>
  Available sub-agents (pass the id as the subAgent / spawn_agent `agent` argument):
  - general — General-purpose subagent.
  - explore — Read-only exploration subagent.
  </agents>
  ```

- 插入位置：`<skills>` 之后（section 顺序 base → project → agent instructions → methods → plan → skills → agents），保证 `tests/test_prompt.py:134-138`（methods < plan < skills）和 `89-93`（默认字节不变）继续成立。
- **按工具面裁剪**：仅当 `tools is None`（调用方不知道表面，保守保留）或 `"subAgent" in tools or "spawn_agent" in tools` 时输出。原因：子代理栈 `include_subagent=False`（`tools/builder.py:142`）没有这两个工具，不能向它宣传“可委派目标”；plan 模式也会把 `subagent`/`spawn_agent` 从表面裁掉（`agents/registry.py:70-88`），自然不输出。

`core/agent_loop.py`（唯一需要的接线，见 §6 白名单偏差）：

```python
surface = self._prompt_tools(mode, readonly, session_rules, agent_rules,
                             _armed_profile(session), loaded, plan_implementing=...)
return self.prompt.build(
    ...,
    tools=surface,
    agents=self.agents.subagents() if self.agents is not None else None,
)
```

当前 `tools=self._prompt_tools(...)` 是内联实参（`3541-3551`），改为先算局部变量即可；行为不变。

### 测试计划

`tests/test_prompt.py`（白名单内）：

| 用例 | 断言要点 | 修复前失败 |
|---|---|---|
| `test_agents_block_lists_delegation_targets` | `build(agents=[general, explore], tools=("read","subAgent"))` 输出含 `<agents>`、`general —`、`explore —` | 是 |
| `test_agents_block_is_omitted_without_the_tool` | 同样 agents，但 `tools=("read",)` → 不含 `<agents>` | 是 |
| `test_agents_block_is_bounded` | 20 个 spec、每个描述 500 字 → 每行 ≤120，整段 ≤600，条数受限 | 是 |
| `test_agents_block_is_after_skills` | `index("<skills>") < index("<agents>")` | 是 |
| `test_agents_block_default_is_byte_identical` | `build()` 仍等于 base + METHODS_SECTION（护栏） | 否 |

`tests/test_agents.py`（新文件，白名单内）或 `tests/test_agents_tools.py`：

| 用例 | 断言要点 |
|---|---|
| `test_custom_agent_reaches_the_rendered_block` | `load_agents([tmp_root])` 写入一个 `reviewer.md`，`render_agents_block(catalog.subagents())` 含 `reviewer —` |
| `test_a_hidden_subagent_is_not_advertised` | hidden=true 的 agent 不出现 |

### 风险与回退

- 风险：每请求多 ≤600 字符（约 150-200 tokens）。可接受；如需更省，可只列 `id — description` 首句。
- 风险：`agents=` 传入的是 `Iterable`，若调用方传生成器且 `build()` 内多次消费会耗尽。实现里只消费一次（先 `list()` 再渲染）即可。
- 风险：`agent_loop.py` 不在白名单（见 §6），若评审拒绝扩白名单，则本项无法落地，需降级为 `BLOCKED`。
- 回退：只加一个默认参数 + 一个纯函数，回退无副作用。

---

## 5. SUB-05 子代理 gate 固定主 workspace

### 现状（证据）

- `core/subagent.py:1702-1727` `_child_gate()`：

  ```python
  # The main workspace, not the worktree: grants are keyed by the path the
  # operator was asked about, and the child has the same authority there.
  return PermissionGate.from_config(
      self._app_config, workspace=self._workspace, skill_resolver=resolve
  )
  ```

- 调用点 `core/subagent.py:1919-1924`：`AgentLoop(..., gate=self._child_gate(), workspace=workdir or self._workspace, ...)`。**同一个 loop 里，工具的 sandbox 用 workdir，dispatcher 的 gate 用主 workspace。**
- 与工具栈对比：`core/subagent.py:1910` `registry = self._build_tools(workdir)` → `tools/builder.py:134-147` 用 `workdir or main_ws` 调 `build_tool_registry`；后者在 `tools/builder.py:300-317` 以同一 `workspace` 构造 `PermissionGate.from_config(..., workspace=workspace)` 与 `Sandbox(workspace, ...)`。也就是说 `build_tool_registry(workdir)` **已经**是 workdir 源，只有 loop 的 gate 是主 workspace。
- gate 的 workspace 决定两件事：`_outside_resource_reason`（`security/permission_gate.py:782-809`）把 worktree 内的绝对路径判成 WARN“outside workspace”，以及 `_resolved_subject`（`811-835`）把绝对路径相对化失败后返回 `None`（规则不匹配）。
- `from_config`（`security/permission_gate.py:1081-1116`）用 workspace 做 `GrantStore(workspace)`（`1116`）与 `load_file_rules(workspace)`（`1157`）。

### 根因

`_child_gate` 在抽取时沿用了“子代理只继承策略、路径边界由 sandbox 管”的旧假设，但 `build_tool_registry(workdir)` 早已把 gate/sandbox 的 workspace 统一到 workdir；loop 侧新增的独立 gate 没跟着改。结果是同一个子代理有**两个 workspace 基准**：文件工具按 worktree 判，dispatcher 按主树判。

### 候选方案与取舍

| 方案 | 做法 | 兼容性 | 回归面 | 测试成本 | 风险 |
|---|---|---|---|---|---|
| A 不做 | — | 高 | 0 | 0 | worktree 绝对路径被误判 outside / 规则失配 |
| B `_child_gate(workdir or main)`（推荐） | 最小改动，基准对齐 | 高（workdir=None 时行为不变） | 小 | 低 | 子代理 grant 的 workspace 维度改变（见风险） |
| C 复用 registry 内的同一个 gate 对象 | sandbox 与 dispatcher 同源 | 中 | 中（需 `ToolRegistry` 增字段/`setattr`，registry.py 不在白名单） | 中 | mypy 属性未声明 |
| D 给 gate 增加 extra roots | 保留主 workspace + worktree 根 | 低 | 大（gate API/规则匹配） | 高 | 越权面变大 |

推荐 **B**。

### 推荐方案（含签名）

```python
def _child_gate(self, workspace: str | None = None) -> Any:
    """The operator's own policy, bound to the workspace the child actually uses."""
    ...
    return PermissionGate.from_config(
        self._app_config,
        workspace=workspace or self._workspace,
        skill_resolver=resolve,
    )
```

调用点：`core/subagent.py:1924` 改为 `gate=self._child_gate(workdir or self._workspace)`。

同步修正 `1723-1724` 的注释（“The main workspace, not the worktree…”）为：无 workdir（非隔离/降级/只读）时用主 workspace；有 workdir 时用 worktree，与 `build_tool_registry(workdir)` 同源。

错误路径：

- `workdir is None`（`readonly`、`isolate=False`、`worktree_unavailable`、`worktree_create_failed`）→ 主 workspace，行为逐字节不变。
- worktree 已被删除 → `_outside_resource_reason` 只做字符串 `relative_to`，不 stat；`_resolved_subject` 的 `Path.resolve()` 容忍缺失；`load_file_rules` 缺文件返回空规则（`security/permissions.py:520-529`）。不会崩。
- `handle.loop` 重建（resume / re-attach）：`_build_loop_for` 每次都调用 `_child_gate(workdir or ...)`，与新 workdir 一致。

### 测试计划

`tests/test_subagent_runner.py`：

| 用例 | 断言要点 | 修复前失败 |
|---|---|---|
| `test_the_child_gate_is_bound_to_the_worktree` | `gate = runner._child_gate(str(wt))`；`gate.check("write", {"path": str(wt / "a.py")})` 的 reason 不含 `outside workspace`；而 `_child_gate(None)` 含 | 是 |
| `test_a_degraded_child_keeps_the_main_workspace_gate` | `_child_gate(None)` 的 workspace 仍是主树（护栏） | 否 |
| `test_a_worktree_child_still_obeys_a_config_deny` | 用带 `mcp` deny 的 app 配置构造 `_child_gate(wt)`，`check("srv__danger")` 仍为 DENY | 是（workspace 改变不得削弱硬拒绝） |

`tests/test_subagent_guards.py`（若可扩白名单）：补一个“worktree 下 config deny 仍生效”的集成用例。

### 风险与回退

- 风险（必须声明）：`GrantStore` 按 workspace 键存盘（`security/grants.py:142-150`）。gate 的 workspace 从主树变成 worktree 后，**子代理 dispatch gate 读不到操作者在该主树给的“always”授权**。影响有限：子代理没有人类可问，WARN 由 `AutoApproveCallback` 视为同意（`core/subagent.py:283-287`），而硬 DENY 来自配置文件（`load_file_rules` 读用户/项目/policy，`security/permissions.py:513-531`），不依赖 workspace 键。且子代理的 **sandbox gate 今天就已经是 worktree**（`tools/builder.py:300-308`），本项只是把 loop gate 对齐到既成事实，不新增暴露面。
- 风险：`PermissionGate.from_config` 会对 worktree 读一次项目规则文件；worktree 是 HEAD 检出，项目规则若已提交则一致；未提交的本地规则不可见。记录为已知限制。
- 回退：一个默认参数 + 一处调用点。

---

## 6. 批次内顺序

| 顺序 | 项 | 理由 | 依赖 |
|---|---|---|---|
| 1 | SUB-01 | 纯文案、单文件、零并发风险；先建立信心 | 无 |
| 2 | SUB-03 | `scheduler.py` 的 `run()`/`_idem` 小改；先稳定 `run()` 语义 | 无 |
| 3 | SUB-02 | 在稳定的 `run()` 之上加 `shutdown()` / stop 引用 / TTL；`aclose` 内嵌 | SUB-03（同文件 `scheduler.py`） |
| 4 | SUB-05 | `subagent.py` 的 gate 基准；与 SUB-02 的 `_prune_background` 区域不重叠 | 无 |
| 5 | SUB-04 | 需要白名单决策（`core/agent_loop.py`）；最后做，不阻塞其余四项 | §7 决策 |

同文件冲突提示：

- `core/scheduler.py`：SUB-03（`run`/`_idem`）→ SUB-02（`stop`/`aclose`/`shutdown`），顺序执行避免同一段 `run()` 冲突。
- `core/subagent.py`：SUB-02（`_prune_background`，1319-1335）与 SUB-05（`_child_gate`，1702-1727 + `_build_loop_for`，1924）区域分离，但建议串行。
- `ai/prompt.py`：本批只动 SUB-04；W2 的 B3 也会改该文件（多批次计划的冲突提示），不同波次，无并行冲突。

---

## 7. 明确不做

1. 不把 subagent 产物复制回主 workspace，不自动 merge（破坏隔离边界，属 C3/merge 队列职责）。
2. 不为空闲 TTL 新增配置键（`config/models.py` 不在 B5 白名单；加了却未接线会成为死配置）。用模块常量 `_MAX_IDLE_HANDLE_SECONDS = 1800.0`。
3. 不把 `Scheduler.stop` 改成 `async`，不改其 `-> bool` 契约。
4. 不删除“已完成 key 返回缓存结果”的既有语义；只删除“创建失败 / 取消 / 抛异常”的登记。
5. 不把 `<agents>` 放进 `request_assembly` 的 volatile tail，不改 MCP catalog 通道。
6. 不改 `AgentHandle` 的持久化 schema（`meta/*.json`）与 session 格式。
7. 不改 `_MAX_BACKGROUND_ROWS`（`core/subagent.py:102`）的既有行裁剪行为，只新增 handle TTL。
8. 不改 `_render` 之外的 `data` 形状（`workdir` 仍是诊断字段）。
9. 不引入 atexit / signal handler 做停机（无法 await，且跨 loop）。
10. 不改任何运行默认值（`default_model`、`context_window`、`turn_timeout_seconds`、并发上限等）。
11. 不新增第三方依赖，不联网，不 commit / push。

---

## 8. 文件清单

### 8.1 代码

| 文件 | 项 | 改动 |
|---|---|---|
| `ai_coding/tools/subagent_tool.py` | SUB-01 | 新增 `_artifact_lines`；`_render` 87-89 改调用 |
| `ai_coding/core/scheduler.py` | SUB-03 | `_idem_forget`；`run()` 提前 `_idem_put(status="running")` + 失败删除 |
| `ai_coding/core/scheduler.py` | SUB-02 | `_SHUTDOWN_CLOSE_SECONDS`、`shutdown()`、`aclose()` 开头调用、`_live_run_ids()`、`stop()` 任务入 `_tasks` + done callback |
| `ai_coding/core/subagent.py` | SUB-02 | `_MAX_IDLE_HANDLE_SECONDS`；`_prune_background` 追加 `_retire_idle_handles()`；`_retire_done` |
| `ai_coding/core/subagent.py` | SUB-05 | `_child_gate(workspace=None)`；`_build_loop_for` 调用点传 `workdir or self._workspace`；更新注释 |
| `ai_coding/ai/prompt.py` | SUB-04 | `render_agents_block`；`build(..., agents=...)`；`<agents>` section；`__all__` |

`core/worktree.py`、`tools/builder.py` **无需改动**：本方案只把它们现有的“workdir 同源”事实作为对齐基准（`tools/builder.py:134-147`、`300-317`；`core/worktree.py:224-234` 是 SUB-01 的证据与既定清理语义）。

### 8.2 测试

| 文件 | 项 | 用例 |
|---|---|---|
| `tests/test_tool_subagent.py` | SUB-01 | 3 条（见 §1） |
| `tests/test_scheduler.py` | SUB-02/03 | shutdown/stop 4 条 + 并发 4 条（见 §2、§3） |
| `tests/test_subagent_runner.py` | SUB-05 | gate 基准 3 条（见 §5）；可选 close 护栏 |
| `tests/test_subagent_handles.py` | SUB-02 | 2 条 TTL（该文件不在白名单，见 §9） |
| `tests/test_prompt.py` | SUB-04 | 5 条（见 §4） |
| `tests/test_agents.py`（新建） | SUB-04 | 2 条 catalog 渲染（白名单允许该路径） |

---

## 9. 与已定方向的偏差、白名单缺口（需在 S2 评审确认）

1. **`server/world.py` 不在 B5 白名单，但 SUB-02 的方向要求它调用 `shutdown()`。**
   - 解决方案（推荐）：让 `Scheduler.aclose()` 开头 `await self.shutdown()`；`world.aclose` 已经先调 `scheduler.aclose()`（`server/world.py:60-66`），从而在关主 loop 前完成清理，**无需改 `world.py`**。
   - 若评审坚持显式调用，则必须把 `server/world.py` 加入 B5 白名单（该文件同时被 W1 的 B1、W2 的 B6 触碰，见多批次计划 §2.1 冲突表）。
2. **`core/agent_loop.py` 不在 B5 白名单，但 SUB-04 必须在那里接线。**
   - 最小改动：`_build_instructions`（`3494-3552`）把内联的 `tools=` 提为局部变量，并多传一个 `agents=` 参数。
   - 无法绕开：`PromptAssembler.build` 的唯一调用点就是 `core/agent_loop.py:3533`；`PromptAssembler` 的构造点在 `cli.py:258`，改那里同样越界且覆盖不到子代理 loop。
   - 若评审拒绝，SUB-04 只能记 `BLOCKED`，不能以“改工具 description”降级替代（会与方向冲突，且 `spawn_agent` 需同步）。
3. **测试文件名与白名单不符（实际仓库现状）：**
   - 白名单 `tests/test_subagent_tool.py` → 实际是 `tests/test_tool_subagent.py`（含 `_render` 与 artifacts 用例）。
   - 白名单 `tests/test_agents.py` → 不存在；现有的是 `tests/test_agents_tools.py`、`tests/test_agents_command.py`、`tests/test_domain_agents.py`。
   - TTL 剪枝的自然归属是 `tests/test_subagent_handles.py`（已有 `_runner(tmp_path)` 与 `_settle`，且现有 prune 用例就在 159-170），但它不在白名单。
   - 建议：把白名单修正为 `tests/test_tool_subagent.py`、`tests/test_subagent_handles.py`；`tests/test_agents.py` 可按白名单新建。
4. **多批次计划第 328 行写“TTL 默认 30 分钟，可在 settings 配”。** 本方案取 30 分钟（`1800.0`），但**不**新增 settings 键：B5 白名单不含 `config/models.py`，且未接线的配置项属于死配置。若确实要可配，需把 `config/models.py`（B1/B4 独占）+ `server/world.py`/`tools/builder.py` 的传递链一并纳入，超出本批范围。
5. **SUB-03 方向写的是「`create_task` 之前」**，因此只对 `run()` 生效（`spawn()` 没有 `create_task`，且整段无 `await`）。这不是推翻方向，而是按代码事实收窄到真正存在竞态的路径。
6. **`core/scheduler.py` 的 `_Record` 缺字段（调查中发现，与 5 项无关，不在本批修）**：`scheduler.py:513-519`、`531`、`560` 读取 `record.is_terminal/branch/commit`，但 `_Record`（`75-93`）没有这些字段；`merge()` 路径一旦命中会 `AttributeError`。**本批不修**（不属于 SUB-01~05），但建议记入下一轮清单，避免与 B5 的 merge 相关改动混淆。
