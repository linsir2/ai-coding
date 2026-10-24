# M-A4 实施方案 · 环境伪失败隔离（让 CI 重新可信）

> 状态：**评审已回，方案已修订**（评审对象 = 当前**未提交**的工作区 diff）
> 关联：[roadmap-milestones.md](roadmap-milestones.md) §1 M-A4
> 依赖：M-A1（已完成，全量失败 10→3）
> 前置实测：[eval-01](eval-01-offline-baseline.md) §1
> 评审：子智能体实跑评审（见 §12），P0/P1 共 6 项已全部落实

---

## 一、要实现的内容（一句话）

**让 3 条"靠 chmod 制造拒绝"的测试在任何用户下都能真的测到东西** ——
现在的它们在 root 下**静默失效**（假装通过），这让 CI 变绿却什么都没验证。

---

## 二、调研结论：3 条测试，同一个根因

### 2.1 失败清单（M-A1 之后）

```
FAILED tests/integration/test_r17_defects.py::test_undo_names_the_files_it_could_not_restore
FAILED tests/test_grant_store.py::test_an_unwritable_store_does_not_raise
FAILED tests/test_snapshots.py::test_a_file_that_cannot_be_deleted_is_reported_not_raised
```

基线：`pytest -q` → **3 failed / 3797 passed / 6 skipped**（6 条 skip 是 MCP stdio 门，超范围）。

### 2.2 根因：**它们用 `chmod` 模拟"操作系统拒绝"，而 root 绕过 DAC**

| 测试 | 制造拒绝的手段 | 期望发生 | root 下实际 |
|---|---|---|---|
| `test_snapshots.py:80` | `os.chmod(locked, 0o555)` | `_remove` 删不掉 `ro/kept.txt` → 进 `failed` | root 照删 → `failed=[]` → 断言失败 |
| `test_grant_store.py:102` | `chmod(S_IRUSR\|S_IXUSR)` | `write_text` 抛 `OSError` → 被吞 | root 照写 → `path.exists()` 为 True → 断言失败 |
| `test_r17_defects.py:997` | `os.chmod(locked, 0o555)` | undo 删不掉 `ro/stuck.txt` → 进 `reason` | root 照删 → `reason=''` → 断言失败 |

**实测证据**（`euid = 0`）：

```
A chmod   -> failed = []                        ← 拒绝没发生（bug）
C inject  -> failed = ['ro/kept.txt']           ← 注入后正确
B _remove -> failed = ['ro/kept.txt']           ← 注入后正确
```

**DAC 机制级验证**（评审以 `nobody` 跑 stdlib 探针）：`unlink` / `write_text` 均抛
`[Errno 13] Permission denied` —— 证明非 root 下原写法本就有效，**这 3 条是 root 专属失败**。

### 2.3 关键判断：**这些测试测的是"错误处理契约"，不是"操作系统权限"**

| 测试 | docstring 声称的契约 |
|---|---|
| `test_snapshots.py:67` | "restore 是一个事务：要么说清什么回来了，要么说清什么没回来" |
| `test_grant_store.py:97` | "尽力而为，像本项目所有持久化一样；store 该做的是安静失败" |
| `test_r17_defects.py:958` | "部分回滚不能再被报成干净的重卷 —— 报告是 undo 唯一的证据" |

**三者都不是在测 chmod**。`chmod` 只是**制造一次拒绝的手段**，而这个手段在 root 下不成立。

→ **第一性原理**：把"制造拒绝"从**操作系统**搬到**代码边界**，测试就能在任何用户下跑，
而且测的仍是同一个契约。

### 2.4 项目已有的惯例

`grep` 显示项目**已经在用 `skipif`**（`test_r17_parity.py:183`、`test_server_vcs.py:14`、
`test_mcp_stdio.py:31`、`test_mcp_progress.py:30`、`test_mcp_end_to_end.py:45`）——
但那些 skipif 的理由是"**外部工具不存在**"（ripgrep / git / MCP 门），
**不是"当前用户身份让断言不成立"**。这两者性质不同，不能照搬。

### 2.5 范围完整性（评审实测确认）

全仓扫描 `chmod` / `S_IRUSR` / `S_IWRITE`，**只有这 3 处**同类依赖，无遗漏。

---

## 三、方案取舍（含已否决项）

### 3.1 候选与裁决

