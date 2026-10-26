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

**修法**：构造**分级尝试**，选第一个能跑通的档位，并**上报选中档位**。

```python
#: 构造档位，从强到弱。第一个能跑通的被选中并上报。
#: **不含 --proc+--unshare-pid 的档必须加 --tmpfs /proc** —— 因为 --ro-bind / /
#: 是递归绑定，宿主的 /proc 与 --proc 无关地被带进沙箱（评审 P0-1）。
_CONSTRUCTION_TIERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("full",    ("--unshare-user-try", "--ro-bind", "/", "/",
                 "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev",
                 "--unshare-pid", "--die-with-parent", "--new-session")),
    ("no-proc", ("--unshare-user-try", "--ro-bind", "/", "/",
                 "--tmpfs", "/tmp", "--tmpfs", "/proc", "--dev", "/dev",
                 "--die-with-parent", "--new-session")),
)
```

#### A.1 **关闭 `/proc/<pid>/root` 逃逸（评审 P0-1 —— 本轮最重要的一条）**

原方案有 `no-pid`（保留 `--proc /proc`、只去 `--unshare-pid`）。**这一档会击穿整个文件系统墙**：

* **`/proc` 是否暴露宿主 pid，取决于有没有 `--unshare-pid`。**
  不 unshare-pid 时 procfs 展示的是**宿主 pid 命名空间** —— **实测**：宿主 9 个 pid，
  沙箱内（无 `--unshare-pid`）可见 **10 个**。
* 于是沙箱内可读 `/proc/<host-pid>/root/...` —— 这是**指向宿主根的符号链接**，
  **绕过 `--ro-bind /dev/null <file>` 的读遮蔽**（遮蔽只作用于沙箱挂载命名空间里的路径）。
* 同一条通道还绕过 `--tmpfs /tmp` 对 `IpcService` 套接字的**偶然**遮蔽。
* **更糟的是方案自己的测试抓不到它**：V3–V5 只 `cat ~/.ssh/id_rsa`（被遮蔽 → 失败 → 绿），
  不会去 `cat /proc/<pid>/root/...`。于是"enforce 下读不到 `~/.ssh`"这条验收判据
  **在该档下是假的，而测试全绿**。

**第 2 轮评审的关键更正（P0 未修复）**：第 1 轮把修法定为"删掉 `no-pid`"，**不成立** ——
**`--ro-bind / /` 是递归绑定**，宿主的 `/proc`（以及 `/sys`、`/run`）**与 `--proc` 无关**地被带进沙箱。
**实测**：

| 构造 | `/proc` | `/proc/1/comm` | 读 `/proc/1/root/...` |
|---|---|---|---|
| 选择性 bind（**不含 `/`**） | **不存在** | — | — |
| `--ro-bind / /`（= `no-proc` 档） | **存在，61 条** | **`tini`（宿主 init）** | `Permission denied`（**仅 userns 挡住**） |
| `--ro-bind / / --unshare-pid` | 存在，62 条 | **`tini`** | `Permission denied`（仅 userns） |
| `--ro-bind / / --proc /proc` | 存在，61 条 | `tini` | `Permission denied`（仅 userns） |
| **`--ro-bind / / --tmpfs /proc`** | **空** | — | **`No such file or directory`** ✅ |

**`--unshare-pid` 单独并不能关闭逃逸** —— 递归 bind 进来的是宿主 procfs 超级块，仍显示宿主 pid。
**唯一能关闭逃逸的是 `--tmpfs /proc`**（或 `--proc /proc` + `--unshare-pid`，而本环境后者 EPERM）。

→ **规则写死**：

1. **档位表**：不含 `--proc`+`--unshare-pid` 的档，**必须加 `--tmpfs /proc`**。
2. **V1b 补行为断言**（不只是 argv 不变量）：沙箱内 `ls /proc` **为空**、
   读 `/proc/<pid>/root/...` **失败** —— 否则测试仍抓不到（这正是第 1 轮 P0 的原始教训）。

**实测补充**：本环境的 userns 恰好挡住了 `/proc/1/root`（子 userns 不能 ptrace 初始 userns 的 pid 1），
所以**这里没复现**。但这**不是机制保证** —— 在不使用 userns 的部署下，同 uid 即可读。
**不能把"恰好被 userns 挡住"当防护。**

**连带更正**：
* §B 局限 3 与 §D.4 原写"`--proc /proc` 挂着时"才暴露 `/proc/<pid>/environ` —— **错**，
  `/proc` **始终**存在（递归 bind）。
* §7.1 需把 **`/proc/<pid>/root`** 列入残余风险。
* **递归 bind 还意味着沙箱能看到 `/sys`、`/run` 等宿主子挂载** —— 读边界与 AF_UNIX 的
  暴露面比原方案假设的**更大**（写进残余风险）。

#### A.2 档位探测必须用**完整构造**（评审 P1-2）

档位是**前缀**，而真实 `wrap()` 还会追加 `--bind <roots>`、`--bind <writable>`、读遮蔽、
`--unshare-net`、代理 bind。若探测只试前缀，会出现"探针过、真实调用炸"——
**这正是 `plan-m-d.md` §8.2 的 P2-4 在本方案里复现**。

→ **探测 = `wrap(["/bin/true"], roots)` 带上当次全部附加项**，逐级试，选第一个成功的。
（`plan-m-d.md` 已实现"探针复用 `wrap()`"，本轮沿用并扩展到分级。）

#### A.3 档位上报的接线（评审 P1-1）

现状：`spawn_argv` 只在 `available()` 为假时用 `emit`；`BwrapBackend.available()` 只返回 `bool`。
**档位信息存在于 backend 内部，`spawn_argv` 拿不到** —— 按现文实现，档位根本不会上报。

→ **写死通路**：

```python
class BwrapBackend:
    @property
    def tier(self) -> str: ...          # "full" | "no-proc" | "none"，探测后缓存
    def gap(self) -> str: ...           # "none" 时返回 BWRAP_GAP

# spawn_argv 内：
if backend.tier != "full":
    if emit is not None:
        emit(f"sandbox.tier.{backend.tier}")     # 降级必须可见
```

* `full` **不上报**（无降级）；`no-proc` 上报 `sandbox.tier.no-proc`。
* **频次**：探测在 backend 实例上做**一次**并缓存 → 整个进程生命周期最多上报一次
  （在组合根构造后）。**不是每次 bash 都发**（评审 P2-1）。
* **语义区分**：`Degraded` 只用于"**增强放弃了**"（档位降级）。
  "路径不存在所以跳过遮蔽"**不是**降级 —— 走单独通道（见 §B.2），**不污染 `/trace` 的 degraded 计数**。

