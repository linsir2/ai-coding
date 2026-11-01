# P0a 实施计划：把共享物迁出旧计划模块（TDD / RIPR）

> 目标：在删除旧计划执行器之前，把**被 codex 路径与主闭环消费**的共享物搬到独立模块，
> 行为逐字节不变、**不改任何既有测试**。P0a 结束时旧代码仍在，但已不拥有共享物。
>
> 依据：`docs/orchestration-unified-design.md` §1.1；核对命令：
> `grep -rn "outside_workspace_paths|outside_workspace_message|PlanReport" --include=*.py ai_coding tests`。

## 1. 影响分析

### 1.1 共享物与真实消费者（不是测试消费者）

| 符号 | 真实消费者（业务代码） | 测试消费者 |
|---|---|---|
| `PlanReport`（`plan_runner.py:533-579`，字段 + `render()`） | `core/plan_codex.py:14,90,110`（返回类型）；`app/commands.py:637`（读 `report.stopped`） | `tests/test_plan_codex_parity.py:22,228` |
| `outside_workspace_paths`（`plan_runner.py:234-312`） | `core/request_assembly.py:39,585`（首轮路径提示）；`core/acceptance.py:76,84`（验收命令的路径门） | `tests/test_plan_workspace_gate.py`、`test_plan_mode_turn.py:247` |
| `outside_workspace_message`（`plan_runner.py:375-381`） | `core/acceptance.py` 间接；`present_plan_tool.py:17`（P0b 删除） | 同上 |
| 私有依赖：`_OUTSIDE_PATH_RE`、`_FILE_SUFFIX_RE`、`_DIRECTORY_BEFORE_RE`、`_SHEBANG_MARKER`、`_DEVICE_PATHS`、`_is_device_path`、`_shebang_interpreters`、`_has_directory_context`、`_looks_like_path`（`plan_runner.py:156-232,315-372`） | 只被上面两个公开函数用 | 无直接引用 |

### 1.2 为什么必须迁出（而不是"随文件一起删"）

`plan_runner.py` 在 P0b 会被整文件删除。若不先迁出，codex 的 `PlanReport` 与
`request_assembly`/`acceptance` 的 workspace 门会断。P0a 只做"搬家 + 转发"，
不改变任何行为、不改任何测试。

### 1.3 不改的东西

- `PlanRunner`、`plan_only`、`TaskDriver` 调用、`PLAN_PROMPT` 等仍留在 `plan_runner.py`。
- `acceptance.py` 的逻辑不改，只改 import 来源。
- `request_assembly.py` 的逻辑不改，只改 import 来源。
- 所有既有测试文件不改（用转发导出保证）。

## 2. 文件级实现步骤

### 2.1 新建 `ai_coding/core/workspace_guard.py`

搬入（原样复制，注释一并搬，不重写文案）：

- 常量：`_OUTSIDE_PATH_RE`、`_FILE_SUFFIX_RE`、`_DIRECTORY_BEFORE_RE`、`_SHEBANG_MARKER`、
  `_DEVICE_PATHS`
- 私有函数：`_is_device_path`、`_shebang_interpreters`、`_has_directory_context`、
  `_looks_like_path`
- 公开函数：`outside_workspace_paths`、`outside_workspace_message`
- import：`re`、`Iterable/Sequence`、`Path`、`FILE_PATH_EXTENSIONS`（`core/file_mentions.py`）、
  `Sandbox/SandboxViolation`（`security/sandbox.py`）、`path_candidates as _shell_path_candidates`
  （`ai_coding/shell_parse.py`）
- `__all__ = ["outside_workspace_message", "outside_workspace_paths"]`

模块 docstring：说明它是"计划/验收/首轮提示共用的工作区可达性门"，不是计划专属。

### 2.2 新建 `ai_coding/core/plan_report.py`

搬入 `PlanReport`（`plan_runner.py:532-579`）原样，含 `render()` 与全部注释；
import `dataclass/field`、`StepDelivery`（`domain/plan_delivery.py`）。
`__all__ = ["PlanReport"]`。

### 2.3 修改 `ai_coding/core/plan_runner.py`（只删定义 + 转发）

- 删除 2.1/2.2 搬走的定义与私有常量。
- 顶部改为：
  ```python
  from ai_coding.core.plan_report import PlanReport
  from ai_coding.core.workspace_guard import (
      outside_workspace_message,
      outside_workspace_paths,
  )
  ```