| 方案 | 可移植 | 覆盖 `_remove` 的 try/except | 覆盖 restore 契约 | 裁决 |
|---|---|---|---|---|
| **A** `skipif(euid==0)` | ❌ root 下不跑 | ❌ | ❌ | **否决** |
| **B** patch `_remove` → `False` | ✅ | ❌（绕过） | ✅ | **否决** |
| **C** 注入真实 `PermissionError` | ✅ | ✅ | ✅ | **✅ 采用** |
| **D** 以 non-root 用户运行 | ✅ | ✅ | ✅ | **否决**（见 3.2） |
| **E** chmod + 探测回退 | ✅ | ✅（非 root） | ✅ | **否决**（见 3.2） |

**为什么 A 不可接受**：它把"环境不支持"和"断言不成立"混为一谈 ——
我们的环境（root 容器）恰恰是最需要验证这个契约的环境。三条测试会累积成"永远跳过"的死代码。

**为什么 B 不够**：`_remove` 的 `try/except OSError` 是 r20/F1 修复的核心，
而 `grep` 确认它**零覆盖**。B 绕过了它。

### 3.2 已考虑并否决的两个方案

**(D) 以 non-root 用户运行测试**
- 做法：`os.seteuid(65534)` 或 CI 里用 `run-as`
- 否决理由：① 需要 root 才能降权，非 root 环境下无法执行；② 降权后 `.venv`、
  `tmp_path`、git 凭据全部要重新授权，测试会变脆；③ **治标不治本** ——
  测试仍然依赖"OS 会拒绝"，只是换了个用户。而 C 直接测"代码处理拒绝"，更接近契约。

**(E) chmod + 探测回退（混合策略）**
- 做法：先真跑 chmod；若发现"拒绝没发生"（root）再回退到注入
- 否决理由：① **两套代码路径**，非 root 走真的、root 走注入，等于同一用例有两种行为；
  ② 探测"拒绝有没有发生"本身要做一次真实删除，有副作用；
  ③ 收益是"非 root 下更真实"，但 C 的 `PermissionError` 本来就是操作系统会给出的那个错误 ——
  **语义等价**。为等价的东西引入两套路径，不划算。

### 3.3 采用：方案 C（注入真实 `PermissionError`）

```python
with refused(monkeypatch, locked / "kept.txt", "unlink"):
    report = await store.restore("s/1", created={"ro/kept.txt"})
```

| 优点 | 代价 |
|---|---|
| **anywhere 可跑**（root / 非 root / CI 一致） | 多 6 行 |
| **走真实代码路径**：`_remove` 的 `try/except OSError` 被真正执行 | patch 的是 `Path.unlink`（全局），但**按路径守卫** |
| 语义最诚实：模拟的正是操作系统会给出的那个错误 | |

> 📌 **措辞修正**（评审 P3-12）：方案 C 覆盖的是 `_remove` 的**文件分支**（`unlink`）。
> **目录分支（`shutil.rmtree`, `ignore_errors=False`）仍无覆盖** —— 本方案不声称覆盖它。

---

## 四、执行范围（逐测试、逐步）

### 4.0 新建 `tests/conftest.py` —— ⚠️ 该文件**当前不存在**

`grep **/conftest.py` 全仓无命中。**本方案新建它**，放一个泛化 helper：

```python
"""Shared fixtures and test helpers."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest


@contextmanager
def refused(monkeypatch, target: Path, attr: str = "unlink") -> Iterator[None]:
    """Make ``target.<attr>()`` raise EACCES -- portably.

    ``chmod`` cannot produce a refusal for root, and root is the normal
    container user, so the refusal is injected at the boundary the code
    handles instead of at the filesystem.  Path-guarded, so nothing else's
    unlink/write_text is affected; ``monkeypatch`` undoes it at test end.
    """
    cls = type(target)
    real = getattr(cls, attr)

    def refused(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        if self == target:
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **kw)

    monkeypatch.setattr(cls, attr, refused)
    yield
```

**为什么是"泛化"而不是一个 `refused_deletion`**（评审 P3-14 + 用户拍板）：
`unlink`（snapshots / undo）与 `write_text`（grant_store）是**同一类注入、不同方法**。
合成一个 `refused(monkeypatch, path, attr)` 后，三处调用点各自一行，且不重复代码。
**说不清作用就砍 —— 这个说得清**（3 处复用，名字即契约）。

### 4.1 `tests/test_snapshots.py::test_a_file_that_cannot_be_deleted_is_reported_not_raised`

**步骤 1**：替换 chmod（注意：`os` 导入会变成未用，必须删）

