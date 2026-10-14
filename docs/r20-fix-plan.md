# r20 设计文档：M1–M9 + 一条我自己引入的回归

> **状态**：待拍板。逐项 TDD（RIPR，缺一不可），每项单独提交。
> **基线**：`5a6e048`。**基线绿**：pytest `2588 passed / 8 skipped`，mypy 132 干净，前端 248 绿。

---

## 0. 结论摘要

第二轮调研（压力测试方案本身，而非重复验证缺陷）得到：

| # | 发现 | 结论 |
|---|---|---|
| 1 | **M9**：`undo→redo` 静默丢失回合新建的文件，`files: []` 还显示"没事" | **新增缺陷**，r17 已发布行为，并入 r20 |
| 2 | 我 r20 初稿里 F0 的修正版会把 `./sub/b.py` 加成 `sub/sub/b.py` | **我的方案设计错误**，已重新推导 |
| 3 | F2 合并回退会因 `created` 漏删文件 | **证伪**，实测文件系统正确 |
| 4 | F5/M7 要新增 IPC 回读 | **证伪**，`.then()` 已在等响应 |
| 5 | `edit_tool` 编辑既有文件会被 undo 删掉 | **证伪**，`restore` 的 `cat-file -e` 分支保护了它 |

拍板后执行 **F0–F7** 共 8 项。

---

## 1. 调研证据

### 1.1 M9（新，最严重）

```
回合 write new.txt → undo: files=['new.txt'] ✓ 删掉
                   → redo: files=[]        ✗ 文件没回来
```
根因链（逐行核验）：
- `SnapshotStore.capture` → `git stash create` → **只含 tracked 改动**
- 回合新建的文件是 untracked → **不在快照里**
- undo 能删（靠 `message.artifacts` 的 `created` 集合）
- redo 靠 `git checkout <baseline>` → **无 untracked 可恢复**

不修的话，**F2 合并回退会让 redo 丢更多文件**（丢两个回合的），所以 F2 与 M9 必须同批。

### 1.2 F0 的正确规则（推翻我的初稿）

rg 的输出**回显我们给它的搜索参数**（实测三种）：

| 我们传的 target | rg 返回 | 真实代码路径可达? |
|---|---|---|
| `/abs/ws` | `/abs/ws/a.py`（绝对） | ✅ 唯一可达 |
| `/abs/ws/sub` | `/abs/ws/sub/b.py`（绝对） | ✅ 唯一可达 |
| `./sub` | `./sub/b.py` | ❌ 我们恒传 `str(target)` |
| `.` | `./a.py` | ❌ 同上 |

**结论：只需处理绝对分支**，相对分支直接原样返回（`PurePosixPath` 本身会吃掉 `./`）。
我的初稿把绝对分支也强行相对 target 再接前缀 → `./sub/b.py` 变 `sub/sub/b.py`，是错的。

### 1.3 被证伪的担心（记录以免重犯）

- **F2 的 `created`**：两次 undo 各自撤各自那一回合，实测磁盘正确为空。
- **F5 的 IPC**：`session.tsx:125` 的 `.then()` 已接收 `client.request` 的响应，后端在现有返回值加字段即可。
- **`edit_tool` 误删**：`restore` 先 `git cat-file -e ref:rel`，存在就走 `checkout`，**只有快照里没有的路径才删**。

### 1.4 M9 内存上界（决定不加护栏）

`created` 只由 `write_tool`（自身上限 2M 字符）与 `edit_tool` 产生，回合迭代上限 `max_iterations = 10`（`core/turn_policy.py:69`）→ 上界 ≈ 20MB/回合，且与 `_undo_tail` 同生命周期。

**决定不加体积护栏**：护栏会制造"redo 静默不恢复"的**同类缺陷**，正是本项要消灭的东西。改为在文档里写明上界。

---

## 2. 复用的既有契约（本轮一个都不新造）

