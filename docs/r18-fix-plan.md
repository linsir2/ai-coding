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

## 5. F4 — ToolArtifact 接线：undo 只动模型写过的文件

### 5.1 病灶：一份悬空的契约

`ToolArtifact`（`domain/message.py:45`）的文档写着 *"a created/changed file … lets the runtime, UI and session records describe what a tool actually did without parsing prose"*。它甚至**自带 `to_dict` / `from_dict` 往返**（`:57-72`）——往返契约都写好了。

但全仓：

| 环节 | 状态 |
|---|---|
| `ToolArtifact` + `to_dict`/`from_dict` | ✅ 存在 |
| `ToolResult.artifacts` | ✅ 存在（`message.py:87`） |
| `ToolOutput.artifacts` | ✅ 存在（`message.py:104`） |
| `TurnCloser.artifacts` 收集 | ✅ 存在（`turn_close.py:169`） |
| `subagent.py:98` 落盘 | ✅ 存在（**持久化先例**） |
| `ToolArtifact.from_dict` 的调用方 | ❌ **零** |
| **谁产出 `ToolResult.artifacts`** | ❌ **零个工具**（`write_tool.py:108` / `edit_tool.py:120` 都不填） |
| `tool_message()` 把它放进 `ChatMessage` | ❌ **丢弃**（`run.py`） |
| session `_message_to_dict` | ❌ 不落盘 |

于是 undo 只能靠 git 推断「本轮新建了哪些文件」：untracked 且不在基线里 ⇒ 删。
**而 git 分不出「模型建的」和「你手打的」** → H3（删用户文件）；**基线 ref 写失败时连兜底都没有** → H4（清空所有 untracked）。

> OpenCode 的做法（`revert.ts` + 官方文档："only paths attributed to cleanly completed assistant steps"）是**用会话自己记录的 patch 路径**，不用 git 推断。我们正好有这份契约——只是没人产出。

### 5.2 影响面（执行范围）

| 文件 | 现状 | 改动 |
|---|---|---|
| `tools/write_tool.py:108` | `ToolResult(success=True, output=…)` | 追加 `artifacts=(ToolArtifact(path=rel, kind="file", bytes=len(content)),)` |
| `tools/edit_tool.py:120` | 同上 | 追加 `artifacts=(ToolArtifact(path=rel, kind="file"),)` |
| `domain/message.py` | `ChatMessage` 有 images/synthetic | 加 `artifacts: tuple[ToolArtifact, ...] = ()` |
| `domain/run.py` `tool_message()` | **丢弃** artifacts | 带上 |
| `domain/session.py` | 不落盘 | 只在非空时写 `artifacts` 键；读回用**已有的** `ToolArtifact.from_dict` |
| `core/agent_loop.py` `undo_last_turn` | `restore(key)` 全量推断删除 | `restore(key, created=该回合 artifact 路径)` |
| `core/snapshots.py` | 基线 blob 整套 | **删除**（`baseline_ref` 参数、capture 的 ls-files/hash-object/update-ref、`_baseline_paths`、restore 的删除推断、prune 的第二命名空间） |
| `tests/test_snapshots.py:99` | `test_a_file_that_was_already_untracked_survives`（测基线机制） | 同一意图、新机制：不删除任何未列入 artifacts 的 untracked。**该用例针对的机制被有意删除，属"用例失效"例外** |

**顺带删掉的死代码**：基线 blob 机制整块。`restore` 的签名从「全量推断」变成「你告诉我删什么」——更小的模块、更少的 git 调用（capture 从 4 次 git 降到 1 次）。

### 5.3 复用契约（不造轮子）

| 复用 | 位置 |
|---|---|
| `ToolArtifact` / `to_dict` / `from_dict` | `domain/message.py:45-72`（往返已实现，接上即可） |
| `ToolResult.artifacts` | `domain/message.py:87` |
| `TurnCloser.artifacts` | `turn_close.py:154-169`（digest/handoff 已在用，接上后 undo 也能用） |
| `subagent.py:98` 的落盘形状 | 「artifacts 只在有值时写」的既有先例 |
| `_message_to_dict`「只在非空时写键」 | 与 images/reasoning/synthetic 同款 |
| `Sandbox.resolve` | write/edit 已有 `target`，取相对路径零成本 |

### 5.4 还原语义：三分法（与 OC 一致）

| 文件来源 | 处理 | 依据 |
|---|---|---|
| 已跟踪文件被改（**含 bash 改的**） | `git checkout <ref> -- path` | 快照里确实有它 → 还原。**这条覆盖绝大多数真实价值，且完全不依赖 artifacts** |
| 模型 write/edit 新建的 untracked | 删除 | artifacts 里有它，且快照里没有 |
| 用户自己新建的 untracked | **保留** | artifacts 里没有它 |
| bash 新建的 untracked | **保留**（保守） | OC 同样不追踪 bash；已列为已知限制 |

### 5.5 每文件每步

**Step 1 — 两个写入工具产出 artifact**（路径用 `target.relative_to(sandbox.workspace)` 归一，不直接用模型给的拼写）
```python
return ToolResult(
    success=True, output=f"wrote {len(content)} chars to {path_str}",
    artifacts=(ToolArtifact(path=rel, kind="file", bytes=len(content.encode())),),
)
```

**Step 2 — `ChatMessage.artifacts` + `tool_message()` 带上**

**Step 3 — 落盘/读回**（`_message_to_dict` 非空才写；`_message_from_dict` 用 `ToolArtifact.from_dict`）

**Step 4 — `undo_last_turn` 收集被撤回合的 artifact 路径**
```python
created = {a.path for m in session.messages[index:] for a in m.artifacts}
...
restored = await self.snapshots.restore(key, created=created)
```
注意：**必须在截断之前**从 `messages[index:]` 取（截断后就拿不到了）。

