# 批次 8-6 方案（R22·A-4 `bwrap.unavailable` 被报成 `denied`）

> 只出方案，不改代码。依据：`docs/r22-batch8-queue.md` 第 6 项、`docs/r22-feature-matrix.md` §5 A-4、
> `turn-2.jsonl` 的 `ToolFinished{error:"denied", output_preview:"error: bwrap.unavailable"}`。

## 1. 调研：一次"环境起不来"长成了"有人拒绝"

1. `security/process_sandbox.py::SandboxGap` 携带稳定原因码（`bwrap.unavailable` /
   `sandbox.wrap.no-op` / `sandbox.proxy.unavailable` / `sandbox.mask.failed`），文档明确它是
   "机制不可用且策略要求停"（process_sandbox.py:214-225）。
2. `tools/bash_tool.py` 两处（449-466、295-300）把它翻成 `ToolResult(error="denied")`：
   命令**没有执行**，但错误码说的是"被拒绝"。
3. `core/jobs.py:304-320`（后台作业）同样给 `detail="denied: <原因>"`，`denied_reason` 保留原因码。
4. 桥把 `error` 原样送到前端（`server/bridge.py`），`util/toolmeta.ts` 的
   `denied → "被拒绝"`、tone `denied`（⊘ + 删除线）。于是 UI 读作"有人拒了这次调用"，
   和真实原因（本机不能建命名空间）无关。

### 已有两个码为什么不够

| 码 | 现有语义 | 与本次的区别 |
| --- | --- | --- |
| `sandbox_violation` | 文件工具遇到"路径越界"（`write/edit/read/...`），是**对这次调用的策略判定** | 那是"这个路径不行"，不是"沙箱跑不起来" |
| `spawn_failed` | 沙箱通过后 exec 起不来（`bash_tool.py:482-489`） | 那是**起进程失败**，沙箱本身是好的 |

所以需要一个**机制不可用**的码：`sandbox_unavailable`。

## 2. 方案

**FIX-A4-1：把 `SandboxGap` 的翻译改成 `sandbox_unavailable`。**

| 位置 | 现状 | 改为 |
| --- | --- | --- |
| `tools/bash_tool.py:460-466`（无 registry 的前台路径） | `error="denied"` | `error=SANDBOX_UNAVAILABLE` |
| `tools/bash_tool.py:295-300`（`_run_as_job`，有 registry 的前台路径） | `error="denied"` | 同上 |
| `tools/bash_tool.py:246-250`（`_start_background`，`run_in_background`） | `error="denied"` | 同上（评审指出的第三处，原方案漏了） |
| `core/jobs.py:313` | `detail=f"denied: {reason}"` | `detail=f"未执行 · {reason}"` |

模块常量（`tools/bash_tool.py` 顶部）：

```python
#: A-4: the mechanism could not run here -- not a human/gate refusal.
SANDBOX_UNAVAILABLE = "sandbox_unavailable"
```

评审还指出注释会自相矛盾，一并同步：`security/process_sandbox.py:219,405`（"turn it into a
denial" / "catch sites … denial"）、`core/jobs.py:161,285`（"denied_reason"/"answers denied"）。

保持不动的部分：

- `ToolResult.success=False`、`data["reason"]=原因码`、`output="error: <原因>"`——
  模型读到的还是那个稳定原因码，fail-closed 行为不变（`enforce` 仍然拒绝执行）；
- `Job.denied_reason` 仍是原因码（它是"为什么没跑"的字段，不是"拒绝类别"）；
- `DispatchOutcome.denied` 不因此变真：它不是分发器的门禁拒绝，而是一次工具结果。

**FIX-A4-2：前端映射补一条。**

```ts
sandbox_unavailable: { label: "沙箱不可用（命令未执行）", tone: "failed" },
```

tone 用 `failed`（✗），不用 `denied`（⊘ + 删除线）：这不是一次人为拒绝，重试/换实现都比
"换个路径"更接近出路。真实的 `denied`（审批拒绝、门禁拦截）保持原样。

**FIX-A4-3：契约文档补一行。**

`docs/data-contracts.md` 的 `ToolResult.error` 表（R2 新增那节）加：

| `ToolResult.error` | `sandbox_unavailable` | 进程沙箱机制在本机不可用（原因码在 `data.reason`，如 `bwrap.unavailable`）；命令未执行，不是审批/门禁拒绝 |

## 3. 测试（先红后绿）

