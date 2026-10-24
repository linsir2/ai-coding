# M-A3 实施方案 · undo 的 `ok` 字段门控（含 redo 的 reload 判据）

> 状态：**评审已回，方案已修订**（评审发现 redo 有同类缺陷，已纳入范围）
> 关联：[roadmap-milestones.md](roadmap-milestones.md) §1 M-A3
> 依赖：M-A4（已完成 —— 注入式 `refused` fixture 让部分回滚可复现）
> 评审：子智能体实跑评审（见 §十四），S1~S4 已全部落实

---

## 一、要实现的内容（一句话）

**部分回滚不得被报成"成功"，也不得让前端不重投影** ——
`undo` 的 `ok` 恒为 `True`；`undo`/`redo` 的 `RELOAD` 判据都用了错的信号，
导致"历史已截断但前端不知情"。

---

## 二、调研结论：两处断链，同一形状

### 2.1 undo：`ok` 硬编码

[agent_loop.py:2488-2495](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L2488-L2495)：

```python
report = {
    "ok": True,            # ★ 恒为 True，从未引用 failed / reason
    "text": text,
    "files": restored,
    "reason": reason,
    "messages_before": ...,
    "messages_after": ...,
}
```

而**同一个函数里** `failed` 早已正确填充并用于生成 `reason`：

```python
2428    restored, failed = report.restored, report.failed
2439    if failed:
2445        reason = "restore-partial: " + ...      # 只写进 reason
```

→ 断链点唯一：`ok` 与 `failed` 解耦。

### 2.2 redo：`ok` 是对的，但 **reload 判据** 是错的（评审 S1）

redo 的 `ok` 确实用了 `not reason`（[agent_loop.py:2567](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L2567)），
**但历史是无条件追加的**（[agent_loop.py:2551](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L2551)）：

```python
2551    session.messages = list(session.messages) + tail     # ← 无条件执行
2567    return {"ok": not reason, ...}
```

