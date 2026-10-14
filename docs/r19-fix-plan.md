# r19 — r18 验收发现：一条自己引入的回归 + 三处不闭环

> **执行约定**：逐项 TDD，每项完成打勾并提交。测试先行，除非用例本身失效否则不改测试。

**背景**：`scripts/r18_acceptance.py`（31/32 通过）与独立审查交叉发现了这些问题。本轮只做**验收已证实**的，一条不多。

---

## 0. 先说一条**被证伪**的（不修）

### F4 · 「两条 harness user 消息漏标 synthetic」——**不成立，不改**

审查称 `context_compression.py:112`（snip 占位符）与 `:756`（压缩摘要 note）构造的 `ChatMessage(role="user")` 没标 `synthetic`，undo 可能把它们当作"最后一个真人回合"，**收进 created 集合删掉该会话创建过的每一个文件**。

**实测证伪**：
```
session.messages 里的 user 消息:
   synthetic=False | '一'
   synthetic=False | '二'
→ session.messages 中存在 synthetic=False 的 harness 文本: False
→ session.messages 中存在 snip 占位符:               False
```
压缩在**副本**上工作（`ContextManager` 文档："The caller's message list is never mutated"），`request_assembly` 只把 `history` 用于本次请求，从不回写 `session.messages`。而 undo 扫描的正是 `session.messages`。

**结论**：给这两处加 `synthetic=True` 是**零效果的投机修改**。反倒是 F3 把 `split_tail` 的尾部锚点改成跳过 synthetic 之后，这条路径的行为**已经**是正确的（压缩摘要不会被当成 verbatim 尾部的起点）。**本轮不动它。**

---

## 1. F1 · CRITICAL — `grep(pattern, path="<子目录>")` 恒返回 `(no matches)`

### 1.1 病灶（两条路径都有病，且是同一种病）

```python
# 兜底：grep_tool.py:293-299
def _walk(self, root: Path):
    return [
        (self._sandbox.workspace / rel, rel)     # ← 错：workspace / rel
        for rel, is_dir in walk_workspace(root)
        if not is_dir
    ]
```
`walk_workspace(root)` 产出的是**相对 `root`**（= 搜索目标）的路径。`target == workspace` 时 `workspace / rel` 恰好等于 `root / rel`，所以**所有不带 `path=` 的测试全绿**；一旦 `path="sub"`，拼出的是 `<ws>/b.py`（不存在），`stat()` 抛 `OSError` 被吞 → 零命中。

```python
# rg：grep_tool.py:247-251
path = str(data.get("path", {}).get("text", ""))
path = str(Path(path).resolve().relative_to(self._sandbox.workspace))   # ← 错：相对**进程** cwd
```
`run_process_async(cwd=workspace)` 只改**子进程**的 cwd。rg 输出的 `path.text` 已经相对 workspace（实测 `rg … .` → `./a.py`），但 `Path(...).resolve()` 用的是**服务端进程**的 cwd，`relative_to` 必然失败，被 `except` 吞掉后恰好保留了 rg 的原始字符串——**这次是"错得刚好对"**。换句话说它依赖了一个异常路径的副作用。

### 1.2 正确契约（一处说清）

**展示给模型的路径 = workspace 相对路径**，与 `path=` 无关。两条路径都必须把「相对搜索目标的路径」换成「相对 workspace 的路径」：
```python
root_rel = target.relative_to(workspace)          # "." 或 "sub"
display  = root_rel / rel_from_target             # "sub/b.py"
absolute = target / rel_from_target               # 读文件用
```
`target == workspace` 时 `root_rel == "."`，退化成原行为——**这就是为什么必须补带 `path=` 的用例**。

### 1.3 影响面

| 文件 | 改动 |
|---|---|
| `tools/grep_tool.py` `_walk` | 返回 `(root / rel, root_rel / rel)`；root==workspace 时 `root_rel` 归一为 `""` |
| `tools/grep_tool.py` `_grep_with_rg` | 不再 `resolve()`；直接用 rg 的 `path.text`，拼上 `root_rel` |
| `tests/integration/test_r17_parity.py` | **新增**带 `path=` 的双跑一致性用例（本机无 rg 时自动 skip） |
| `tests/test_grep_tool.py` | **新增**兜底路径带 `path=` 的用例（不需要 rg） |

### 1.4 RIPR
- **Red**：`test_the_fallback_searches_the_directory_it_was_pointed_at` —— `path="sub"` 必须命中 `sub/b.py`，且输出前缀是 `sub/b.py` 而非 `b.py`
- **Red**：`test_a_scoped_search_reports_workspace_relative_paths`（rg 分支，注入 runner）
- **Red**：`test_both_runners_agree_on_a_scoped_search`（skipif 无 rg）
- **Pass**：`scripts/r18_acceptance.py` 由 31/32 → 32/32