**Step 5 — `SnapshotStore.restore(key, *, created: set[str])`**
- `git diff --name-only <ref>` 的结果：快照里有 → checkout 还原；没有 → **只有当它在 `created` 里**才删。
- `created` 里但 diff 没列出的（新建的 untracked 本来就不出现在 diff 里）→ 显式删。
- 其余一律不动。
- **删除基线相关全部代码**。

**Step 6 — `prune` 只剩一个命名空间**（删 baseline 那一半循环）

### 5.6 数据流影响

```
write/edit ──► ToolResult.artifacts ──► TurnCloser.artifacts ──► tool_message()
   （新增）        （已有收集）              （已有，digest 在用）
                                          │
                                    ChatMessage.artifacts   ✦新增
                                          │
                                    session.json artifacts  ✦新增（只在非空时）
                                          │
/undo ──► 读被撤回合的 tool 消息 ──► created 集合
   ──► restore(key, created=…)
       ├ diff 命中 & 快照有 ──► git checkout 还原
       ├ diff 命中 & 快照无 & ∈created ──► 删
       └ 其余 ──► 不动（用户的、bash 的）
```

### 5.7 完成后效果

| 场景 | 改前 | 改后 |
|---|---|---|
| 回合用 write 建了 `new.py`，undo | 删 ✅ | 删 ✅（能力没丢） |
| 回合运行中你手打了 `draft.txt`，undo | **删 ❌** | **保留 ✅** |
| 回合用 bash 建了 `x.sh`，undo | 删 ❌ | 保留（保守，OC 同） |
| 基线 ref 写失败 | **清空所有 untracked ❌** | 基线已不存在，无此路径 |
| capture 的 git 调用次数 | 4 次 | **1 次**（capture 变便宜） |
| undo 后重启再 undo | — | artifacts 从 session 读回，**仍然可用** |

### 5.8 RIPR 用例

- **Red（摘既有绊线）**：`test_undo_must_not_delete_a_file_the_user_created`
- **Red（重写 `test_snapshots.py:99`）**：`test_a_file_that_was_already_untracked_survives` → 断言 restore 在**不带 created 集合**时不动任何 untracked
- **Red（新增 5 条）**
  1. `test_undo_deletes_a_file_the_turn_created`：真实 write 工具建文件 → undo 后消失（**证明能力没被砍**）
  2. `test_a_bash_created_file_is_left_alone`：bash 建文件 → undo 保留（保守默认被守住）
  3. `test_artifacts_survive_a_restart`：tool 消息落盘重载后 artifacts 仍可读 → undo 跨重启仍生效
  4. `test_restore_deletes_only_what_it_is_told_to`：直接对 `SnapshotStore.restore` 断言——传 `created={"a.md"}` 只删 a.md，旁边另一个 untracked 不动
  5. `test_write_tool_reports_the_path_it_wrote`：`write` 的 ToolResult 里 artifacts[0].path 是 **workspace 相对路径**（不是模型给的拼写）
- **Pass**：`tests/test_snapshots.py` 其余 4 条 + `tests/test_write_tool.py` / `test_edit_tool.py` 保持绿
- **Refactor**：capture 路径上不再需要 `shutil`？——检查（`restore` 仍需 `shutil.rmtree` 删目录）

### 5.9 架构图（F4 后）

```
 write/edit ──┐
              ├─► ToolResult.artifacts ─► TurnCloser.artifacts ─► tool_message()
 bash ────────┘        （新）                    （已有）              │
              （不产出）                                          ChatMessage.artifacts ✦
                                                                    │
                                                          session.json ✦（非空才写）
 capture（1 次 git）                                              │
   git stash create ─► refs/ai-coding/undo/<sid>/N     undo 读回 ┘
   ✦ 基线 blob 机制整块删除
                                                                    │
                                              undo: created = 被撤回合的 artifact 路径
                                                     │
                                              restore(key, created=…)
                                          ┌─────────┴─────────┐
                                    快照里有该文件        快照里没有
                                    git checkout 还原    ∈created ? 删 : 保留
```

### 5.10 最终形态自检

1. 模型 `write("notes.md")` → 你 `/undo` → `notes.md` 消失，`a.py` 的改动还原 → 与 OC 一致。
2. 模型 `write` 的同时你在编辑器手建 `scratch.txt` → `/undo` → `scratch.txt` **还在**（本轮的核心修复）。
3. 模型全程用 bash 改文件 → `/undo` → 已跟踪文件还原（走 git diff，不依赖 artifacts）；bash 新建的 `.log` 保留 → 保守且可解释。
4. 回合结束后**重启进程**再 `/undo` → artifacts 从 session 读回 → 新建文件仍会被删 → 安全网不会因重启失效。
5. 老的 session 文件（tool 消息无 `artifacts` 键）→ 读回 `()` → undo 只还原 tracked、不删任何东西 → **向后兼容，且方向是保守的**。
6. 模型写了 `.ai-coding/` 里的东西？write/edit 的 `Sandbox.resolve` 允许 workspace 内任意路径 → artifact path 会指向它 → undo 会删掉自己刚写的附件。**这是可接受的**（它确实是本轮产生的），但值得在文档写一句。

## 6. F5–F8 — undo 链收尾：redo 失效 / 跨会话隔离 / 忙时拒绝 / 告知原因

> 这四条都在 undo 这条链上，共用同一批代码，一起做。

### 6.0 先说两个核实结果（其中一条已不用做）

**F6 已经完成**——F4 重写 `capture` 时删掉了基线，它现在天然是 all-or-nothing：
```python
if (await git(self._root, "update-ref", ref, sha, timeout=...))[0] != 0:
    return None          # ← 写不成就等于没有快照，调用方不会去还原
```
> r17 的 F6 是「基线 ref 写失败仍返回 undo ref」的复合故障，基线删除后该分支不存在了。
**所以 F6 不再改代码，只补一条测试把这条不变量钉住**（否则将来有人"顺手"改成吞掉错误，会静默退化）。

