# 领域 D 收尾 · 读边界 + 网络墙（M-D1b）

> 调研：一轮仓库侦察 + 一轮联网（28 引用源）。方案与评审记录见 §八。
>
> **前情**：[plan-m-d.md](plan-m-d.md) 完成了**写边界**，读边界与网络墙被显式裁剪
> （"双墙缺一不可"）。本轮补上。
>
> **一句话**：调研里那句"**只有文件系统墙没有网络墙会泄漏**"在本仓库的落点 ——
> `--ro-bind / /` 让 `~/.ssh/id_rsa` 可读（**实测**），而 bash 拥有完整宿主网络（**实测**）。
> 于是**读泄漏可以直接外带**。

---

## 一、调研结论

### 1.1 联网调研主线

**读边界默认不是白名单**：主流代理（Claude Code、Codex）默认"**全盘可读、仅写受限**"，
`~/.ssh`、`~/.aws/credentials` **默认可读**，必须显式配置 `denyRead` / `credentials` 规则才关闭。
→ **"拒绝读敏感路径"是配置项而非默认行为**，这是读边界的第一失效点。

**白名单 vs 遮蔽是两种语义**：

| | 白名单 / deny | 遮蔽（mask / 空目录 / `/dev/null` 覆盖） |
|---|---|---|
| 抗绕过 | 强（内核/LSM 仲裁） | 中（依赖代理回填，存在回填旁路） |
| 可用性 | 差（易误伤） | 好（工具可继续鉴权） |
| 典型风险 | 路径拼写绕过（symlink/相对路径） | 回填逻辑漏洞、非预期通道泄露 |

**bwrap 没有路径级读 denylist** —— 它的语义是"你 bind 什么就有什么"，所以读边界只能靠**遮蔽**：
`--tmpfs <dir>`（目录变空）或 `--ro-bind /dev/null <file>`（文件不可读）。

**网络墙的主流做法是"独立 netns + 代理桥接"**：`--unshare-net` 造出只有 loopback 的 netns，
再由 `socat` 把沙箱内 Unix socket 桥接到沙箱外的 HTTP/SOCKS 代理，代理按域名白名单放行。
**代理必须在 netns 之外**（它需要宿主连通性才能转发）。

**seccomp 无法仲裁 `connect()` 的 sockaddr 路径**（BPF 不能解引用指针）→
纯 seccomp 只能粗粒度禁 `socket(2)` 的**域**，**AF_UNIX 路径级控制做不到**。
这正是 CVE-2026-47128 的根因之一。

**CVE-2026-47128**（nono，CVSS 6.1，CWE-863）：Landlock/seccomp 未拦截本地 AF_UNIX
（含 abstract 命名空间）→ 沙箱内进程经 per-user systemd D-Bus 套接字执行 `systemd-run --user`
在沙箱外派生未受限兄弟进程。修复引入 Landlock v6 抽象套接字作用域 +
**基于 seccomp user-notification 的 AF_UNIX 路径名中介**。

**2026-07「Week of Sandbox Escapes」**：多数逃逸并非打破隔离原语，而是
**沙箱外可信组件**（IDE hook、Python 扩展、Git 子系统、Docker daemon）消费了沙箱产出的文件
—— 即"**信任交接**"型逃逸。

### 1.2 失效模式（与本轮直接相关的）

| # | 失效模式 | 本仓库落点 |
|---|---|---|
| F1 | **默认全盘可读** | `--ro-bind / /` → `~/.ssh/id_rsa` 可读（**实测**） |
| F5 | **环境变量泄露** | `--proc /proc` 挂着 → `/proc/<pid>/environ` 可读（含 API key） |
| F8 | **AF_UNIX/D-Bus 逃逸** | `IpcService` 的 `/tmp/ai-coding-*.sock`（暴露 dispatcher 全部 handler）；`/run/user/*/bus` |
| F11 | **通配白名单 = 无墙** | `allowedDomains: ["*"]` 时不应建 netns |
| F14 | **沙箱外消费产物** | hook / MCP / Git 子系统（不在本轮范围） |

---

## 二、仓库侦察（子智能体 + 我逐条实测）

### 2.1 **必须纠正的一条环境事实**（侦察最重要的发现）

`plan-m-d.md §2.5` 与 `process_sandbox.py` docstring 都写着"**本内核拒绝命名空间构造**"。
**实测不成立** —— 真实原因是**容器缺 `CAP_SYS_ADMIN` + root 身份**，与内核无关：

