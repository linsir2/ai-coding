# r18 — r17 P1 验收缺陷修复方案（20c1468 review）

> **执行约定**：每完成一项，在该节的 `- [ ]` 上打勾并提交一次。测试先行（RIPR），只改根源，最小化。

**背景**：`20c1468` 的集成验收（`tests/integration/test_r17_dataflow.py`，13/13 通过）证明五项功能主链路闭环，但 16 条边界缺陷成立，其中 2 条为安全边界。本方案逐条给出影响面、复用契约、分步实现、数据流影响与验收用例。

**拍板结论（6 项）**

| 决策 | 结论 | 依据 |
|---|---|---|
| SSRF 重定向 | **httpx + 项目内逐跳重校验**（httpx 官方 `follow_redirects=False` 手动循环，复用 `_is_public_url`） | 调研 6 个候选无一可用；实测 **drawbridge 0.1.2 的 HTTPS 全崩**（`_transport.py:49` 把 `bytes` 塞进 httpx 的 `sni_hostname` 扩展，降级 httpx/httpcore 仍复现；http 正常）→ 开源方案作废，改用已实测通过的 httpx 手动循环 |
| undo 删文件 | 按 OC 方式接线 `ToolArtifact` | OC 官方："只还原归属于已完成 assistant step 的路径"；`revert.ts` 收集 `part.type=="patch"` 后 `Snapshot.revert(patches)`。我们已有该契约，只缺生产者 |
| undo 回合编号 | 回退 `turn_count` + 裁该轮 digest | 保证 working-memory 不出现两条"第 N 轮" |
| grep 同形 | 全对齐 + 引入 pathspec | pathspec 月下载 5.05 亿，已在环境里；rg 的忽略语义以此为准 |
| Home 图片 | 支持附件透传 | OC 是单入口故无此问题；我们的 Home 是自有第二入口，静默丢数据是 bug |
| undo 告知 | 后端补 `reason` + TUI 打印 | 契约已写明"明说"，当前未实现 |

**⚠️ 记录在案的踩坑**：drawbridge 0.1.2 实测 HTTPS 全崩（`_transport.py:49` 的 `request.extensions["sni_hostname"] = hostname.encode("idna")` 塞 bytes，anyio `idna2008_resolve()` 期望 str）。降级 httpx 0.27.2 + httpcore 0.18.0 仍复现 → 是包本身的 bug，不是版本不兼容。它能拦住重定向到私网（已实测），但 80% 的网页是 HTTPS，采用即等于 webfetch 对多数站点失效。**"引入成熟库更安全"在这里不成立，库本身比 20 行自己的代码风险更高。**

---

## 0. 执行范围与顺序

```
F1 SSRF（httpx 逐跳守卫）      ─┐ 安全边界
F2 _load_image 沙箱收口          ─┘
        ↓
F3 synthetic 标记 + turn_count 回合键   ← 根因，后续 undo 类修复的前置
        ↓
F4 ToolArtifact 接线      F5 redo 失效 + 按会话隔离
F6 快照基线 all-or-nothing F7 undo 走 turn_lock   F8 reason 字段
        ↓
F9 grep 假答案 + 阻塞      F10 grep 两路同形（pathspec）
F11 htmltext embed         F12 websearch 形状与上限
        ↓
F13 Home 附件透传          F14 slash 命令图片不渲染
        ↓
F15 工作区文件枚举单一真相源（被 F10/F13 前置依赖，最后收敛）
F16 mime 表合并为一处投影
```

**统一不动的部分**（明确排除，避免范围蔓延）：`websearch` 的 HTTP 客户端（它的 host 是模块常量，模型无法控制 URL，**无 SSRF 面**，不动）；压缩/工作记忆本体（2 个 `test_loop_digest` 基线红与 r17 无关，不在本轮）；bash 工具的写入溯源（OC 同样不做，见 F4）。

---

## 1. 复用契约表（动手前必须知道的东西）

| 契约 | 位置 | 本轮怎么用 |
|---|---|---|
| `Sandbox.resolve(path)` | `security/sandbox.py:22` | F2 直接复用，**不自己写 resolve+relative_to** |
| `ToolArtifact(path, kind, bytes, description)` | `domain/message.py:45` | F4 接线（已存在，零生产者） |
| `ToolResult.artifacts` | `domain/message.py:87` | F4 write/edit 产出 |
| `TurnCloser.artifacts` | `core/turn_close.py:154` | F4 已收集，验证链路 |
| `SessionData.turn_count` | `domain/session.py:137` | F3 回合键真相源（`source_turn`/`last_compacted_turn` 已用它） |
| `user_message()` 工厂 | `domain/run.py:20` | F3 加 `synthetic` 关键字（唯一 user 消息入口，6 处调用） |
| `AgentLoop._turn_lock` | `core/agent_loop.py:279` | F7 undo/redo 复用（OC 对应 `assertNotBusy`） |
| `_message_to_dict` "只在非空时写键" | `domain/session.py:51-75` | F3/F4 新字段一律遵此惯例 |
| `_is_public_url()` | `tools/webfetch_tool.py:36` | F1 保留为前置（复用，不删） |
| `_VOID`/`_SKIP`/`_BOILERPLATE` | `tools/htmltext.py:20-28` | F11 只改 `_VOID` 一处 |
| `gitcmd.git()` | `core/gitcmd.py` | F6/F5 复用，唯一 git 调用形状 |

---

## 2. F1 — webfetch SSRF：httpx + 逐跳重校验

### 影响面
| 文件 | 现状 | 改动 |
|---|---|---|
| `tools/webfetch_tool.py` | stdlib urllib；`_is_public_url` 只查首跳；`max_bytes` 读完才判 | `_urllib_fetch` → `_http_fetch`（httpx 异步 + 手动跳 + 流式限长）；`execute` 兼容同步/异步 opener |
| `pyproject.toml` | 无 httpx | 加 `httpx>=0.27` |
| `tests/test_webtools.py` | 同步 opener 注入 | **一行不改**（`execute` 对 awaitable 与普通返回值都支持） |
| `tests/integration/test_r17_defects.py` | 绊线 1 条 | 重写为真正的"公网→私网"重定向场景并摘 xfail |