**跨会话泄漏是真的**，不是推测：
- `world.py:135` 只造**一个** loop；TUI 切 session 复用它。
- `_undo_tail`（`agent_loop.py:229`）是单槽 list，`redo_last_turn` 不检查属于哪个会话。
- `handlers.py:415` 把 `loop.last_undo` 直接发给**刚执行命令的那个 client**。

实测（缺陷绊线已复现）：
```
undo(A) → undo(B) → _undo_tail 里是 B
redo(A) → session A 的历史被塞进 ['B SECRET MESSAGE', 'B reply']
```

### 6.1 F5 病灶与两个设计岔路

**病灶一：新回合不使 redo 失效**。`_undo_tail` 与 git 侧的 `<sid>/redo` ref 都没有在新回合开始时被清。于是：
```
undo → 发新消息 → redo
  → 历史变成 ['TURN TWO', 'TURN TWO 的回答', 'TURN ONE', 'TURN ONE 的回答']（重复）
  → 工作区被回滚到「TURN ONE 当时」的状态（TURN TWO 的改动消失）
  → 报告 ok: True
```
OC 的规则是 *"Sending a new prompt while an undo is staged **commits the revert**"*。我们不需要新状态机：**`_capture_snapshot` 是 `_run_turn` 的第一行**，正好是"新回合开始"的唯一钩子。

**病灶二：状态是 loop 级而非 session 级**（上面的实测）。

**岔路：undo 连续按两次的语义**。现状是单槽——第二次 undo 覆盖第一次，所以只能 redo 最后一次。OC 是「合并成一个更宽的暂存回退」。

### 6.2 影响面

| 文件 | 现状 | 改动 |
|---|---|---|
| `core/agent_loop.py:226` | `last_undo: dict \| None` | 改为按 session_id 取（见下方决策） |
| `core/agent_loop.py:229` | `_undo_tail: list` | 同上 |
| `core/agent_loop.py:1297` `_capture_snapshot` | 只 capture + prune | **开头**作废本会话的 redo |
| `core/agent_loop.py:1357/1369` | 写单槽 | 按 session 写 |
| `core/agent_loop.py:1380-1392` `redo_last_turn` | 不看 session | 取本会话的；没有则 `nothing to redo` |
| `core/agent_loop.py:1312/1378` | 无锁 | 加忙时判定（见下方决策） |
| `core/snapshots.py` | 无 `forget` | 新增 `forget(key)`：删 `<sid>/redo` 一条 ref（capture 的旧 ref 由 prune 管） |
| `server/handlers.py:415` | `getattr(world.loop, "last_undo", None)` | 改按 `state.session_id` 取 |
| `app/commands.py:150-170` | 固定文案 | 按 `reason` 打印（见 F8） |

### 6.3 复用的契约（不造轮子）

| 复用 | 位置 | 用法 |
|---|---|---|
| `_capture_snapshot` | `agent_loop.py:1297` | 已是"新回合唯一钩子"，不新增生命周期状态 |
| `_turn_lock` | `agent_loop.py:279` | `process_input` 已用它串行化回合；undo 只需读它的 `locked()` |
| `SnapshotStore.prune` 的 ref 删除形状 | `snapshots.py` | `forget` 与它同一形状（`update-ref -d`），不新造删除机制 |
| `handlers.py:415` 的 `reload_in_place` 判定 | 既有 | undo/redo 载荷本来就只在该分支发 |

### 6.4 每文件每步

**Step 1 — `_capture_snapshot` 开头作废 redo**
```python
async def _capture_snapshot(self, session):
    self._forget_redo(session)          # ← 新回合作废上一次的暂存回退（OC 同规则）
    if self.snapshots is None: return
    ...（原有 capture / prune）
```
`_forget_redo` 丢弃内存尾巴 + `await snapshots.forget(f"{sid}/redo")`（best effort）。

**Step 2 — 状态按会话隔离**
```python
self.last_undo: dict[str, dict[str, Any]] = {}      # session_id -> report
self._undo_tail: dict[str, list[ChatMessage]] = {}   # session_id -> 尾巴
```
对外读取加一个具名方法，handlers 不再直接摸属性（避免第二个读者各自 `.get`）：
```python
def last_undo_for(self, session_id: str) -> dict[str, Any] | None:
    return self.last_undo.get(session_id)
```

**Step 3 — `redo_last_turn` 只认本会话**

**Step 4 — 忙时判定**（undo 与 redo 各自入口第一行）
```python
if self._turn_lock.locked():
    return {"ok": False, "reason": "a turn is running"}
```

**Step 5 — `SnapshotStore.forget`**
```python
async def forget(self, key: str) -> None:
    """Drop one snapshot ref (the redo slot). Best effort."""
    await git(self._root, "update-ref", "-d", self._ref(key), timeout=self._timeout)
```

**Step 6 — `reason` 字段与文案**

`undo_last_turn` 的报告增加 `reason`：
| reason | 何时 | TUI 文案 |
|---|---|---|
| 不出现（ok） | 文件已还原 | `[undo] 6 → 4 条，还原 2 个文件` |
| `not-a-repo` | 工作区不是 git 仓库 | `[undo] 回退 6 → 4 条；该工作区不是 git 仓库，文件未还原` |
| `no-snapshot` | 捕获时没拿到 ref（`exists()` 落空） | `[undo] 回退 6 → 4 条；本回合没有可用快照，文件未还原` |
| `restore-failed` | `restore` 抛错 | `[undo] 回退 6 → 4 条；快照还原失败（…），文件可能仍是改动后的状态` |
| `nothing to undo` | 历史里没有真人消息 | 现有文案不变 |
| `nothing to redo` / `a turn is running` | — | 现有文案 |

`/redo` 的文案改为**依赖 restore 是否真的跑了**，而不是无条件「已恢复上一回合」（现状会撒谎：`redo` 把 restore 异常吞成 `pass`）。

### 6.5 数据流影响