而 [commands.py:195-199](file:///workspace/ai-coding/ai_coding/app/commands.py#L195-L199) 用 `ok` 决定 `RELOAD`：

```python
if report.get("ok"):
    return REPLAction.RELOAD, session
console.info("[redo] " + _reason_text(report.get("reason", "")))
return REPLAction.CONTINUE, session      # ← 部分 redo / no-snapshot / 异常 都落这里
```

→ **部分 redo、`no-snapshot`、`restore-failed` 三种情况都是"历史已追加但不重投影"**，
即 transcript 与 tree 不一致 —— 与 undo 完全同形。

**评审实测**（注入 `write_bytes` 拒绝）：
```
redo partial -> action: REPLAction.CONTINUE | lines: [('[redo] restore-partial: ro/stuck.txt',)]
messages after redo: 4        ← 历史已追加，前端不重投影
```

> ⚠️ 修正我初稿的错误论断：我原写"redo 侧早就是对的" —— 那只对 `ok` 字段成立，
> 对 **reload 决策不成立**。§三 的计数也随之修正。

### 2.3 契约层是对的，无需改

[snapshots.py:33-50](file:///workspace/ai-coding/ai_coding/core/snapshots.py#L33-L50) 的 `RestoreReport(restored, failed, saved)`：

- `restore()` 在 [snapshots.py:165](file:///workspace/ai-coding/ai_coding/core/snapshots.py#L165) 返回它
- `_remove` 失败只返回 `False`，**不抛**（[snapshots.py:194-209](file:///workspace/ai-coding/ai_coding/core/snapshots.py#L194-L209)）
- 契约 docstring 明写"把 git 拒绝 checkout 的路径报成 restored，就是 undo 谎称回滚的根源"

→ **契约层已修对，M-A3 纯粹是消费端的问题。**

### 2.4 残留缺陷（同一函数）

[agent_loop.py:2450-2452](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L2450-L2452)：

```python
except (OSError, TimeoutError, ValueError) as exc:
    restored = []
    reason = f"restore-failed: {exc}"    # ← 只写 reason，不写 failed
```

→ 即使 `ok = not failed`，**`restore` 抛异常时 `ok` 仍是 `True`**。本方案一并堵上。

---

## 三、关键判断：**不能用 `not reason`**

`reason` 的取值域比想象中宽，且含**良性**分支：

| `reason` | 含义 | 历史是否被改写 | 该报 ok 吗 |
|---|---|---|---|
| `""` | 全部成功 | ✅ 是 | ✅ 是 |
| `no-snapshot` | **良性**：不是 git 仓库 / 捕获失败 / ref 不存在 | ✅ 是（历史仍截断/追加） | ✅ **是** |
| `restore-partial: …` | 部分文件没回来 | ✅ 是 | ❌ **否** |
| `restore-failed: …` | restore 抛异常 | ✅ 是 | ❌ **否** |

若用 `not reason`，`no-snapshot` 会被误判为失败 → **3 条本应成功的测试变红**：

| # | 位置 | 场景 |
|---|---|---|
| 3 | [test_r17_defects.py:952](file:///workspace/ai-coding/tests/integration/test_r17_defects.py#L952) | 非 git 仓库 + 有 SnapshotStore |
| 5 | [test_crash_recovery.py:300](file:///workspace/ai-coding/tests/test_crash_recovery.py#L300) | loop 未传 snapshots |
| 6 | [test_undo_command.py:111](file:///workspace/ai-coding/tests/test_undo_command.py#L111) | 非 git 目录 |

> 评审 S2 实测：这三条的 `reason` 都是 `"no-snapshot"`，`not reason` 全为 `False`。
> 初稿写"2 条"是错的，已改为 3 条。

→ **`ok` 的正确判据是"文件半边是否完整成功"，不是"reason 是否为空"。**

---

## 四、数据流影响

### 4.1 四段闭环

```
① 文件半边   SnapshotStore.restore() → RestoreReport(restored, failed, saved)
             失败进 failed，不抛
                    ↓
② 会话半边   无条件执行：undo 截断 messages / redo 追加 messages /
             turn_count 与 digest 回退、stage 落盘
             —— 只要越过早退，历史一定被改写
                    ↓
③ 报告组装   undo: {ok, text, files, reason, messages_before, messages_after}
             redo: {ok, reason, files, messages}
             早退: {ok: False, reason}            ← 只有这两个键
                    ↓
④ 消费方     commands.py → REPLAction.RELOAD | CONTINUE
             → handlers.py → turn.finished{reload, undo}
             → frontend/src/routes/session.tsx：只有 reload===true 才重投影
```

### 4.2 唯一的正确判据：**历史是否被改写**

`RELOAD` 与 `CONTINUE` 的分界**不是** `ok`，而是"transcript 是否变了"：

- 真拒绝（回合进行中 / 无可撤销回合）：**历史未动** → `CONTINUE`
- 部分还原 / 全部还原 / `no-snapshot` / `restore-failed`：**历史已动** → `RELOAD`

早退路径实测只返回 `{ok, reason}`（[agent_loop.py:2391](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L2391)、
[2402](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L2402)、[2508](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L2508)）：

```python
return {"ok": False, "reason": "a turn is running"}
return {"ok": False, "reason": "nothing to undo"}
return {"ok": False, "reason": "nothing to redo"}
```

→ **"报告是否携带历史记账字段"即可区分**，无需给报告加新字段（不扩大契约）。

---

## 五、执行范围（逐文件、逐步）

### 步骤 1 · `agent_loop.py` —— undo 引入 `restore_ok` 布尔

**1a** 在 `restored/failed` 初始化处加布尔（[agent_loop.py:2414-2415](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L2414-L2415) 附近）：

```python
 reason = "no-snapshot" if self.snapshots is None else ""
 restored: list[str] = []
 failed: list[str] = []
+restore_ok = True
```

**1b** `if failed:` 分支置 False（[agent_loop.py:2439-2446](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L2439-L2446)）：

```python
 if failed:
+    restore_ok = False
     reason = "restore-partial: " + ...
```

**1c** `except` 分支置 False（[agent_loop.py:2450-2452](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L2450-L2452)）：

```python
 except (OSError, TimeoutError, ValueError) as exc:
+    restore_ok = False
     restored = []
     reason = f"restore-failed: {exc}"
```

**1d** 报告改用布尔（[agent_loop.py:2489](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L2489)）：

```python
-    "ok": True,
+    "ok": restore_ok,
```

**为什么用布尔而不是 `not failed`**：`failed` 是"路径列表"，而 `restore` 抛异常时
我们**不知道哪些路径失败** —— 往里塞假路径（如 `"<restore raised>"`）是撒谎。
布尔表达的是"文件半边是否完整成功"，语义准确，同时覆盖 partial 与 raised。

**为什么不是 `not reason`**：见 §三，`no-snapshot` 是良性。

### 步骤 2 · `commands.py` —— 两个分支都改用"历史是否被改写"

**2a** 加一个判据 helper（放 `_undo_outcome` 旁边，[commands.py:75-80](file:///workspace/ai-coding/ai_coding/app/commands.py#L75-L80)）：

```python
def _rewrote_history(report: dict[str, Any]) -> bool:
    """True when the report carries history accounting -- i.e. the transcript
    was rewritten and the caller must reload.  A refusal carries only
    ``{ok, reason}`` and must leave the view alone (r20/F1)."""
    return "messages_before" in report or "messages" in report
```

> 一个 helper 同时服务 undo 与 redo —— 两者字段名不同（`messages_before` vs `messages`），
> 但语义相同。**不造轮子**：这是 2 行谓词，替代两处重复且易漂移的条件。

**2b** undo 分支（[commands.py:180-188](file:///workspace/ai-coding/ai_coding/app/commands.py#L180-L188)）：

```python
 report = await undo(session)
-if not report.get("ok"):
+if not _rewrote_history(report):
     console.info("[undo] " + _reason_text(report.get("reason", "")))
     return REPLAction.CONTINUE, session
 console.info(
     f"[undo] {report['messages_before']} → {report['messages_after']} 条，"
     + _undo_outcome(report)
 )
 return REPLAction.RELOAD, session
```

**2c** redo 分支（[commands.py:194-199](file:///workspace/ai-coding/ai_coding/app/commands.py#L194-L199)）：

```python
 report = await redo(session)
-if report.get("ok"):
+if _rewrote_history(report):
     console.info(f"[redo] 已恢复上一回合，还原 {len(report.get('files') or [])} 个文件")
     return REPLAction.RELOAD, session
 console.info("[redo] " + _reason_text(report.get("reason", "")))
 return REPLAction.CONTINUE, session
```

> ⚠️ redo 的文案会变化：部分 redo 现在打印"已恢复上一回合，还原 N 个文件"，
> 但实际有文件没回来。**文案必须体现部分失败** —— 见 2d。

**2d** redo 文案对齐（[commands.py:196](file:///workspace/ai-coding/ai_coding/app/commands.py#L196)）：

```python
 if _rewrote_history(report):
-    console.info(f"[redo] 已恢复上一回合，还原 {len(report.get('files') or [])} 个文件")
+    console.info(
+        f"[redo] 已恢复上一回合，还原 {len(report.get('files') or [])} 个文件"
+        + (f"（{_reason_text(report['reason'])}）" if report.get("reason") else "")
+    )
     return REPLAction.RELOAD, session
```

> 这样部分 redo 会显示 `[redo] 已恢复上一回合，还原 0 个文件（restore-partial: ro/stuck.txt）`，
> 既不谎报，也保留了路径。

### 步骤 3 · `tests/integration/test_r17_defects.py` —— 唯一需要改的断言

[test_r17_defects.py:1005](file:///workspace/ai-coding/tests/integration/test_r17_defects.py#L1005)：

```python
-assert report["ok"] is True, "the conversation half still unrolls"
+assert report["ok"] is False, "a partial rewind must not be reported as success"
```

改后该用例同时锁住两件事：
- `:1002` `reason` 点名未恢复文件
- `:1005` `ok` 不再谎报成功

### 步骤 4 · 新增测试（覆盖判据变更，当前无任何覆盖）

**4a** undo 部分回滚仍 `RELOAD`（`tests/test_undo_command.py`）：

```python
def test_a_partial_restore_still_reloads_the_transcript(loop, session, console):
    """历史已被截断，就必须重投影 —— 否则 transcript 与 tree 不一致。"""
    # 用 M-A4 的 refused fixture 制造 failed 非空，或直接 stub undo 返回部分报告
    # 断言：REPLAction.RELOAD，且 console 输出含 reason
```

**4b** redo 部分回滚仍 `RELOAD`（同一文件）：

```python
def test_a_partial_redo_still_reloads_the_transcript(loop, session, console):
    """redo 的历史是无条件追加的；不重投影就是同样的谎。"""
```

**4c** 早退仍 `CONTINUE`（`tests/test_undo_command.py`）：
锁住"拒绝路径不带历史记账字段"这个前提 —— 它是 2a 判据的基石。

### 步骤 5 · 文档同步

| 文件 | 动作 |
|---|---|
| [roadmap-milestones.md:57-59](file:///workspace/ai-coding/docs/roadmap-milestones.md#L57-L59) | 症状描述**已过期**（记的是 `files: ['ro/stuck.txt'], reason: ''`）；改为实测值并标 ✅ |
| [data-contracts.md:875](file:///workspace/ai-coding/docs/data-contracts.md#L875) | 补：`restore-partial`/`restore-failed` ⇒ `ok=False`；`no-snapshot` ⇒ `ok=True`；**`RELOAD` 判据 = 报告是否带历史记账字段** |

### 步骤 6 · 不做什么

- ❌ 不改 `snapshots.py`（契约已正确）
- ❌ 不改 `_REASON_TEXT`（见 §七 的取舍）
- ❌ 不改 `handlers.py` / `session.tsx`（`reload` 语义不变）
- ❌ 不动其余 5 处 `assert report["ok"] is True`（见 §六）
- ❌ 不给报告加新字段（不扩大 `undo`/`redo` 契约）

---

## 六、6 处 `assert report["ok"] is True` 逐处核验

| # | 位置 | 场景 | `failed` | 新判据下 | 处置 |
|---|---|---|---|---|---|
| 1 | [test_r17_dataflow.py:195](file:///workspace/ai-coding/tests/integration/test_r17_dataflow.py#L195) | 真 restore，全成功 | `[]` | `ok=True` | **不改** |
| 2 | [test_r17_dataflow.py:443](file:///workspace/ai-coding/tests/integration/test_r17_dataflow.py#L443) | 真 restore，全成功 | `[]` | `ok=True` | **不改** |
| 3 | [test_r17_defects.py:952](file:///workspace/ai-coding/tests/integration/test_r17_defects.py#L952) | 非 git，`no-snapshot` | `[]` | `ok=True`（良性） | **不改** |
| 4 | [test_r17_defects.py:1005](file:///workspace/ai-coding/tests/integration/test_r17_defects.py#L1005) | **部分回滚** | `["ro/stuck.txt"]` | `ok=False` | **必改** |
| 5 | [test_crash_recovery.py:300](file:///workspace/ai-coding/tests/test_crash_recovery.py#L300) | 无 snapshots，`no-snapshot` | `[]` | `ok=True` | **不改** |
| 6 | [test_undo_command.py:111](file:///workspace/ai-coding/tests/test_undo_command.py#L111) | 非 git 目录 | `[]` | `ok=True` | **不改** |

→ **只有 1 条需要改**。这正是 §三"不能用 `not reason`"的直接验证：
若用 `not reason`，#3/#5/#6 会一起变红。

### 6.1 现成的"早退形状"安全网（评审 S5）

两条**全字典相等**断言正好锁住 2a 判据的前提，无需新增即可复用：

| 位置 | 断言 |
|---|---|
| [test_r17_defects.py:1020](file:///workspace/ai-coding/tests/integration/test_r17_defects.py#L1020) | `assert report == {"ok": False, "reason": "a turn is running"}` |
| [test_undo_command.py:94](file:///workspace/ai-coding/tests/test_undo_command.py#L94) | `assert report == {"ok": False, "reason": "nothing to undo"}` |

→ 只要这两条保持绿，`_rewrote_history` 的"早退不带历史字段"前提就没被破坏。

---

## 七、取舍说明：为什么不动 `_REASON_TEXT`

[commands.py:69-72](file:///workspace/ai-coding/ai_coding/app/commands.py#L69-L72)：

```python
def _reason_text(reason: str) -> str:
    base = reason.split(":", 1)[0]
    return _REASON_TEXT.get(base, reason or "failed")
```

`restore-partial` 不在 `_REASON_TEXT` 里 → 兜底返回**原串** `restore-partial: ro/stuck.txt`。

**如果加一条中文映射**（`"restore-partial": "部分文件未能还原"`），
`_reason_text` 会**只返回映射文本、丢掉 `: ro/stuck.txt`** —— 而路径才是用户可操作的信息。

→ **保持兜底**：宁可中英混杂，也不丢文件名。这是刻意取舍，不是遗漏。

---

## 八、TDD / RIPR

### 8.1 红相（改前）

现有用例在改前是**绿**的（它断言的是 bug 行为），所以红相靠**先落断言**：

```
$ # 先做步骤 3（断言 is True → is False）
$ pytest tests/integration/test_r17_defects.py::test_undo_names_the_files_it_could_not_restore -q
FAILED  AssertionError: a partial rewind must not be reported as success
        assert True is False
```

→ **红**，且红在正确位置（`ok` 仍硬编码 True）。

### 8.2 RIPR

| 相 | 动作 | 判据（可机检） |
|---|---|---|
| **R** | 只做步骤 3 → 跑 | `test_undo_names_the_files_it_could_not_restore` **1 failed** |
| **I** | 步骤 1（`restore_ok`）→ 跑 | 上述用例 **变绿** |
| **I** | 步骤 2（`_rewrote_history`）+ 步骤 4a/4b → 跑 | 新增 2 条 **passed** |
| **P** | 全量 `pytest` | **0 failed**，skipped 数不增（=6） |
| **P** | `ruff check .` | **6 errors（不增）** |
| **P** | `mypy ai_coding` | **3 errors（不增）** |
| **P** | **突变**：`"ok": restore_ok` 改回 `True` | `test_undo_names_the_files_it_could_not_restore` **变红** |
| **P** | **良性回归**：`no-snapshot` 三条仍绿 | `test_r17_defects.py:952`、`test_crash_recovery.py:300`、`test_undo_command.py:111` **3 passed** |
| **P** | **早退形状**：全字典相等仍成立 | `test_r17_defects.py:1020`、`test_undo_command.py:94` **2 passed** |
| **R** | `git status --porcelain` 白名单 | 见 §8.3 |

### 8.3 R 相白名单（评审 S4：口径统一）

```
M  ai_coding/core/agent_loop.py
M  ai_coding/app/commands.py
M  tests/integration/test_r17_defects.py
M  tests/test_undo_command.py
M  docs/roadmap-milestones.md
M  docs/data-contracts.md
?? docs/plan-m-a3.md
```

即 **4 个代码文件 + 2 份文档 + 1 份方案**。不含任何其他路径。

---

## 九、架构图

```
                    改前                                  改后
  ┌──────────────────────────────┐        ┌──────────────────────────────┐
  │ undo_last_turn               │        │ undo_last_turn               │
  │ failed = ['ro/stuck.txt']    │        │ restore_ok = False           │
  │ reason = 'restore-partial:…' │        │ reason = 'restore-partial:…' │
  │ "ok": True   ← 断链          │        │ "ok": restore_ok  ← 接上     │
  └──────────────┬───────────────┘        └──────────────┬───────────────┘
                 ↓                                        ↓
  ┌──────────────────────────────┐        ┌──────────────────────────────┐
  │ commands: if not ok          │        │ commands:                    │
  │   → CONTINUE（不重投影）     │        │   if not _rewrote_history:   │
  │   ← 历史已截断但前端不知情    │        │       → CONTINUE（真拒绝）    │
  │                              │        │   else: → RELOAD（部分还原也）│
  └──────────────────────────────┘        └──────────────┬───────────────┘
                                                          ↓
  ┌──────────────────────────────┐        ┌──────────────────────────────┐
  │ redo_last_turn               │        │ redo_last_turn（不变）        │
  │ messages = messages + tail   │        │ messages = messages + tail   │
  │ "ok": not reason  ← 对       │        │ "ok": not reason             │
  └──────────────┬───────────────┘        └──────────────┬───────────────┘
                 ↓                                        ↓
  ┌──────────────────────────────┐        ┌──────────────────────────────┐
  │ commands: if ok → RELOAD     │        │ commands:                    │
  │ 部分 redo → CONTINUE         │        │   if _rewrote_history:       │
  │   ← 同一谎                    │        │       → RELOAD（部分也重投影）│
  └──────────────────────────────┘        └──────────────────────────────┘
```

---

## 十、修改成功后的效果

| 观测点 | 改前 | 改后 |
|---|---|---|
| 部分 undo 的 `ok` | `True`（谎报） | **`False`** |
| 部分 undo 的 TUI | `[undo] 4 → 0 条，restore-partial: ro/stuck.txt` + `RELOAD` | 同文案 + **仍 `RELOAD`**（判据换源，行为不变） |
| 部分 redo 的 TUI | `[redo] restore-partial: …` + **`CONTINUE`（不重投影）** | `[redo] 已恢复上一回合，还原 0 个文件（restore-partial: …）` + **`RELOAD`** |
| redo 的 `no-snapshot` | `CONTINUE`（历史已追加却不重投影） | **`RELOAD`** |
| `no-snapshot` 的 `ok` | `True` | **`True`**（良性，不回归） |
| `restore` 抛异常时 `ok` | `True`（残留缺陷） | **`False`** |
| 真拒绝的 TUI | `CONTINUE` | `CONTINUE`（不变） |
| 前端 transcript/tree 一致性 | 部分 undo/redo 时不一致 | **一致** |

> 修正初稿的 S8：部分 undo 的 TUI **改前就已打印 reason**（`_undo_outcome` 会调
> `_reason_text`），并非"打印成功"。真正的缺陷是 **reload 判据**，不是文案。

---

## 十一、自我核验

1. **推演**：改完步骤 1-4，全量 `pytest` 应 **0 failed**。
   依据：唯一会变的既有断言是 #4；其余 5 处 `failed` 均为空 → `restore_ok` 保持 True。
2. **推演**：#3/#5/#6（`no-snapshot`）必须仍绿。
   依据：`restore_ok` 只在 `if failed:` 与 `except` 两处置 False；`no-snapshot` 不经过。
3. **反向检查**：`restore_ok` 会不会漏掉某条失败路径？
   `restore()` 只有三种结局（全成功 / 部分 / 抛异常），三种都被覆盖。
   `_remove` 不抛（只返回 False），不会绕过 `failed`。
4. **反向检查**：`_rewrote_history` 会不会误判？
   早退路径实测只返回 `{ok, reason}`。若将来有人给早退加 `messages_before`，
   会误判为"历史已改写" → `RELOAD` 一个没变的会话。**这是脆弱的耦合**，
   故 §6.1 用两条全字典相等断言 + §4c 新增用例锁住。
5. **反向检查**：只改 `ok` 不改 `commands.py` 会怎样？
   部分 undo 进"拒绝"分支 → `CONTINUE` → 前端不重投影，但历史已截断 →
   **transcript 与 tree 不一致**。这正是必须同改 commands 的原因。
6. **反向检查**：只改 undo 不改 redo 会怎样？
   两个分支判据不一致（一个看 `messages_before`、一个看 `ok`），
   且 redo 的同类谎继续存在 → 评审 S1 实测的 `CONTINUE + 历史已追加` 会保留。

---

## 十二、边界声明

| 项 | 归属 |
|---|---|
| **本方案改** | `agent_loop.py`（4 处）、`commands.py`（helper + 2 分支 + redo 文案）、`test_r17_defects.py:1005`、`test_undo_command.py`（新增 3 条）、2 份文档 |
| **不含** | `snapshots.py`（契约正确）、`handlers.py`、`frontend/src/routes/session.tsx`、`_REASON_TEXT` |
| **不含** | 其余 5 处 `assert report["ok"] is True` |
| **范围扩张说明** | redo 的 reload 判据（评审 S1）**纳入本方案** —— 否则 undo/redo 判据不一致，且同类谎继续存在 |
| **不在本方案** | roadmap §十三 N-1（`gitcmd` stderr 合并）、N-2（flaky 用例） |

---

## 十三、待评审的关键问题

1. `restore_ok` 布尔 vs `not failed` —— 本方案选前者（覆盖异常路径）。
2. `_rewrote_history` 用"字段存在性"判据 —— 无需扩契约，但耦合较隐晦；
   备选是加显式 `"history": True` 字段（更显式，但要同步 handlers + 前端类型）。
3. 是否补 `_REASON_TEXT` 的 `restore-partial` 映射 —— 本方案**不补**（会丢路径）。
4. redo 的 reload 判据是否应拆成独立里程碑 —— 本方案**并入**（同文件、同形状、1 行）。

---

## 十四、评审记录

子智能体实跑评审结论（含注入 `write_bytes` 拒绝复现 redo 部分失败）：

| # | 严重度 | 问题 | 处理 |
|---|---|---|---|
| **S1** | 🔴 | **漏了 redo**：`ok=not reason` 对，但 `commands.py` 用 `ok` 决定 RELOAD → 部分 redo / `no-snapshot` / `restore-failed` 都是"历史已追加但不重投影"。实测 `CONTINUE` + `messages=4` | ✅ 步骤 2c/2d 纳入；§2.2 修正原错误论断 |
| **S2** | 🟠 | §三 计数错：`not reason` 会打红 **3** 条（#3/#5/#6），非 2 条 | ✅ 已改为 3 条并列表 |
| **S3** | 🟠 | RIPR "跑 TUI 相关测试"不可机检（且该改动无现成覆盖） | ✅ §8.2 枚举用例 ID + §4a/4b/4c 新增用例 |
| **S4** | 🟠 | §8.2 白名单"4 个文件"与 §十二"6 个文件"矛盾 | ✅ §8.3 统一为"4 代码 + 2 文档 + 1 方案" |
| **S5** | 🟡 | RIPR 缺"早退形状"回归行，且未引用现成全字典相等断言 | ✅ §6.1 引用 `test_r17_defects:1020` / `test_undo_command:94` |
| **S6** | 🟡 | "回归探针 #3/#6"漏了 #5 | ✅ 已补为 #3/#5/#6 |
| **S7** | 🟡 | `session.tsx` 路径不全 | ✅ 已改为 `frontend/src/routes/session.tsx` |
| **S8** | 🟡 | §十"部分回滚的 TUI 打印成功"不准确（实际已打印 reason） | ✅ 已修正为"缺陷在 reload 判据，不在文案" |

**评审确认的通过项**：`restore_ok` 覆盖全部失败路径；不能用 `not reason`（3 条实证）；
6 处处置表准确；`messages_before` 判据当前不会误判；契约层无需改；
只改 `ok` 会引入 TUI 回归；基线 0 failed / 6 skipped；ruff 0、mypy 3 处既有。

---

## 十五、第二轮评审（代码评审）与修复

子智能体对**实施后的代码**（`3ff733e..9fa18ca`）再做一轮实跑评审，含 4 组突变实验。

### 15.1 发现与处置

| # | 严重度 | 问题 | 处置 |
|---|---|---|---|
| **P1** | 🔴 | **`restore_ok` 漏第 4 条失败路径**：[snapshots.py:129-130](file:///workspace/ai-coding/ai_coding/core/snapshots.py#L129-L130) 在 `git diff` 非 0 时**静默返回空 `RestoreReport()`**（不抛、不填 `failed`）→ `ok` 仍为 `True`、`reason` 为空、`files` 为空 —— 谎称"什么都没变"。**方案 §11.3"只有三种结局"的前提不成立** | ✅ 改为 `raise OSError`；调用方 undo/redo 已有 `except (OSError, TimeoutError, ValueError)` → 落成 `restore-failed`。补测 `test_a_failed_diff_is_raised_not_reported_as_a_clean_restore`，**突变回 `return RestoreReport()` 时该测试报 `DID NOT RAISE`** |
| **P2** | 🟠 | **redo 的 `ok` 与 undo 不一致**：undo `no-snapshot` ⇒ `ok=True`（良性），redo 仍 `not reason` ⇒ `ok=False`。同一概念两个语义 | ✅ redo 也引入 `restore_ok` 布尔（两处 `if failed:` + `except` 置 False）。补测 `test_a_no_snapshot_redo_stays_a_success_at_the_loop_level` |
| **P3** | 🟡 | **redo `no-snapshot` 文案误导**：`_REASON_TEXT["no-snapshot"]` 是 undo 语境（"工作区不是 git 仓库"），复用到 redo 时失真，且嵌套括号 | ✅ 新增 `_redo_outcome`，redo 分支不再前缀"还原 0 个文件"再被 reason 自我矛盾。补测 `test_a_no_snapshot_redo_reloads_and_says_why` |
| **P4** | 🟡 | 3 条新用例抓 bug 能力不均（改前 `.F.`：只有 redo 那条直接抓原始缺陷） | ✅ 不改代码，仅记录 —— 三条各有作用（原始缺陷 / 半修复回归 / 前提锁定） |
| **P5** | 🟡 | [test_r17_defects.py:1041](file:///workspace/ai-coding/tests/integration/test_r17_defects.py#L1041) 死分支：`report.get("restored")` 键在 redo 报告中不存在 | ✅ **不采纳评审建议的 `files`**（`files` 恒存在 → 整个断言恒真，更差）；改为删掉死分支：`assert report["ok"] is False` |
| **P6** | 🟡 | `_rewrote_history` 属隐式耦合（字段存在性推断） | ✅ 记录；已由两条全字典相等断言 + 新用例兜底，长期建议加显式 `history` 字段 |

### 15.2 第二轮评审确认的通过项（实跑）

- `_rewrote_history` 对**全部**当前报告形状判据正确（undo 3 种 + redo 2 种），无假阳/假阴
- 4 组突变各自精确打红对应用例（`ok`→`True`、undo 判据→`ok`、redo 判据→`ok`、`ok`→`not reason` 恰好 3 条）
- 无越界改动：`handlers.py` / `frontend/src/routes/session.tsx` 未动
- 无功能回归

### 15.3 最终实测（全部修复后）

```
pytest  3812 tests / 0 failures / 0 errors / 6 skipped
ruff    6 errors（全部既有，零新增）
mypy    3 errors（零新增）
git status  干净
```

> 说明：`3812 = 3806（M-A4 基线）+ 3（步骤 4）+ 3（第二轮 P1/P2/P3 补测）`。