---

## 2. F2 · HIGH — `IMAGE_MIMES` 是 import 期快照，「单一真相源」只做了一半

### 2.1 先说清楚严重性：**这是潜在漂移，不是现网缺陷**

`IMAGE_MIMES = frozenset(_message.IMAGE_MIME_BY_SUFFIX.values())` 在 import 时求值；`_allowed_mime()` 每次调用实时读表。改**源码**里的表后重启进程，两者同时更新——**现网没有能触发分叉的场景**。

但它让"F16 的单一真相源"这个说法只成立一半，而且给后人留了一条极窄的路（任何在进程存活期内改表的机制都会让粘贴接受、`load_image` 拒绝）。

### 2.2 改动
- **删掉 `IMAGE_MIMES` 常量**，`load_image` 直接用 `_allowed_mime()` 的键。
  - 成本：实测 `_allowed_mime()` 2.5µs/次；`load_image` 每请求每图一次，一次回合个位数毫秒以内。
  - 收益：仓库里**不再有任何第二份 mime 集合**，`grep -rn "image/png" ai_coding/` 只会命中那一张表。

### 2.3 RIPR
- **Red**：`test_no_second_copy_of_the_image_mime_set` —— 断言 `ai_coding/` 下没有第二处硬编码的 mime 集合（结构性守卫，与 ignore 清单那条同风格）
- **Red（改写）**：`test_a_new_suffix_reaches_the_paste_whitelist` 改为同时断言 `load_image` 的白名单（现在会随表变）
- **Refactor**：同步 `__all__` 与 `docs/data-contracts.md` 里那条「⚠️ 已知缺口」

---

## 3. F3 · HIGH — 图片对压缩预算贡献 0 token

### 3.1 病灶

`context_compression.py` 的四处计费（`:240/249/259` 用 `_text_bytes`，`:611` 用 `estimate_tokens(m.content)`，`:527/551/578` 用 `estimate_tokens(_joined_history_text(...))`）**全部只读文本**。而 `openai_engine` 在**每一次** `engine.step` 都把 `ImageRef` 重新 base64 成 data URL。

触发：粘一张 5MB 截图 → 粘贴允许（上限 20MB）→ 之后每步、每次重试都重发 ~7MB base64，而预算恒为 0 → **压缩永不触发**，直到 hard-trim 恰好丢掉那条消息，或 provider 先 400。

### 3.2 改动

**(a) `ImageRef` 记录字节数**——在**已经知道**的地方记，不在计费路径做 I/O：
- `attachments.save_data_url`：`len(raw)` 已有
- `AgentLoop._extract_image_mentions`：已在调 `is_file()`，加一个 `stat()` 是免费的
- `_message_to_dict/_from_dict`：沿用"只在非空时写键"；**旧 session 无此字段 → 默认 0 → 行为不回退**（退化成今天的"免费"）

**(b) 压缩预算按 `bytes // 4` 计**（base64 约 4/3 膨胀，`//4` 是线上体积的保守下界——宁可高估触发提前压缩，不可低估到炸请求）：
```python
def _image_tokens(messages: list[ChatMessage]) -> int:
    return sum(ref.bytes // 4 for m in messages for ref in m.images)
```
一处 helper，四个计费点统一改调 `estimate_tokens(...) + _image_tokens(...)`。

### 3.3 RIPR
- **Red**：`test_a_large_image_stops_being_free_in_the_budget` —— 一条带 5MB 图的消息，`estimate` 必须 ≥ 1.3M
- **Red**：`test_a_text_only_message_costs_what_it_costed_before` —— 回归守卫
- **Red**：`test_an_old_image_ref_without_bytes_is_not_charged` —— 旧 session 兼容
- **Red**：`test_the_pasted_size_is_recorded_on_the_ref` / `test_the_referenced_size_is_recorded_on_the_ref` —— 两条产出路径都记
- **Refactor**：`ImageRef.bytes` 的注释写清「不在计费路径 stat」的理由

---

## 4. F5 · HIGH — 前端猜"是不是命令"，而命令词汇表本来就有两份

### 4.1 病灶（三层，且最深的那层先于 r18 存在）

```
后端 app/slash.py::_ALIASES      ← 谁是命令由它说了算（parse_slash 白名单）
前端 util/slash.ts::SLASH_COMMANDS ← 手写子集，给补全用
本轮 buildSubmission             ← 第三种规则：text.strip().startsWith("/")
```
`commands.py:109` 的 `stripped.startswith("/")` 只是**未知命令的分支**，真实裁决是 `parse_slash`。所以：

```
"/path/to/file.py" → 前端：命令，扣下图片并提示「命令不发送图片」
                   → 后端：普通文本，照常发给模型（但没有图）
```
用户被告知"这是命令不发图"，实际模型收到一条无图的裸文本——**与我这一路消灭的"用户/模型看到的事实与实际不一致"同类**。