### 分步
1. **`opener` 契约扩展为"同步或异步皆可"**：`execute` 里 `outcome = self._opener(url, timeout)`，`inspect.isawaitable(outcome)` 则 await。
   - 理由：生产默认是 async（httpx 原生），测试注入是 sync（既有 6 个测试不改）。这是复用既有 seam，不是加第二个 seam。
2. **`_http_fetch(url, timeout, *, max_bytes, max_redirects, send=None)`**：
   ```
   async with httpx.AsyncClient(follow_redirects=False) as client:
     for _ in range(max_redirects + 1):
       ok, why = _is_public_url(url)          # 每跳都查（复用现有函数）
       if not ok: raise FetchRefused(why)
       status, headers, chunks = await send(client, url)   # 单跳，不跟
       if 3xx and Location: url = urljoin(url, location); continue
       边收 chunk 边计长，> max_bytes 立刻 raise FetchRefused（不读完）
     raise FetchRefused("too many redirects")
   ```
3. **`send` 是唯一的内部注入点**（`_httpx_send` 为默认）：让"跳"这一层可测。对应 grep 的 `runner` 注入风格。
4. **去掉 `asyncio.to_thread`**（httpx 原生 async）与 `webfetch_tool.py:145` 的事后长度判断。

### 数据流影响
`模型 → _is_public_url(每跳) → httpx(单跳/不自动跟) → 流式限长 → htmltext → 输出`。守卫从 1 次变每跳 1 次。

### 完成后效果
302→127.0.0.1 被拒；>5MB 在读取中截断；HTTP/HTTPS 均正常。

### RIPR 用例
- **Red**：`test_a_redirect_into_a_private_address_must_be_refused` —— 注入 `send`：第一跳返回 `302 Location: http://127.0.0.1:8080/latest/meta-data/`，断言**内网地址一次都没被请求**且工具报 refused。
- **Red**：`test_max_bytes_stops_the_read_mid_stream` —— 注入 10MB 的 chunk 流，`max_bytes=1024`，断言 refused 且**已收字节 ≤ 1MB**。
- **Red**：`test_too_many_redirects_is_refused` —— 无限 302，断言 refused 而非死循环。
- **Pass**：既有 `tests/test_webtools.py` 6 条**保持绿**（opener 兼容）。
- **Refactor**：`FetchRefused` 统一带 reason，`execute` 只有一条 except。

## 3. F2 — 图片读出口收口（沙箱 + mime + 尺寸）

### 3.1 现状与病灶

```python
# agent_loop.py:1289-1295现状
def _load_image(self, ref: ImageRef) -> str | None:
    try:
        raw = (Path(self.workspace).expanduser() / ref.path).read_bytes()   # ← 病灶
    except OSError:
        return None
    return f"data:{ref.mime};base64,{base64.b64encode(raw).decode('ascii')}"
```

三个缺口，全部集中在这 7 行里：

| 缺口 | 后果 | 实测证据 |
|---|---|---|
| 无路径包含性检查 | `Path("/ws") / "/etc/passwd"` == `/etc/passwd`（Python 语义），session JSON 里的任意 `path` 被读成 base64 外发给模型厂商 | `/etc/hostname` → `data:image/png;base64,bG9jYWxob3N0Cg==` |
| 无 mime 校验 | `session.py:33` 缺 mime 默认 `""` → 拼出 `data:;base64,...`，是非空串 → mapping 走 image_url 分支 → **provider 400 整轮**，且 replay 永久毒化 | `data:;base64,iVBOR...` 进入 content parts |
| 无尺寸上限 | `@huge.png` 无paste 时的 20MB 限制；60MB 文件 → 80MB base64 塞进单个请求 | 未设限 |

**并且与既有代码自相矛盾**：`_extract_image_mentions`（`agent_loop.py:1279-1284`）**已经**做了 `resolve()` + `relative_to()` + `is_file()` 检查。同一个类里两套边界规则，一套有一套没有。

### 3.2 影响面（执行范围）

| 文件 | 现状 | 改动 | 为什么必须动 |
|---|---|---|---|
| `ai_coding/attachments.py` | 只有 `save_data_url`（写路径）+ `_ALLOWED_MIME` + `DEFAULT_MAX_BYTES` | 新增 `load_image(workspace, ref, *, max_bytes)` | 模块职责已是「图片在磁盘上」，读路径属于它；放这里 `DEFAULT_MAX_BYTES` 与白名单是就近的唯一使用点 |
| `ai_coding/core/agent_loop.py:1289` | 7 行裸读| 缩成 3 行委派 | 它只剩「把 workspace 交给 attachments」，不再自己拼路径 |
| `ai_coding/ai/mapping.py` | 不变 | — | 它只管 content parts 形状，不该知道文件 |
| `ai_coding/domain/session.py` | 不变（**明确不改**，见 3.6） | — | — |
| `pyproject.toml` | 不变 | — | 复用 `Sandbox`，不新增依赖 |

**不动**：`process_input(images=)` 的签名（`ImageRef` 由调用方构造，但字节只从 `load_image` 出）、engine/mapping 的 image_loader 契约（形状不变）。

### 3.3 复用的契约（不造轮子）

| 复用 | 位置 | 用法 |
|---|---|---|
| `Sandbox.resolve()` | `security/sandbox.py:22` | 它内部已 `resolve()`（跟随符号链接）+ `relative_to()` + 抛 `SandboxViolation`。**不自写 resolve/relative_to** |
| `SandboxViolation` | 同上 | 与 `OSError` 一起捕获 |
| `IMAGE_MIME_BY_SUFFIX` | `domain/message.py` | 白名单 = `frozenset(IMAGE_MIME_BY_SUFFIX.values())`，**不新建第三张 mime 表** |
| `DEFAULT_MAX_BYTES` | `attachments.py:29` | 粘贴与读取共用同一预算 |

`Sandbox` 每张图构造一次（`__init__` 只做一次 `.resolve()`）。一张图一次系统调用量级，与「每个工具各自持有 Sandbox」（`builder.py:113`）的既有做法一致，不引入新的生命周期状态。