```python
 @pytest.mark.asyncio
-async def test_a_file_that_cannot_be_deleted_is_reported_not_raised(tmp_path):
+async def test_a_file_that_cannot_be_deleted_is_reported_not_raised(tmp_path, monkeypatch):
     """restore is a transaction: ...（docstring 不变）"""
     root = _repo(tmp_path)
     store = SnapshotStore(root)
     await store.capture("s/1")
     locked = root / "ro"
     locked.mkdir()
     (locked / "kept.txt").write_text("x\n", encoding="utf-8")
     (locked / "after_in_locked.txt").write_text("z\n", encoding="utf-8")
     (root / "after.txt").write_text("y\n", encoding="utf-8")
-    os.chmod(locked, 0o555)
-    try:
-        report = await store.restore("s/1", created={"ro/kept.txt"})
-
-        assert "ro/kept.txt" in report.failed, (
-            f"the undeletable file is not reported as failed: {report}"
-        )
-        assert (locked / "kept.txt").exists(), "the test did not exercise a real refusal"
-    finally:
-        os.chmod(locked, 0o755)
+    with refused(monkeypatch, locked / "kept.txt", "unlink"):
+        report = await store.restore("s/1", created={"ro/kept.txt"})
+
+        assert "ro/kept.txt" in report.failed, (
+            f"the undeletable file is not reported as failed: {report}"
+        )
+        assert (locked / "kept.txt").exists(), "the test did not exercise a real refusal"
```

**步骤 2**：删掉文件顶部已无用的 `import os`（第 14 行），补 helper 导入

```python
-from tests.conftest import refused   # 若本文件用 from tests.conftest import ...
```

> ⚠️ **评审 P0-1 关键**：不删 `import os` 会引入 `F401`，`ruff check .` 直接红。
> **不改就是踩响自己设的门禁。**

### 4.2 `tests/test_grant_store.py::test_an_unwritable_store_does_not_raise`

**步骤 3**：替换 chmod（注意：`stat` 导入会失效；`Path` 需补导入）

```python
-from pathlib import Path        # 若原文件只有 import pathlib
+from pathlib import Path
 from tests.conftest import refused
```

```python
-def test_an_unwritable_store_does_not_raise(store):
+def test_an_unwritable_store_does_not_raise(store, monkeypatch):
     """Best effort, like every other persistence in this project. ...（不变）"""
     store.path.parent.mkdir(parents=True, exist_ok=True)
-    store.path.parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
-    try:
-        store.add("edit:a.py")  # must not raise
-
-        assert not store.path.exists()
-    finally:
-        store.path.parent.chmod(stat.S_IRWXU)
+    with refused(monkeypatch, store.path, "write_text"):
+        store.add("edit:a.py")  # must not raise
+        assert not store.path.exists()
```

**步骤 4**：删掉已无用的 `import stat`（第 17 行）

> 📌 覆盖边界（评审 P3-15）：这条只断言 `not exists()`，**不验证"grant 仍在会话内有效"** ——
> 而 docstring 那么声称。那是**这条用例自身的覆盖边界**，非本方案引入，也不在本方案扩大。

### 4.3 `tests/integration/test_r17_defects.py::test_undo_names_the_files_it_could_not_restore`

**步骤 5**：替换 chmod（函数内的 `import os` 会失效）

```python
     import os
-    from ai_coding.domain.loop import StepResult
+    from ai_coding.domain.loop import StepResult
```
（`import os` 在删除两处 `os.chmod` 后未用 → **删掉**）

```python
 @pytest.mark.asyncio
-async def test_undo_names_the_files_it_could_not_restore(tmp_path):
+async def test_undo_names_the_files_it_could_not_restore(tmp_path, monkeypatch):
     ...
     await loop.process_input(session, "建个文件")
     assert (locked / "stuck.txt").exists(), "the turn did not create the file"
-    os.chmod(locked, 0o555)
-    try:
-        report = await loop.undo_last_turn(session)
-    finally:
-        os.chmod(locked, 0o755)
+    with refused(monkeypatch, locked / "stuck.txt", "unlink"):
+        report = await loop.undo_last_turn(session)
```

**步骤 6**：补 helper 导入

```python
 from tests.conftest import refused
```

> ⚠️ **这条测试同时是 M-A3 的目标**（第 1006 行 `assert report["ok"] is True`）。见 §十。

### 4.4 让 CI 日志能区分"真失败 / 跳过"（roadmap M-A4 验收②）

`pyproject.toml`：