### 4.2 改动：后端暴露命令表，前端不猜

| 文件 | 改动 |
|---|---|
| `server/handlers.py` | 新增 `slash.list` → `{"commands": ["help", "undo", …]}`（`app/slash.py` 已有 `_ALIASES`，数据源就是它） |
| `app/slash.py` | 导出一个 `command_names()`（单一出处） |
| `frontend/src/app.tsx` | 与 `skills.list` / `files.find` 同频拉取 `slash.list` |
| `frontend/src/util/slash.ts` | **删掉**手写的 `SLASH_COMMANDS` 词汇表，改用拉取结果（补全仍需要标题/描述字段，可由后端一并给出） |
| `frontend/src/util/submission.ts` | `buildSubmission(commands, sessionId, text, images)`——按**名字**判定，不再猜前缀 |
| `frontend/src/routes/session.tsx` / `home.tsx` | 把命令表传下去 |

> `slash.list` 是否也要给出 title/description（补全要用）？若给，则前端 `slash.ts` 的**词汇**彻底搬走后只剩排序/渲染逻辑；若只给名字，补全文案还得留在前端——那还是两份。本轮**给全量字段**，让 `slash.ts` 退化为纯渲染。

### 4.3 RIPR
- **Red**：`buildSubmission` 用名字判定——`/path/to/file.py`、`/unknown`、`/` 都不是命令（与后端一致）；`/help`、`/undo` 是
- **Red（Python）**：`slash.list` 返回的名字集合 == `parse_slash` 能认出来的集合（后端自证）
- **Red**：`test_the_command_rule_mirrors_the_backend` 改为**直接拿后端表**驱动，不再在测试里复述规则

---

## 5. 不在本轮范围（已确认但另议）

| # | 问题 | 理由 |
|---|---|---|
| M1 | `restore()` 返回"尝试过"而非"还原了"的路径 | 需区分 partial failure 的报告形状，另议 |
| M2 | undo partial failure 时状态不自洽 | 同上 |
| M3 | 第二次 `/undo` 静默覆盖 redo 槽 | 语义问题（是否该支持 OC 的"合并成宽回退"），需拍板 |
| M4 | `redo_last_turn` 在 `snapshots is None` 时谎报成功 | 与 undo 不对称，顺手可修（一行），但属 M1 一组 |
| M5 | `walk_workspace` 只对**文件**应用 gitignore；LIFO 让 `files.find` 先填靠后子树 | 需改遍历结构，`files.find` 的 `limit` 语义一起看 |
| M6 | `webfetch` 输出无字符上限 | 需定一个数字并写进契约 |
| M7 | handlers 静默丢弃被拒图片而 transcript 已画 | 需要"被拒"事件进 IPC，前端改动 |
| M8 | rg 与兜底在**文件大小/二进制嗅探**上不同形 | 与 F10 同族，需先定 rg 侧的参数 |

---

## 6. 打勾清单

- [x] **F1** grep 两条路径的路径归一（兜底 + rg），补 `path=` 用例；验收脚本 32/32
- [x] **F2** 删 `IMAGE_MIMES`，`load_image` 直接用 `_allowed_mime()`；同步契约文档
- [x] **F3** `ImageRef.bytes` + 压缩预算按体积计（含旧 session 兼容）
- [ ] **F4** ~~两条 harness user 消息~~ —— **已证伪，不改**（见 §0）
- [x] **F5** `slash.list` IPC + 前端不猜（删除前端手写词汇表）
- [ ] `scripts/r18_acceptance.py` 全绿
- [ ] `docs/data-contracts.md` 去掉本轮已修条目的「已知缺口」
- [ ] 全门禁：pytest / mypy / ruff / bun / tsc

## 7. 修完后的终态自检

1. `grep(pattern, path="sub")` 在有/无 rg 的机器上都返回 `sub/b.py:1: …`。
2. `grep(pattern)`（不带 path）行为与今天逐字节相同（`root_rel` 退化为 `.`）。
3. 往 `IMAGE_MIME_BY_SUFFIX` 加 `.bmp`：粘贴接受、`@x.bmp` 被提取、`load_image` 放行——**三处同时**，且仓库里没有第二份 mime 集合。
4. 粘一张 5MB 图后，压缩预算立刻把它算进去（≥1.3M token），不再每步白送 7MB 却永不压缩。
5. 一个不含 `bytes` 的旧 session 打开：图片照常显示，预算回到今天的行为（不回归）。
6. 输入 `/path/to/file.py` 带图：按**普通文本**发给模型，图片跟着走；输入 `/undo` 带图：图片退回 composer 并说明。
7. 前端 `util/slash.ts` 里不再有任何命令名字面量。