- `__all__` 保持 `PlanReport` 等名字（`:1299`），因为 `plan_only`/`PlanRunner` 内部仍用。
- 不再使用的 import 删掉（`re`、`FILE_PATH_EXTENSIONS`、`Sandbox/SandboxViolation`、
  `_shell_path_candidates`、`Path` 若只被搬走代码使用——以 `ruff` 的 F401/F811 为准）。
- **保留** `_persist_session`、`_emit_task`、`_tail`（旧路径 P0b 才删）。

### 2.4 修改 `ai_coding/core/request_assembly.py`

`from ai_coding.core.plan_runner import outside_workspace_paths`
→ `from ai_coding.core.workspace_guard import outside_workspace_paths`。
注释里"复用 plan_runner.outside_workspace_paths"同步改为新模块名（仅注释）。

### 2.5 修改 `ai_coding/core/acceptance.py`

函数内延迟 import `from ai_coding.core.plan_runner import outside_workspace_paths`
→ `from ai_coding.core.workspace_guard import outside_workspace_paths`。

### 2.6 新建 `tests/test_workspace_guard.py`（TDD 红先）

只测**模块契约与迁移等价**，不改既有测试：

| # | 用例（Red 先写） | 断言 |
|---|---|---|
| R1 | `test_guard_module_exports_the_two_public_functions` | `__all__` 恰为两个名字；可 import |
| R2 | `test_prose_route_is_not_a_path_but_etc_passwd_is` | `["GET /api/v1/users"]` → `[]`；`["/etc/passwd"]` → `["/etc/passwd"]` |
| R3 | `test_shell_candidates_are_read_by_the_grammar` | `from_shell=True` 时 `D=/data/x; rm -rf $D` 报 `/data/x` |
| R4 | `test_extra_roots_are_reachable` | 声明 `extra_roots` 后不报 |
| R5 | `test_device_and_shebang_are_exempt` | `2>/dev/null`、`#!/usr/bin/env python3` 不报 |
| R6 | `test_plan_report_is_the_same_type_from_both_modules` | `plan_report.PlanReport is plan_runner.PlanReport`（转发等价） |

## 3. 数据流

改前：
```
request_assembly ─┐
acceptance ───────┼→ plan_runner.outside_workspace_paths
present_plan ─────┘        （与 PlanRunner/plan_only 同模块）
plan_codex → plan_runner.PlanReport
```
改后：
```
request_assembly ─┐
acceptance ───────┼→ core.workspace_guard.outside_workspace_paths
present_plan ─────┘
plan_codex ────────→ core.plan_report.PlanReport
plan_runner ───────→ re-export 两者（P0b 删除）
```
调用关系、参数、返回值、异常均不变；只是 import 路径变化。

## 4. 成功后的样子

- `core/workspace_guard.py` 与 `core/plan_report.py` 存在，`plan_runner.py` 只做转发。
- `grep "from ai_coding.core.plan_runner import"` 只剩旧路径自身（P0b 处理）。
- 全量 `pytest` 与改前一致（0 failed），`ruff`/`mypy` 干净。
- 没有新增重复实现：`outside_workspace_*` 只有一份，`PlanReport` 只有一份。

## 5. 自核（RIPR 缺一不可）

- **Red**：先写 `tests/test_workspace_guard.py`，在旧结构下 R1/R6 失败（模块不存在）。
- **Implement**：2.1–2.5。
- **Prove**：新测试 + 既有 `test_plan_workspace_gate.py`、`test_plan_codex_parity.py`、
  `test_acceptance.py`、`test_plan_mode_turn.py` 全绿；全量 `pytest`、`ruff`、`mypy`。
- **Refactor**：`plan_runner.py` 的转发 import 加一行注释"P0b 删除本文件时一并删除"。

## 6. 风险

| 风险 | 处置 |
|---|---|
| 搬迁时漏掉私有依赖（正则/常量） | 以 `ruff` F821 + 既有测试为门；R2–R5 覆盖四条分支 |
| `plan_runner.__all__` 被 `from ... import *` 使用 | 保留 `__all__`，转发导出 |
| `Path`/`re` 变成未使用 import | 由 `ruff` 报错后删除 |
| 循环导入 | 新模块只依赖 `security.sandbox`、`shell_parse`、`file_mentions`，不依赖 core 其他模块 |

## 7. 不在 P0a 范围

删除 `plan_runner`/`plan_executor`/`present_plan`、旧字段、旧事件、前端评审卡、
`plan_orchestrator` 配置——全部属于 P0b，待 P0a 全绿后单独出计划。