```toml
 [tool.pytest.ini_options]
 testpaths = ["tests"]
-addopts = "-q"
+addopts = "-q -rs"
```

> 现状：基线有 **6 条 skip**（MCP stdio 门），`pytest -q` 只打印 `6 skipped`、**不打印原因** ——
> roadmap 的"一眼区分"并未达成。加 `-rs` 后 skip 原因会逐条列出。
> **本方案不新增 gate 变量**（`ci.yml` 已有的 `AI_CODING_MCP_STDIO` 保持不动）。

### 4.5 不做什么

- ❌ 不改 `snapshots.py` / `grants.py` / `agent_loop.py` 的**产品代码**（它们是对的）
- ❌ 不用 `skipif`（§3.1 方案 A 已否决）
- ❌ 不合并/删除任何现有测试
- ❌ 不处理 MCP OAuth 的 CI 门禁 —— **M-A1 已把那 7 条修好**，不再是环境依赖

---

## 五、对数据流的影响

### 5.1 修改前（root 下）

```
测试 os.chmod(ro, 0o555)
  → root 绕过 DAC，删除成功
  → restore 的 failed = []
  → 断言失败
  → CI 红（但红的是"测试环境不对"，不是产品坏了）
```

### 5.2 修改后

```
测试 monkeypatch：对目标 Path.unlink 抛 PermissionError
  → _remove 的 try/except OSError 真的执行
  → _remove 返回 False
  → restore 把 ro/kept.txt 收进 failed
  → 断言通过
  → 在 root / 非 root / CI 上**行为一致**
```

### 5.3 不受影响的部分

- **产品代码**：`snapshots.py` / `grants.py` / `agent_loop.py` **一行不改**
- **其他测试**：不受影响（改动只在 3 个测试文件 + 新建 1 个 conftest + pyproject 一行）
- **`_remove` 的调用方**：`restore()` 的签名与语义不变
- **`GrantStore._write`**：不变

---

## 六、TDD 用例设计（RIPR）

### 6.1 红相：**先证明旧写法在 root 下会失败**

```
$ .venv/bin/python -m pytest tests/test_snapshots.py::test_a_file_that_cannot_be_deleted_is_reported_not_raised -q
FAILED ... AssertionError: the undeletable file is not reported as failed:
        RestoreReport(restored=['ro/kept.txt'], failed=[], saved={'ro/kept.txt': b'x\n'})
```

→ **红**。这条红不是产品坏了，是**测试的假设有问题** —— 红得正确。

### 6.2 RIPR 执行序

| 相 | 动作 | 判据 |
|---|---|---|
| **R** | 跑 3 条测试，记录全红 | 3 failed |
| **I** | 步骤 1-2（snapshots）→ 重跑该文件 | 该文件全绿 |
| **I** | 步骤 3-4（grant_store）→ 重跑 | 全绿 |
| **I** | 步骤 5-6（r17_defects）→ 重跑 | 全绿 |
| **P** | `pytest -q` | **0 failed 且 skipped 数不增（=6）** |
| **P** | `ruff check .` | **0 errors**（← 评审 P0-1：必须真的去删那 3 个导入） |
| **P** | **突变测试**：把 `snapshots._remove` 临时改成恒 `True`，重跑 → 该测试**变红**；改回 | 证明测试不空转 |
| **R** | `git status --porcelain` 白名单 | 见 §6.4 |

> 📌 **判据修正**（评审 P3-13）：原写"3800 passed"过脆（任何新增用例都会破坏）。
> 改为 **"0 failed 且 skipped 数不增（=6）"**。

### 6.3 P 相判据（**环境无关**）

```
pytest -q 的失败项 = ∅
且 skipped 数 = 6（不增）
ruff check . 的 error 数 = 0
```

**"root 与非 root 结果一致"不作为机检判据**（评审 P1-3）—— 本沙箱无法以 non-root 跑全量
（`.venv` 在 `/root` 下不可遍历）。改为**声明式结论 + 附 DAC 机制证据**：

> 评审已以 `nobody` 跑 stdlib 探针，确认 `unlink`/`write_text` 在非 root 下抛 `EACCES`；
> 本方案的注入与用户身份无关（`PermissionError` 是显式抛的），故行为一致。

### 6.4 R 相判据：**必须用 `git status --porcelain`**（评审 P0-2）

`git diff --stat` **不显示未跟踪文件** —— 而本方案要**新建 `tests/conftest.py`**。
若用 `git diff --stat`，新文件会被漏掉，判据形同虚设。