### 3.4 每文件每步

**Step 1 — `attachments.py` 新增读出口**
```python
def load_image(workspace, ref, *, max_bytes: int = DEFAULT_MAX_BYTES) -> str | None:
    """Read one message image as a data URL, or None when it may not be read."""
    if ref.mime not in IMAGE_MIMES:          # 白名单一张表
        return None
    try:
        target = Sandbox(workspace).resolve(ref.path)   # 复用沙箱
        if target.stat().st_size > max_bytes:           # 先 stat 再读，不把 60MB 读进内存
            return None
        raw = target.read_bytes()
    except (OSError, ValueError, SandboxViolation):
        return None
    return f"data:{ref.mime};base64,{base64.b64encode(raw).decode('ascii')}"
```
导入：`from ai_coding.domain.message import IMAGE_MIME_BY_SUFFIX, ImageRef`、`from ai_coding.security.sandbox import Sandbox, SandboxViolation`。
无循环依赖：`attachments → domain.message` 与 `attachments → security.sandbox`，两者都不反向依赖 core。

**Step 2 — `agent_loop.py` 改为委派**
```python
def _load_image(self, ref: ImageRef) -> str | None:
    """The one place image bytes leave the workspace (r18/F2)."""
    return load_image(self.workspace, ref)
```
`_load_image` **保留为方法**：它是 `engine.step(image_loader=…)` 的命名注入点（`agent_loop.py:1126/1172`），也是集成测试的调用面。

**Step 3 — 清死导入**：`agent_loop.py` 若不再用 `base64` / `Path` 于此处，检查是否别处仍在用，ruff 兜底。

### 3.5 数据流影响

```
session.json images[].path
      │  ← 不可信（可被手工编辑/跨机同步）
      ▼
AgentLoop._load_image ──委派──► attachments.load_image(workspace, ref)
                                   ├ mime ∈ IMAGE_MIMES ?        ──否→ None
                                   ├ Sandbox.resolve(path)       ──越界→ SandboxViolation→ None
                                   ├ stat().st_size ≤ 预算 ?      ──超→ None
                                   └ read_bytes() → data URL
                                   ▼
mapping.to_chat_message  （loader 返回 None → 文本 part「（图片 X 无法读取）」）
                                   ▼
                              provider
```
失败一律返回 `None`，走**既有**降级路径，模型侧无感、请求不失败。数据流形状不变，只是多三道闸。

### 3.6 为什么不在 `session.py` 反序列化时丢弃非法条目

`session.py:37` 目前只校验 `item.get("path")` 存在。若在那里再加一层「path 必须像 workspace 相对路径」，就是**第二份边界规则**——正是本轮要消灭的东西。而且那里的判断只能看字符串形状，拦不住符号链接，拦不住实际读取。
**`_load_image` 是唯一的字节出口，在这里收口即 100% 覆盖**。session 里存了越界 path 的唯一后果是 UI 显示一张读不出来的图（不外泄）。
> 这条如果你不认同，请拍板——见问卷第 2 题。

### 3.7 完成后效果

| 场景 | 改前 | 改后 |
|---|---|---|
| `path="/etc/hostname"` | base64 外发 | `None` → 文本降级 |
| `path="../../../../etc/x.png"` | base64 外发 | `None` |
| 指向外部的符号链接 | base64 外发 | `None`（`Sandbox.resolve` 跟随后越界） |
| `mime=""`（缺字段） | `data:;base64,` → provider 400 | `None` → 降级，本轮正常 |
| `mime="image/bmp"` | 发出（mime 不在白名单） | `None` |
| 60MB 的 png | 80MB base64 进请求 | `stat` 阶段即拒 |
| 正常 workspace 内图片 | data URL | **不变** |

### 3.8 RIPR 用例

- **Red（摘 3 条既有绊线）**
  - `test_an_absolute_path_outside_the_workspace_must_not_be_read`
  - `test_a_dot_dot_path_outside_the_workspace_must_not_be_read`
  - `test_a_session_message_without_a_mime_must_not_produce_an_image_part`
  现状：全部 XPASS→摘标；改前应仍是 XFAIL（返回值非 None）。
- **Red（新增 3 条）**
  1. `test_a_symlink_out_of_the_workspace_is_refused`：workspace 内建 symlink 指向外部文件 → `None`。
  2. `test_an_image_over_the_budget_is_refused`：写一个超过 `max_bytes` 的文件 → `None`，且**不把文件读进内存**（断言 `stat` 早于 `read_bytes`，用注入的假 Path 难以断言则改为断言返回 None + 单元级 `_MAX` 常量一致）。
  3. `test_a_mime_outside_the_whitelist_is_refused`：`ImageRef(mime="image/bmp")` → `None`。
- **Pass**：既有 `test_image_input.py` 与集成闭环 `test_a_pasted_image_reaches_the_provider_as_an_image_part` 保持绿（正常图片必须仍然工作）。
- **Refactor**：`attachments.__all__` 补 `load_image`；`save_data_url` 与 `load_image` 共用 `IMAGE_MIMES`（F16 再合并扩展名表）。

### 3.9 架构图（F2 后）

```
                    ┌── 写：粘贴 data URL ──► save_data_url ──┐
                    │      （20MB + mime 白名单）              │
  TUI paste ────────┤                                        ├──► .ai-coding/attachments/<sha1>.<ext>
                    └── 引用：@shot.png ──► ImageRef ─────────┘            │
                                        │                                │
  session.json ──images[]──► _load_image ─┘(委派)                         │
                                   │                                      │
                                   ▼                                      │
                        attachments.load_image(workspace, ref)  ★F2      │
                          ① mime ∈ IMAGE_MIMES（domain 那一张表）           │
                          ② Sandbox.resolve(path)  ← 复用 security/sandbox │
                          ③ stat ≤ DEFAULT_MAX_BYTES  ← 复用 attachments   │
                          ④ read_bytes → data URL                          │
                                   │ None → 降级文本 part                   │
                                   ▼                                      │
                        mapping.to_chat_message ────────────────────────┘
                                   ▼
                              provider（只看到 workspace 内的、白名单内的、有预算内的图片）
```