#### A.4 本环境预期

* 本环境：`--proc` + `--unshare-pid` 冲突 → 选中 **`no-proc`**（上报 `sandbox.tier.no-proc`）。
* CI（真 VM）：预期 **`full`**。
* 两处都能跑，不再出现"7 条全 skip"。

#### A.4b **`no-proc` 档的代价必须写明（评审 P1-1 —— 实测）**

`--tmpfs /proc` 关掉逃逸的**代价是 `/proc` 完全不可用**。**实测**（`no-proc` 档）：

| 坏 | 好 |
|---|---|
| `ps` / `pgrep` / `free` / `uptime` / `df` / `mount` | `lsof -v` / `ss -l` |
| `readlink /proc/self/exe` | `git --version` / `node --version` / `python3 -V` |
| **bash 进程替换 `cat <(echo hi)`** | `pip` / `make` / `gcc` / `pytest` |

最隐蔽的是**进程替换**：`--dev /dev` 会建 `/dev/fd → /proc/self/fd`，`/proc` 一空就**悬空**
→ `cat: /dev/fd/63: No such file or directory`。

→ **三处写明**：① §7.1 残余风险；② `sandbox.tier.no-proc` 的上报文案带一句"代价：`/proc` 不可用"；
③ **加断言钉住**（沙箱内 `ps` 非零、`<(...)` 失败）—— 否则将来有人"顺手修好"它会直接重新打开逃逸。

**这是降级档的已知代价，不是缺陷。**

#### A.5 必须同步改的既有用例（评审 P1-3）