```
新回合 ──► _capture_snapshot ──► _forget_redo（内存尾巴 + git redo ref）✦新增
                                    ↓
undo(A) ──► _undo_tail[A] = 尾巴          （不再共用一个槽）
undo(B) ──► _undo_tail[B] = 尾巴
redo(A) ──► 只读 _undo_tail[A]  ✦隔离      （改前会把 B 的尾巴塞进 A）
handlers ─► loop.last_undo_for(state.session_id)  ✦按会话取
commands ─► 按 report['reason'] 选文案       ✦
```

### 6.6 完成后效果

| 场景 | 改前 | 改后 |
|---|---|---|
| undo → 发新消息 → redo | 历史重复 + 工作区回滚到旧状态，报 ok | redo 报 `nothing to redo`（新回合已提交回退） |
| A、B 会话交替 undo/redo | B 的历史被塞进 A | 互不干扰 |
| 非 git 工作区 undo | 「还原 0 个文件」（与"没变化"无法区分） | 明说「不是 git 仓库，文件未还原」 |
| 回合进行中 `/undo` | 与运行中的回合同时改 messages（竞态） | 按决策：等待或明说「回合进行中」 |
| `/redo` 但 restore 失败 | 谎报「已恢复上一回合」 | 按 reason 说清 |

### 6.7 你已拍板的两处

| 决策 | 结论 | 实现要点 |
|---|---|---|
| undo/redo 暂存状态 | **按会话分槽** | `_undo_tail: dict[str, list[ChatMessage]]`、`last_undo: dict[str, dict[str, Any]]`，键为 `session_id`；`redo_last_turn` 只取本会话；条目在 redo 或该会话新回合时 pop（见 Step 1/3），不无限增长 |
| 回合进行中敲 undo/redo | **直接拒绝并明说** | `if self._turn_lock.locked(): return {"ok": False, "reason": "a turn is running"}`——非阻塞检查后再 acquire（asyncio 的 `Lock.acquire` 在未锁时不会挂起，中间没有让出点，检查+加锁实际是原子的）。对齐 OC `assertNotBusy` |

### 6.8 RIPR 用例

- **Red（摘 1 条既有绊线）**：`test_redo_after_a_new_turn_must_not_duplicate_history`
- **Red（新增 6 条）**
  1. `test_undo_in_one_session_does_not_leak_into_another`：A undo → B redo → `nothing to redo`，且 A 的历史为空
  2. `test_the_undo_report_reaches_the_session_that_asked`：`last_undo` 取值按 session_id，A 的报告不会发给 B
  3. `test_redo_ref_does_not_survive_a_new_turn`：新回合后 `snapshots.exists(f"{sid}/redo")` 为 False
  4. `test_undo_says_why_no_file_was_restored`：非 git 仓 → `reason == "not-a-repo"`，且文案含「未还原」
  5. `test_redo_does_not_claim_success_when_restore_failed`：restore 抛错 → report 里带失败原因，命令文案不含「已恢复」
  6. `test_capture_returns_none_when_the_ref_write_fails`：锁住 6.0 的 F6 不变量
- **Pass**：`tests/test_undo_command.py`、`tests/test_snapshots.py` 保持绿
- **Refactor**：`forget()` 与 `prune()` 的 ref 删除形状各测一次

### 6.9 架构图（F5–F8 后）

```
                        新回合开始
                            │
                 _capture_snapshot ──► _forget_redo(session)
                            │            ├ 内存尾巴 pop
                    capture(sid/N)        └ update-ref -d refs/ai-coding/undo/<sid>/redo
                            │
  /undo ──► [忙? 按决策] ──► 定位最后一条真人消息
            │                  key = turn_count
            │                  created = 该回合 artifacts
            ├───────────────► _undo_tail[sid] = 尾巴 ; last_undo[sid] = report(含 reason)
            └───────────────► restore(key, created)
  /redo ──► [忙?] ──► _undo_tail.get(sid)? ──无─► "nothing to redo"
                          └─有─► restore(redo ref) → 尾巴写回 → pop
                                    │
 handlers ──► last_undo_for(state.session_id)   （不再全局单槽）
 commands ─► report['reason'] → 对应人话
```

### 6.10 最终形态自检

1. undo → 发新消息 → `/redo` → TUI 说「已无可恢复」；工作区保持新回合的结果。
2. A 会话 undo → 切到 B 会话 redo → B 说「nothing to redo」；A 的历史仍是撤销后的样子。
3. 在非 git 目录 undo → 历史回退，composer 回填原话，并明说「该工作区不是 git 仓库，文件未还原」。
4. 回合跑到一半敲 `/undo` → 按决策：等待，或立刻明说「回合进行中」（不会出现两个协程同时改 messages）。
5. 老 session（没有 artifacts、没有 synthetic）→ 全部走保守分支 → 不崩、不误删。

## 7. （原 F6+F7+F8 已并入 §6）

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

## 8. F9 + F11 + F12 — 三个独立的"工具给了自信的错答案"

> 这三条互不依赖，各自独立可验。F10（grep 两路同形）与 F15（文件枚举单一真相源）另起一轮，因为它们共用同一个新模块。

### 8.1 F9 病灶：rg 路径从不检查 runner 是否真的跑起来了

```python
# grep_tool.py:186-191 现状
outcome = await self._runner(argv, cwd=..., timeout=...)
if outcome.timed_out:
    raise TimeoutError
parse_error = "regex parse error" in (outcome.stderr or "").lower()
if outcome.returncode not in (0, 1) and parse_error:
    raise re.error(...)
# ← 没有检查 outcome.error / returncode is None
```
`infra/subprocess_runner.py:139-144`：Popen 失败时返回 `ProcessOutcome(returncode=None, error="FileNotFoundError: ...")`，`stdout=""`。
于是流程直落：解析空 stdout → 零命中 → `_render([], total=0)` → **`success=True, "(no matches)"`**。

实测（本机无 rg，显式 `use_ripgrep=True`）：
```
[basic]       RG ok=True: (no matches)
[include=*.py] RG ok=True: (no matches)
[limit=1]     RG ok=True: (no matches)
```
模型收到"没有匹配"，据此得出错误结论继续往下走。**这是比"报错"更坏的一种失败：它看起来像答案。**