| 契约 | 位置 | 本轮怎么用 |
|---|---|---|
| `SnapshotStore.capture/restore/forget` | `core/snapshots.py` | 扩展返回值，不加新方法 |
| `message.artifacts` → `ToolArtifact` | `domain/message.py` | F2/F9 的"回合写了什么"唯一来源 |
| `session.turn_count` / `turn_digests` | `domain/session.py` | F2 需要复原 |
| `service.notify("event", {"type","data"})` | 既有事件通道 | F7 的回音（与 `turn.finished` 同形） |
| `client.request(...).then(res)` | `session.tsx:125` | F7 已存在，不新增 IPC |
| `git stash create` + `cat-file -e` + `checkout` | `snapshots.py` | F1/F2/F9 全部复用 |
| `_MAX_FILE_BYTES = 1_000_000` | `grep_tool.py` | F6 的 rg `--max-filesize` 对齐值 |
| `bash_tool._DEFAULT_MAX_OUTPUT = 50_000` | `bash_tool.py:23` | F4 的 50k 上限来源 |
| `save_data_url` 的三种拒绝理由 | `attachments.py` | F7 直接透传，不重写判定 |

**不引入**：新的事件系统、新的状态容器类型（除 F2 的 frozen dataclass）、新的文件 IO 路径。

---

## 3. F0（最高优先）修我自己的回归：rg 绝对路径

### 3.1 病灶
`grep_tool.py:306` 的 `_workspace_relative` 用 `prefix / tail`，**pathlib 的 `/` 遇绝对右操作数返回右侧**，绝对路径原样穿透。

### 3.2 执行范围
| 文件 | 改动 |
|---|---|
| `tools/grep_tool.py` `_workspace_relative` | 加 `is_absolute()` 分支 |
| `tests/integration/test_r17_parity.py` | 补一条注入式用例（不依赖真 rg） |
| `scripts/r18_acceptance.py` | 新增 `parity` 节：装了 rg 就跑，没装就**打印"未验证"** |

### 3.3 分步
```python
def _workspace_relative(self, target: Path, from_target: str) -> PurePosixPath:
    """Re-anchor a path the search root produced onto the workspace.

    rg echoes the search argument it was given, and we always pass an absolute
    one, so its matches come back absolute.  Strip them back to ``target``
    first; the ``/`` operator would otherwise return an absolute right operand
    unchanged and leak the absolute prefix to the model.
    """
    tail = PurePosixPath(from_target)
    prefix = PurePosixPath(target.relative_to(self._sandbox.workspace).as_posix())
    if tail.is_absolute():
        tail = tail.relative_to(PurePosixPath(target.as_posix()))
    return tail if prefix == PurePosixPath(".") else prefix / tail
```

### 3.4 RIPR
- **Red** `test_a_runner_returning_an_absolute_path_is_re_anchored`：注入返回 `/ws/sub/b.py`，`path="sub"` → 必须显示 `sub/b.py`
- **Red** `test_both_runners_answer_the_same_question` / `..._agree_on_a_scoped_search`：真 rg（本轮安装并执行）
- **Pass** 验收脚本 parity 节

### 3.5 效果
| rg 返回 | 改前 | 改后 |
|---|---|---|
| `/ws/a.py` | `/ws/a.py` ❌ | `a.py` |
| `/ws/sub/b.py`（path=sub） | `/ws/sub/b.py` ❌ | `sub/b.py` |

---

## 4. F1（M1+M2）`restore` 说真话

### 4.1 病灶
```python
code, _ = await git(..., "checkout", ref, "--", rel, ...)   # 返回码丢弃
restored.add(rel)                                            # 无条件计入
# _remove: rmtree(ignore_errors=True) 不抛；unlink 遇 PermissionError 抛 → 冒泡中断整批
```
实测：只读目录下 `ok.txt` 已删、`ro/locked.txt` 抛 `PermissionError` → 上层记 `restore-failed`，但**已经还原的文件退不回去**，且 redo 基线拍的是半还原态。