```text
$ bwrap --version
bubblewrap 0.9.0
$ bwrap --ro-bind / / --dev /dev /bin/true
bwrap: Creating new namespace failed: Operation not permitted      # exit 1
$ bwrap --unshare-user-try --ro-bind / / --dev /dev /bin/true        # exit 0  ← 可用
```

**`--unshare-user-try` 是本环境的解锁开关**，而仓库的 `wrap()` 没有它 →
`available()` 恒返回 `False` → **7 条真实子进程用例全部 skip**，
**写边界在开发机上一条都没跑到**（而四能力其实全都可用）。

### 2.2 逐项实测（前缀统一 `--unshare-user-try`）

| 能力 | 结果 | 判定 |
|---|---|---|
| `--ro-bind / /` | exit 0 | ✅ |
| `--bind`（写边界） | 内成功 / 外 `Read-only file system` | ✅ |
| `--tmpfs` 在 `--bind` 之前 | 工作区存活 | ✅（顺序敏感，与 U7 一致） |
| `--proc /proc` | exit 0 | ✅ |
| `--unshare-pid` | exit 0 | ✅ |
| **`--proc /proc` + `--unshare-pid` 同时** | **`Can't mount proc on /newroot/proc: Operation not permitted`** | ❌ **本环境冲突** |
| `--unshare-net` | `Network is unreachable` | ✅ |
| `--seccomp <fd>`（手写 BPF） | `openat`→ENOENT；`socket`→EPERM；`socket`→KILL | ✅ |
| 空 seccomp 过滤器 | `prctl(PR_SET_SECCOMP) reported EINVAL` | ⚠️ 过滤器必须非空 |

**读边界实测**（`/root/rd` 下放了 `SECRET-KEY` 与 `AWSSECRET`）：

```text
① 现状（--ro-bind / /）      : SECRET-KEY / AWSSECRET        ← 可读
② --tmpfs <dir> + --ro-bind /dev/null <file> : cat: No such file or directory   ← 不可读
```

**网络墙实测**：默认 `TimeoutError: timed out`（有网）；`--unshare-net` → `Network is unreachable`。

### 2.3 接入点

| 关注点 | 接入点 |
|---|---|
| **唯一 wrap 入口** | `spawn_argv()`（返回 `list[str]`，类型上排除 join） |
| **机制组装处** | `BwrapBackend.wrap()` 的 prefix 构造 —— **读边界与网络墙都收敛在这里** |
| **策略层** | `ProcessSandboxPolicy`（`mode` / `bypass` / `writable_paths`） |
| **配置面** | `ProcessSandboxConfig`（`config/models.py`） |
| **组合根** | `build_process_sandbox()` ← `server/world.py` |
| **调用方** | `bash_tool`（前台）/ `jobs.py`（后台）/ 子智能体栈 |
| **路径层（正交）** | `security/sandbox.py` 的 `resolve()` / `open_inside()` |

### 2.4 三条必须原样保持的硬契约

1. `spawn_argv` 返回 `list[str]`，**禁止 join**（join + `shell=True` 会二次解析 —— 评审 P0-1）。
2. `roots` 来自 `Sandbox.roots` —— 路径层与进程层**同一个可写面**。
3. **fail-closed 三态**：`enforce`+无机制→拒；`enforce`+`wrap()` 返 `None`→拒；`warn`→继续但发 `Degraded`。

### 2.5 测试载体

| 文件 | 覆盖 | 本轮动作 |
|---|---|---|
| `tests/test_process_sandbox.py` | U1–U7 + 子智能体接线 | **修正 skip reason**（"kernel refuses" → userns/CAP_SYS_ADMIN）；**U2b 反转**；新增读边界/网络墙/AF_UNIX 用例 |
| `tests/test_sandbox.py` | 路径层 | **不动**（正交） |
| `tests/test_bash_red_lines.py` | 危险命令文本层 | **不动**（非 OS 层） |

---

## 三、方案：逐项实现

### A. 前置 · 修构造（探针误判）

**问题**：`wrap()` 缺 `--unshare-user-try` → `available()` 恒 `False` → 7 条真实用例全 skip。

**修法**：构造**分级尝试**，选第一个能跑通的档位，并**上报选中档位**（评审 P3-6 的"fail-closed 必须由产品机制保障"）。