> 注意：`use_ripgrep=None`（autodetect）走 `shutil.which`，不会到这里。出问题的是显式 `use_ripgrep=True` 或 `rg_path=` 这两条公开入口。

### 8.2 F9 病灶之二：兜底路径同步跑在事件循环上

```python
# grep_tool.py:140 现状
return self._grep_with_python(...)     # ← 没有 await，也没 to_thread
```
`_grep_with_python` 是同步的（`os.walk` + `read_bytes` + 正则）。整个扫描期间 SSE bridge 与 TUI 渲染完全停摆。
实测（403 个文件）：`gather()` 里 grep 与一个 10ms ticker 并发，ticker 的第一次 tick 在 **107ms** 才出现（应为 ~10ms）。

对照：`read_tool` 的同类操作走 `asyncio.to_thread`（既有约定）。

### 8.3 F11 病灶：`<embed>` 是 void 元素，却在 `_SKIP` 里

```python
# htmltext.py:20-26 现状
_VOID = frozenset({"meta", "link", "base"})          # 空元素：不进抑制计数器
_SKIP = frozenset({"script","style","noscript","iframe","object","embed","template","svg"})
```
`_SKIP` 的容器会 `self._suppressed += 1`，靠闭合标签减回来。`<embed>` **没有结束标签**，于是计数器永不归零，后面全部正文被丢弃。

实测：`html_to_text('<body><embed src=x.pdf><p>REAL</p></body>')` → `''`；对照组 `<meta>` → `'REAL'`。

**这正是 r17 已修过的 meta/link bug，同一个病，换了个标签还活着。** 一个正常的 PDF/插件页面就以 `<embed>` 开头。

核对 HTML void 元素全集（area, base, br, col, embed, hr, img, input, link, meta, param, source, track, wbr）：`_SKIP` 里**只有 `embed` 一个**是 void，所以这是一处的孤例，不是系统性遗漏。

### 8.4 F12 病灶：provider 返回什么都往渲染器里塞

```python
# websearch_tool.py:121-126 现状
results = data.get("results") if isinstance(data, dict) else None
if not results:
    return ToolResult(success=True, output="(no results)")
for item in results[: max(1, limit)]:
    title = str(item.get("title") or ...)      # ← 假设每个元素都是 dict
```
实测：`{"results": ["不是 dict"]}` → `AttributeError: 'str' object has no attribute 'get'` 逃出 `execute`。dispatch 会兜住，但**Python 的内部报错文本会进模型上下文**。

且 `if not results` 把两种情况混为一谈：
- `results: []` → 真的没搜到 → `success=True` 正确
- `{"answer": "x"}`（没有 results 键）→ **provider 改了契约**，却被报告成"搜不到" → 模型据此断言"网上没有答案"

`max_results` 也没有上限（`max(1, limit)`），模型可以传 10000 给 Tavily。

### 8.5 影响面（执行范围）

| 文件 | 现状 | 改动 |
|---|---|---|
| `tools/grep_tool.py:186-191` | 不看 `error`/`returncode is None` | 检出后按下方决策处理 |
| `tools/grep_tool.py:140` | 同步调用 | `await asyncio.to_thread(...)` |
| `tools/htmltext.py:22` | `_VOID = {meta, link, base}` | 加 `embed` |
| `tools/websearch_tool.py:121-137` | 形状不校验、无上限 | 校验形状 + `min(..., 20)` |

**明确不动**：`grep._walk` 的 `rglob` 无剪枝（属于 F15，和 `files.find` 一起改成共享枚举器）；`grep` 的 `precheck` 缺失（MEDIUM，另议）；F10 的三路同形。

### 8.6 复用的契约

| 复用 | 位置 |
|---|---|
| `run_process_async` 的 `ProcessOutcome`（`error`/`timed_out`/`returncode`） | `infra/subprocess_runner.py` |
| `asyncio.to_thread` | `read_tool.py` 既有做法 |
| `_VOID` / `_SKIP` 两张表 | `htmltext.py:20-26`，只动 `_VOID` 一处 |
| `_MAX_SNIPPET_CHARS` 等既有常量 | `websearch_tool.py` |
| Tavily 自身上限 20 | provider 契约，取上限不拍脑袋 |

### 8.7 每文件每步

**Step 1 — grep：runner 没跑起来就说出来**
```python
if outcome.error or outcome.returncode is None:
    → 按决策（见问卷）
if outcome.returncode not in (0, 1):   # 现有 parse_error 分支之后
    → 非 0/1 视为失败而非"无匹配"
```
> 注意 `returncode` 语义：rg 的 0=有命中、1=无命中、2=错误。现有代码只对 `parse_error` 做了处理，`2`（其它错误，如权限）会被当成"无命中"。

**Step 2 — grep：兜底进线程**
```python
return await asyncio.to_thread(
    self._grep_with_python, target, pattern,
    include=include, literal=literal, case_sensitive=case_sensitive, limit=limit,
)
```

**Step 3 — htmltext**：`embed` 进 `_VOID`

**Step 4 — websearch**：
```python
results = data.get("results") if isinstance(data, dict) else None
if not isinstance(results, list):
    return ToolResult(success=False, output="search failed: provider response had no 'results' list")
items = [item for item in results if isinstance(item, dict)]   # 形状净化
if not items:
    return ToolResult(success=True, output="(no results)")
limit = min(max(1, limit), 20)       # Tavily 自身上限
```

### 8.8 数据流影响

```
模型 → grep.execute
         ├ rg 分支：runner 失败/returncode=2 ──► 【新增】说清，而不是 "(no matches)"
         └ 兜底分支：os.walk+read ──►【新增】to_thread（事件循环不再停摆）

模型 → webfetch(html) → htmltext：<embed> 不再吞掉整篇正文

模型 → websearch → Tavily
         ├ 无 results 键 ──►【新增】success=False（契约变了，不是搜不到）
         ├ 元素非 dict ──►【新增】跳过脏元素
         └ max_results ──►【新增】封顶 20
```

