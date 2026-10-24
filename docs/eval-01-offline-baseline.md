# eval-01 · 离线质量基线

> 全部离线（无网络、无模型）。命令在 `/workspace/ai-coding` 下用 `.venv/bin` 执行。

## 1. pytest

```
$ .venv/bin/python -m pytest -q --tb=no -p no:warnings
```

| 指标 | 值 |
|---|---|
| 收集用例 | 约 3799 |
| 通过 | 3789 |
| 失败 | **10** |

### 10 条失败分类

| # | 用例 | 现象 | 归因 |
|---|---|---|---|
| 1 | `tests/integration/test_r17_defects.py::test_undo_names_the_files_it_could_not_restore` | undo 报告 `ok:True` 却隐藏了部分回滚（`files:['ro/stuck.txt']` 未在 reason 中点名） | **疑似真实缺陷**（只读目录下的部分回滚未上报） |
| 2 | `tests/test_grant_store.py::test_an_unwritable_store_does_not_raise` | 期望「不可写 store 不抛错」，实际文件被创建 | **环境伪失败**：以 `root` 运行，`chmod` 挡不住 root |
| 3-8 | `tests/test_mcp_oauth.py`（6 条） | 远程 streamable-HTTP MCP 未连上（`status:'failed'`） | **环境/网络依赖**：需外网 OAuth 端点 |
| 9 | `tests/test_snapshots.py::test_a_file_that_cannot_be_deleted_is_reported_not_raised` | 不可删除文件未报 `failed` | **环境伪失败**：root 可删任何文件 |
| 10 | （同上 9） | | |

> **判读**：10 条里 **2 条是 root 权限导致的伪失败**（#2、#9），**6 条是 MCP OAuth 外网依赖**（#3-8），
> **1 条疑似真实缺陷**（#1，undo 部分回滚上报）。**核心离线逻辑没有大面积失败**。

## 2. ruff / mypy

```
$ .venv/bin/ruff check . --output-format=concise
scripts/plan_lifecycle_live_check.py:63:1: I001 [*] Import block is un-sorted or un-formatted
scripts/w2_live_check.py:21:8:    F401 [*] `shutil` imported but unused
scripts/w2_live_check.py:31:1:    I001 [*] Import block is un-sorted or un-formatted
scripts/w2_live_check.py:147:5:   F841 Local variable `wire` is assigned to but never used
Found 4 errors. (3 fixable)
```

```
$ .venv/bin/mypy ai_coding
ai_coding/core/events.py:581:    error: Redundant cast to "_T"            [redundant-cast]
ai_coding/infra/deadline.py:171: error: Argument 2 to <set> has incompatible type  [arg-type]
ai_coding/core/mcp.py:637:       error: Module "mcp.client.auth" has no attribute "AuthorizationCodeResult"
ai_coding/core/mcp.py:668:       error: Incompatible return value type      [return-value]
ai_coding/core/mcp.py:787:       error: Cannot find implementation or library stub for module named "httpx2"  [import-not-found]
ai_coding/core/mcp.py:789:       error: Too many values to unpack (2 expected, 3 provided)  [misc]
Found 6 errors in 3 files (checked 152 source files)
```

### ⭐ 真实缺陷：`ai_coding/core/mcp.py:787` 的 `import httpx2`

```python
# ai_coding/core/mcp.py:787（远程 streamable-HTTP MCP + OAuth 分支）
import httpx2          # ← 模块不存在，正确名是 httpx
...
http_client=httpx2.AsyncClient(auth=provider, headers=headers or None),
```

- **影响**：任何走「远程 HTTP MCP 服务器 + OAuth」的路径都会在运行时 `ModuleNotFoundError`。
- **证据**：mypy `import-not-found`；PyPI 无 `httpx2` 这个包（`httpx` 才是）。
- **归属**：**项目原生缺陷**（非本次评估引入）。已纳入最小修复清单。

其余 5 条 mypy 为类型层面的小问题（冗余 cast、`mcp` 库版本漂移导致的 attr/return 不匹配）。

## 3. 离线验收脚本

| 脚本 | 结果 |
|---|---|
| `scripts/r18_acceptance.py` | **36 passed, 0 failed** |
| `scripts/trace_cases.py` | **7/7 passed** |
| `scripts/r10_cases.py` | **22/22 passed**（可达 R@3=0.99 / FP@5=0.459 / paraphrase@3=0/46） |
| `scripts/r9_cases.py` | **12/12 passed**（HTML 转义、OTLP 确定性、prune 一致性） |
| `scripts/r8_cases.py` | **13/13 passed**（取消杀进程组、结构化取消、chunk 不外泄） |
| `scripts/r6_cases.py` | **all offline passed**（stall 阶梯、ask_user、证据索引） |
| `scripts/r7_cases.py` | **9/11 passed**（另 2 条为 `--real` 报告项，不计分） |
| `scripts/retrieval_cases.py` | **guardrails OK**（可达 R@3=0.99、FP@5=0.46、paraphrase@3=0/46、no-answer 4/10） |
| `scripts/memory_cases.py` | **9/11 passed** |

### 两条 memory_cases 失败（实测偏差）

| 用例 | 期望 | 实测 | 判读 |
|---|---|---|---|
| `case7-delta-only` | 每轮收割批次 ≤3（用户+回答，不含历史） | `batch_sizes=[3, 4]` | **实测偏差**：有一轮把 4 条消息喂给了提取器，说明「只吃回合增量」在某条路径上多带了一条 |
| `case11-cache-prefix` | append-only 前缀命中 ≥90%（文档记 92%） | `prefix_hit append-only=87%` / `per-turn-trim=15%` | **实测偏差**：低于文档自述的 92%，但仍远高于每轮压缩的 15% |

> 这两条是**文档自述与实测不一致**的案例，值得在报告中单列。

## 4. 结论

- 离线基线**整体健康**：约 3799 用例中 10 条失败，其中 8 条可归因环境（root 权限 / 外网 MCP OAuth）。
- 发现 **1 个真实运行时缺陷**（`httpx2`）+ **2 处文档与实测不一致**（memory case7/case11）。
- 下一步：阶段 2 功能矩阵 → 阶段 3 真机逐项实测。