### 3.10 最终形态自检

1. 用户粘贴截图 → 会话跑 20 轮 → replay → 图片仍能加载（mime/path 都在，预算内）。
2. 同事发来一个 session.json，`images[].path` 写成 `../../../../root/.ssh/id_rsa` → 打开会话时该图降级为文本提示，**不发任何字节**。
3. 同一 session 里一张 25MB 的图 → paste 时就被拒（20MB），`@` 引用时也被拒 → 用户看到「无法读取」，模型看到降级文本 → **不会**出现 provider 400。
4. `Sandbox.resolve` 抛 `SandboxViolation`（Exception，不是 OSError）→ 必须显式捕获，否则整轮崩。**这是最容易漏的一步，Step 1 的 except 已包含。**
5. 集成闭环里那张 1×1 PNG（68 字节）远小于预算 → 不受影响，正常产出 image part。
6. `_load_image` 变成一行委派后，`base64` 是否还在 `agent_loop.py` 别处使用？→ ruff 兜底，不留死导入。

## 4. F3 — 合成消息标记 + 回合键改用 turn_count（根因）

> 这一条一次修掉四个症状：undo 键错位、composer 拿到系统串、`<referenced-files>` 被顶掉、`turn_count` 不回退。

### 4.1 病灶：两个真相源在打架

`session.messages` 里，`role="user"` 同时意味着两种东西：

```
① 人在说话            703: user_message(user_input, …, images)
② harness 自己注入    485: decision.inject        （todo/acceptance 补问）
                      549: "只输出了思考内容…"     （thinking-only 重试）
                      610: decision.inject        （fatigue/strategy 补问）
                      745: "[steering] …"        （运行中插话）
                      1114: _CLOSING_PROMPT       （收尾用，从不落盘）
```

而**三处代码都假设 ①**：

| 读者 | 现状 | 后果 |
|---|---|---|
| `agent_loop.py:1305` | `turn = sum(1 for m in messages if m.role=="user")` | 回合号被合成消息撑大 |
| `agent_loop.py:1323/1332` | 取最后一条 `role=="user"` 当"这一回合" | 拿到的是 nudge；`before` 也被撑大 → **键错位 → `exists()` 落空 → 零文件还原却报 ok** |
| `request_assembly.py:387` | 同上 | `<requested-skills>` 与 `<referenced-files>` 在 nudge 之后消失 |

实测证据（`tests/integration/test_r17_defects.py`）：
```
无 nudge : files=['a.txt']  content='v1\n'          ✅
有 nudge : files=[]         content='TURN CHANGED IT' ❌  composer='[系统] 还有未完成的任务项，请继续。'
```

### 4.2 为什么回合键改用 `turn_count`（而不是"数真人消息"）

`session.turn_count`（`domain/session.py:137`）**已经是**回合语义的真相源：`source_turn`、`last_compacted_turn`、`<working-memory>` 表头、router 的 `record_load` 全读它。再数一遍消息就是**第二份真相源**——正是本轮要消灭的东西。

时序（`agent_loop.py:377` → `:703` → `:710`，中间无 `await`，原子）：

```
第 N 回合开始   _run_turn 第一行 _capture_snapshot   turn_count == N-1
               key = _turn_key(session, turn_count)  →  "<sid>/N"      ★capture
_begin_turn    add_message(真人) → turn_count += 1    turn_count == N
undo 时         最后一条真人消息属于第 N 回合
               key = _turn_key(session, turn_count-1) → "<sid>/N"      ★undo  对齐
```

`synthetic` 标记**只用于定位截断点与提及来源**，不参与算回合号——职责单一。

### 4.3 影响面（执行范围）

| 文件 | 现状 | 改动 |
|---|---|---|
| `domain/message.py` | `ChatMessage` 有 `images` | 加 `synthetic: bool = False`（同款位置） |
| `domain/run.py:20` | `user_message(content, ts, images)` | 加**关键字参数** `synthetic: bool = False` |
| `domain/session.py` | images/reasoning "只在非空时写键" | `synthetic` 只在 True 时写键 |
| `core/agent_loop.py` | 5 处合成注入均无标记 | 逐处标 `synthetic=True`（703 真人不动） |
| `core/agent_loop.py:1305` | 数 user 消息 | 删，改 `turn_count` |
| `core/agent_loop.py:1319-1333` | 取最后 user / 数 before | 跳过 synthetic；键改 `turn_count` |
| `core/agent_loop.py:1341-1350` | 只截 messages | 追加回退 `turn_count` + 裁 digest |
| `core/request_assembly.py:386-390` | 取最后 user | 跳过 synthetic |
| `core/context_compression.py:626` | 取最后 user 当 verbatim 尾部锚点 | 跳过 synthetic（你已拍板：同一类病，一次统住） |

**明确不动**：`ai/mapping.py`（不读该字段 → **对发给 provider 的报文零影响**）、`memory_search_tool.py:456`（你已拍板：harness 文本进记忆索引属召回质量，留作后续）。

### 4.4 复用契约（不造轮子）

| 复用 | 位置 | 用法 |
|---|---|---|
| `user_message()` 工厂 | `domain/run.py:20` | 全仓唯一的 user 消息入口（6 处调用），加一个关键字参数即覆盖全部注入点，**不新增第二个工厂** |
| `AgentLoop._turn_key` | `agent_loop.py:1260` | 已存在的键拼装（`f"{sid}/{n+1}"`），capture/undo 共用，不改签名 |
| `SessionData.turn_count` | `domain/session.py:137` | 回合真相源 |
| `SessionData.turn_digests` | `domain/session.py:138` | 有界环，直接按 `turn` 过滤 |
| `_message_to_dict` 惯例 | `domain/session.py:51-75` | "只在非空时写键"（images/reasoning 同款） |

### 4.5 每文件每步

**Step 1 — `domain/message.py`**：`ChatMessage` 末尾加
```python
#: r18/F3: True when the loop injected this "user" message (a nudge, a steering
#: line, the closing prompt) rather than the human typing.  Nothing on the wire
#: reads it; the runtime does, to tell "which turn did the user start" apart
#: from "what did the harness say during it".
synthetic: bool = False
```