```python
#: 构造档位，从强到弱。第一个能跑通的被选中并上报。
_CONSTRUCTION_TIERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("full",      ("--unshare-user-try", "--ro-bind", "/", "/",
                   "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev",
                   "--unshare-pid", "--die-with-parent", "--new-session")),
    ("no-pid",    ("--unshare-user-try", "--ro-bind", "/", "/",
                   "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev",
                   "--die-with-parent", "--new-session")),
    ("no-proc",   ("--unshare-user-try", "--ro-bind", "/", "/",
                   "--tmpfs", "/tmp", "--dev", "/dev",
                   "--die-with-parent", "--new-session")),
)
```

* **档位选择**：构造时逐级试 `-- /bin/true`，选第一个成功的；**缓存**（避免每次 bash 都试）。
* **上报**：选中 `no-pid` → `Degraded(reason="sandbox.tier.no-pid")`；
  `no-proc` → `Degraded(reason="sandbox.tier.no-proc")`。`full` 不上报（无降级）。
  —— **降级必须是可见的**，不能"静默换一个更弱的构造"。
* **`none`**：三档全失败 → `available()` 为假 → fail-closed（现状语义不变）。
* **本环境预期**：`no-pid` 或 `no-proc`；**CI（真 VM）预期**：`full`。
  两处都能跑，不再出现"7 条全 skip"。

### B. 读边界（遮蔽）

**机制**：`ProcessSandboxPolicy.read_deny_paths: tuple[Path, ...]` —— **与 `writable_paths` 对称**：
**写面是 allow-list，读面是 denylist**，两者不能互相推导。

**默认清单**（用户拍板：标准集 + 本仓库自身凭据）：

| 类别 | 路径 | 为什么 |
|---|---|---|
| SSH | `~/.ssh` | 横向移动 |
| 云凭据 | `~/.aws`、`~/.config/gcloud`、`~/.azure` | 云账号接管 |
| 密钥环 | `~/.gnupg`、`~/.netrc`、`~/.docker/config.json`、`~/.kube/config` | 签名/拉镜像/集群 |
| **本仓库自身** | `~/.config/ai-coding`（config.yaml 的 API key）、`~/.local/share/ai-coding/mcp-auth.json`（MCP OAuth token） | 泄露即交出模型凭据与 MCP 授权 |
| 系统 | `/etc/shadow`、`/etc/ssh/ssh_host_*_key` | 提权 |

**构造规则**：

| 目标类型 | bwrap 参数 | 备注 |
|---|---|---|
| **目录** | `--tmpfs <dir>` | 目录存在但为空 |
| **文件** | `--ro-bind /dev/null <file>` | 文件不可读 |
| **glob**（`ssh_host_*_key`） | 构造时**展开**（读目录 + fnmatch）后逐条遮蔽 | bwrap 无 glob |

**三条必须写进的局限**：

1. **顺序敏感**：遮蔽项必须在 `--ro-bind / /` **之后**（与 `--bind <roots>` 同一序位问题，U7 已钉）。
2. **符号链接**：`--ro-bind /dev/null <link>` 遮的是**链接本身**而非目标 → **目录用 `--tmpfs` 更稳**。
3. **`/proc/<pid>/environ`**：`--proc /proc` 挂着时，沙箱内可读**父进程的 env**（含 API key）。
   → 本轮**不修**（要修需改 env 传递方式，是独立工程），但**写进残余风险表**。

**fail-closed**：`enforce` 下若某条遮蔽**无法构造**（路径不存在且非 glob）→ **拒绝而非跳过**
（与 `SANDBOX_WRAP_GAP` 同一精神）。路径不存在时：目录/glob **跳过**（不是错误），
**显式文件**（如 `~/.netrc`）缺失 → 也跳过（它可能确实不存在）。
→ **判定规则**：`--tmpfs` 的父目录不存在 → 跳过；`--ro-bind /dev/null` 的目标不存在 → 跳过。
**不静默**：跳过了哪些、为什么，进 `Degraded` 事件。

**U2b 反转**（受评审的语义变更）：从"读工作区外**仍成功**"改为"读 `~/.ssh/id_rsa` **失败**"。
**必须在同一 PR 里记录这是一次受评审的语义变更** —— 基准/测试的 golden 会强制它显式发生。

### C. 网络墙

**配置面**（与 `writable_paths` 对称）：