[test_wrap_keeps_the_isolation_switches](file:///workspace/ai-coding/tests/test_process_sandbox.py#L476-L480)
断言 `--unshare-pid` 在 argv 里，且**没有** `@_NEEDS_BWRAP` 门控。`no-proc` 档不含它 → **本环境必红**。
→ 改为断言"**选中档位应有的开关**"（`full` → 有 `--unshare-pid`；`no-proc` → 无），
并保留 A.1 的不变量断言。

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

**四条必须写进的局限**：

1. **顺序敏感（两条，不只一条 —— 评审 P2-3）**：遮蔽项必须在 `--ro-bind / /` **之后**，
   **且**在 `--tmpfs /tmp` **之后**（否则 deny 路径在 `/tmp` 下时会被 tmpfs 抹掉）。
   T3 的顺序断言要覆盖这两种。
2. **符号链接（评审 P1-4 —— 第 1 轮的对策不成立，第 2 轮更正表述）**：
   原写"`--tmpfs <link>` 同样作用在链接路径上"、"`--ro-bind /dev/null <link>` 遮的是链接本身"
   —— **都不对**。**实测**（bubblewrap 0.9.0）：

   | 操作 | 结果 |
   |---|---|
   | `--tmpfs <符号链接>` | **报错** `Can't mount tmpfs ... No such file or directory`，exit 1 |
   | `--ro-bind /dev/null <符号链接>` | **报错** `Can't create file ... No such file or directory`，exit 1 |
   | 遮蔽 `resolve()` 后的**目标** | **有效**：经链接读 → `No such file or directory`；直接读目标 → `Permission denied` |

   → **规则**：**先 `resolve()` 再遮蔽**，且**同时遮蔽原始路径与解析目标**（双保险）。
   * **残余缺口**：`resolve()` 只解析 deny 路径**自身**的符号链接；构造期快照，
     **运行期重指向不覆盖**（与 glob 同）。这仍是"遮蔽路径名"而非"遮蔽 inode"
     → 写进残余风险（与调研的 F3 同源）。
   * **V16 补断言**：直接对符号链接做 `--tmpfs`/`--ro-bind` **会报错** —— 锁死"必须先 resolve"。
3. **`/proc/<pid>/environ` 与 `/proc/<pid>/root`**：**`/proc` 始终存在**（`--ro-bind / /` 是递归绑定，
   与 `--proc` 无关）。A.1 的档位规则用 `--tmpfs /proc` 关掉整块 `/proc`；
   **`full` 档保留 `/proc`**（工具需要），此时 `/proc/<pid>/root` 与 `/proc/<pid>/environ`
   的通道**靠 `--unshare-pid` 缩小**（沙箱只看到自己的 pid），但**不是零**。
   → 本轮**不修**（要修需改 env 传递方式 / 完全不挂 `/proc`），写进残余风险。
4. **`--tmpfs` 要求目标可创建**：父目录不存在时 `--tmpfs` 会失败 → 按 §B.2 的规则跳过。

#### B.2 跳过 vs 报错（评审 P2-1）

| 情况 | 处置 | 是否上报 |
|---|---|---|
| deny 路径**不存在**（目录/文件/glob 无匹配） | **跳过**（它可能确实不存在） | **不进 `Degraded`** —— 这是正常情况，不是"增强放弃" |
| deny 路径存在但**遮蔽构造失败**（权限/IO） | **拒绝**（fail-closed） | `Degraded` |

* **`Degraded` 的语义是"enhancement gave up and fell back"** —— `~/.aws`、`~/.gnupg`、`~/.netrc`
  在普通开发机上**通常不存在**，把它们塞进 `Degraded` 会**污染 `/trace` 的 degraded 计数**。
* 跳过项走**单独通道**：`SandboxMaskReport`（一次性的、构造期汇总），供面板/日志查看。
* **频次**：构造期汇总**一次**，不是每次 bash。

#### B.3 读遮蔽与可写 bind 的重叠（评审 P2-2）

若某 `writable_paths` 落在某个 `read_deny_paths` 目录之下，`--bind`（可写）会**覆盖**前面的遮蔽
→ **静默解遮蔽**。

→ **规则写死**：**读遮蔽永远在写 bind 之后**（**deny 赢**）。
并在文档显式声明"**可写即默认可读**"是**不成立**的 —— 两者是独立的两面。
加一条用例（V6b）：`writable_paths` 与 `read_deny_paths` 重叠时，读仍被拒。

#### B.4 glob 展开（评审 P2-4）

* 抽一个 `_expand_globs(patterns) -> tuple[Path, ...]`，**读遮蔽与 AF_UNIX 遮蔽共用**
  （两套实现就是两处漂移源）。
* **glob 是构造期快照** —— 运行期新增的密钥文件**不会**被重新遮蔽。写进文档（这是能力边界）。
* 展开时同样做 §B 的 `resolve()`。

#### B.5 默认清单的完整性（评审 P2-5 / P2-1 / P2-2）

读面是 **denylist** —— **"永远少一条"正是调研批评的失效模式**（Pillar："allow-default 不是沙箱，
而是一张永远少一条的清单"）。因此：

* 默认清单**扩充**：`~/.git-credentials`、`~/.npmrc`、`~/.pypirc`、`~/.config/gh/hosts.yml`、
  `~/.cargo/credentials`、`~/.terraform.d/credentials`、`~/.bash_history`、`~/.python_history`、`~/.viminfo`。
* **按 XDG 展开，不写死 `~/.config`（评审 P2-1）**：[paths.py](file:///workspace/ai-coding/ai_coding/paths.py#L37-L54)
  用的是 `config_home()` / `data_home()`（尊重 `XDG_CONFIG_HOME` / `XDG_DATA_HOME`）。
  写死 `~/.config/ai-coding` 的话，**XDG 被覆盖时掩码落到空目录，API key 照样可读**。
* **补其他用户主目录**（评审 P3-2）：`/home/*/.ssh`、`/root/.ssh`（非当前用户时）。
* **但必须在残余风险里显式写**：
  * "denylist **不完整**，这是**刻意的能力边界**，不是保证"；
  * **工作区级配置不在读边界内**（评审 P2-2）：`config.yaml` / `config.local.yaml` 可含**同一个 API key**，
    而工作区**必然可读**（否则工具没法干活）→ "保护本仓库自身凭据"**只覆盖用户级**，不覆盖工作区级。

**fail-closed**：`enforce` 下若某条遮蔽**无法构造**（路径存在但 IO 失败）→ **拒绝而非跳过**。

**U2b 反转**（受评审的语义变更）：从"读工作区外**仍成功**"改为"读 `~/.ssh/id_rsa` **失败**"。
**必须在同一 PR 里记录这是一次受评审的语义变更** —— 基准/测试的 golden 会强制它显式发生。

### C. 网络墙

**配置面**（与 `writable_paths` 对称）：

```python
class NetworkConfig:
    mode: Literal["off", "deny", "proxy"] = "deny"   # 用户拍板：默认 deny + 可配代理桥接
    #: mode == "proxy" 时：代理监听的 **Unix socket 路径**（在沙箱外）
    proxy_socket: Path | None = None
    #: 域名白名单（交给代理判定）。空 = 全拒。**绝不允许 "*"**
    allow: tuple[str, ...] = ()
    #: mode == "proxy" 时，沙箱内桥接监听的 loopback 端口
    bridge_port: int = 0        # 0 = 自动选
```

**三档构造**：

| mode | bwrap 参数 | 效果（实测） |
|---|---|---|
| `off` | 不加 | 完整宿主网络（今天的行为） |
| `deny` | `--unshare-net` | `Network is unreachable` |
| `proxy` | `--unshare-net` + `--bind <proxy_socket> <proxy_socket>` + **沙箱内桥接** + env | 只有经代理的流量能出 |

#### C.1 **沙箱内桥接：与 `list[str]` 契约相容的落地方式（评审 P1-1）**

`HTTP_PROXY=unix://<sock>` **不是标准代理 scheme**，**curl 直接拒绝**：

```text
$ curl ... (ALL_PROXY=unix:///tmp/nonexistent-proxy.sock)
curl: (7) Unsupported proxy scheme for 'unix:///tmp/nonexistent-proxy.sock'
```

（`http://unix/...`、`socks5h://unix/...` 同样不支持。）

**为什么不能"argv 前置 socat"**：`socat` 是**服务器**，前置到 bwrap argv 后
**用户命令不会执行**；若改成 `sh -c "socat ... & exec ..."` 包装，则**重新引入 shell 解析层**
—— 正是 `plan-m-d.md` P0-1 禁止的 join+shell 路径。

→ **落地方式：一个由组合根注入的小程序作为 `argv[0]` 前置**（`wrap()` 仍返回 `list[str]`）：

```
bwrap ... -- <sandbox-bridge> --unix <proxy_socket> -- <用户 argv...>
```

* `<sandbox-bridge>` 是一个**小 Python 程序**（随包分发，`/usr` 已 bind → `python3` 可用；
  或直接用一个 `socat` 调用 + `exec`，取决于实现期实测）。
* **生命周期模型（评审 P1-2 —— 原设计自相矛盾）**：原写"起后台**线程**转发 + `execv`" ——
  **`execv` 会杀掉所有线程，转发线程必死**，于是"桥启动成功但转发已死" →
  **网络静默挂死**，而 §C.1 的 fail-closed 只覆盖"启动失败"，覆盖不到这种。
  → **写死**：**`fork` 转发子进程**（父进程 `execv` 用户命令；子进程跑转发循环），
  并用 **`PR_SET_PDEATHSIG`**（或父子退出联动）保证用户命令退出时转发子进程被清理。
  备选：桥**不** `execv`，而是 `spawn` 用户命令并 `wait`。
* **参数协议**（评审 P3-3）：`--unix <sock>` 之后**首个 `--`** 为界，其后全部是用户 argv。加用例覆盖歧义。
* **桥路径必须从读边界豁免**（评审 P1-3 —— 实测）：桥若落在被 `--tmpfs` 遮蔽的目录下
  → `execvp ... ENOENT`，整条命令不执行（fail-closed，但 **`proxy` 档直接不可用**）。
  → **写死**：桥路径 `resolve()` 后**显式从 `read_deny_paths` 排除**；
  并加一条断言（桥路径永不被遮蔽）。
* **`block_af_unix=True` 与 `network.mode="proxy"` 互斥**（评审 P1-3）：
  seccomp 档会拒 `socket(AF_UNIX)` → 桥无法连代理套接字。
  → **配置校验期报错**（与 C.3 的 `"*"` 同一层）。
* **端口协调**：端口由桥选，env 由桥设 —— **不存在"env 无从得知端口"的问题**（评审 P1-1 的缺口）。
* **env 的基线清理**走 bwrap 的 `--setenv` / `--unsetenv`（`wrap()` 只返回 argv，
  env 必须走 bwrap 参数 —— 评审 P3）。清理 `NO_PROXY` / `http_proxy` / `all_proxy`。
* **桥挂了怎么办**（评审 P1-1 未覆盖）：桥在**沙箱内部**，其失败对外不可见。
  → **fail-closed**：桥无法启动时**以非零退出**（用户命令根本不会执行）→ 表现为"网络不可用"，
  且**不会**退化成"有网"。桥自身的错误信息要清晰可辨（"sandbox proxy bridge failed: ..."）。
* **loopback 无需额外处理**（评审 P3）：`--unshare-net` 下 loopback **已 UP**（实测），
  桥监听 `127.0.0.1` 可行 —— 作为 V10 的一条断言固化。

**顺序**：`--bind <proxy_socket>` 必须在 `--tmpfs /tmp` **之后**（否则被遮蔽）；
若代理套接字在 `/tmp` 下 → **必须**显式 `--bind`，**不能依赖"没被遮蔽"**。

#### C.2 代理白名单的匹配语义（评审 P1-6）

**必须写死**，否则子串匹配就是安全洞（`"example.com" in host` 会让
`evil-example.com`、`example.com.evil.com` 都命中）：

```
host == allow_entry  or  host.endswith("." + allow_entry)
```

* `CONNECT` 请求的 `host:port` **先剥离端口**再匹配。
* 大小写不敏感（host 统一 `lower()`）。
* 空 `allow` = **全拒**。
* **`"*"` 一律拒绝**（见 C.3）。

#### C.3 `"*"` 的拒绝点（评审 P2-10）

**在配置校验期拒绝**（`NetworkConfig` 校验 → 加载期报错），**不是**构造期。
理由：配置错误应当在**启动时**响亮失败，而不是在第一条 bash 命令时。
并与 §4.2 呼应 —— `network` 进 `EXECUTION_KEYS`，**项目层不能靠 `"*"` 开后门**。

#### C.4 代理不可用时的降级方向（评审 P1-7）

**写死**：代理进程崩了/未就绪时，`mode="proxy"` **只能降级到 `deny`（无网络）**，
**绝不能降级到 `off`**（那等于**静默打开宿主网络**）。
上报 `Degraded(reason="sandbox.proxy.unavailable")`。

#### C.5 代理进程（新，`server/sandbox_proxy.py`）

* 最小 HTTP/CONNECT 代理，监听 **Unix socket**（`--bind` 进沙箱）。
* 按 C.2 的白名单判定：**空 = 全拒**；命中放行；**不接受 `"*"`**。
* 支持 `CONNECT`（HTTPS）与 plain HTTP；非 HTTP 协议（数据库、裸 TCP）**不支持** ——
  调研明确指出域名白名单对 IP/CIDR/UDP/非 HTTP 是**盲区**，本轮**不做**（写进残余风险）。
* **并发模型**（评审 P2-12）：**每连接一线程**（`threading.Thread(daemon=True)`）——
  单线程阻塞式会让一个慢连接拖垮全部。给一条并发用例。
* **生命周期与就绪顺序**（评审 P2-6）：
  - 组合根在**第一条沙箱命令之前**必须已完成 bind（否则 `--bind <sock>` 失败 → spawn 失败）。
    → 代理在 `build_process_sandbox` 内**同步启动并等待就绪**（`bind()` 返回即就绪）。
  - `mode="proxy"` 且 `proxy_socket=None` → **加载期报错**（不是默认路径）。
  - 随 `AppWorld.aclose()` 关闭（补进 aclose，今天只关 loop+mcp）。
* **`--bind` 套接字文件的可靠性**（评审 P2-7）：bwrap 的 `--bind` 对 **socket inode** 的行为
  （是否等价 `mount --bind`、是否保留 socket 语义）**必须实测**并在实现注释记录 ——
  V10 会覆盖它。

**为什么默认 `deny` 而不是 `off`**：与 `writable_paths` 的取舍一致 ——
写面已经默认收紧并付出了"打断真实构建"的代价，读/网两面若默认放开，
等于"**只有文件系统墙没有网络墙会泄漏**"白做。`proxy` 档是可用性的出口。

#### C.6 代理作为单点新增的攻击面（评审 P1-2 / P2-2）

代理是**所有出网流量的唯一通道**，它本身成了攻击面：

| 风险 | 说明 | 处置 |
|---|---|---|
| **DNS rebinding / metadata SSRF** | 白名单按**域名**判定，但代理**解析后按 IP 连接**。白名单里的 `evil.com` 可解析到 `169.254.169.254`（云 metadata）或内网 IP → 沙箱借代理访问内网/元数据 | 代理**解析后**再做一次判定：**拒私网 / loopback / link-local / metadata 段**（`10/8`、`172.16/12`、`192.168/16`、`169.254/16`、`127/8`、`::1`） |
| **同进程逻辑绕过** | 代理与持有 API key 的 ai-coding **同进程**（每连接一线程）。请求走私、头注入、CONNECT 隧道滥用即等于白名单失效 | 本轮**不降权**（独立进程是另一个工程）；写进残余风险。解析要做 **hardening**（拒绝畸形请求行/超长头/非法端口） |
| **套接字权限** | 代理监听 Unix socket 并被 `--bind` 进沙箱；若 world-accessible，**其他本地用户**可连接 | socket 文件权限 **`0600`**（仅属主），构造时显式 `chmod` |
| **对抗性输入** | 沙箱内进程会**主动构造畸形 HTTP** | 解析 hardening（见上）+ 单条连接超时 |

**迁移说明**（评审 P2-11 / P2-5）：默认从 `off` 改成 `deny` 会**直接打断** `pip install`、`npm install`、
`git clone`、`go mod download` —— 代价**比写边界大得多**。`config.example.yaml` 必须给
`network.mode` 与 `proxy` 档的**最小可用配置示例**，并写明"**默认 deny 会打断联网构建，proxy 是出口**"。

**读边界的迁移代价（评审 P2-5 —— 原方案只写了网络墙的）**：遮蔽默认清单会打断常见工作流 ——

| 被遮蔽 | 坏掉的 | 替代 |
|---|---|---|
| `~/.ssh` | `git push`（SSH）、`scp` | `ssh-agent`（agent 套接字在 `/tmp`，需评估）；或只遮 `id_*` 留 `config` |
| `~/.gnupg` | 签名提交 | 用 `gpg-agent`；或只遮私钥文件 |
| `~/.docker/config.json` | `docker push`（registry 凭据） | 改用 credential helper |
| `~/.aws` | `aws` CLI | 用 IAM role / env 凭据（env 也要遮，见 §7.1） |

→ `config.example.yaml` 与本文档都要写明这些代价与替代方案。**这是刻意的范围裁剪，不是疏漏。**

### D. AF_UNIX 防护

**问题**：bwrap **没有** AF_UNIX 的 `connect`/`bind` 过滤粒度；seccomp **无法仲裁路径**
（BPF 不能解引用指针）→ **路径级白名单做不到**。

**本轮的两层防护**：

| 层 | 机制 | 覆盖 |
|---|---|---|
| **默认遮蔽**（默认开） | `--ro-bind /dev/null <sock>` 遮蔽 **pathname** 套接字 | `/run/user/*/bus`（D-Bus）、`/var/run/docker.sock`、`/run/docker.sock` |
| **seccomp 全域阻断**（可选加强档） | `--seccomp <fd>` + BPF：`socket(AF_UNIX)` → `EPERM` | 所有 AF_UNIX（**含 abstract**；**会断合法用法**：npm 的部分 IPC、docker CLI） |

#### D.1 abstract 命名空间不被 pathname 遮蔽覆盖（评审 P1-8）

方案 §1.1 自己引用的 CVE-2026-47128 明确点出"**含 abstract 命名空间**"，而
**abstract socket（`@/...`）没有路径，无法用 `--bind`/`--ro-bind` 遮蔽**。

→ **必须显式写明**：**abstract namespace 只能靠 seccomp 档**。
调研里 nono 的修法（Landlock v6 抽象套接字作用域 + seccomp user-notification 路径中介）
**本轮不做**（复杂度高一个量级）→ 写进残余风险。

#### D.2 "允许清单 = bind 清单"的诚实表述（评审 P2-9）

**"可配允许清单"的实现方式**：**允许 = 显式 `--bind` 进沙箱的套接字**（如代理桥）。

**但必须诚实**：`--ro-bind / /` **已经 bind 了整个宿主根** → pathname socket 的"清单"实际是
"**宿主上所有 pathname socket**，减去被遮蔽的 3 个，再减去 `--bind <root>` 之下的"。
→ **在没有 seccomp 档时，AF_UNIX 路径级控制基本等于没有。**

这一点必须写进 §D 的结论与残余风险，**不能让读者以为存在一张真正的白名单**。

#### D.3 seccomp 档的实现细节（评审 P2-8）

* **BPF 程序本体**要给（不是只写 `--seccomp <fd>`）：至少过滤
  `socket(domain=AF_UNIX)` → `SECCOMP_RET_ERRNO(EPERM)`；其余放行。
* **fd 传递**：`subprocess` 的 `pass_fds` 或写临时文件后 `--seccomp <path>`（bwrap 支持路径）。
* **实测约束**：**空过滤器 → `prctl(PR_SET_SECCOMP) reported EINVAL`** → 程序必须非空。
* **配置字段**：`NetworkConfig` 增 `block_af_unix: bool = False`（默认关，因为会断合法用法）。

#### D.4 本仓库的具体落点（必须写进残余风险）

1. `IpcService` 的 `/tmp/ai-coding-*.sock`（`server/service.py:85`）—— 暴露 dispatcher 的**全部 handler**。
   当前被 `--tmpfs /tmp` **偶然**遮蔽（**巧合而非机制**）；一旦套接字路径改到 `/run`，墙即失效。
   （A.1 的 `/proc/<pid>/root` 通道已随 `no-pid` 档删除而消除。）
2. **`/proc/<host-pid>/environ`**（评审 P3-3 更正）：`launcher.py:57-65` 把 `AI_CODING_SOCKET`
   通过 env 交给**前端子进程**，**不在** ai-coding 进程自己的 `os.environ` 里 ——
   所以沙箱 bash（继承 ai-coding 的 env，`jobs.py` 的 `env={**os.environ, "AI_CODING_JOB": ...}`）
   **不会**继承它。真正能读到它的通道是 `/proc/<host-pid>/environ`（`full` 档 + 无 userns 保护）。
   **不要**去"清理 env"，要修的是 `/proc`（已由 A.1 的档位规则覆盖）。
3. `--ro-bind / /` 让 `/run/user/*/bus`、`/var/run/docker.sock` 等**任意**宿主套接字文件可见。

#### D.5 错误码要断言（评审 P3-4）

`--ro-bind /dev/null <sock>` 把 socket 路径变成**字符设备**。
**实测**（bubblewrap 0.9.0）：`connect()` 返回 **`ECONNREFUSED`(111)** ——
不是 `ENOTSOCK`（方案原写 `ENOTSOCK` 是**误测**，实现期已更正）。

→ V12 **断言具体错误码 `ECONNREFUSED`**，而不是只断言"失败"
（"失败"也可能是权限问题，遮蔽是否真的生效就说不清）。

---

## 四、逐文件改动清单

| # | 文件 | 改动 | 为什么 |
|---|---|---|---|
| 1 | `security/process_sandbox.py` | `_CONSTRUCTION_TIERS`（**两档，无 `no-pid`**）+ 分级探测（**完整构造**）+ `--unshare-user-try` + 读遮蔽 + 网络开关 + AF_UNIX 遮蔽 + seccomp 档 + **`tier` 属性** | 机制组装处 |
| 2 | `security/process_sandbox.py` | `ProcessSandboxPolicy.read_deny_paths` / `NetworkConfig` / `SandboxProxy` 协议 / `SandboxMaskReport` | 策略层 |
| 3 | `config/models.py` + `config.example.yaml` | `read_deny_paths` / `network`（含 `proxy` 最小可用示例） | 配置 |
| 4 | `config/loader.py` | **显式加一行**：把"能关墙的键"纳入 `EXECUTION_KEYS`（评审 P2-13 / P2-3）。**放置层级要先定死**：`read_deny_paths` / `network` 若嵌在 `process_sandbox` 下，则整块已被丢弃、加行是**空操作**；只有**顶层键**才需要加。→ **本轮把它们放在 `process_sandbox` 下**（已被 `EXECUTION_KEYS` 覆盖），并在 `data-contracts.md` 写明"项目层不能关沙箱/读边界/网络墙" | 信任边界 |
| 5 | `server/sandbox_proxy.py`（新） | 最小 HTTP/CONNECT 代理（Unix socket + C.2 白名单 + 每连接一线程） | 网络墙的 `proxy` 档 |
| 6 | `server/world.py` / `cli.py` | 代理进程生命周期（同步就绪 + 补进 `aclose`）+ 配置接线 | 组合根 |
| 7 | `tools/builder.py` | 透传新配置给工具栈与子智能体栈 | 接线 |
| 8 | `core/events.py` | **复用** `Degraded`（档位降级 / 代理不可用 / 遮蔽构造失败） | 上报 |
| 9 | `security/process_sandbox.py` docstring | **改**：删掉"this kernel (6.18) refuses the namespace construction"（评审 P3-1：与 §2.1 矛盾），改为"root + 无 `CAP_SYS_ADMIN` 时需要 `--unshare-user-try`" | 文档一致性 |
| 10 | `docs/data-contracts.md` | 契约同步 | |
| 11 | `docs/plan-m-d.md` §七/§7.1 | 从"不做"改为本轮范围 | |

**测试条目**：

| # | 文件 | 改动 |
|---|---|---|
| T1 | `tests/test_process_sandbox.py` | 修正 skip reason；**U2b 反转**；**`test_wrap_keeps_the_isolation_switches` 改为按选中档位断言**（评审 P1-3：否则本环境必红）；新增 V1–V14 |
| T2 | `tests/test_sandbox_proxy.py`（新） | 代理白名单：空=全拒、精确/后缀匹配、`host:port` 剥离、**`"*"` 被拒**、CONNECT 与 plain HTTP、并发 |
| T3 | `tests/test_process_sandbox.py` | 顺序断言扩展：遮蔽项在 `--ro-bind / /` **与** `--tmpfs /tmp` 之后；代理 bind 在 `--tmpfs` 之后；**`--proc` ⟺ `--unshare-pid` 不变量** |
| T4 | `tests/test_config.py`（或同类） | `read_deny_paths` / `network` 进 `EXECUTION_KEYS`（项目层不能关墙）；`allow=["*"]` 加载期报错 |

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
| V1b | `test_proc_escape_is_closed_per_tier` | **按档位断言**（评审 P1-4 —— 无条件断言在 `full` 档必红）：**`no-proc`** → `ls /proc` **为空** + 读 `/proc/<pid>/root` **ENOENT**；**`full`** → `/proc` 非空但 **`/proc/1/comm` ≠ 宿主 init** 且 **读 `/proc/<宿主 pid>/root` 不可达**。另加 argv 不变量（`--proc` ⟺ `--unshare-pid`）。**这条把 `full` 档的逃逸闭合钉在 CI** —— 本环境恒 EPERM、该组合从未被验证 |
| V2 | `test_the_mechanism_runs_without_cap_sys_admin` | 加 `--unshare-user-try` 后 `available()` 为真（今天恒 False → 7 条真实用例全 skip） |
| V3 | `test_a_sandboxed_process_cannot_read_ssh_keys` | `enforce` 下 `cat ~/.ssh/id_rsa` → **失败**（今天：成功读到） |
| V4 | `test_a_sandboxed_process_cannot_read_cloud_credentials` | 同上，`~/.aws/credentials` |
| V5 | `test_a_sandboxed_process_cannot_read_the_repos_own_api_key` | `~/.config/ai-coding/config.yaml` 不可读 |
| V6 | `test_the_read_boundary_hides_a_directory_and_a_file` | `--tmpfs <dir>` 与 `--ro-bind /dev/null <file>` 各一条 |
| V6b | `test_a_writable_path_under_a_read_deny_is_still_unreadable` | 重叠时 **deny 赢**（评审 P2-2） |
| V7 | `test_the_read_boundary_expands_globs` | `ssh_host_*_key` 展开后逐条遮蔽 |
| V8 | `test_a_missing_read_deny_path_is_skipped_without_degraded` | 路径不存在 → 跳过，**不进 `Degraded`**（评审 P2-1） |
| V9 | `test_a_sandboxed_process_has_no_network_by_default` | 默认档 `curl 1.1.1.1` → **不可达**（今天：可达） |
| V10 | `test_the_proxy_bridge_lets_an_allowed_domain_through` | `proxy` 档 + 白名单 → 经代理可达；未列域名被拒；**`--bind` 套接字可用**（评审 P2-7） |
| V11 | `test_the_proxy_refuses_a_wildcard_allow_list` | `allow=["*"]` → **加载期**报错（评审 P2-10） |
| V12 | `test_a_sandboxed_process_cannot_reach_the_dbus_socket` | `/run/user/*/bus` 被遮蔽，`connect()` 返回 **`ENOTSOCK`**（评审 P3-4） |
| V13 | `test_seccomp_blocks_af_unix_when_enabled` | seccomp 档 → `socket(AF_UNIX)` → `EPERM` |
| V14 | `test_the_proxy_socket_bind_comes_after_the_tmpfs` | 顺序断言（代理在 `/tmp` 下时仍可达） |
| V15 | `test_the_proxy_degrades_to_deny_never_to_off` | 代理不可用 → **降级到无网络**，不是打开宿主网络（评审 P1-7） |
| V16 | `test_a_symlinked_deny_path_masks_the_target` | `~/.ssh` 是指向 `/data/keys` 的符号链接时，**目标**也被遮蔽（评审 P1-4） |

> V3–V16 需要真 bwrap：`@pytest.mark.skipif(...)` —— **显式理由**（V2 修复后本环境会跑起来）。

### Prove

1. `.venv/bin/python -m pytest -q`
2. `.venv/bin/ruff check .` / `.venv/bin/mypy ai_coding`
3. **突变测试**：
   * 去掉 `--unshare-user-try` → V2 变红
   * 去掉读遮蔽项 → V3/V4 变红
   * 把 `network` 默认改回 `off` → V9 变红
   * 允许 `allow=["*"]` → V11 变红
   * 把遮蔽项放到 `--ro-bind / /` **或** `--tmpfs /tmp` **之前** → V6/V7 变红（顺序错则遮蔽无效）
   * 把代理 bind 放到 `--tmpfs /tmp` **之前** → V14 变红
   * **加回 `no-pid` 档**（`--proc` 无 `--unshare-pid`）→ **V1b 变红**
   * 把 `unix://` 直接当代理 scheme（不做沙箱内桥接）→ V10 变红
   * 代理不可用时降级到 `off` → V15 变红
   * 去掉 `resolve()` → V16 变红
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
| **`no-proc` 档的 `/proc` 不可用** | 实测：`ps`/`pgrep`/`free`/`uptime`/`df`/`mount` 坏，**bash 进程替换 `<(...)` 坏**（`/dev/fd` 悬空） | 已知代价（A.4b）；断言钉住 |
| **`full` 档的 `/proc` 通道** | 有 `--unshare-pid` 时**宿主 pid 命名空间不可见**，`/proc/<pid>/root` 通道**已关**（与 §A.1 一致 —— 评审 P2-4 的统一表述）。**残余场景**：若 userns 不可用导致 `--unshare-pid` 失效，通道重开 | V1b 按档位钉住 |
| **递归 bind 带进 `/sys`、`/run`** | 同上 —— 沙箱能看到宿主 `/sys`、`/run` 等子挂载，读边界与 AF_UNIX 的暴露面**比原假设更大** | 写进本文档 |
| **符号链接绕过读遮蔽** | 遮蔽的是**路径名**不是 **inode**；`resolve()` 只解析 deny 路径自身，**运行期重指向不覆盖**（调研 F3） | 已做 `resolve()` + 遮蔽原始路径与目标；**仍不完整** |
| **读 denylist 不完整** | denylist **"永远少一条"** —— 这是**刻意的能力边界**，**不是保证**（Pillar） | 已扩充清单；**显式声明不完整** |
| **glob 是构造期快照** | 运行期新增的密钥文件不会被重新遮蔽 | 写进文档 |
| **代理单点 / DNS rebinding** | 域名白名单对**解析后的 IP** 无控制；`evil.com` 可解析到 metadata/内网 | 代理**解析后**再拒私网/loopback/link-local/metadata 段（§C.6） |
| **代理同进程** | 代理与持有 API key 的 ai-coding 同进程；逻辑绕过 = 白名单失效 | 本轮**不降权**；解析 hardening + 残余风险 |
| **IP/CIDR/UDP 绕过** | 域名白名单对裸 IP/UDP 无效（调研 F12/F13） | 本轮**不修** |
| **abstract AF_UNIX 不被遮蔽覆盖** | abstract socket 无路径，只能靠 seccomp 档（默认关） | 写进文档 |
| **AF_UNIX 路径级控制基本等于没有** | `--ro-bind / /` 已 bind 整个宿主根；无 seccomp 档时"允许清单"是空洞的 | **诚实记录** |
| **seccomp user-notification 路径中介** | nono 的修法，复杂度高一个量级 | 本轮**不做** |
| **信任交接型逃逸** | 沙箱外组件（hook / MCP / Git 子系统）消费沙箱产物（调研 F14） | **不在本轮范围** |
| **MCP 子进程不受读边界约束** | `plan-m-d.md` §七 已声明不覆盖 MCP，且它持有自己的凭据（评审 P2-14） | 写进本文档 |
| **macOS / Windows** | 本轮只做 Linux（bwrap）；Seatbelt 已弃用、Windows 用受限令牌 | 平台相关，另立 |

---

## 八、评审记录

### 8.1 方案评审（子智能体，第 1 轮）

**1 个 P0 · 8 个 P1 · 14 个 P2 · 6 个 P3（全部已修，见 §8.1.1）**

**已实测验证**（评审/我逐条复核）：

| 断言 | 方式 | 结果 |
|---|---|---|
| 本环境 bwrap 可用，缺的是 `--unshare-user-try` | 直接调用 | ✅ `available()` 今天恒 False，加 flag 后 exit 0 |
| `--proc` 与 `--unshare-pid` 在本环境冲突 | 逐项实测 | ✅ `Can't mount proc on /newroot/proc: EPERM` |
| **无 `--unshare-pid` 时暴露宿主 pid 命名空间** | 实测 | ✅ 宿主 9 个 pid，沙箱内可见 **10 个** |
| 读边界：现状可读 `~/.ssh`，遮蔽后不可读 | 实测（`/root/rd`） | ✅ `SECRET-KEY` → `No such file or directory` |
| 网络：默认有网，`--unshare-net` 断网 | 实测 | ✅ `TimeoutError` → `Network is unreachable` |
| **`curl` 不支持 `unix://` 代理 scheme** | 实测 | ✅ `curl: (7) Unsupported proxy scheme for 'unix://...'` |

#### 8.1.1 本轮 findings 的处置（全部已修）

| 级别 | 问题 | 修法 |
|---|---|---|
| **P0-1** | **`no-pid` 档 + `--proc /proc` = `/proc/<pid>/root` 逃逸** —— `/proc` 是否暴露宿主 pid 取决于 `--unshare-pid`；该档让沙箱可读 `/proc/<host-pid>/root/...`，**绕过读遮蔽**，且**方案自己的测试抓不到**（V3–V5 只 `cat ~/.ssh`）。实测确认无 `--unshare-pid` 时沙箱可见宿主 10 个 pid | **删除 `no-pid` 档**，写死"**`--proc` 与 `--unshare-pid` 同生共死**" + **V1b 不变量用例**。（本环境 userns 恰好挡住 `/proc/1/root`，已如实记录"这不是机制保证"） |
| P1-1 | 档位上报**没有接线**（档位存在 backend 内部，`spawn_argv` 拿不到） | §A.3 写死 `tier` 属性 + `emit` 通路 |
| P1-2 | 档位探测**不含完整构造** → 假阳性（复现 `plan-m-d.md` 的 P2-4） | §A.2 写死"探测 = `wrap()` 完整构造" |
| P1-3 | `test_wrap_keeps_the_isolation_switches` 会红，改动清单未列 | §四 T1 显式列入；改为按选中档位断言 |
| P1-4 | 读遮蔽的**符号链接对策不成立**（`--tmpfs <link>` 也只遮链接） | §B 局限 2 重写：`resolve()` + 遮蔽目标 + 实测 bwrap 行为 |
| P1-5 | **`unix://` 代理 scheme 未被客户端支持** | 实测确认 curl 拒绝 → §C.1 改为**沙箱内桥接**（socat / Python 桥） |
| P1-6 | 白名单**匹配语义未定义**（子串匹配 = 安全洞） | §C.2 写死精确/后缀 + 剥离端口 |
| P1-7 | 代理失败时的**降级方向未定义** | §C.4 写死"只能降到 `deny`，绝不降到 `off`" |
| P1-8 | **abstract 命名空间**未被遮蔽覆盖 | §D.1 显式写明"只能靠 seccomp 档" |
| P2-1 | `Degraded` 被滥用 + 频次未定义 | §A.3 / §B.2：区分"跳过（正常）"与"构造失败（降级）"；探测一次性 |
| P2-2 | 读遮蔽与可写 bind 重叠时谁赢未定义 | §B.3：**deny 赢**（遮蔽在写 bind 之后）+ V6b |
| P2-3 | 遮蔽顺序只声明了一条 | §B 局限 1：**两条**（`--ro-bind / /` 之后 **且** `--tmpfs /tmp` 之后） |
| P2-4 | glob 展开三个洞（缓存/符号链接/两套实现） | §B.4：抽 `_expand_globs` 共用 + 快照语义 |
| P2-5 | 默认 denylist 不完整且未记残余风险 | §B.5 扩充 + **显式声明不完整** |
| P2-6 | 代理生命周期与就绪顺序未定义 | §C.5：同步就绪 + `aclose` 补关 + `proxy_socket=None` 报错 |
| P2-7 | `--bind` 套接字文件的可靠性未验证 | §C.5 + V10 覆盖 |
| P2-8 | seccomp 档的 BPF/fd/配置字段缺失 | §D.3 补齐 |
| P2-9 | "允许清单 = bind 清单"接近空洞 | §D.2 **诚实表述**："无 seccomp 档时基本等于没有" |
| P2-10 | `"*"` 的拒绝点未定义 | §C.3：**加载期**拒绝 |
| P2-11 | 默认 `deny` 的可用性代价无迁移说明 | §C 末尾补迁移说明 + 示例要求 |
| P2-12 | 代理并发模型未说明 | §C.5：每连接一线程 + 用例 |
| P2-13 | `read_deny_paths`/`network` 未进 `EXECUTION_KEYS` | §四 #4 **显式加一行** |
| P2-14 | 读边界不覆盖 MCP 子进程 | §7.1 补残余风险 |
| P3-1 | docstring 与 §2.1 矛盾 | §四 #9 列入改动 |
| P3-2 | 测试里的重复探测 | 实现期收敛（探测已缓存，影响有限） |
| P3-3 | `AI_CODING_SOCKET` 表述不准 | §D.4 更正（env 只给前端子进程；真通道是 `/proc/<pid>/environ`） |
| P3-4 | AF_UNIX 遮蔽应断言具体错误码 | §D.5：断言 **`ENOTSOCK`** |
| P3-5 | 代理 env 未清理 `NO_PROXY`/小写变体 | §C.1 步骤 3 |
| P3-6 | 档位探测应覆盖 userns 可用/不可用两条路径 | §五 Prove 5 补（CI 两 job 分别覆盖） |

### 8.2 方案评审（子智能体，第 2 轮）

**1 个 P0 · 2 个 P1 · 2 个 P2 · 3 个 P3（全部已修，见 §8.2.1）**

#### 8.2.1 本轮 findings 的处置（全部已修）

| 级别 | 问题 | 修法 |
|---|---|---|
| **P0-1** | **第 1 轮的修复不成立**：`--ro-bind / /` 是**递归绑定**，宿主 `/proc`（及 `/sys`、`/run`）**与 `--proc` 无关**地被带进沙箱。实测：`no-proc` 档下 `/proc` **存在 61 条**、`/proc/1/comm` = **`tini`（宿主 init）**；`--unshare-pid` **单独并不能关闭逃逸**。**唯一能关闭的是 `--tmpfs /proc`**（实测 → `No such file or directory`） | **`no-proc` 档加 `--tmpfs /proc`**；**V1b 补行为断言**（`ls /proc` 为空 + 读 `/proc/<pid>/root` 失败），否则测试仍抓不到（第 1 轮 P0 的原始教训）；§B 局限 3 / §D.4 / §7.1 连带更正 |
| P1-1 | **桥接与 `list[str]` 契约冲突**：`socat` 前置到 argv 后用户命令不执行；`sh -c "... & exec"` 包装则**重新引入 shell 解析层**（`plan-m-d.md` P0-1 禁止的路径）。且 env 无处表达（`wrap()` 只返回 argv）、端口协调缺口、**桥挂了未定义** | §C.1 重写：**组合根注入的小程序作 `argv[0]` 前置**（桥选端口 + 设 env + `execv`）；env 走 bwrap `--setenv`/`--unsetenv`；桥失败 **fail-closed**（非零退出，命令不执行） |
| P1-2 | **代理作为单点的攻击面**：域名白名单对**解析后的 IP** 无控制 → `evil.com` 可解析到 `169.254.169.254`（metadata）或内网 | §C.6：代理**解析后**再拒私网/loopback/link-local/metadata 段 |
| P2-1 | `resolve()` 方向对但**表述错**（实测 `--tmpfs <link>` / `--ro-bind /dev/null <link>` **都直接报错**，不是"只遮链接"） | §B 局限 2 按实测重写；**同时遮蔽原始路径与解析目标**；V16 补"直接遮符号链接会报错"断言 |
| P2-2 | 代理其余攻击面（同进程逻辑绕过、套接字权限、对抗性输入） | §C.6 补：socket `0600`、解析 hardening、连接超时 |
| P3 | env 走 `--setenv`；loopback 已 UP（作 V10 断言）；代理 socket 清理与冲突 fail-closed | §C.1 / §C.5 补 |

### 8.3 实现评审（子智能体）

_待填。_

#### 8.2.2 第 3 轮评审的处置（全部已修）

**0 个 P0 · 4 个 P1 · 5 个 P2 · 4 个 P3**

| 级别 | 问题 | 修法 |
|---|---|---|
| P1-1 | **`no-proc` 档的 `/proc` 工具断裂完全漏记** —— 实测 `ps`/`pgrep`/`free`/`uptime`/`df`/`mount`/`readlink /proc/self/exe` 全坏，**bash 进程替换 `<(...)` 也坏**（`/dev/fd` 悬空） | §A.4b：三处写明（残余风险 / 上报文案 / **断言钉住**） |
| P1-2 | **桥设计自相矛盾**：`execv` 会杀掉后台转发线程 → "桥启动成功但转发已死" → **网络静默挂死**，fail-closed 覆盖不到 | §C.1：**`fork` 转发子进程** + `PR_SET_PDEATHSIG`（或 spawn+wait）；参数协议明确 |
| P1-3 | **桥路径未被读边界豁免**（实测：桥在被遮蔽目录下 → `execvp ENOENT`，`proxy` 档直接不可用）；**`block_af_unix` 与 `proxy` 互斥** | §C.1：桥路径 `resolve()` 后显式排除 + 断言；互斥在**配置校验期**报错 |
| P1-4 | **V1b 无条件断言在 `full` 档必红**；且 `full` 档的逃逸闭合**从未被验证**（本环境恒 EPERM） | V1b 改**按档位断言**（no-proc：`/proc` 空；full：`/proc/1/comm` ≠ 宿主 init + `/proc/<宿主 pid>/root` 不可达）—— **钉在 CI** |
| P2-1 | 掩码**写死 `~/.config`**，忽略 `XDG_CONFIG_HOME` → XDG 被覆盖时掩码落空、key 可读 | §B.5：按 `config_home()`/`data_home()` 展开 |
| P2-2 | **工作区级配置未被遮蔽**（`config.yaml` 可含同一 API key，工作区必然可读） | §B.5 / §7.1 显式声明"只覆盖用户级" |
| P2-3 | `EXECUTION_KEYS` 改动**落点不清**（嵌套则加行是空操作） | §四 #4：**定死放在 `process_sandbox` 下**（已被覆盖） |
| P2-4 | §7.1 与 §A.1 **自相矛盾** | §7.1 统一表述（有 `--unshare-pid` 时通道已关；残余场景写明） |
| P2-5 | 读边界的**可用性代价未写迁移说明** | §C 补表（ssh/gpg/docker/aws 的替代方案） |
| P3-1 | 整目录 `--tmpfs` 连带隐藏非机密兄弟项，且遮蔽目录在沙箱内**可写**（临时） | 文档点明；需要时改逐文件 `--ro-bind /dev/null` |
| P3-2 | 只遮当前用户 `~` | §B.5 补 `/home/*/.ssh`、`/root/.ssh` |
| P3-3 | 桥的参数协议未定义（`--` 歧义） | §C.1：首个 `--` 为界 + 用例 |
| P3-4 | `no-proc` 档丢了 `--unshare-pid` → 与宿主共享 pid 命名空间 | 记入降级档说明 |