**Step 2 — `domain/run.py`**：
```python
def user_message(content, timestamp, images=(), *, synthetic: bool = False) -> ChatMessage:
    return ChatMessage(role="user", content=content, timestamp=timestamp,
                       images=images, synthetic=synthetic)
```

**Step 3 — `core/agent_loop.py` 五处注入点**加 `synthetic=True`（485 / 549 / 610 / 745 / 1114）。

**Step 4 — 回合键**：
```python
async def _capture_snapshot(self, session):
    if self._snapshots is None: return
    try:
        await self.snapshots.capture(self._turn_key(session, session.turn_count))
        await self.snapshots.prune(session.session_id)
    except (OSError, TimeoutError, ValueError): pass

async def undo_last_turn(self, session):
    index = next((i for i in range(len(session.messages) - 1, -1, -1)
                  if session.messages[i].role == "user"
                  and not session.messages[i].synthetic), None)
    ...
    key = self._turn_key(session, max(0, session.turn_count - 1))
```

**Step 5 — undo 收回回合状态**：
```python
    session.messages = list(session.messages[:index])
    session.turn_count = max(0, session.turn_count - 1)
    session.turn_digests = [d for d in session.turn_digests if d.turn <= session.turn_count]
```

**Step 6 — 两个「最后一条 user」读者**跳过 synthetic：
```python
# request_assembly.py:386
for m in reversed(session.messages):
    if m.role == "user" and not m.synthetic:
        return (m.content or "").strip()

# context_compression.py:625
latest = max((index for index, m in enumerate(messages)
              if m.role == "user" and not m.synthetic),
             default=len(messages) - 1)          # 无真人消息时回落不变
```

### 4.6 为什么 `turn_count` 回退、`last_compacted_turn` 不动

| 状态 | 动作 | 理由 |
|---|---|---|
| `turn_count` | 回退 | "收回"语义：undo 掉第 N 轮后，下一条仍是第 N 轮。否则编号永久空洞，工作记忆说"第 N 轮"而实际是第 N+1 轮 |
| `turn_digests` | 裁掉该轮 | 否则工作记忆里留着已收回那轮的一行（你已拍板） |
| `last_compacted_turn` | **不动** | 压缩边界是物理事实（历史确实被重写过）；动它会让 `live_prompt_tokens()` 的"未压缩部分"算错 |
| `turn_usages` | **不动** | 成本账：钱已经花了，删行等于抹掉真实支出 |

### 4.7 数据流影响

```
用户输入 ──► user_message(synthetic=False) ──► session.messages
harness  ──► user_message(synthetic=True)  ──► session.messages
                                              │
                    ┌─────────────────────────┼──────────────────────────┐
                    ▼                         ▼                          ▼
        _extract_user_input            undo_last_turn            turn_count(=N)
        跳过 synthetic                 跳过 synthetic              不数消息
             │                              │                          │
      <referenced-files>            "这一回合"+ composer 文本     capture/undo 键
      <requested-skills>            文件还原
```
报文形态**不变**（`mapping` 不读该字段）；变的只是运行时"哪条是人在说话"的判断。

### 4.8 完成后效果

| 场景 | 改前 | 改后 |
|---|---|---|
| 回合触发 nudge 后 `/undo` | 文件零还原，composer 拿到「[系统] 还有未完成的任务项，请继续。」 | 文件还原，composer 拿回真人原话 |
| nudge 后本轮继续迭代 | `<referenced-files>` 消失 | 仍在（直到下一条真人消息） |
| undo 后再发消息 | 编号继续涨（空洞） | 重新是第 N 轮 |
| undo 后工作记忆 | 留着已收回那轮 | 该轮摘要被裁 |
| 真人消息 | 不变 | 不变 |

### 4.9 RIPR 用例

- **Red（摘 3 条既有绊线）**
  - `test_undo_must_work_after_a_nudge_was_injected`（同时断言 `files`、`a.txt` 内容、`text` 三项）
  - `test_undo_must_rewind_the_session_turn_counter`
  - `test_the_referenced_file_must_survive_a_nudge_in_the_same_turn`
- **Red（新增 4 条）**
  1. `test_steering_does_not_become_the_undo_subject`：注入 `[steering] …` 后 undo，composer 拿回真人原话。
  2. `test_synthetic_survives_a_restart`：落盘重载后 `_extract_user_input` 仍返回真人消息（证明 `synthetic` 真持久化了，而不是只在内存里有效）。
  3. `test_undo_trims_the_digest_of_the_undone_turn`：回合后 `turn_digests` 有第 N 行 → undo 后没有。
  4. `test_the_model_never_sees_the_synthetic_flag`：断言发给 provider 的 message dict 里没有 `synthetic` 键（证明零报文影响）。
  5. `test_compaction_tail_anchors_on_a_real_user_message`：nudge 在最后时，verbatim 尾部仍从真人消息开始（`context_compression.py:626`）。
- **Pass**：既有 `test_skill_reference.py`、`test_request_assembly.py`、集成闭环 `test_a_typed_mention_reaches_the_provider_without_a_tool_call` 保持绿。
- **Refactor**：`_turn_key(session, 0)` 与 `max(0, …)` 的边界各测一次（turn_count=0 时 undo 不得生成 `-1` 键）。

### 4.10 架构图（F3 后）

```
                    ┌──────────── AgentLoop ────────────┐
 真人输入 ──────────► _begin_turn: user_message(synthetic=False)
                    │                turn_count += 1        ← 回合号唯一真相源
                    │                     │
                    ├─► nudge/steering ──► user_message(synthetic=True)  ✦新增
                    │                     │
                    │  ┌──────────────────┴───────────────────┐
                    │  ▼                                      ▼
                    │ _capture_snapshot                  _extract_user_input()
                    │  key=turn_count+1                  最后一条 non-synthetic
                    │  → refs/ai-coding/undo/<sid>/N       → <referenced-files>
                    │                                      → <requested-skills>
                    │ /undo
                    │  截断点 = 最后一条 non-synthetic user
                    │  key=turn_count → 还原文件
                    │  turn_count -= 1 ; 裁该轮 digest      ✦新增
                    └──────────────────────────────────────┘
                              │ 不读 synthetic
                    mapping.to_chat_message → provider
                    （报文形态与 r17 完全一致，零变化）
```