```python
class NetworkConfig:
    mode: Literal["off", "deny", "proxy"] = "deny"   # 用户拍板：默认 deny + 可配代理桥接
    #: mode == "proxy" 时：代理监听的 **Unix socket 路径**（在沙箱外）
    proxy_socket: Path | None = None
    #: 域名/IP 白名单（交给代理判定）。空 = 全拒。绝不允许 "*"
    allow: tuple[str, ...] = ()
```

**三档构造**：

| mode | bwrap 参数 | 效果（实测） |
|---|---|---|
| `off` | 不加 | 完整宿主网络（今天的行为） |
| `deny` | `--unshare-net` | `Network is unreachable` |
| `proxy` | `--unshare-net` + `--bind <proxy_socket> <proxy_socket>` + env `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY=unix://<sock>` | 只有经代理的流量能出 |

**顺序**：`--bind <proxy_socket>` 必须在 `--tmpfs /tmp` **之后**（否则被遮蔽）。
若代理套接字在 `/tmp` 下 → 必须显式 `--bind`，**不能依赖"没被遮蔽"**。

**代理进程**（新，`server/sandbox_proxy.py`）：

* 最小 HTTP/CONNECT 代理，监听 **Unix socket**（`--bind` 进沙箱）。
* 按 `allow` 白名单判定：**空 = 全拒**；命中放行；**绝不接受 `"*"`**（调研 F11：通配 = 无墙）。
* 支持 `CONNECT`（HTTPS）与 plain HTTP；非 HTTP 协议（数据库、裸 TCP）**不支持** ——
  调研明确指出域名白名单对 IP/CIDR/UDP/非 HTTP 是**盲区**，本轮**不做** IP/CIDR（写进残余风险）。
* 生命周期由组合根管理（随 world 起停）。

**为什么默认 `deny` 而不是 `off`**：与 `writable_paths` 的取舍一致 ——
写面已经默认收紧并付出了"打断真实构建"的代价，读/网两面若默认放开，
等于"**只有文件系统墙没有网络墙会泄漏**"白做。`proxy` 档是可用性的出口。

### D. AF_UNIX 防护

**问题**：bwrap **没有** AF_UNIX 的 `connect`/`bind` 过滤粒度；seccomp **无法仲裁路径**
（BPF 不能解引用指针）→ **路径级白名单做不到**。

**本轮的两层防护**：

| 层 | 机制 | 覆盖 |
|---|---|---|
| **默认遮蔽**（默认开） | `--ro-bind /dev/null <sock>` 遮蔽危险套接字 | `/run/user/*/bus`（D-Bus）、`/var/run/docker.sock`、`/run/docker.sock` |
| **seccomp 全域阻断**（可选加强档） | `--seccomp <fd>` + BPF：`socket(AF_UNIX)` → `EPERM` | 所有 AF_UNIX（**会断合法用法**：npm 的部分 IPC、docker CLI） |

**"可配允许清单"的实现方式**（因为 seccomp 做不到路径级）：
**允许 = 显式 `--bind` 进沙箱的套接字**（如代理桥）。其余 AF_UNIX 要么被遮蔽、要么被 seccomp 全域阻断。
→ **"清单"是 bind 清单，不是 seccomp 规则**。这一点必须在文档里写清楚（否则会被误读）。

**本仓库的具体落点**（必须写进残余风险）：

1. `IpcService` 的 `/tmp/ai-coding-*.sock`（`server/service.py:85`）—— 暴露 dispatcher 的**全部 handler**。
   当前被 `--tmpfs /tmp` **偶然**遮蔽（**巧合而非机制**）；一旦套接字路径改到 `/run`，墙即失效。
2. `launcher.py:57-65` 把 `AI_CODING_SOCKET` 通过 **env** 交给前端子进程 —— env 在沙箱内可读。
3. `--ro-bind / /` 让 `/run/user/*/bus`、`/var/run/docker.sock` 等**任意**宿主套接字文件可见。

---

## 四、逐文件改动清单