| 测试 | 断言 |
| --- | --- |
| `tests/test_process_sandbox.py::test_enforce_refuses_when_the_mechanism_is_missing` | `error == "sandbox_unavailable"`，`data["reason"] == "bwrap.unavailable"`，`success is False` |
| 同文件 `test_the_gap_reaches_the_model_as_denied`（改名 `..._as_sandbox_unavailable`） | 同上（第二条路径） |
| 新增：有 registry 的前台路径（`_run_as_job`） | `ToolContext(jobs=JobRegistry(同 policy/backend))` → `error == "sandbox_unavailable"` |
| 新增：`run_in_background=True` 路径（`_start_background`） | 同上（评审要求覆盖第三处） |
| 新增 `test_a4_a_refused_job_names_the_sandbox_not_a_denial` | `Job.detail` 以 `未执行 · ` 开头，`denied_reason == "bwrap.unavailable"`，状态 failed |
| `test/toolmeta.test.ts` | `toolFailure("sandbox_unavailable")` → `{label:"沙箱不可用（命令未执行）", tone:"failed"}`；`denied` 仍是被拒绝 |

## 4. 验收

1. `pytest tests/test_process_sandbox.py tests/test_jobs.py tests/test_tool_bash.py -q` 绿；
2. 前端 `bun test test/toolmeta.test.ts` + 全量绿、`tsc` 干净；
3. 离线复核：`bwrap` 在本沙箱仍不可用，故用单测 + 现有 `turn-2.jsonl` 说明"修的是错误码，
   不是沙箱本身"；真机 `enforce` 仍会拒绝，卡片会显示"沙箱不可用（命令未执行）"而不是"被拒绝"。
4. `ruff` / `mypy` 干净。

## 5. 完成记录（实现 + 代码评审 + 真机复验）

实现按 FIX-A4-1/2/3 全部落地，三处翻译点（`_start_background`、`_run_as_job`、直接 `execute`）
统一写 `SANDBOX_UNAVAILABLE`；`core/jobs.py` 的拒单 `detail` 改为 `未执行 · <原因码>`，
`denied_reason` 仍是原因码；`process_sandbox.py` 与 `jobs.py` 的注释同步。
测试：`tests/test_process_sandbox.py` 59 通过 / 2 跳过（含新增的三条路径：`_run_as_job`、
`run_in_background`、后台拒单），连同 `test_jobs.py`/`test_tool_bash.py`/`test_job_tools.py`
共 132 通过；前端 `toolmeta.test.ts` 28 通过；ruff/mypy 干净。

**真机复验的一个反转（记录在案）**：先按原设想把 `process_sandbox.mode` 设为 `enforce` 直接跑，
`ls` 居然**成功**了（卡片 `✓ 已运行命令 · ls · 16.9s（审批 16.8s）`）。直测得 `bwrap` 的
`full` 层确实失败（`Can't mount proc on /newroot/proc: Operation not permitted`），但
`no-proc` 层（`--tmpfs /proc` 代替 `--proc`+`--unshare-pid`）在本沙箱**是能跑的**，后端按层回退选中它，
所以"机制不可用"根本没有发生——这不是 A-4 的回归，是 R22 §3.1 那条"本机 bwrap 不可用"的记录
只对 `full` 层成立。**修正备忘录**：本机 `enforce` 可用（走 `no-proc`），要复现 gap 得让
`bwrap` 自身起不来。

复验方式：用 `PATH=/tmp/fakebin:$PATH` 放一个只回失败、退出码 1 的 `bwrap` 桩，其余 PATH 不变，
以 `-p /tmp/config.enforce.yaml` 重启 TUI，提交 `run ls in the workspace` 并批准。得到：

```
✗ 已运行命令 · ls · 24.5s（审批 24.5s）
沙箱不可用（命令未执行）
[medium] bash 调用失败（1 次）：直接原因是什么？
 · error: bwrap.unavailable
```

轨迹行（`r22-s1/.ai-coding/traces/7bd0766d…/turn-1.jsonl`）同值：
`{"success": false, "error": "sandbox_unavailable", "output_preview": "error: bwrap.unavailable",
"duration_ms": 24481, "approval_ms": 24458}`。即：模型仍拿到稳定原因码、UI 不再说"被拒绝"、
审批等待仍按 A-10 显示。帧：`/tmp/r22-shots/156-a4-sandbox-unavailable.txt`。

**顺带发现 A-17（P3，不在 A-4 内修）**：卡片标题的动词只看"是否结束"，不看结果，
于是失败卡片写成 `✗ 已运行命令 · ls`，下面一行却是"沙箱不可用（命令未执行）"——同一张卡自相矛盾。
这是 `toolTitle()` 的既有行为（`timeout`/`spawn_failed` 同样如此），改它要动卡片标题的通用语义，
单独记入矩阵。