### 8.9 完成后效果

| 场景 | 改前 | 改后 |
|---|---|---|
| rg 缺失/损坏，rg 路径 | `success=True "(no matches)"` | 见问卷决策 |
| rg 返回码 2（权限等错误） | 同上假"无匹配" | `success=False` + 原因 |
| 大仓 grep | 事件循环停 100ms+ | 并发任务正常推进 |
| 含 `<embed>` 的网页 | 正文全空 | 正文正常 |
| Tavily 改了响应结构 | `"(no results)"` | `success=False`「provider 无 results 列表」 |
| results 里有脏元素 | AttributeError 进上下文 | 跳过脏元素 |
| `max_results=10000` | 真的请求 10000 条 | 最多 20 |

### 8.10 RIPR 用例

- **Red（摘 3 条既有绊线）**：grep 假 `(no matches)`、grep 阻塞事件循环、websearch 形状崩溃
- **Red（改 1 条既有绊线）**：websearch 无 results 键（现状报 success=True）
- **Red（新增 6 条）**
  1. `test_a_broken_rg_binary_does_not_answer_no_matches`
  2. `test_a_rg_error_exit_code_is_not_an_empty_result`：runner 返回 `returncode=2` → `success=False`
  3. `test_a_search_over_a_large_tree_does_not_stall_the_loop`（10ms ticker ≥3 次）
  4. `test_a_void_tag_in_the_skip_set_does_not_swallow_the_page`：遍历 HTML void 元素全集，断言与 `_SKIP` 无交集（**防同类 bug 复发的结构性守卫**）
  5. `test_websearch_skips_malformed_entries_but_keeps_the_good_ones`：`{"results":[None,"x",{"title":"T"}]}` → 只渲染 T
  6. `test_websearch_never_asks_the_provider_for_more_than_it_can_return`
- **Pass**：`tests/test_grep_tool.py`、`test_webtools.py`、`test_htmltext.py` 保持绿
- **Refactor**：`_SKIP`/`_VOID` 的不变量用一条测试表达，而不是每次靠人记得

### 8.11 需要你拍板

见问卷（rg 失败时是回落还是报错）。

### 8.12 架构图（F9+F11+F12 后）

```
                     ┌──────────────────────────────────────────┐
模型 ──grep(pattern)─►│ execute：校验参数 / 沙箱解析 target      │
                     └───────────────┬──────────────────────────┘
                                     │
                    ┌────────────────┴────────────────┐
                    ▼                                 ▼
        ┌─── rg 分支（await runner）───┐    ┌── 兜底分支（await to_thread）──┐
        │ ✦ runner 失败 / returncode=2  │    │     os.walk + read + regex     │
        │   → success=False + 原因     │    │   （事件循环不阻塞）            │
        │ （改前：一律 "(no matches)"） │    │                                │
        └──────────────┬───────────────┘    └───────────────┬────────────────┘
                       └──────────────┬─────────────────────┘
                                      ▼
                          _render：path:line: text（两路同形）

模型 ──webfetch──► htmltext：_VOID 含 embed → 正文不再被吞

模型 ──websearch─► Tavily ──► ✦ 形状校验（results 是 list / 元素是 dict）
                                ✦ max_results 封顶 20（provider 契约）
```

### 8.13 最终形态自检

1. 装 rg 的机器上 rg 崩了 → 工具说清崩了，模型知道要换策略，而不是继续"搜不到"的错误前提往下走。
2. 不装 rg 的机器（当前）→ 走兜底，全程异步，大仓搜索时 TUI 不冻结。
3. 抓一个以 `<embed>` 开头的 PDF 页面 → 拿到正文而不是空字符串。
4. Tavily 改接口 → 工具明确报"provider 没有 results 列表"，模型知道这是上游问题而不是"网上没有"。
5. 模型传 `max_results=10000` → 实际最多 20 条，不浪费配额也不被 provider 拒绝。

## 9. F10 + F15 — 工作区文件枚举的单一真相源 + grep 两路同形

> 必须一起做：F10 的兜底要读 `.gitignore`，而"哪些目录跳过、哪些文件被忽略"的定义在 F15 收成一处。本轮**装了临时 rg 15.1.0 实测它的每一个行为**（装完即卸），否则"同形"只是猜测。

### 9.1 F15 病灶：同一件事被写了两遍，且两遍都慢

| | `grep_tool._walk:269` | `handlers.files_find:506` |
|---|---|---|
| 遍历 | `sorted(root.rglob("*"))` | `sorted(root.rglob("*"))` |
| 跳过 | `if any(part in _SKIP_DIRS …)` | `skip = {".git",".venv","venv","node_modules","__pycache__",".ai-coding"}` ← **逐字副本** |
| 剪枝 | **无**（rglob 先把 `.git`/`node_modules` 全走完，再逐条过滤） | **无**，且 `sorted()` 先全量物化，20k 上限在过滤之后才生效 |
| 预算 | 无 | `scanned > 20_000` 在 `found` 之前 break |

> **更正**：r17 审核报告说"grep 的 `_walk` 已用 `os.walk` + `dirs[:]` 正确剪枝"，我未核实就写进了方案。实际两处**都是** `rglob` 无剪枝。在一个 8k 文件的树上实测：`files.find` 枚举了 8068 条才让计数器走到 228 —— 也就是说 20k 预算大部分被垃圾路径吃掉，真正能进 `@` 补全的文件远不到 200 条。

### 9.2 F10 病灶：三条分歧，全部实测确认（本轮装了 rg 15.1.0 对照）

**分歧一：`.gitignore` 只有 rg 认**
```
有 .git 的仓库：rg → ./a.py ./deep/nested/x.py ./sub/c.txt          （x.log、sub/b.py 被忽略）
无 .git 的目录：rg → 上面三个 + ./sub/b.py + ./x.log                （不认 .gitignore）
```
Python 兜底**从不**读 `.gitignore` → 在**任何**情况下都会命中 `x.log` / `sub/b.py`。