### 4.11 最终形态自检

1. 用户发消息 → 模型跑 5 轮工具 → 触发 todo nudge → 用户 `/undo`：
   → 截断到最后一条**真人**消息；键 = `turn_count`；文件还原；composer 拿回用户原话；`turn_count` 回到上一轮；该轮 digest 消失。
2. undo 后用户改个措辞重发 → 新回合仍是同一个编号 → 工作记忆里只有一行该编号 → **不重复**。
3. 回合无任何注入 → 与 r17 行为逐字节一致（回归守住）。
4. 老 session 文件（无 `synthetic` 键）→ `_message_from_dict` 走 `.get` 默认 False → 历史里所有 user 消息都被当真人 → **undo 退化为 r17 的行为，不崩**。这是向后兼容的关键。
5. 回合在 `_begin_turn` 前崩掉 → `turn_count` 未增 → undo 取到上一轮真人消息的键 → 语义正确，多余 ref 交给 `prune`。
6. `turn_count=0` 时 undo → `max(0, -1)=0` → 键 `sid/1`；若无该 ref → `exists()` 落空 → 不还原（且 F8 会说明原因）。

## 5. F4 — ToolArtifact 接线（undo 只动模型写过的文件）

### 影响面
| 文件 | 现状 | 改动 |
|---|---|---|
| `tools/write_tool.py:108` | `ToolResult(success=True, output=...)` | 加 `artifacts=(ToolArtifact(path=rel, kind="file", bytes=len(content)),)` |
| `tools/edit_tool.py:120` | 同上 | 加 `artifacts=(ToolArtifact(path=rel, kind="file"),)` |
| `domain/run.py:64` `tool_message()` | 丢弃 artifacts | 带上（写入 `ChatMessage.artifacts`） |
| `domain/message.py` | 无 `artifacts` | 加 `artifacts: tuple[ToolArtifact, ...] = ()`（与 images 同款） |
| `domain/session.py` | 未序列化 | 只在非空时写 `artifacts` 键 |
| `core/agent_loop.py` `undo_last_turn` | `restore(key)` 全量删除推断 | 改为按 artifact 路径还原 |
| `core/snapshots.py:117` `restore` | `ls-files --others` 推断删除 | 改签名 `restore(key, *, created: set[str])`，**只删这个集合** |

### 还原语义（三分法，与 OC 一致）
| 文件来源 | 处理 | 依据 |
|---|---|---|
| 已跟踪文件被改（含 bash 改的） | `git checkout <ref> -- path` | 快照里**确实有**该文件 → 还原 |
| 模型 write/edit 新建的 untracked | 删除 | `artifacts` 里有它，且快照里没有 |
| 用户自己新建的 untracked | **保留** | artifacts 里没有它 |
| bash 新建的 untracked | **保留** | 保守默认；OC 同样不追踪 bash |

### 为什么删掉「基线推断删除」
`baseline`（`refs/ai-coding/untracked/<key>`）原本只为区分"回合建的 vs 用户原有的"。有了 artifacts，这个区分由**写入记录**直接给出，基线 blob 的推断既多余又是 H3/H4 两个缺陷的根源。**基线整套删除**（含 `snapshots.py:75-95,111-115,134-147` 与 F6 的一半）——这就是本轮要砍的冗余。

### 数据流影响
`write/edit → ToolResult.artifacts → TurnCloser.artifacts（既有）→ tool_message → ChatMessage.artifacts → 落盘 → undo 扫描尾巴 → restore(created=…)`。新增一个字段，链路每一环都已存在。

### RIPR 用例
- **Red**：绊线 `test_undo_must_not_delete_a_file_the_user_created` 去 xfail。
- **Red（新增）**：`test_undo_deletes_a_file_the_turn_created`——write 工具建文件后 undo，文件消失（证明能力没被砍掉）。
- **Red（新增）**：`test_artifacts_survive_a_restart`——artifacts 落盘重载后仍可读（undo 跨重启可用）。
- **Red（新增）**：`test_a_bash_created_file_is_left_alone`——bash 建文件后 undo，文件保留（保守默认被守住）。
- **Refactor**：`SnapshotStore.baseline_ref` 参数与其全部方法一并删除。

---

## 6. F5 — redo 失效 + 跨会话隔离

### 影响面
| 文件 | 改动 |
|---|---|
| `core/agent_loop.py:229` | `_undo_tail: list` → `dict[str, list[ChatMessage]]`（按 session_id） |
| `core/agent_loop.py:226` | `last_undo: dict` → `dict[str, dict]`（按 session_id） |
| `core/agent_loop.py:1297` `_capture_snapshot` | 开头 `_undo_tail.pop(session_id, None)` + 删 `<sid>/redo` 两个 ref |
| `core/server/handlers.py:415` | `loop.last_undo.get(session.id)` |
| `core/snapshots.py` | 新增 `forget(key)`（删 undo+baseline ref 两条命名空间的同一 key） |

### 为什么在 `_capture_snapshot` 里清
OC 文档：*"Sending a new prompt while an undo is staged **commits the revert**"*。而 `_capture_snapshot` 是 `_run_turn` 的第一行，正好是"新回合开始"的唯一钩子——不需要新状态机。

### RIPR 用例
- **Red**：绊线 `test_redo_after_a_new_turn_must_not_duplicate_history` 去 xfail。
- **Red（新增）**：`test_undo_in_session_a_does_not_leak_into_session_b`——A undo → B redo 报 `nothing to redo`（当前会把 A 的尾巴塞进 B）。
- **Red（新增）**：`test_last_undo_is_scoped_to_the_session_that_asked`。

---

## 7. F6+F7+F8 — 快照基线 all-or-nothing / 回合锁 / 告知字段