### 4.2 执行范围
| 文件 | 改动 |
|---|---|
| `core/snapshots.py` | 新增 `RestoreReport` frozen dataclass；`restore` 返回它；`_remove` 不再向上抛 |
| `core/agent_loop.py` `undo_last_turn`/`redo_last_turn` | 读 `.restored` / `.failed`，`failed` 非空进 `reason` |

### 4.3 分步
1. ```python
   @dataclass(frozen=True)
   class RestoreReport:
       restored: list[str]
       failed: list[str]
       saved: dict[str, bytes]   # F9 用，见 §6；此处先占位同一形状
   ```
2. `checkout` 返回码 ≠ 0 → `failed`；`_remove` 包 `try/except OSError` → `failed`
3. `undo_last_turn`：`files = report.restored`；`failed` 非空 → `reason = f"还原失败：{n} 个文件未还原（{前3个}…）"`

> **为什么失败仍截断历史**：undo 的语义是"把这一回合收回去"。已经还原的文件退不回去，若因此放弃截断，用户会得到"消息还在、文件半还原"的更糟状态。**说清楚 > 回滚得漂亮。**

### 4.4 RIPR
- **Red** `test_a_file_that_cannot_be_deleted_is_reported_not_raised`
- **Red** `test_restore_reports_only_what_it_actually_did`
- **Red** `test_the_undo_report_names_what_did_not_come_back`
- **Pass** `test_snapshots.py` 现有 5 条（改用 `.restored`）

---

## 5. F2（M3）+ F9 暂存回退合并 · 对齐 OpenCode

> F2 与 F9 同批：F9 是 redo 的数据完整性，F2 是 redo 的范围。二者共用同一份"暂存状态"。

### 5.1 契约
> 连续的 `/undo` 不覆盖，而是把暂存回退变宽；`/redo` 一次全部恢复（含**新建的文件**）；发新消息才提交它。

### 5.2 算术（已验算）
```
S(k)=第k回合开始时的工作区；<sid>/k 快照 = S(k)
两回合后工作区 = B
undo#1: [拍 redo 基线 B] restore(<sid>/N) → A   隐藏[回合N]      turn_count N→N-1
undo#2: [不拍，沿用 B]   restore(<sid>/N-1) → S(N-2) 隐藏[回合N-1,回合N] turn_count N→N-2
redo : restore(基线 B) → tracked 全回 ; 写回 saved 文件 ; 消息全回 ; turn_count → N
```
**工作区还原天然正确**（各自 restore 自己的快照），缺的只有状态管理。

### 5.3 执行范围
| 文件 | 改动 |
|---|---|
| `core/agent_loop.py` | 新增 `_undo_state` / `_redo_files`；`undo_last_turn` 累积；`redo_last_turn` 全量复原；`_forget_redo` 清三个槽 |
| `core/snapshots.py` | `restore` 在删 `created - paths` 前先读出字节，放进 `report.saved` |

### 5.4 分步
```python
# ① 快照侧：删之前先把内容留底（F9 的根）
for rel in sorted((created or set()) - paths):
    target = self._root / rel
    if target.is_file():
        saved[rel] = target.read_bytes()      # 只有快照里没有的才需要留底
    _remove(target)
    restored.append(rel)
```

```python
# ② 循环侧：暂存状态（frozen dataclass，与 _undo_tail 同寿命）
@dataclass(frozen=True)
class _Staged:
    turns: int
    turn_count: int
    digests: tuple[TurnDigest, ...]

# ③ undo：redo 基线只拍一次；尾巴累积；状态累积
staged = self._undo_state.get(sid)
if staged is None:
    await self.snapshots.capture(f"{sid}/redo")      # 只有第一次拍
self._undo_tail[sid] = removed + self._undo_tail.get(sid, [])
self._undo_files[sid] = {**self._undo_files.get(sid, {}), **report.saved}
self._undo_state[sid] = _Staged(
    turns=(staged.turns + 1) if staged else 1,
    turn_count=(staged.turn_count + 1) if staged else session.turn_count,
    digests=(staged.digests + tuple(session.turn_digests)) if staged else tuple(session.turn_digests),
)

# ④ redo：基线 + 文件 + 消息 + 计数 + 摘要，全量复原
self._undo_tail.pop(sid, None); self._undo_files.pop(sid, None); self._undo_state.pop(sid, None)
```