| # | 文件 | 改动 | 为什么 |
|---|---|---|---|
| 1 | `security/process_sandbox.py` | `_CONSTRUCTION_TIERS` + 分级探测 + `--unshare-user-try` + 读遮蔽 + 网络开关 + AF_UNIX 遮蔽 + seccomp 档 | 机制组装处 |
| 2 | `security/process_sandbox.py` | `ProcessSandboxPolicy.read_deny_paths` / `NetworkConfig` / `SandboxProxy` 协议 | 策略层 |
| 3 | `config/models.py` + `config.example.yaml` | `read_deny_paths` / `network` 配置面 | 配置 |
| 4 | `server/sandbox_proxy.py`（新） | 最小 HTTP/CONNECT 代理（Unix socket + 域名白名单） | 网络墙的 `proxy` 档 |
| 5 | `server/world.py` / `cli.py` | 代理进程生命周期 + 配置接线 | 组合根 |
| 6 | `tools/builder.py` | 透传新配置给工具栈与子智能体栈 | 接线 |
| 7 | `core/events.py` | **复用** `Degraded`（档位降级 / 遮蔽跳过 / 代理不可用） | 上报 |
| 8 | `docs/data-contracts.md` | 契约同步 | |
| 9 | `docs/plan-m-d.md` §七/§7.1 | 从"不做"改为本轮范围 | |

**测试条目**：

| # | 文件 | 改动 |
|---|---|---|
| T1 | `tests/test_process_sandbox.py` | 修正 skip reason；**U2b 反转**；新增：读遮蔽生效（目录/文件/glob）、网络三档、AF_UNIX 遮蔽与 seccomp、代理桥连通、档位降级上报 |
| T2 | `tests/test_sandbox_proxy.py`（新） | 代理白名单：空=全拒、命中放行、**`"*"` 被拒**、CONNECT 与 plain HTTP |
| T3 | `tests/test_process_sandbox.py` | 顺序断言扩展：遮蔽项在 `--ro-bind / /` 之后、代理 bind 在 `--tmpfs` 之后 |

### 4.1 语义等价性

* **`network="off"` 且 `read_deny_paths=()` 且档位 `full`** → 与今天**逐字节相同**（除 `--unshare-user-try`，
  它是"尝试启用 userns，失败则继续"，**不改变成功路径的语义**）。
* **`read_deny_paths` 默认非空** → **行为变更**（今天能读 `~/.ssh`，之后不能）—— 这是**本轮的目的**。
* **`network` 默认 `deny`** → **行为变更**（今天 bash 有完整宿主网络）—— 同上。
* **U2b 反转** → 受评审的语义变更（记录在案）。

### 4.2 对数据流的影响

* **bash/job 的进程树**：argv 多一层遮蔽项与网络开关；`proxy` 档多一个常驻代理进程。
* **配置面**：`ProcessSandboxConfig` 增两个字段；项目层**不能**关（`read_deny_paths`/`network`
  应进 `EXECUTION_KEYS` 同类对待 —— **本轮补上**，与 `process_sandbox` 一致）。

---

## 五、RIPR 用例

### Red（先写，必须变红）

| 编号 | 用例 | 期望红相（今天必须失败） |
|---|---|---|
| V1 | `test_a_construction_tier_is_selected_and_reported` | 档位探测选中并**上报降级**（今天无档位概念） |
| V2 | `test_the_mechanism_runs_without_cap_sys_admin` | 加 `--unshare-user-try` 后 `available()` 为真（今天恒 False → 7 条真实用例全 skip） |
| V3 | `test_a_sandboxed_process_cannot_read_ssh_keys` | `enforce` 下 `cat ~/.ssh/id_rsa` → **失败**（今天：成功读到） |
| V4 | `test_a_sandboxed_process_cannot_read_cloud_credentials` | 同上，`~/.aws/credentials` |
| V5 | `test_a_sandboxed_process_cannot_read_the_repos_own_api_key` | `~/.config/ai-coding/config.yaml` 不可读 |
| V6 | `test_the_read_boundary_hides_a_directory_and_a_file` | `--tmpfs <dir>` 与 `--ro-bind /dev/null <file>` 各一条 |
| V7 | `test_the_read_boundary_expands_globs` | `ssh_host_*_key` 展开后逐条遮蔽 |
| V8 | `test_a_missing_read_deny_path_is_skipped_and_reported` | 路径不存在 → 跳过 + `Degraded`（**不静默**） |
| V9 | `test_a_sandboxed_process_has_no_network_by_default` | 默认档 `curl 1.1.1.1` → **不可达**（今天：可达） |
| V10 | `test_the_proxy_bridge_lets_an_allowed_domain_through` | `proxy` 档 + 白名单 → 经代理可达；未列域名被拒 |
| V11 | `test_the_proxy_refuses_a_wildcard_allow_list` | `allow=["*"]` → **拒绝构造**（调研 F11：通配 = 无墙） |
| V12 | `test_a_sandboxed_process_cannot_reach_the_dbus_socket` | `/run/user/*/bus` 被遮蔽 |
| V13 | `test_seccomp_blocks_af_unix_when_enabled` | seccomp 档 → `socket(AF_UNIX)` → `EPERM` |
| V14 | `test_the_proxy_socket_bind_comes_after_the_tmpfs` | 顺序断言（代理在 `/tmp` 下时仍可达） |