| 编号 | 文件 | 改动 | 用例（Red） |
|---|---|---|---|
| F6 | `snapshots.py:62` | `capture` 变成 all-or-nothing：任一 ref 写失败即回滚已写的 ref 并返回 `None`（随 F4 基线删除后自然消失大半，只保留"undo ref 写失败返回 None"） | `test_capture_returns_none_when_the_ref_write_fails` |
| F7 | `agent_loop.py:1312,1353` | `async with self._turn_lock:` 包住 undo/redo（对齐 OC `assertNotBusy`） | `test_undo_waits_for_a_running_turn`（回合持锁时并发 undo 阻塞） |
| F8 | `agent_loop.py:1344` + `app/commands.py:155` | `last_undo` 加 `reason`：`ok` / `not-a-repo` / `capture-failed` / `restore-failed` / `no-changes`；`commands.py` 按 reason 打不同中文 | `test_the_report_says_why_nothing_was_restored`（非 git 仓 → `reason == "not-a-repo"`） |

**F8 的 TUI 形态**（`console.info` 一行，不新增 UI）：
```
[undo] 回退 6 → 4 条，还原 2 个文件
[undo] 回退 6 → 4 条；该工作区不是 git 仓库，文件未还原
[undo] 回退 6 → 4 条；快照还原失败（detail），文件可能仍是改动后的状态
```

---

## 8. F9+F10 — grep：先修假答案与阻塞，再对齐两路

| 编号 | 文件 | 改动 |
|---|---|---|
| F9a | `tools/grep_tool.py:186` | 检查 `outcome.error` / `returncode is None`：runner 不可用 → **回落到 Python 兜底**（不是失败，因为兜底本就完整），两条都不行才失败 |
| F9b | `tools/grep_tool.py:140` | `return await asyncio.to_thread(self._grep_with_python, ...)` |
| F10a | 新模块 `core/workspace_files.py` | `iter_workspace_files(root) -> Iterator[Path]`：`os.walk` + `dirs[:]` 剪枝 + `pathspec` 读根 `.gitignore`。**grep 兜底与 `files.find` 共用**（F15） |
| F10b | `grep_tool.py:228` | `include` 改：`PurePosixPath(rel).match(include)`（`*` 不跨 `/`，对齐 rg） |
| F10c | `grep_tool.py` 渲染前 | 两个分支的命中都 `sort(key=(path, line))` **再**套 `limit`；尾注的 total 用排序后总数 |

### 明确不做的（避免过度）
- **嵌套 `.gitignore`**：只读搜索根一份，写进文档。rg 支持多层，自研会引入语义漂移。
- **rg 加 `--max-count`**：rg 的 `-m` 是 per-file 语义，与全局 limit 不同义，硬套会错。排序后截断即可保证两路同形。

### pyproject
加 `pathspec>=0.12`（环境已有，PyPI 月下载 5.05 亿）。

### RIPR 用例
- **Red**：绊线 2 条（假 `(no matches)`、阻塞事件循环）去 xfail。
- **Red（新增，双跑一致性）**：`test_both_runners_answer_the_same_question`——`@pytest.mark.skipif(not shutil.which("rg"))`，同一棵树分别跑两路，断言**输出逐行相同**。本机无 rg 时自动 skip，装了 rg 的机器上会真的抓住分歧。
- **Red（新增）**：`test_gitignored_files_are_not_searched`——`.gitignore` 写 `*.log`，两路都不得命中 `x.log`。

---

## 9. F11–F14 — 清洗、websearch、前端

| 编号 | 文件 | 改动 | 用例（Red） |
|---|---|---|---|
| F11 | `htmltext.py:22` | `_VOID` 增加 `embed`（它是 void 元素却在 `_SKIP`，抑制计数器永不归零） | 绊线去 xfail |
| F12a | `websearch_tool.py:125` | `results` 非 list / 元素非 dict → `success=False` + 明确原因 | 绊线去 xfail |
| F12b | `websearch_tool.py:92` | `limit = min(max(1, limit), 20)` | 绊线去 xfail |
| F13 | `app.tsx:290` `home.tsx:43` `session.tsx` | `startSession(text, images)`；`pendingPrompt` 改 `{text, images}`；会话路由消费后清空（与现有 pendingPrompt 生命周期合并） | 前端：Home 提交带 images → `startSession` 收到 images（`frontend/test/`） |
| F14 | `session.tsx:150` | `method` 先算，`apply({kind:"submitted", text, images})` 只在 payload 真带 images 时传 images —— **所见即所发** | 前端：slash 提交不渲染 image part |

**F13 的生命周期**：`pendingPrompt` 目前是 `createSignal<string|null>`。改成对象后，`session.tsx` 现有"消费后清空"的代码路径不动，只是取值方式变 `pending().text` / `pending().images`。

---

## 10. F15+F16 — 真相源收敛

| 编号 | 改动 |
|---|---|
| F15 | 新模块 `core/workspace_files.py` 持有 `SKIP_DIRS` + `iter_workspace_files()`；`grep_tool` 与 `handlers.files.find` 都改为调用它，**删除两份副本**。`files.find` 同时从 `rglob("*")+sorted()` 换成带剪枝的 `os.walk`（现在先全量物化再撞 20k 上限，实测 8k 树枚举 8068 条而计数器只到 228） |
| F16 | `attachments._ALLOWED_MIME` 改为从 `domain.IMAGE_MIME_BY_SUFFIX` 派生：`{mime: suffix.lstrip(".") for suffix, mime in IMAGE_MIME_BY_SUFFIX.items()}`。两张表合一，`message.py` 里"One table, two readers"才成立 |

---

## 11. 修完后的数据流 / 架构图