### 5.5 数据流影响
```
                 ┌──────────── _undo_state[sid] ────────────┐
undo ──► 拍基线(仅首次) ──► tail 累积 ──► files 累积 ──► state 累积
redo ──► restore(基线) ──► 写回 files ──► 写回 tail ──► 复原 count/digest ──► 清三槽
新回合 ──► _forget_redo ──► 清三槽（暂存回退被提交，与 OC 同规则）
```
**唯一新增的持久化风险**：`snapshots` 为 None 时无基线 → 行为不变（仍只动消息）。

### 5.6 RIPR
- **Red（M9 核心）** `test_redo_restores_a_file_the_turn_created` ← **本轮最重要的新用例**
- **Red** `test_two_undos_then_one_redo_restores_everything`
- **Red** `test_the_redo_baseline_is_captured_once`（计数 fake 断言第二次 undo 不调 `capture`）
- **Red** `test_redo_puts_the_digests_back` / `test_redo_restores_the_turn_count`
- **Red** `test_a_new_turn_commits_the_whole_staged_revert`
- **Pass** `test_undo_command.py` 现有 3 条（单次 undo 行为不变）

### 5.7 效果
| 场景 | 改前 | 改后 |
|---|---|---|
| 单次 undo→redo，回合建了 `new.txt` | 文件回不来，报告 `files: []` | 文件回来，报告含 `new.txt` |
| undo×2 → redo | 只恢复一个回合，另一个不可达 | 一次全恢复 |
| undo×3 → 新消息 → redo | — | `nothing to redo` |

---

## 6. F3（M5）`walk_workspace`：gitignore 管到目录 + 按序下降

### 6.1 病灶（实测）
```
.gitignore: build/
walk → ['.gitignore','a_dir','build','root.py','z_dir','z_dir/z1.py','a_dir/a1.py']
  'build' 出现 → files.find 把它当候选，用户选中得到空 listing
  且 20k 预算花在它身上（docstring 承诺 "never entered"）
  子树按 LIFO 反序（z_dir 先于 a_dir）→ files.find 的 200 条被后部子树先吃掉
```

### 6.2 执行范围：`core/workspace_files.py` 单文件
### 6.3 分步
1. 目录也过 gitignore：`spec.match_file(rel + "/")`（pathspec 支持尾部 `/` 的目录规则），命中则**既不 yield 也不 push**（剪枝，不是过滤）
2. `pending.pop()` → `pending.pop(0)`：深度优先、同级按字母序

### 6.4 RIPR
- **Red** `test_a_gitignored_directory_is_not_offered`
- **Red** `test_a_gitignored_directory_is_never_entered`（里面放 2000 文件，断言结果 < 20 条 → 证明剪枝）
- **Red** `test_siblings_are_walked_in_order`
- **Pass** walk 5 条 + `files.find` 13 条

---

## 7. F4（M6）webfetch 输出封顶 50k

> 数字有先例：`bash_tool._DEFAULT_MAX_OUTPUT = 50_000`（`tools/bash_tool.py:23`）。实测 120KB 网页 → 40,112 字符无上限输出。

### 7.1 执行范围：`tools/webfetch_tool.py` 单文件
### 7.2 分步
清洗后按字符截断，头部保留来源、尾部保留不可信警示、中间写「已截断（N/共 M 字符）」。
**顺序是硬要求**：① 截断必须在清洗之后（清洗去掉 script/style，未清洗就截会让垃圾占满预算）② 警示必须在末尾（模型最后看到它）。

