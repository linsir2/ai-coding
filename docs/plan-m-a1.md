# M-A1 实施方案 · 远程 MCP / OAuth 路径修复

> 状态：**评审已回，方案已修订；必改项已实施**（评审对象 = 当前**未提交**的工作区 diff）
> 关联：[roadmap-milestones.md](roadmap-milestones.md) §1 M-A1
> 依据：[eval-01](eval-01-offline-baseline.md) §2、[eval-07](eval-07-competitor-analysis.md)
> 评审：子智能体严苛评审（见 §11），必改项 1–6 已全部落实

---

## 一、要实现的内容（一句话）

**让 `_remote_session_factory` 这条「远程 MCP + OAuth」路径在 `mcp` SDK 1.30.0 下真的能跑通。**

它现在**一次都跑不通**：7 条 OAuth 用例全红，远程服务器永远 `status='failed'`。

---

## 二、调研结论：不是 1 个缺陷，是 4 处（3 处产品 + 1 处测试）

用探针逐个剥离，每修一处就往前走一步（**实测记录**，非推断）：

| # | 位置 | 症状（实测原文） | 性质 |
|---|---|---|---|
| 1 | `ai_coding/core/mcp.py:787` | `detail: "No module named 'httpx2'"` | **产品缺陷** |
| 2 | `ai_coding/core/mcp.py:789` | 修完 1 后立刻 `ValueError: too many values to unpack` | **产品缺陷** |
| 3 | `ai_coding/core/mcp.py:637/645` | `cannot import name 'AuthorizationCodeResult' from 'mcp.client.auth'` | **产品缺陷** |
| 4 | `tests/test_mcp_oauth.py:123-126` | `Invalid registration response: 1 validation error for OAuthClientInformationFull / redirect_uris / Field required` | **测试夹具缺陷** |

**逐层实测结果**（与 §9 推演逐条吻合）：

```
基线          test_mcp_oauth  7 failed / 10 passed      全量 10 failed / 3789 passed
修 1+2 后     test_mcp_oauth  5 failed / 12 passed      ← 远程服务器已能连上并列出工具
修 1+2+3+4+5  test_mcp_oauth  0 failed / 17 passed      ← 全绿
             全量 pytest      3 failed / 3796 passed    ← 剩余 3 条是 root 伪失败（M-A3/A4）
             mypy             3 errors（原 6）
```

**这直接推翻了 roadmap 的两处判断**：
- roadmap 说 MCP OAuth 那几条"需外网" —— **错**。夹具用的是 `127.0.0.1` 的假 AS，**完全离线**。
- roadmap 把 `test_r17_defects` 归到 M-A3 —— 它**也是 root 伪失败**（见 M-A4）。
  ⚠️ 但这**不代表 M-A3 已闭环**：M-A3 的 `ok` 字段门控（`agent_loop.py:2489`）仍是有效的防御性修复，
  需独立推进（见 §10）。

---

## 三、原有设计 vs 目前设计

### 3.1 原有设计（代码意图，读注释得出）

`_remote_session_factory`（M4 引入）的职责链：

```
远程 server 返回 401
  → OAuthClientProvider 发现 → 元数据发现链（PRM → OASM）
  → 动态客户端注册（POST /register）
  → 起本地 _CallbackServer 收浏览器回调
  → 用 authorization_code + PKCE 换 token
  → 带着 token 的 httpx.AsyncClient 建 streamable_http_client 传输
  → ClientSession.initialize() 握手
  → yield session（交给 _MCPSessionOwner）
```

设计意图（`mcp.py:723-732` docstring）：**`interactive` 是"连接"与"登录"的全部差别** ——
带它就 401 驱动浏览器；不带就立刻结束并落到 `needs_auth`（一个状态，不是一次重试）。

### 3.2 目前设计（实际代码，与 SDK 1.30.0 对照）

| 处 | 代码写的 | SDK 1.30.0 实际 | 后果 | 性质 |
|---|---|---|---|---|
| 传输层客户端 | `import httpx2` + `httpx2.AsyncClient` | **无 `httpx2` 此包**；项目已装 `httpx 0.28.1` | `ModuleNotFoundError` | **笔误级缺陷** |
| 传输层解包 | `as (read, write)` | yield **3 元组** `(read_stream, write_stream, get_session_id)`（`streamable_http.py:695-699`） | `ValueError` | API 形状变更 |
| 回调返回值 | `AuthorizationCodeResult(code=, state=)` | `callback_handler: Callable[[], Awaitable[tuple[str, str \| None]]]`（`oauth2.py:103/242`） | `ImportError` | API 形状变更 |