```
git status --porcelain 的输出 ⊆ {
    M  tests/test_snapshots.py
    M  tests/test_grant_store.py
    M  tests/integration/test_r17_defects.py
    M  pyproject.toml
    ?? tests/conftest.py
}
且其中不含任何 ai_coding/**（产品代码零改动）
```

---

## 七、架构图

```
        修改前（root）                              修改后（任意用户）
  ┌──────────────────────────┐            ┌──────────────────────────┐
  │  测试                     │            │  测试                     │
  │  os.chmod(ro, 0o555)     │            │  refused(monkeypatch,    │
  │        ↓                 │            │    path, "unlink")      │
  │  操作系统                  │            │        ↓                 │
  │  ── root 绕过 DAC ──→ ✗  │            │  Path.unlink 对目标       │
  │  删除成功                 │            │  抛 PermissionError      │
  │        ↓                 │            │        ↓                 │
  │  failed = []             │            │  _remove 的              │
  │        ↓                 │            │  try/except OSError      │
  │  断言失败（CI 红）         │            │        ↓                 │
  └──────────────────────────┘            │  failed = [path]         │
                                           │        ↓                 │
   非 root 下：3 条本就通过                │  断言通过 ✅              │
   （评审以 nobody 实测 DAC）              └──────────────────────────┘
           产品代码不动                              产品代码不动
```

---

## 八、修改成功后的效果

| 观测点 | root（修改前） | root（修改后） | 非 root |
|---|---|---|---|
| `pytest -q` | 3 failed / 3797 passed / 6 skipped | **0 failed / 3800 passed / 6 skipped** | **0 failed**（原本就绿） |
| 3 条测试是否真的在跑 | **空转**（断言前提不成立） | **在跑**，且断言成立 | 在跑 |
| `_remove` 文件分支的 try/except | 无覆盖 | **有覆盖** | 有覆盖 |
| `ruff check .` | 6 errors（既有） | **6 errors（不增）** | 同 |
| CI 日志的 skip 原因 | 只显示 `6 skipped` | **逐条列出原因**（`-rs`） | 同 |
| 产品代码 | — | **零改动** | — |

> 📌 **这 3 条是 root 专属失败**（评审以 `nobody` 实测 DAC 机制确认）。
> 所以"非 root 修改前"列**本来就是绿的** —— 初稿写"3 failed"是错的，已修正。

---

## 九、自我核验：假设实施之后，最终模样是什么？

1. **推演**：改完 3 条测试，全量 `pytest -q` 应该 **0 failed**。
   依据：3 条失败全部由 root 绕过 chmod 造成，改成注入后拒绝一定发生。
2. **推演**：非 root 环境下，改动前后**同样全绿**。
   依据：`PermissionError` 是显式抛的，与 DAC 无关。评审已用 `nobody` 验证 DAC 机制。
3. **反向检查**：方案 C 会不会让测试**过于宽松**（永远通过）？
   不会 —— 断言是 `"ro/kept.txt" in report.failed`。**突变测试**（把 `_remove` 改成恒 `True`）
   会让它变红，已列为 P 相判据。
4. **反向检查**：patch `Path.unlink` 全局会不会误伤？
   已加**路径守卫**（`if self == target`），其他路径走真实实现；`monkeypatch` 测试结束自动回滚。
   评审实测注入期间无其他 `unlink` 被波及。
5. **反向检查**：`test_grant_store` 用 `write_text` 注入，会不会和 `unlink` 注入互相干扰？
   不会 —— 两次注入在不同测试里，`monkeypatch` 各自回滚；且都按路径守卫。
6. **反向检查**：这条测试（r17_defects）会不会和 M-A3 冲突？
   **会** —— 见 §十。M-A4 改完后 `ok` 仍是硬编码的 `True`，断言 `is True` 仍通过；
   M-A3 修 `ok` 时再改这条断言。

---

## 十、边界声明

### 10.1 本方案改什么

| 文件 | 动作 |
|---|---|
| `tests/conftest.py` | **新建**（泛化 helper `refused`） |
| `tests/test_snapshots.py` | 步骤 1-2（注入 + 删 `import os` + 补 helper 导入） |
| `tests/test_grant_store.py` | 步骤 3-4（注入 + 删 `import stat` + 补 `Path`/helper 导入） |
| `tests/integration/test_r17_defects.py` | 步骤 5-6（注入 + 删函数内 `import os` + 补 helper 导入） |
| `pyproject.toml` | `addopts` 加 `-rs` |

**产品代码零改动。**