### 7.3 RIPR
- **Red** `test_a_huge_page_is_truncated_with_a_visible_marker`
- **Red** `test_the_untrusted_note_survives_truncation`
- **Red** `test_a_page_under_the_cap_is_untouched`
- **Pass** `test_webtools.py` 6 条

---

## 8. F5（M7）被拒的图片要有回音

### 8.1 病灶
```python
ref = save_data_url(...)
if ref is not None: images.append(ref)     # 拒绝 → 静默丢弃
```
而前端 `apply({kind:"submitted", images})` 已先渲染 `[Image: …]`。**显示已发送、实际被丢弃**。

### 8.2 执行范围
| 文件 | 改动 |
|---|---|
| `server/handlers.py` `session_prompt` | 收集 `rejected`（含 `save_data_url` 的拒绝理由），放进既有返回值 |
| `routes/session.tsx` `dispatch` | 既有 `.then(res)` 里判 `rejected`，`apply` 新动作 |
| `state/session.ts` reducer | `attachment-rejected` → 该条 user 消息的 image part 换成 notice part |

**不需要新增 IPC**：`session.tsx:125` 的 `.then()` 已接收 `client.request` 的响应。
**不在前端拦**：mime/大小规则在后端 `_allowed_mime`，前端再写一份就是新的漂移。

### 8.3 RIPR
- **Red（Py）** `test_a_rejected_image_is_reported_not_dropped`（21MB data URL → 响应含 `rejected`）
- **Red（TS）** reducer：`attachment-rejected` 后该消息含 notice part、不含 image part
- **Pass** 现有前端 248 条

---

## 9. F6（M8）两路都跳 >1MB

**证伪不修**：二进制嗅探分歧复现不出（四种文件两路一致）。
**要修**：大小分歧（`big.py` >1MB，rg 命中、兜底跳过）。

### 执行范围：`tools/grep_tool.py` 单文件，加 `--max-filesize=1M`，对齐兜底 `_MAX_FILE_BYTES`
### RIPR
- **Red** `test_a_file_over_the_size_budget_is_not_searched`（两条路径各断言一次）
- **Pass** parity 用例（本轮装 rg 必跑）

---

## 10. 打勾清单

| # | 项 | 提交 |
|---|---|---|
| 1 | **F0** `_workspace_relative` 绝对分支 + 注入式回归用例 + 验收脚本 parity 节 | `dde6414` |
| 2 | **F1**（M1+M2）`RestoreReport(restored, failed)`，失败不再抛、报告点名 | `d92b121` |
| 3 | **F2**（M3+M9）暂存回退合并：基线只拍一次 / 尾巴累积 / `saved` 留底与写回 / redo 复原 count+digest | `cae1293` |
| 4 | **F3**（M5）gitignore 管到目录 + 按序下降 | `8bb3f0c` |
| 5 | **F4**（M6）webfetch 50k（清洗后截断，警示留末尾） | `d9f9e27` |
| 6 | **F5**（M7）被拒图片回音（`rejected` + reducer notice part） | `8255856` |
| 7 | **F6**（M8）rg `--max-filesize` | `7e8c6c6` |
| — | **证伪不修**：M8 二进制嗅探（复现不出）、`edit_tool` 误删（有 `cat-file -e` 分支保护）、F2 的 `created` 漏删（实测文件系统正确） | — |
| — | `docs/data-contracts.md` 同步（含"已知缺口"逐条改成已修 + 剩余项） | 本提交 |

**最终门禁**（全绿）
- pytest `2610 passed / 6 skipped / 0 failed`（基线 2588 → +22）
- mypy `132 source files` 干净
- `ruff check ai_coding/ tests/integration/ scripts/r18_acceptance.py` 干净
- 前端 `250 pass / 26 files`（基线 248 → +2）、`tsc --noEmit` 干净
- `scripts/r18_acceptance.py` `36 passed / 0 failed`（rg 缺席时显式打印 UNVERIFIED）
- **真实业务链路 9/9**（走真 git / 真 write 工具 / 真快照，只有模型是替身）：
  ① rg 绝对路径已重锚定 ② 删不掉的文件被点名 ③ redo 带回合新建文件 ④ undo×3 → redo 一次全回
  ⑤ 新回合提交暂存回退 ⑥ 被忽略目录不吃预算 ⑦ 5MB 网页截断到 50k ⑧ >1MB 两路不搜 ⑨ 四类粘贴拒绝各带理由