**分歧二：`node_modules` 只有兜底跳**
```
.gitignore 清空、无任何忽略规则 → rg 搜到了 ./node_modules/dep.js
```
rg 只按 `.gitignore` 跳，**没有内建忽略**。我们硬编码 `_SKIP_DIRS` 里的 `node_modules`/`venv` 对 rg 路径完全无效 → 同一棵树，两路结果不同。
（当前 rg 的 argv 只有 `--glob=!**/.git/**`。）

**分歧三：`include` 的 glob 语义两路不同**
| pattern | rg 实测 | Python `fnmatch(path.name, …)` |
|---|---|---|
| `*.py` | a.py, deep/nested/x.py, **sub/b.py** | 同 ✅（rg 无 `/` 的 glob 匹 basename） |
| `sub/*.py` | **sub/b.py** | ❌ 空（`fnmatch('b.py','sub/*.py')` 为假） |
| `**/*.py` | a.py, deep/nested/x.py, sub/b.py | ❌ 空 |

rg 的规则：**glob 不含 `/` → 匹 basename；含 `/` → 匹相对 cwd 的路径**。

**分歧四：输出顺序与 `limit` 截断的子集不同**
rg 的 `--json` 行按并行 walk 完成顺序到达（代码原样保序）；兜底按 `sorted()` 保序。于是 `limit=2` 时两路**截到不同的两条**，尾注里的 total 也跟着撒谎。

### 9.3 rg glob 与 `PurePosixPath.match` 的对齐（含 `**/` 陷阱）

```
a.py           match *.py   -> True    rg: True   ✅
sub/b.py       match *.py   -> True    rg: True   ✅   （从右匹配 = basename 语义）
deep/nested/x.py match *.py -> True    rg: True   ✅
sub/b.py       match sub/*.py -> True  rg: True   ✅
a.py           match sub/*.py -> False  rg: False  ✅
a.py           match **/*.py -> False  rg: True   ❌ 分歧
```
`PurePath.match` 把 `**` 当普通 `*`，所以 `**/*.py` 要求路径有 2 段，而 rg 的 `**/` 表示"任意深度含零层"。
**修法（已实测 4 例全中）**：模式以 `**/` 开头时先剥掉那 3 个字符，再交给 `match`——
```
strip('**/') 后：a.py→True  sub/b.py→True  deep/nested/x.py→True  sub/c.txt→False   与 rg 完全一致 ✅
```

### 9.4 pathspec 的 gitwildmatch 语义（实测）

```
'*.log' + '!keep.log'  →  x.log=True  keep.log=False  sub/x.log=True      ✅ 否定规则
'node_modules/'        →  node_modules/dep.js=True  node_modules=False   ✅ 目录模式
```
`pathspec.PathSpec.from_lines('gitwildmatch', lines)` 正好是 gitignore 语义。

### 9.5 影响面（执行范围）

| 文件 | 现状 | 改动 |
|---|---|---|
| **新增** `ai_coding/core/workspace_files.py` | 无 | `SKIP_DIRS` + `walk_workspace(root, *, respect_gitignore=True)` |
| `tools/grep_tool.py` | `_SKIP_DIRS` 私有副本 + `_walk` rglob | 删两者，改调 `walk_workspace` |
| `server/handlers.py` | `skip` 逐字副本 + rglob | 删，改调 `walk_workspace` |
| `tools/grep_tool.py` argv | 只有 `--glob=!**/.git/**` | 追加每个 SKIP_DIR 的 `--glob=!**/<dir>/**`（见 9.7 第 2 题） |
| `tools/grep_tool.py` 渲染 | rg 原序、兜底 sorted | 两路都按 `(rel, line)` 排序**后**再套 limit |
| `pyproject.toml` | — | 加 `pathspec>=0.12`（月下载 5.05 亿） |

### 9.6 新模块的形状（**说明它凭什么不是造轮子**）

```python
# ai_coding/core/workspace_files.py
SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".ai-coding"})

def walk_workspace(root, *, respect_gitignore=True, max_entries=20_000):
    """(rel_posix, is_dir) for everything under ``root`` that is not skipped.

    os.walk with ``dirs[:]`` pruning — the ignored directories are never
    entered, which rglob could not do.  ``respect_gitignore`` layers the
    .gitignore files git would apply (root, plus any found while descending --
    see 9.7 Q1).  One definition, two callers: grep's fallback and the
    composer's file list, which must not disagree about what exists.
    """
```
- 不是新概念：`os.walk` 剪枝 + pathspec 都是标准做法，`pathspec` 是成熟库。
- 存在的理由是**两个调用方已经各自写了一份**，而且写错（无剪枝）。合并本身就是消除真相源漂移。

### 9.7 需要你拍板的三处

见问卷。

### 9.8 数据流影响

```
                    ┌────────────── core/workspace_files.py ──────────────┐
                    │ SKIP_DIRS（唯一）+ walk_workspace（os.walk 剪枝）  │
                    │ pathspec：.gitignore 规则（按目录叠加）             │
                    └───┬──────────────────────────────┬──────────────────┘
                        │                              │
        grep_tool._grep_with_python              handlers.files_find
        （文件，rel 路径）                        （文件+目录，rel + '/'）
                        │                              │
              include 过滤：PurePosixPath.match       query 子串过滤（保持现状）
                        │                              │
                        └──────► 两路同一批文件 ◄───────┘

  rg 分支：argv 追加每个 SKIP_DIR 的 --glob=!**/<dir>/**   （让 rg 也跳）
           解析后按 (rel, line) 排序 → 截断 → 渲染
```

### 9.9 完成后效果