> V3–V14 需要真 bwrap：`@pytest.mark.skipif(...)` —— **显式理由**（现在会因 V2 的修复而在本环境跑起来）。

### Prove

1. `.venv/bin/python -m pytest -q`
2. `.venv/bin/ruff check .` / `.venv/bin/mypy ai_coding`
3. **突变测试**：
   * 去掉 `--unshare-user-try` → V2 变红
   * 去掉读遮蔽项 → V3/V4 变红
   * 把 `network` 默认改回 `off` → V9 变红
   * 允许 `allow=["*"]` → V11 变红
   * 把遮蔽项放到 `--ro-bind / /` **之前** → V6/V7 变红（顺序错则遮蔽无效）
   * 把代理 bind 放到 `--tmpfs /tmp` **之前** → V14 变红
4. **3.10 venv 跑 CI 同构七步**
5. **端到端**：`enforce` 下真跑 `cat ~/.ssh/id_rsa`（失败）+ `curl 1.1.1.1`（不可达）

### Refactor

* 确认读遮蔽与写可写面**互不推导**（写是 allow-list，读是 denylist）
* 确认 `spawn_argv` 仍返回 `list[str]`（无 join）

---

## 六、验收判据

* **读边界**：`enforce` 下沙箱内进程**读不到** `~/.ssh`、`~/.aws`、本仓库自身凭据、`/etc/shadow`；
  遮蔽项顺序正确；跳过的路径**可见**（不静默）。
* **网络墙**：默认档**无网络**；`proxy` 档只有白名单域名可达；**`"*"` 被拒**。
* **AF_UNIX**：危险套接字被遮蔽；seccomp 档可全域阻断。
* **构造**：本环境与 CI **都能跑**（不再 7 条全 skip）；降级**可见**。
* 3.10 与 3.13 全量绿；真实 CI 双 job 绿。

---

## 七、不做的事

* **不做 `/proc/<pid>/environ` 的隔离** —— 要修需改 env 传递方式（独立工程），写进残余风险。
* **不做 IP/CIDR/UDP 级网络管控** —— 调研明确指出域名白名单对裸 IP/UDP 是盲区；
  本轮只到域名白名单，IP/CIDR 留待下一轮。
* **不做 seccomp user-notification 的 AF_UNIX 路径中介** —— nono 的修法，复杂度高一个量级；
  本轮用"遮蔽 + 全域阻断"覆盖。
* **不做沙箱外可信组件的治理**（hook / MCP / Git 子系统）—— 调研的"信任交接"型逃逸，
  要改的是宿主的信任边界，不是进程沙箱。
* **不改路径层**（`security/sandbox.py`）—— 与进程层正交。
* **不修 `known_miss`**（`x="rm -rf /"; $x`）—— 独立的语义变更，留在 M-D2 的基准里可见。

### 7.1 残余风险（显式记录）

| 风险 | 说明 | 处置 |
|---|---|---|
| **`/proc/<pid>/environ` 泄露** | `--proc /proc` 挂着 → 沙箱内可读父进程 env（含 API key） | 本轮**不修**；写进本文档 |
| **IP/CIDR/UDP 绕过** | 域名白名单对裸 IP/UDP 无效 | 本轮**不修** |
| **AF_UNIX 路径级控制** | seccomp 无法仲裁路径（BPF 不能解引用指针） | 用"遮蔽 + 全域阻断"覆盖 |
| **信任交接型逃逸** | 沙箱外组件消费沙箱产物 | **不在本轮范围** |
| **macOS / Windows** | 本轮只做 Linux（bwrap）；Seatbelt 已弃用、Windows 用受限令牌 | 平台相关，另立 |

---

## 八、评审记录

### 8.1 方案评审（子智能体，第 1 轮）

_待填。_

### 8.2 方案评审（子智能体，第 2 轮）

_待填。_

### 8.3 实现评审（子智能体）

_待填。_