```
                        ┌──────────────── 提交入口（两个，都带附件）──────────────┐
   TUI Prompt ──paste/@──┤ Home: startSession(text, images) → pendingPrompt       │
                        │ Session: session.prompt / session.command（slash 不带图）│
                        └────────────────────────┬───────────────────────────────┘
                                                 ▼
   handlers ──files.find────────► core/workspace_files.iter_workspace_files()  ★F15 单一真相源
                                  (os.walk 剪枝 + pathspec 读 .gitignore)
   handlers ──session.prompt───► attachments.save_data_url ─► ImageRef
                                                 ▼
  ┌────────────────────────── AgentLoop.process_input ───────────────────────────┐
  │ _capture_snapshot:  key = "<sid>/<turn_count+1>"        ★F3 回合键真相源      │
  │     ⤷ 同时作废本会话的 redo（_undo_tail.pop + 删 redo ref）  ★F5             │
  │ _begin_turn: user_message(text, images)  real  /  synthetic=True           │
  │     turn_count += 1                                              ★F3        │
  │ 循环每轮: RequestAssembler.assemble                                        │
  │     ├ <requested-skills>    ← _extract_user_input() 跳过 synthetic  ★F3      │
  │     ├ <referenced-files>    ← 同上（同一个真相源）               ★F3        │
  │     └ volatile tail                                                      │
  │ 工具: write/edit ─► ToolResult.artifacts ─► tool msg ─► 落盘    ★F4        │
  │       grep  ─► rg(--json) │ os.walk+pathspec      两路排序后截断  ★F9★F10    │
  │       webfetch ─► _is_public_url ─► drawbridge ─► httpx(手动跳)  ★F1        │
  │       websearch ─► urllib POST 常量 host（无 SSRF 面，不动）                │
  │ _load_image: Sandbox.resolve + mime 白名单 + 尺寸上限         ★F2          │
  └──────────────────────────────┬───────────────────────────────────────────────┘
                                 ▼ mapping.to_chat_message → content parts → provider
                                 ▼ SessionService.save（synthetic / artifacts / images）
                                 ▼
   /undo:  _turn_lock 内 ★F7
           定位最后一条**真人** user 消息                     ★F3
           key = "<sid>/<turn_count>" ; 不存在 → reason=not-a-repo/capture-failed
           截断 messages + turn_count -= 1 + 裁该轮 digest   ★F3
           restore(key, created=该回合 artifacts 的路径)     ★F4
              ├ 快照里有 → git checkout 还原（bash 改的也算）
              └ 只有 artifacts 里有 → 删除
                 其余 untracked（用户的、bash 的）→ 一律保留   ★F3
           /redo: 新回合已作废 ★F5 ；按 session_id 隔离 ★F5
           reason 字段 → console.info 打印人话                ★F8
```

---

## 12. 最终形态自检（设想全部修完后，检查中途细节是否成立）

1. **用户在脏树上工作**：非 git 也能 undo（只截历史，`reason=not-a-repo` 说清文件没动）。
2. **回合中模型写入 `a.py` + `new.py`，用户同时手建 `note.md`** → undo：`a.py` 还原、`new.py` 删、`note.md` **在**。这正是 H3 的目标形态。
3. **回合触发 R6b 疲劳 nudge 后 undo** → 键对齐（`turn_count`），文件还原，composer 拿到**真人的那句话**，`<referenced-files>` 仍在本轮有效。
4. **undo 后不重新提问，直接发新消息** → redo 自动失效（TUI 打印"已无可恢复"），不会出现陈旧尾巴叠加。
5. **两个会话交替 undo/redo** → 互不串（`last_undo` 按 session 取）。
6. **webfetch 抓一个 301 到 https 的正常站点** → 跟到 https 仍合法，正常返回；抓一个 302 到 `169.254.169.254` → 两层守卫都拒。
7. **drawbridge 被升级且防护失效** → `test_the_ssrf_library_still_blocks_a_redirect_into_a_private_address` 红，CI 拦住，**不是**用户内网被读。
8. **装了 rg 的机器与没装的机器跑同一个 grep prompt** → 输出逐行相同（双跑一致性测试守住）。
9. **session.json 里有一张 60MB 的图**（外部导入）→ `_load_image` 因尺寸上限返回 None → 降级成文本提示，本轮请求正常，**不会**把 80MB base64 塞给 provider。
10. **用户在首页粘贴图片回车** → 附件随会话创建后的首轮发出，零丢失。

**未覆盖、明确留作已知限制**（写进 `docs/data-contracts.md`）：
- 嵌套 `.gitignore`（只读根一份）；rg 与兜底仍可能逐字节不同（rg 有索引）。
- bash 工具写入的新建文件不会被 undo 删除。
- 剪贴板图片在 tmux 里无法真机验证（无剪贴板）。
- 2 个 `test_loop_digest` 基线红与本轮无关。

---

## 13. 打勾清单

### 安全边界
- [x] **F1** webfetch 换 httpx：逐跳 `_is_public_url` 守卫 + `max_bytes` 边读边限 + 跳数上限
- [x] **F2** `_load_image` 复用 `Sandbox.resolve` + mime 白名单 + 尺寸上限

### undo 正确性
- [x] **F3** `synthetic` 标记（4 处注入点 + 落盘）+ 回合键改 `turn_count` + undo 回退 `turn_count` 并裁 digest
- [ ] **F4** `ToolArtifact` 接线：write/edit 产出 → 落盘 → `restore(created=…)`，删掉基线推断
- [ ] **F5** 新回合作废 redo + `_undo_tail`/`last_undo` 按 session 隔离
- [ ] **F6** `capture` all-or-nothing（写失败即回滚并返回 None）
- [ ] **F7** undo/redo 走 `_turn_lock`
- [ ] **F8** `report.reason` + `commands.py` 按 reason 打印

### 工具正确性
- [ ] **F9** grep runner 失败不再报 `(no matches)`；兜底 `to_thread`
- [ ] **F10** grep 两路同形：pathspec + `PurePosixPath.match` + 排序后截断 + 双跑一致性测试
- [ ] **F11** `_VOID` 加 `embed`
- [ ] **F12** websearch 形状校验 + `max_results ≤ 20`

### 前端与真相源
- [ ] **F13** Home 附件透传（`pendingPrompt` 带 images）
- [ ] **F14** slash 命令不渲染未发送的 image part
- [ ] **F15** `core/workspace_files.py` 单一真相源，`files.find` 换 `os.walk`
- [ ] **F16** mime 白名单从 `IMAGE_MIME_BY_SUFFIX` 派生

### 收尾
- [ ] `tests/integration/test_r17_defects.py` 全部 xfail 摘除并转绿
- [ ] `docs/data-contracts.md` 更新 r17 契约 + 记录已知限制
- [ ] 全门禁：pytest / mypy / ruff / bun / tsc