| 场景 | 改前 | 改后 |
|---|---|---|
| `.gitignore` 写了 `*.log` | rg 跳、兜底不跳 | 两路都跳 |
| 无 git 的目录里 `.gitignore` 写了 `*.log` | 两路都不跳 | 两路都跳（rg 侧加 `--no-require-git`，见 Q3） |
| `node_modules` 未被 ignore | rg 搜进去、兜底跳 | 两路都跳 |
| `include='sub/*.py'` / `'**/*.py'` | 兜底返回空 | 两路一致 |
| `limit=2` | 两路截到不同两条 | 两路截到**同样**的两条（按 `(rel,line)` 排序后） |
| 8k 树的 `files.find` | 枚举 8068 条才走 228 | 剪枝后只走真实文件；20k 预算用在该用的地方 |
| `files.find` 第 200 条之后 | 选不到（`limit` 在排序后截断） | 行为不变（客户端仍需传 query）——**本次不改**，见下 |

> `files.find` 的 `limit=200` 截断是 r17 已知的取舍（客户端从不传 query，所以只有排序后的前 200 个可选）。本轮**不动**：客户端侧 fuzzysort 过滤 + `files.find` 全量返回需要一起改，属 F13 的范围，单独评估。

### 9.10 RIPR 用例

- **Red（既有绊线 3 条）**：grep 两路同形相关（摘 xfail 标注）
- **Red（新增 7 条）**
  1. `test_both_runners_answer_the_same_question` —— `@pytest.mark.skipif(not shutil.which("rg"))`，同一棵树两路各跑一次，断言输出**逐行相同**（装了 rg 的机器上才跑；本机跳过）
  2. `test_the_fallback_respects_gitignore`（无 rg 也能验）：`.gitignore` 写 `*.log` → 两路都不命中 `x.log`
  3. `test_include_globs_match_what_rg_matches`：参数化 `*.py` / `sub/*.py` / `**/*.py` 三个 pattern，各自断言期望文件集（**用 9.3 实测出的 rg 真值当期望**）
  4. `test_the_ignore_list_has_one_home`：`SKIP_DIRS` 只在 `workspace_files` 定义；grep_tool 与 handlers 都不再自带副本（import 即约束）
  5. `test_the_walk_prunes_instead_of_filtering`：建一棵含 5k 个 `.git` 内文件的树，断言枚举条数 < 100（证明是剪枝不是事后过滤）
  6. `test_limit_selects_the_same_rows_on_both_paths`：用注入 runner 的 rg 路径与兜底，对同一份数据断言 `limit=2` 截到同样两行且 total 一致
  7. `test_a_nested_gitignore_is_applied`（取决于 Q1）
- **Pass**：`tests/test_grep_tool.py`、`test_webtools.py` 保持绿；`files.find` 的既有前端测试保持绿
- **Refactor**：删掉 `grep_tool._SKIP_DIRS` 与 `handlers` 的 `skip`（用 ruff/grep 确认无残留引用）

### 9.11 架构图

```
                       真相源：core/workspace_files.py
                       ┌──────────────────────────────────────────┐
                       │ SKIP_DIRS = {.git,.venv,venv,            │
                       │   node_modules,__pycache__,.ai-coding}  │
                       │ walk_workspace():                         │
                       │   os.walk + dirs[:] 剪枝（不进入）        │
                       │   pathspec: .gitignore（按目录叠加）     │
                       └───────┬───────────────────────┬──────────┘
                               │                       │
              ┌────────────────┘                       └──────────────┐
              ▼                                                       ▼
   grep 兜底                                          files.find（@ 补全）
   只取 is_dir=False                                  文件 + 目录（rel + "/"）
   include: match(rel, strip('**/'))                  query 子串过滤（不变）
              │                                                       │
              └──────────► 同一批文件 ◄────────────────────────────────┘
                                    │
   rg 分支：argv 也带 --glob=!**/<每个 SKIP_DIR>/** + --no-require-git
            解析 → 按 (rel, line) 排序 → limit → 渲染
                                    │
                                    ▼
                    两条路径产出同一形状：path:line: text
```

### 9.12 最终形态自检

1. 装了 rg 的机器跑 `grep(pattern, include='**/*.py')` → 命中根目录与深层目录的 .py；不装 rg 的机器 → **同样的行**。
2. 仓库根有 `.gitignore` 写 `dist/` → 两路都不搜 dist。
3. 一个 50k 文件的仓库（.git 里就有 40k）→ `files.find` 在几百毫秒内返回，且 20k 预算花在真实文件上。
4. `node_modules` 没被 ignore → 两路都不搜它（这是有意的：vendored 依赖不是搜索目标）。
5. `limit=5` 在两台机器上截到同样 5 行，尾注的 total 也一样。
6. 没有 `.gitignore` 的目录 → 行为退化为"只看 SKIP_DIRS"，两路一致。

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
- [x] **F4** `ToolArtifact` 接线：write/edit 产出 → 落盘 → `restore(created=…)`，删掉基线推断
- [x] **F5** 新回合作废 redo + `_undo_tail`/`last_undo` 按 session 隔离
- [x] **F6** `capture` all-or-nothing（写失败即回滚并返回 None）
- [x] **F7** undo/redo 走 `_turn_lock`
- [x] **F8** `report.reason` + `commands.py` 按 reason 打印

### 工具正确性
- [x] **F9** grep runner 失败不再报 `(no matches)`；兜底 `to_thread`
- [x] **F10** grep 两路同形：pathspec + `PurePosixPath.match` + 排序后截断 + 双跑一致性测试
- [x] **F11** `_VOID` 加 `embed`
- [x] **F12** websearch 形状校验 + `max_results ≤ 20`

### 前端与真相源
- [ ] **F13** Home 附件透传（`pendingPrompt` 带 images）
- [ ] **F14** slash 命令不渲染未发送的 image part
- [x] **F15** `core/workspace_files.py` 单一真相源，`files.find` 换 `os.walk`
- [ ] **F16** mime 白名单从 `IMAGE_MIME_BY_SUFFIX` 派生

### 收尾
- [ ] `tests/integration/test_r17_defects.py` 全部 xfail 摘除并转绿
- [ ] `docs/data-contracts.md` 更新 r17 契约 + 记录已知限制
- [ ] 全门禁：pytest / mypy / ruff / bun / tsc