### 实施中发现并修正的三处自身错误
1. F0 的红用例最初写成子串断言——`/ws/sub/c.txt` **包含** `sub/c.txt`，弱断言在 bug 存在时也绿。改为精确匹配后才真红。
2. F2 写完发现 `taken_digests` 累积会**重复**（第二次 undo 读到的是已被裁过的环），`turn_count` 累加会得 3 而非原值 2。改为：`_Staged` 记**首次 undo 之前**的状态，后续 undo 只加 `turns`。
3. 前端 `updateLastUser` 返回的是整个 state，我误当 `messages` 数组赋值——被自己的用例抓到（`messages[0]` undefined）。

---

## 11. 终态设想（实施完成后跑一遍）

1. `path="sub"` 时，rg 返回绝对/带 `./`/相对三种，输出都是 `sub/b.py`
2. 只读目录里 `restore` 不抛，报告说"还原 1 个，未还原 1 个（ro/locked.txt）"，历史照样截断
3. 回合写了 `new.txt` → `undo` 删掉 → `redo` **`new.txt` 回来了**，报告含它
4. `undo`×3 → `redo` 一次：3 回合的消息、文件、`turn_count`、摘要行全部回来
5. `undo`×3 → 发新消息 → `redo`：`nothing to redo`
6. `.gitignore` 写 `build/`：`build/` 不出现在 `@` 补全，2000 个文件不进预算
7. 抓 5MB 网页：输出 ≤ 50k 字符，头有来源、尾有不可信警示、中间写明已截断
8. 粘 21MB 图：转录区出现"未附加"说明，模型收到的是无图那条消息
9. `big.py`（>1MB）：两路都不命中

## 12. 终态架构图

```
                    用户
                      │  发送(含图片)
                      ▼
        ┌──────────────────────────┐
        │ buildSubmission (F5)     │  命令 vs 普通行 · 图片是否随行
        └────────────┬─────────────┘
                     │ IPC session.prompt
                     ▼
   ┌───────────────────────────────────────────┐
   │ handlers.session_prompt                   │
   │  save_data_url ─┬─ ok   → images         │  ← F7: rejected 带理由返回
   │                 └─ 拒绝 → rejected[name] │
   └────────────┬──────────────────────────────┘
                ▼
   ┌───────────────────────────────────────────┐
   │ AgentLoop.process_input                  │
   │  图片 → 多模态请求；artifacts 记账        │
   └────────────┬──────────────────────────────┘
                ▼
   ┌───────────────────────────────────────────┐
   │ /undo  ──► _undo_state / _undo_tail /     │  ← F2+F9
   │            _undo_files  （进程内存）       │
   │ /redo  ──► restore(基线) + 写回 files      │
   │            + 写回 tail + 复原 count/digest │
   └────────────┬──────────────────────────────┘
                ▼
   ┌───────────────────────────────────────────┐
   │ SnapshotStore                             │
   │  capture : git stash create（tracked）    │
   │  restore : diff → cat-file -e ?           │
   │            yes → checkout                 │  ← F1: 返回 RestoreReport
   │            no  → 留底字节 + _remove       │  ← F9
   └───────────────────────────────────────────┘
                │
   ┌────────────┴──────────────────────────────┐
   │ GrepTool            │ WebFetchTool        │
   │  F0 绝对路径重锚定   │ F4 清洗后 50k 截断   │
   │  F6 两路都跳 >1MB    │                     │
   └───────────────────────────────────────────┘
```