### 10.2 ⚠️ 与 M-A3 的耦合（评审 P1-5：全仓共 6 处，不止 1 处）

全仓 `assert report["ok"] is True` 共 **6 处**：

| 位置 | 是否受 M-A3 影响 | 理由 |
|---|---|---|
| `test_r17_defects.py:1006` | ✅ **受影响** | 唯一一条 `failed` 非空的场景（部分回滚） |
| `test_undo_command.py:111` | ❌ | 完整回滚，`failed` 为空 |
| `test_crash_recovery.py:300` | ❌ | 同上 |
| `test_r17_dataflow.py:195` | ❌ | 同上 |
| `test_r17_dataflow.py:443` | ❌ | 同上 |
| `test_r17_defects.py:952` | ❌ | `reason="no-snapshot"`，无 restore 发生 |

→ M-A3 只需改 `agent_loop.py:2489` 的 `"ok": True` 与 `test_r17_defects.py:1006` 的断言；
其余 5 处**不受影响**（`failed` 为空时 `not failed` 仍为 True）。

### 10.3 ⚠️ roadmap 里 M-A3 的症状描述已过期（评审 P2-9）

`roadmap-milestones.md:57` 记的 M-A3 症状是
`{'ok': True, 'files': ['ro/stuck.txt'], 'reason': ''}`。
**M-A4 注入后实测报告变为**：

```
{'ok': True, 'text': '建个文件', 'files': [], 'reason': 'restore-partial: ro/stuck.txt', ...}
```

`reason` 已非空、`files` 已空。**M-A3 实施者若按 roadmap 的症状去找问题会扑空。**
M-A4 之后，该用例的失败面**只剩 `ok` 字段**（产品侧唯一待改点是 `agent_loop.py:2489`）。

### 10.4 顺序

**先 M-A4（本方案），再 M-A3**（用户拍板）。两步各自可独立验收。

### 10.5 不含

- M-A3 的 `ok` 门控
- M-A5（ruff/mypy 清零）、M-A2（契约漂移）
- MCP OAuth 的 CI 门禁（M-A1 已修好那 7 条）

---

## 十一、待拍板

见对话中的提问。

---

## 十二、评审记录

子智能体**实跑评审**（含 `nobody` DAC 探针、突变测试、ruff 预演）结论：

| # | 严重度 | 问题 | 处理 |
|---|---|---|---|
| **P0-1** | 🔴 | 计划的 diff 会引入 **7 条 ruff 错误**（3 个失效导入 + 缺 `Path`/helper 导入） | ✅ 步骤 2/4/6 已补导入变更 |
| **P0-2** | 🔴 | `tests/conftest.py` **不存在**（要新建）；且 `git diff --stat` 不显示未跟踪文件 | ✅ §4.0 标注新建；§6.4 改用 `git status --porcelain` 白名单 |
| **P1-3** | 🟠 | "root 与非 root 一致"不可机检（本沙箱无法 non-root 跑全量） | ✅ 降级为声明式 + 附 DAC 证据 |
| **P1-4** | 🟠 | roadmap 验收②（CI 一眼区分跳过）未覆盖：6 条 skip 不打印原因 | ✅ §4.4 加 `addopts = "-q -rs"` |
| **P1-5** | 🟠 | M-A3 耦合只点名 1/6 处 | ✅ §10.2 枚举全部 6 处并论证 |
| **P2-6** | 🟡 | §八 "非 root 修改前"列错误，与 §九.2 矛盾 | ✅ 已修正（这 3 条是 root 专属失败） |
| **P2-7** | 🟡 | 漏了 (D) non-root 运行、(E) chmod+回退 两个候选 | ✅ §3.2 补"已考虑并否决"及理由 |
| **P2-8** | 🟡 | "反向验证"不可机检 | ✅ 改为**突变测试**，列为 P 相判据 |
| **P2-9** | 🟡 | roadmap 的 M-A3 症状描述已过期 | ✅ §10.3 点明 |
| **P3-10..15** | ⚪ | 措辞、skip 数、覆盖边界、精确计数、helper 导入、用例覆盖边界 | ✅ 均已修正 |

**评审确认的通过项**（实测）：根因判断成立；方案 C 三例全绿且走真实
`_remove` 的 try/except；方案 B ≡ C；**不空转**（突变后变红）；**不误伤**；
范围完整（全仓仅 3 处同类依赖）；行号引用全部准确；skipif 惯例判断正确；
`_remove` 零覆盖判断成立；基线数字一致；产品代码零改动。