**根因（三者不同，必须分开说）**：

- **`httpx2` 不是任何 SDK 版本的 API**。PyPI 无此包，全库无 `httpx2.py`。
  它极可能是**笔误** —— 而笔误的来源大概率是**本仓库自己**：
  [logger_setup.py:22-26](../ai_coding/infra/logger_setup.py#L22-L26) 有一段**刻意的 `httpx2` logger 命名约定**
  （`#: httpx2 is not a typo -- that is the name the logger actually uses`），
  并被 [test_logger_setup.py:128-135](../tests/test_logger_setup.py#L128-L135) 钉住。
  ⚠️ **本次修复绝不能碰那个 logger**，它是对的。
- **另两处是 SDK 的 API 形状变更**：`pyproject.toml` 原声明 `mcp>=1.19,<3`，
  但实装 1.30.0，而 `docs/r1a-6-analysis.md:242` 记的是 `mcp 2.2.0` —— 代码其实是**按 2.2.0 写的**。

#### 3.2.1 ⭐ 实测版本矩阵（评审后补，决定了依赖范围）

用 `pip download` 逐个解包核验（**实测，非推断**）：

| mcp 版本 | `streamable_http_client` | `AuthorizationCodeResult` | 传输层客户端 | 本修复可用？ |
|---|---|---|---|---|
| 1.19.0 / 1.20.0 / 1.21.0 / 1.22.0 / 1.23.0 | ❌（只有旧别名 `streamablehttp_client`） | ❌ | `httpx` | ❌ |
| **1.24.0 / 1.28.0 / 1.30.0** | ✅ | ❌ | `httpx` | **✅** |
| 2.0.0 / 2.1.0 / 2.2.0 | ✅ | ✅（且从 `mcp.shared.auth` 导出） | **`httpx2`**（2.x 的真实依赖） | ❌ |

**结论**：
- 代码是按 **mcp 2.2.0** 写的（`httpx2` + `AuthorizationCodeResult` 在 2.2.0 里**都真实存在**）。
- 装的是 **1.30.0**，两者都不存在 → 三处全崩。
- **唯一可用的区间是 `mcp>=1.24,<2`**（`streamable_http_client` 已存在、`AuthorizationCodeResult` 尚未引入）。
- 因此 `pyproject.toml` 的 `mcp>=1.19,<3` **两端都错**：1.19 没有 `streamable_http_client`，2.x 要 `AuthorizationCodeResult`+`httpx2`。
  → **已改为 `mcp>=1.24,<2`**（见 §4.4）。

### 3.3 复用的方法契约（不新造任何东西）

| 契约 | 来源 | 用途 |
|---|---|---|
| `streamable_http_client(url, *, http_client, terminate_on_close=True)` → yield 3 元组 | SDK `client/streamable_http.py:619`（签名）/ `:695`（yield） | 已用，只需改解包元数 |
| `httpx.AsyncClient(auth=..., headers=...)` | 项目已有依赖 `httpx>=0.27` | 已用，只需改模块名 |
| `OAuthClientProvider(..., callback_handler=...)` → `Awaitable[tuple[str, str \| None]]` | SDK `client/auth/oauth2.py:103/242` | 已用，只需改回调返回形状 |
| `OAuthClientMetadata.redirect_uris` **必填** | SDK `shared/auth.py:44`（`Field(..., min_length=1)`） | 夹具必须回显 |

**明确不做**（避免造轮子/冗余）：
- ❌ 不升级/降级 `mcp` 版本去迁就代码（会动 CI 与 lockfile，超出"只改根源"）
- ❌ 不重写 `_remote_session_factory`（结构是对的，只是三处 API 形状错）
- ❌ 不新增任何配置项、不新增抽象层
- ❌ 不动 `logger_setup.py` 的 `httpx2` logger 约定（它是对的）

---

## 四、执行范围（逐文件、逐步）

### 4.0 调用方清单（先枚举，确认无遗漏）

`_remote_session_factory` 有**两个**入口，**同路径、同样受益**：

| 入口 | 位置 | 用途 |
|---|---|---|
| `_factory_for(server)` | `mcp.py:1247` | 供 supervisor 定期重连 |
| `login(server, ...)` | `mcp.py:1267` | 供 `/mcp` 面板的登录按钮（内部走 `_factory_for`） |

两者都经 `_MCPSessionOwner._serve` 进入工厂上下文，所以**修一处即两条路径都好**。

### 4.1 `ai_coding/core/mcp.py` —— 3 处（逻辑改动 6 行，含 docstring 共 16 插入 / 8 删除）

**步骤 1**（`:787`）模块名
```python
-            import httpx2
+            import httpx
```

**步骤 2**（`:791`）客户端引用
```python
-                http_client=httpx2.AsyncClient(auth=provider, headers=headers or None),
+                http_client=httpx.AsyncClient(auth=provider, headers=headers or None),
```

**步骤 3**（`:792`）解包元数 + 注释
```python
-            ) as (read, write):
+            ) as (read, write, _session_id):
```
> 注释要点：SDK 1.30.0 起 yield 3 元组，第 3 项是 `get_session_id` 回调（`Mcp-Session-Id` 续接）。
> 本项目由 SDK 自己管会话，这里**接住即弃**（本项目不做 resumption）—— 但**必须接住**，否则解包即崩。

**步骤 4**（`:635-645`）回调返回形状
```python
-    async def result(self, timeout: float) -> Any:
-        """Wait for the browser to come back with a code."""
-        from mcp.client.auth import AuthorizationCodeResult
-
+    async def result(self, timeout: float) -> tuple[str, str | None]:
+        """Wait for the browser to come back with a code.
+
+        Returns ``(code, state)`` — the shape the ``mcp`` SDK's
+        ``callback_handler`` asks for (``Callable[[], Awaitable[tuple[str, str | None]]]``).
+        """
         if self._code is None:
             raise RuntimeError("callback server was never started")
         try:
             code, state = await asyncio.wait_for(self._code, timeout=timeout)
         except (asyncio.TimeoutError, TimeoutError) as exc:
             raise OAuthLoginTimeout(f"no sign-in callback within {timeout:g}s") from exc
-        return AuthorizationCodeResult(code=code, state=state or None)
+        return code, state or None
```
> 为什么不是"改 SDK 版本让 `AuthorizationCodeResult` 回来"：该类在 1.30.0 的
> `mcp/client/auth/__init__.py::__all__` 里**不存在**（实测全库 `grep` 无命中）。
> 而 `callback_handler` 的类型注解明确要 `tuple[str, str | None]`。**代码向 SDK 对齐是唯一正确方向。**

### 4.2 `tests/test_mcp_oauth.py` —— 1 处（5 行，夹具）

**步骤 5**（`:123-126`）假 AS 必须回显 `redirect_uris`
```python
         if path == "/register":
             self.state.registrations.append(flat)
-            self._json({"client_id": "fake-client", "client_id_issued_at": int(time.time())})
+            # RFC 7591 3.2.1: the server MUST echo redirect_uris when the
+            # client sent them, and mcp's OAuthClientInformationFull requires it.
+            response = {"client_id": "fake-client", "client_id_issued_at": int(time.time())}
+            response["redirect_uris"] = (
+                flat["redirect_uris"] if flat.get("redirect_uris") else None
+            )
+            self._json(response)
             return
```
> 为什么改夹具而不是改产品：`OAuthClientMetadata.redirect_uris` 在 SDK 里是**必填字段**
> （`Field(..., min_length=1)`，`shared/auth.py:44`）。真实 AS（如 GitHub）会回显；
> 是**这个假 AS 不符合 RFC 7591 §3.2.1**，导致响应无法通过 SDK 校验。
> 产品侧 `redirect_uris=[AnyUrl(redirect_uri)] if redirect_uri else None`（`:773`）是**正确的**
> —— 非交互时确实没有回调地址。

### 4.3 依赖范围收紧（评审 P1-1）

**步骤 8**：`pyproject.toml` 的 `mcp>=1.19,<3` → **`mcp>=1.24,<2`**

```toml
-     "mcp>=1.19,<3",
+     "mcp>=1.24,<2",
```

依据是 §3.2.1 的**实测版本矩阵**：这是**唯一**能让本修复成立的区间。
原范围两端都会装到跑不起来的版本（1.19 无 `streamable_http_client`；2.x 要 `AuthorizationCodeResult`+`httpx2`）。
**这是"只改根源"** —— 声明与实际能力不符，本身就是缺陷。

### 4.4 不做什么

- 不动 `world.py`（`await mcp_manager.open_all()` 且不用返回值，已正确）
- 不动 `sample_tasks.py`（已用 `or []` 兜底，属 M-A2 范围）
- 不动 `logger_setup.py` 的 `httpx2` logger（见 §3.2 警告）
- 不升级/降级 `mcp` 去迁就代码（那会掩盖真正的契约不匹配）
- **不新增测试文件**（只加 1 条负路径用例，见 §6.1.1）

---

## 五、对数据流的影响

### 5.1 修改前

```
_remote(server, interactive=True)
  → _remote_session_factory._cm()
      → callback = _CallbackServer(port)          ✅
      → import httpx2                            💥 ModuleNotFoundError
  → _MCPSessionOwner._serve 捕获 → status='failed'
  → available() == []  →  7 条用例全红
```

### 5.2 修改后

```
_remote(server, interactive=True)
  → _remote_session_factory._cm()
      → callback = _CallbackServer(port)          ✅ 不动
      → OAuthClientProvider(...)                 ✅ 不动（callback_handler 指向 result）
      → httpx.AsyncClient(auth=provider)         ✅ 步骤 1+2
      → streamable_http_client(...) as (read, write, _sid)  ✅ 步骤 3
      → ClientSession.initialize()               ✅
      → yield session
  → _MCPSessionOwner：status='connected'，toolCount=N
  → available() == [(server, [MCPToolHandle...])]  ✅
```

### 5.3 不受影响的部分（明确边界）

- **本地 stdio 服务器路径**：完全不经过 `_remote_session_factory`，一行不动
- **`_CallbackServer` 的 HTTP 处理逻辑**：`_handle` / `start` / `stop` 不动
- **token 存储**（`_TokenStore`）：不动
- **事件与 trace**：`status` 从 `failed` 变 `connected`，`detail` 从错误信息变空串 —— 这是**期望的**，
  下游 `servers()` / `/mcp` 面板自然显示正确状态
- **回合编排、记忆、压缩**：与 MCP 连接无关，零影响
- **`login()` 路径**：同 §4.0，一并修好

### 5.4 ✅ 已修：`httpx.AsyncClient` 连接池泄漏（评审 P2-3）

`httpx.AsyncClient(...)` 作为 `http_client` 传入后，SDK 因 `client_provided=True`
**不接管其生命周期**（`streamable_http.py:663-668`），而原代码**从不 close** →
连接池泄漏 + `ResourceWarning`。supervisor 定期重试、`login()` 每次新建，**逐次累积**。

**修法**（步骤 6）：把客户端包进自己的 `async with` —— 客户端生命周期归我们，
随工厂上下文一起关闭。**这就是"只改根源"**：既然这行本来就要改，顺手把它的生命周期也归位。

```python
async with httpx.AsyncClient(auth=provider, headers=headers or None) as client:
    async with streamable_http_client(server.url, http_client=client) as (read, write, _session_id):
        ...
```

### 5.5 ✅ 已修：OAuth 拒绝被吞成"超时"（评审 P1-2）

**这是评审挖出的、比"连不上"更隐蔽的缺陷**：`_CallbackServer._handle` 里
`if self._code is not None ...` 原本**嵌在 `else:` 分支里**（只在成功时执行），
所以错误回调**根本不解析 future** → 一直等到超时；即便解析，也是
`set_result(("", state))` 把拒绝吞成空 code。

**修法**（步骤 7）：
- 新增 `OAuthCallbackError(error, description)`（RFC 6749 §4.1.2.1）
- `_handle` 把 `if self._code` **提到 `if error/else` 之外**（这是关键 —— 原缩进本身就是 bug）
- 有 `error` → `set_exception(OAuthCallbackError(...))`；否则 `set_result((code, state))`
- 同时解析 `error_description`

**实测**：修前 `login()` 返回 `{'ok': False, 'reason': 'sign-in failed: No authorization code received'}`
（拒绝被吞）；修后返回 `sign-in failed: access_denied (The user declined the request)`。

---

## 六、TDD 用例设计（RIPR 四相，缺一不可）

> **RIPR = Red → Implement → Prove → Refactor**
> （红：先有失败测试 → 实现：最小改动 → 证明：全量验证 → 重构：清理）

### 6.1 用例：**全部复用现有的，不新增**

`tests/test_mcp_oauth.py` **17 条**就是现成的 Red，且已把 4 个修复点全覆盖：

| 相 | 用例 | 覆盖的修复点 |
|---|---|---|
| **R** | `test_a_remote_server_that_needs_no_login_connects_lists_and_answers` | 步骤 1+2+3（无 OAuth 的远程连接；**隐式断言 `httpx` import 成功** —— 连不上就是红） |
| **R** | `test_a_remote_server_that_needs_no_login_never_reaches_needs_auth` | 步骤 1+2+3 |
| **R** | `test_a_server_that_needs_a_login_is_a_state_not_a_retry` | 步骤 1-4（交互登录 → `needs_auth`） |
| **R** | `test_login_runs_the_real_flow_and_the_server_comes_up` | 步骤 1-4（完整登录往返） |
| **R** | `test_login_times_out_cleanly_when_nobody_consents` | 步骤 4（回调超时 → `OAuthLoginTimeout`） |
| **R** | `test_logout_sends_the_server_back_to_needs_auth` | 步骤 1-4 |
| **R** | `test_a_token_never_reaches_an_event_or_a_trace_field` | 步骤 1-4（脱敏） |
| **R** | 其余 10 条 | 元数据发现 / PRM / 注册 / PKCE / 错误分类 |
| **R** | **`test_a_refusal_is_reported_as_a_refusal_not_a_timeout`**（新增，见下） | 步骤 7（OAuth 拒绝语义） |

**关于 roadmap M-A1 验收③**（"新增一条断言该分支真的 import 到 `httpx` 的回归测试"）：
**由现有 e2e 覆盖** —— `import httpx2` 若不改，远程服务器连不上，
`test_a_remote_server_that_needs_no_login_connects_lists_and_answers` 直接红。
**不需要额外测试**（额外测试是冗余，按"砍掉说不清作用的"原则删除）。

> 📌 **评审纠偏**：初稿曾提出一条 `assert "yield (" in src and src.count(",") >= 2` 的"看门狗"测试。
> 那是**恒真断言**（`inspect.getsource` 的逗号数实测 34，任何函数都 ≥2），
> 且基于源码文本、格式化一变就红（脆弱方向还错了）。**已删除。**

### 6.1.1 新增的唯一一条：OAuth 拒绝语义（评审必改项 3）

评审指出：**P1-2 的错误回调缺陷可以完整通过原有 17 条测试** —— 因为没有一条覆盖负路径。
所以必须补一条**会红**的测试：

```python
async def test_a_refusal_is_reported_as_a_refusal_not_a_timeout(fake_as, tmp_path, monkeypatch):
    """RFC 6749 4.1.2.1: error=access_denied is an answer, not a hang."""
    ...
    fake_as.state.refused = True          # 夹具早已备好这个开关，只是没接线
    result = await manager.login("r", timeout=15)
    assert result["ok"] is False
    assert "access_denied" in result["reason"]
    assert "declined" in result["reason"]
    assert "callback" not in result["reason"]   # 关键：不是超时
```

**Red 相已实测**（把步骤 7 的修复临时改回去）：

```
FAILED test_a_refusal_is_reported_as_a_refusal_not_a_timeout
AssertionError: {'ok': False, 'reason': 'sign-in failed: No authorization code received'}
assert 'access_denied' in 'sign-in failed: No authorization code received'
```

→ 这条测试**真的会红**，且红在正确的地方（拒绝被吞成空 code）。✅

### 6.2 RIPR 执行序

| 相 | 动作 | 判据 |
|---|---|---|
| **R** | 记录当前红：`pytest tests/test_mcp_oauth.py` → **7 failed** | ✅ 已记录 |
| **I** | 步骤 1+2（产品）→ 重跑 → **5 failed** | 远程能连上并列工具 |
| **I** | 步骤 3（产品）→ 重跑 → **4 failed** | — |
| **I** | 步骤 4（产品）+ 步骤 5（夹具）→ 重跑 → **0 failed** | 全绿 |
| **R** | **步骤 7 反向验证**：把错误回调的修复临时改回去，跑新增的负路径用例 → **FAILED** | 证明新测试真的会红 |
| **I** | 步骤 6（客户端生命周期）+ 步骤 7（错误回调）+ 步骤 8（依赖范围） | — |
| **P** | `pytest tests/test_mcp_oauth.py` → **18 passed**；全量 → **3 failed**（`test_r17_defects` / `test_grant_store` / `test_snapshots`，**三条全是 root 伪失败，属 M-A3/A4**）；`mypy` → **3 errors**（原 6）；`ruff` 改动文件 → 0；`pip install "mcp>=1.24,<2"` → 解析通过 | 无回归 |
| **R** | 检查：无新增抽象、无新增配置、无版本变更（除依赖下界）、注释只解释"为什么" | `git diff` 仅 4 文件 |

### 6.3 P 相判据（**环境无关**，是稳定门禁）

> 📌 初稿写"3 failed"，在非 root 环境下那 3 条会转绿、数字变 0 —— **判据会漂移**。已改写为：

```
pytest -q 的失败项 ⊆ { test_r17_defects, test_grant_store, test_snapshots }
且这三条均因 root 绕过 DAC 而失败（M-A4 将给它们加显式 skipif）
其余用例 0 失败
```

---

## 七、架构设计图（改动前后）

```
┌─────────────────────────── 改动前（断在第一步） ───────────────────────────┐
│  _MCPSessionOwner._serve                                                   │
│    └─ _remote_session_factory  ← 两个入口：_factory_for / login()          │
│         └─ _cm()                        [asynccontextmanager]             │
│              ├─ _CallbackServer.start()                                    │
│              ├─ OAuthClientProvider(...)      ✅ 正常                       │
│              ├─ import httpx2                 💥 ModuleNotFoundError        │
│              │     ↓ 被 mcp.py:464 捕获                                    │
│              └─ status = 'failed'  →  available() == []                     │
└────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────── 改动后（四段全通） ─────────────────────────────┐
│  _MCPSessionOwner._serve                                                   │
│    └─ _remote_session_factory   （_factory_for / login() 两入口共用）      │
│         └─ _cm()                                                           │
│              ├─ _CallbackServer.start()              [不动]                │
│              ├─ OAuthClientProvider(                   [不动]                │
│              │     callback_handler = _CallbackServer.result               │
│              │  ─────────────────────────────────────────────               │
│              │  result() -> tuple[str, str|None]     ◀─ 步骤 4（改返回形状）│
│              │  （原为 AuthorizationCodeResult；SDK 1.30 无此类）           │
│              │                                                              │
│              ├─ httpx.AsyncClient(auth=provider)     ◀─ 步骤 1+2（改模块名）│
│              │  （原为 httpx2.AsyncClient；PyPI 无此包）                    │
│              │                                                              │
│              ├─ streamable_http_client(url, http_client=…)                 │
│              │       as (read, write, _session_id)   ◀─ 步骤 3（改解包元数）│
│              │  （SDK 1.30 yield 3 元组；2 元组解包即 ValueError）          │
│              │                                                              │
│              └─ ClientSession(read, write).initialize()                    │
│                        ↓                                                   │
│              status='connected'  →  available() == [(srv, [handles...])]   │
│                                                                            │
│  ── 测试侧 ────────────────────────────────────────────────────────────    │
│  tests/test_mcp_oauth.py::_FakeAsHandler.do_POST("/register")              │
│        response["redirect_uris"] = 客户端所发 or None  ◀─ 步骤 5（RFC 7591）│
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 八、修改成功后的效果（可验证）

| 观测点 | 修改前 | 修改后 |
|---|---|---|
| `pytest tests/test_mcp_oauth.py` | 7 failed / 10 passed | **18 passed** |
| 全量 `pytest -q` | 10 failed / 3789 passed | **3 failed / 3797 passed**（3 条均 root 伪失败） |
| `mypy ai_coding` | 6 errors | **3 errors** |
| 远程 MCP（`type: remote`） | `status='failed'`, `detail="No module named 'httpx2'"` | `status='connected'`, `toolCount=N` |
| `manager.available()` | `[]` | `[('srv', [MCPToolHandle...])]` |
| OAuth 登录流程 | 走不到回调 | 完整走通（浏览器 → 回调 → token） |
| OAuth **拒绝**（`access_denied`） | 被吞成空 code → 报 `No authorization code received` 或超时 | 报 `sign-in failed: access_denied (The user declined the request)` |
| `httpx.AsyncClient` | 每次连接泄漏一个连接池 | 随工厂上下文关闭 |
| `login()` 面板按钮 | 同 `failed` | 同 `connected` |
| 本地 stdio MCP | 正常 | **不变** |
| `logger_setup.py` 的 `httpx2` logger | 正常 | **不变**（本次不碰） |
| `pip install "mcp>=1.24,<2"` | 原范围两端都装到跑不起来的版本 | 解析通过 |

**剩余 3 条失败不是本里程碑的**：root 权限伪失败，归 **M-A4**。

---

## 九、自我核验：假设实施之后，最终模样是什么？

**逼自己推演一遍，检查中途细节是否自洽：**

1. **推演**：改完步骤 1+2，跑 `tests/test_mcp_oauth.py` —— 预期 7→5 failed。
   **实测正是 5 failed**（`..F.FFF.......F..`）✅
2. **推演**：再改步骤 3，预期剩下的都是"注册响应缺 redirect_uris"。
   **实测正是 4 failed，错误原文正是 `redirect_uris Field required`** ✅
3. **推演**：再改步骤 4+5，预期全绿。**实测 17 passed** ✅
4. **推演**：全量跑，预期 10→3 failed。**实测 3 failed**，且剩余 3 条全是 root 伪失败 ✅
5. **反向检查**：步骤 3 的 `_session_id` **未使用** —— 是不是漏了功能？
   查 SDK：`streamable_http_client` 内部用第 3 项管理 `Mcp-Session-Id`；
   针对 **MCP 会话 id** 全库检索，本项目无消费方。
   **结论：不是漏功能，是接住即弃。** ✅
6. **反向检查**：步骤 4 改成返回元组，会不会破坏 `_CallbackServer` 的其他调用方？
   针对 `_CallbackServer.result` 检索 → 只有 `mcp.py:782` 的 `callback_handler` 一处。✅
7. **反向检查**：步骤 5 改夹具，会不会让"产品有问题"被掩盖？
   不会 —— 产品侧**发送** `redirect_uris` 的逻辑（`:773`）是对的，
   改的是**假服务器的响应**不符合 RFC。真实 AS 会回显。✅
8. **反向检查**：`httpx2` 会不会是"本仓库另一处笔误"，需要一起清？
   **不是** —— `logger_setup.py:26` 的 `httpx2` 是**刻意的 logger 名**（被测试钉住），
   与 `mcp.py:787` 的**模块 import** 无关。**两者不能混。** ✅

**结论：方案自洽，8 处推演与实测全部吻合，可以实施。**

---

## 十、边界声明

| 项 | 归属 |
|---|---|
| 本方案吸收的 roadmap 条目 | **M-A1**（`mcp.py:787`）+ **M-A5 的 `mcp.py:637`（`AuthorizationCodeResult`）与 `:789`（解包）** —— 这三处**同文件、同函数、同一次故障**，拆开会让"远程 MCP 修好了"这件事无法独立验收 |
| 本方案**不含** | M-A5 其余（`events.py:581`、`deadline.py:171`、`mcp.py:670`、ruff 4 条、CI 门禁接入） |
| 本方案**不含** | M-A3 的 `ok` 字段门控（`agent_loop.py:2489`）—— 仍是有效修复，独立推进 |
| 本方案**不含** | M-A4 的 root 伪失败隔离 |
| 本方案**不含** | §5.4 的 `httpx.AsyncClient` 连接池泄漏（单独立项） |

---

## 十一、评审记录

子智能体严苛评审结论（必改项已全部落实）：

| # | 严重度 | 问题 | 处理 |
|---|---|---|---|
| S1 | 🔴 | §6.2 的"看门狗"测试是**恒真断言**（`src.count(",")`=34），且与"不新增测试"矛盾 | **已删除**该测试，改为"现有 17 条已覆盖" |
| S2 | 🔴 | 名为 M-A1 实吞 M-A5；验收③未满足 | **已加 §十 边界声明**；验收③ 标注"由现有 e2e 覆盖" |
| S3 | 🔴 | P 相判据随 root/非 root 漂移 | **已改写 §6.3** 为集合包含式判据 |
| S4 | 🔴 | 根因叙述错（把三处都归"版本漂移"）；未提 `httpx2` logger 约定 | **已改写 §3.2**（区分笔误 vs 形状变更 + logger 警告） |
| M1 | 🟠 | §2 数字自相矛盾（7/10 vs 5/12）；漏步骤 5 | **已修正** |
| M2 | 🟠 | 行号引用不准（`yield` 在 695-699 非 619-631）；grep 措辞错 | **已修正** |
| M3 | 🟠 | 遗漏 `login()` 调用方 | **已加 §4.0 调用方清单** |
| M4 | 🟠 | 未交代遗留的 `mcp.py:670` | **已加 §十** |
| M5 | 🟠 | 未提 `httpx` 连接池泄漏 | **已加 §5.4** 并**已修**（步骤 6） |
| L1/L2/L3/L4 | 🟡 | 计数口径、文档状态、M-A3 说明 | **已修正**

---

## 十二、第二轮评审（针对代码 diff）

代码写完后，再次派子智能体评审 `git diff`，又挖出 **2 个发布级问题 + 1 个资源泄漏**：

| # | 严重度 | 问题 | 处理 |
|---|---|---|---|
| **R1** | 🔴 | **依赖声明与实现不兼容**：代码无条件 `from mcp.client.streamable_http import streamable_http_client`，而 mcp **1.19–1.23 根本没有这个名字**（只有旧别名 `streamablehttp_client`）；mcp **2.x 又要 `AuthorizationCodeResult` + `httpx2`**。声明 `>=1.19,<3` 两端都错 | ✅ **实测版本矩阵**（§3.2.1）→ 收紧为 **`mcp>=1.24,<2`**（步骤 8） |
| **R2** | 🔴 | **OAuth 拒绝被吞**：`_handle` 的 `if self._code...` 原本**嵌在 `else:` 里**（只在成功时执行）→ 错误回调根本不解析 future → 一直超时；即便解析也是 `set_result(("", state))` | ✅ 新增 `OAuthCallbackError`，把判断**提到 `if error/else` 之外**（步骤 7） |
| **R3** | 🟠 | `httpx.AsyncClient` 传入后 SDK 不接管生命周期，代码从不 close → 每次连接泄漏 | ✅ 包进 `async with`（步骤 6） |
| **R4** | 🟠 | 负路径**零覆盖** —— R2 那个缺陷能完整通过原有 17 条测试 | ✅ 新增 `test_a_refusal_is_reported_as_a_refusal_not_a_timeout`，并**实测它会红**（§6.1.1） |
| **R5** | 🟡 | RFC 7591 §3.2.1 是"可以回显"不是"MUST"；注释过度声称 | ✅ 已改写注释 |
| **R6** | 🟡 | 提交信息/文档声称与实际 SDK 版本不符；文档用绝对 `file://` 链接 | ✅ 已修正（本文件用仓库相对链接） |

**R1 的实测依据**（`pip download` 解包核验，非推断）：

```
mcp 1.19.0  streamable_http_client=0  AuthorizationCodeResult=0
mcp 1.20.0  streamable_http_client=0  AuthorizationCodeResult=0
mcp 1.21.0  streamable_http_client=0  AuthorizationCodeResult=0
mcp 1.22.0  streamable_http_client=0  AuthorizationCodeResult=0
mcp 1.23.0  streamable_http_client=0  AuthorizationCodeResult=0
mcp 1.24.0  streamable_http_client=1  AuthorizationCodeResult=0   ← 下界
mcp 1.28.0  streamable_http_client=1  AuthorizationCodeResult=0
mcp 1.30.0  streamable_http_client=1  AuthorizationCodeResult=0   ← 实装
mcp 2.0.0   streamable_http_client=1  AuthorizationCodeResult=2   ← 上界（不含）
mcp 2.1.0   streamable_http_client=1  AuthorizationCodeResult=2
mcp 2.2.0   streamable_http_client=1  AuthorizationCodeResult=2   （传输层用 httpx2）
```

**顺带澄清一个谜题**：`httpx2` 在 **mcp 2.2.0 里是真实依赖**（`import httpx2`、`from httpx2 import EventSource`）。
所以原代码**不是笔误，而是按 2.2.0 写的** —— 只是环境装成了 1.30.0。
这解释了为什么三处 API 同时不匹配：**代码与 SDK 版本错配**才是根因。
