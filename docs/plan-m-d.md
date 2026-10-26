# 领域 D · 安全（M-D1 ~ M-D3）

> 调研：一轮仓库侦察 + 一轮联网（55+ 引用源）。方案与评审记录见 §八。
>
> **前情**：roadmap 里 `M-D1~D3 安全 | ⬜ 未开始`；仓库里**没有** `plan-m-d.md`。
> 本方案从零建立。

---

## 一、调研结论

### 1.1 联网调研主线

**防御重心正从"提示层劝阻"转向"OS 层强制"，三者不可互相替代**：

| | 作用 | 缺了会怎样 |
|---|---|---|
| **OS 沙箱** | 限制"能做什么" | 提示注入成功即等于利用成功 |
| **命令检测基准** | 度量"拦住了多少" | 改动引入的漏报无人发现 |
| **最小授权** | 决定"起点有多小" | 一次成功注入的后果溢出聊天 |

### 1.2 OS 级沙箱

* **2026 收敛为"双墙"共识**：文件系统墙（只读允许的内容，写入重定向到副本）+ 网络墙（`unshare-net` + 代理桥接 + seccomp 套接字过滤）。
* **Linux**：Landlock（≥5.13，**非特权**）+ seccomp-bpf + 网络命名空间。
* **macOS**：Seatbelt **已被 Apple 弃用（10.15 起）且无替代** —— 这是**结构性风险**，不是配置问题。
* **关键警告**："**只有文件系统墙没有网络墙会泄漏；只有网络墙没有文件系统墙会被改写**"。
* **失效模式**（Pillar "Week of Sandbox Escapes"）：① denylist 跟不上 OS 复杂度（"**allow-default 不是沙箱，而是一张永远少一条的清单**"）；② **工作区配置本质是可执行代码**（`.vscode` 任务、hook、virtualenv 解释器、git `fsmonitor`）；③ "安全命令"仅凭名字不安全（`git show` 可写文件/触发 hook）；④ **特权守护进程在沙箱之外**（Docker socket）。
* 核心命题：**"如果一个代理能写入某系统未来的输入，那它从一开始就没有被沙箱化。"**
* **CVE-2026-47128**：Landlock/seccomp 未仲裁 `AF_UNIX` 路径 → 沙箱内经 per-user systemd D-Bus 套接字逃逸。**"能连套接字不等于能连那个套接字。"**
* **fail-closed**：原语缺失时**拒绝启动**，而不是静默降级（agentkeeper 对 macOS 显式上报 `DEGRADED` + 稳定原因码 `seatbelt.broad-system-read`）。

### 1.3 危险命令检测基准

* 基准密集涌现（CARE / SABER / QuoteBench / AgentJailbreak / AdvCLI / INFUSE / IssueTrojanBench / APEX-Harm / aegish），但**口径不可比**：F1·DR·FPR（CARE）≠ HSR（SABER）≠ 攻击成功率（INFUSE）≠ 穿透率（IssueTrojanBench）。
* **GuardFall**（影响 11 个开源代理中的 10 个）证明**应用层字符串过滤在结构上无法取胜**：过滤器检查**原始命令字符串**，bash 执行的是**引号移除 / 变量展开 / 命令替换 / globbing / 字段分割之后**的 argv。
* **IssueTrojanBench**：66.5% 的恶意 issue 穿透全部 guardrail，且"**拒绝几乎完全来自 LLM 而非 agent 框架**"。
* **QuoteBench**：基准必须区分"命令生成错误"与"执行传输引入的失败"。

### 1.4 权限授予最小化

* OWASP 拆成**功能过度 / 权限过度 / 自主性过度**（Excessive Agency 升至 2026 第 3 位）。
* 实测主流 runtime 有 **~15× 能力过度供给**；暴露过多工具会造成"**愚钝区**"（模型把 token 花在推理无关能力上）—— **过度供给同时降低性能并扩大攻击面**。
* **Codex CLI**："最具体路径优先"（`/repo` 允许 + `/repo/.env` 显式拒绝）—— 路径级拒绝叠加在目录级允许之上。
* **运行时审批**：呈现**渲染后的确切动作**而非摘要。
* **OWASP LLM06**："**提示注入的防御只能部分实现，而过度代理的防御可以完全实现**"。

---

## 二、仓库侦察（子智能体 + 我逐条核实）

### M-D1 · OS 级沙箱 —— 机制层**完全缺失**

| 断言 | 核实 |
|---|---|
| `security/sandbox.py` 只做路径限制 | ✅ `Sandbox.resolve()` = `Path.resolve()` + `relative_to` 包含性 + `grantor.path_allowed()` |
| 文件工具有 fd 链防 TOCTOU | ✅ `open_inside` / `_walk_to` 用 `O_NOFOLLOW \| O_DIRECTORY` |
| **进程执行完全不受沙箱约束** | ✅ `subprocess_runner.py:132` 只有 `cwd` + `start_new_session=True` + 超时 killpg |
| `landlock\|bwrap\|seatbelt\|seccomp\|unshare` 在 `ai_coding/` **机制层零命中**（仅 [infra/deadline.py:11](file:///workspace/ai-coding/ai_coding/infra/deadline.py#L11) 一处注释提及 `seccomp`） | ✅ |
| `bypass_sandbox` / `execpolicy` **不存在** | ✅ |

**消费方**：`read/write/edit/grep/glob`（文件工具）、`bash_tool`、`core/jobs.py`（后台 job）、`tools/builder.py:188`。

### M-D2 · 危险命令判定 —— 判定扎实，**零基准**

* 三层：`shell_parse.py`（tree-sitter → `CommandView`）→ `bash_redlines.py`（**9 条规则** + 凭据读取）→ `permission_gate.py`（硬层 + **保守回退**）。
* **"双读法"**：`bash_redlines` 与 `permission_gate` 各有一份 `rm -rf /`、`chmod 777` 判定 —— 刻意保守，但两处规则需同步维护。
* **零基准**：`test_bash_red_lines.py` 是 parametrize 用例，**不是可计分基准**；`scripts/` 下无 FP/FN 产出。

### M-D3 · 权限最小化 —— 成熟度最高，缺口集中在**生命周期**

* `grants.py`：`GrantStore` 落在**用户配置目录**（防仓库随 `git add -f` 泄权），按 workspace 分键，best-effort。
* `permission_gate.py`：`grant_key` + **posture 后缀** `key@posture`（已有最小化维度）+ `_once_grants`（session 级、不持久化）+ `revoke_all`（全清）。
* 面板 `permissions_state` 展示 `grants` / `rules` / `storePath`。

**缺口**：

| # | 缺口 | 证据 |
|---|---|---|
| 1 | **无到期** —— 授权永久有效 | `GrantStore` 存的是 `set[str]`，无时间戳 |
| 2 | **无逐条撤销** —— 只有全清 `revoke_all` | `permission_gate.py:1010` |
| 3 | 面板**不显示** `_once_grants`（session 级） | `permissions_state` 只读 `_grants` |
| 4 | `extra_workspace_roots` **启动时一次性展开** | `cli.py:90` `_extra_roots` |

### 2.5 一个必须先说的环境约束

**这个环境里 bwrap 跑不起来**：`bwrap` **未安装**；`unshare --mount` = `EPERM`（`--user` 可用）。landlock 同样不可用（`errno 38 ENOSYS`）。

→ **M-D1 的机制在这里跑不起来**。方案必须：实现机制 + **不可用时显式上报（不假装修了）** + 真实子进程用例在 **CI（装了 bubblewrap 的真内核）** 跑、本地以**显式理由**跳过（不是静默通过）。

---

## 三、方案：逐项实现

### A. M-D1 · OS 级沙箱（写边界）

**本轮范围**：**写边界**（工作区外不可写/建/删）+ **策略与机制分离**。

> **明确声明（评审 P2-6）**：本轮**只保证写边界，不保证读边界**。`--ro-bind / /` 让沙箱内进程
> **仍可读** `~/.ssh/id_rsa`、`~/.aws/credentials`、`/etc/shadow`。这是刻意的范围裁剪，
> 不是疏漏 —— 但必须写出来，否则"沙箱"会被误读为"隔离"。U2b 用**反向断言**钉住这一点。

读边界（`~/.ssh` 等）与网络墙**不在本轮** —— roadmap 的验收判据原文就是"沙箱内进程**写不到**工作区外"；
读白名单是平台相关的，且没有网络墙时读泄漏的后果有限（下一轮做网络墙时一起做）。

#### A.1 契约分离（roadmap 要求②）

```
策略层  ProcessSandboxPolicy      "这个进程要不要隔离、隔离到什么程度"
机制层  SandboxBackend 协议        "真正去隔离"（landlock / 将来的 bwrap / seatbelt）
```

```python
# security/process_sandbox.py（新）

class SandboxGap(Exception):
    """The mechanism is unavailable and the policy says stop."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason          # 稳定原因码，如 "bwrap.unavailable"


SandboxMode = Literal["off", "warn", "enforce"]


@dataclass(frozen=True)
class ProcessSandboxPolicy:
    """策略：要不要隔离。与"怎么隔离"无关。

    ``mode`` 的三档对应 roadmap 的 ALLOW/WARN/DENY 意图：

    * ``off``      —— 不隔离（MCP 子进程、显式 bypass）
    * ``warn``     —— 隔离，但机制不可用时**继续**（并上报 DEGRADED）
    * ``enforce``  —— 隔离，机制不可用时**拒绝**（fail-closed）

    **默认是 ``enforce``**：roadmap 的验收判据①就是"沙箱内进程写不到工作区外"，
    默认 ``off`` 会让该判据开箱即落空（评审 P1-1）。代价见 ``writable_paths``。

    ``bypass`` 是 roadmap 点名的"显式 allow"：操作者明确说这次不隔离。
    """

    mode: SandboxMode = "enforce"
    bypass: bool = False
    #: 除 workspace 与 /tmp 之外**允许写**的路径（构建缓存等）。见 A.2 的取舍。
    writable_paths: tuple[Path, ...] = ()


class SandboxBackend(Protocol):
    """机制：真正去隔离。策略层不关心它是 bwrap 还是别的什么。

    协议只有**"把这条命令包装成被隔离的命令"**一件事 —— 纯 argv 变换，
    **不碰 ``preexec_fn``**（那样会引入多线程不安全，见 A.2 的取舍）。
    """

    name: str

    def available(self) -> bool: ...
    def gap(self) -> str:
        """``available`` 为假时的**唯一**原因码来源（异常从它取，见 P3-1）。"""
        ...

    def wrap(self, argv: Sequence[str], roots: Sequence[Path]) -> list[str] | None:
        """返回包装后的命令；``None`` 表示"没什么可包装的"（例如 ``off`` 模式）。"""
        ...
```

#### A.1b `shell=True` → argv 的规范（评审 P0-1）

`bash_tool` 与 `jobs` 传给 runner 的是 **shell 字符串 + `shell=True`**。若把整串交给
`wrap()`，要么被当成可执行文件名、要么被逐字符拆进 argv；而且 `shell=True` 会**再包一层
`/bin/sh -c`**，外层 shell 会先把 bwrap 的参数重新解析一遍 —— **隔离直接失效**。

**规范**：调用方 wrap，但**全部经过同一个 helper**（评审 P0-1 —— 落点必须唯一，
否则某个调用方忘了就静默不隔离；同时 `run_process` 是通用 infra，**不该耦合安全概念**）：

```python
# security/process_sandbox.py
def spawn_argv(
    command: str,
    policy: ProcessSandboxPolicy | None,
    backend: SandboxBackend | None,
    roots: Sequence[Path],
    *,
    emit: Callable[[str], None] | None = None,
) -> list[str]:
    """shell 命令 → 可直接 spawn 的 argv（规范化 + wrap）。**两个调用方共用这一个入口。**

    规范化的唯一形态是 ``["/bin/sh", "-c", command]``；``shell`` 一律 ``False``。
    """
    argv = ["/bin/sh", "-c", command]
    if policy is None or policy.mode == "off" or policy.bypass:
        return argv
    if backend is None:
        # fail-closed：enforce 下没有机制就是没有隔离，不能静默放行（评审 P1-2）
        if policy.mode == "enforce":
            raise SandboxGap("sandbox.backend.missing")
        return argv
    if not backend.available():
        if policy.mode == "enforce":
            raise SandboxGap(backend.gap())
        if emit is not None:
            emit(backend.gap())          # warn：继续，但要发 Degraded（评审 P2-1）
        return argv
    return backend.wrap(argv, roots) or argv
```

* **`policy=None` 是唯一的"不改变行为"入口**（评审 P1-2）：既有 66 处 `BashTool()` /
  `JobRegistry(root=...)` 构造**不传 policy 就是 `off`**，不会因为本轮改动而突然 fail-closed。
  **组合根**（`builder.py` / `world.py`）传真实的 `enforce` policy —— 默认 `enforce` 指的是
  **组合根的默认**，不是工具构造函数的默认。
* **`emit` 是 `warn` 档的发事件出口**（评审 P2-1）：`spawn_argv` 不自己发事件（它不该依赖
  `world`/事件总线），由调用方传入。
* **`SandboxGap` 的捕获点必须写死（评审 P1-1）**：`spawn_argv` 抛出的异常如果一路冒到
  [dispatch.py:662-668](file:///workspace/ai-coding/ai_coding/core/dispatch.py#L662-L668) 的
  `except Exception`，结果会是 `error="tool_error"` + `data={"tool": "bash"}` ——
  **不是** §A.4 承诺的 `error="denied"` + `data.reason`。
  * `BashTool.execute`：`except SandboxGap as exc: return ToolResult(success=False, error="denied", data={"reason": exc.reason})`
  * `JobRegistry.start`：同样 catch，转成 job 的失败态（不写 ledger 行）。
  * **U3/U5 必须断言 `error == "denied"` 且 `data["reason"]` 正确**，否则测不出这个退化。
* **调用方（`bash_tool` / `jobs`）只做一件事**：`argv = spawn_argv(...)`，然后 `shell=False` 交给 runner。
  `jobs` 额外要把 `_bounded(command)` **折进 `-c` 的字符串**（否则 `ulimit -f` 日志上限静默失效）。
* **jobs 路径只在 `JobRegistry.start` 内 wrap 一次**（评审 P2-2）：jobs 是 `BashTool` → `registry.start`，
  若两边都 wrap 就是**双重 bwrap**。写死：`BashTool` 对 job 类调用**不 wrap**，只有 `start` wrap。
* **jobs 的 pid/kill 语义（评审 P2-3）**：bwrap 带 `--unshare-pid` → 内层在另一个 PID 命名空间。
  * `Job.pid` 对外报 **bwrap 的 pid**（父进程），kill 用 `os.killpg(os.getpgid(bwrap_pid))`
    —— bwrap 会把信号转发给内层，且 `--die-with-parent` 保证父死子亡。
  * **必须在实现时验证一次**（U1c 断言 job 能被 kill），若 bwrap 的 pid 转发不成立则改用
    `--no-pidns`（放弃 pid 隔离换可管理性），并在文档记录这个取舍。
* `wrap()` 只处理 **argv**，不碰 shell 语义 —— 协议干净。
* `shell=False` 是**必须的**：留着 `shell=True` 会让 bwrap 的参数被再解析一次。
* **机理要写准（评审 P2-5）**：`shell=True` 对**列表**参数**不会** join —— CPython 的
  `_execute_child` 是 `args = [unix_shell, "-c"] + args`，于是 bwrap 会以"无参数"**响亮失败**，
  **不是**静默失效。真正会二次解析的是"**先 join 成字符串、再 `shell=True`**"。
  方案**明确禁止** join（`spawn_argv` 的返回类型是 `list[str]`，从类型上排除 join）。
* **ledger 记包装前的 `command`**（运维读日志看到的仍是模型给的那条命令，不是一串 bwrap 参数）。

#### A.2 机制实现：`BwrapBackend`（外部二进制 `bwrap`）

* **为什么是 bwrap**（用户拍板）：Flatpak 同款，**隔离更彻底** —— 自带 mount/PID/user 命名空间，
  不止文件系统，还能 `--unshare-net`。调研里 Linux 侧的共识后端。
* **代价与对策**：需要装包（`apt-get install bubblewrap`）且依赖**非特权 user namespaces**。
  * 本环境实测：`bwrap` 未安装、`unshare --mount` = `EPERM`（`--user` 可用）→ **这里跑不起来**。
  * 对策：CI 里显式 `apt-get install -y bubblewrap`；`available()` 探测失败时**给出稳定原因码**，
    绝不静默（§A.4）。
* **写边界的构造**（bwrap 的语义是"你 bind 什么就有什么"，**天然是 allow-list**，
  正好是 Pillar 批评的"allow-default denylist"的反面）：

  ```
  bwrap --ro-bind / /                 # 整个根只读
       --bind <workspace> <workspace> # 工作区可写（后绑覆盖先绑）
       --bind <writable_path> ...     # 构建缓存等（见下）
       --tmpfs /tmp                   # 临时文件需要可写
       --proc /proc --dev /dev
       --unshare-pid --die-with-parent --new-session
       -- <原命令>
  ```

  * `--die-with-parent`：父进程死时子进程跟着死（防孤儿）。
  * `--new-session`：脱离调用者的控制终端。
  * **顺序重要**：`--bind <ws>` 必须在 `--ro-bind / /` **之后**，否则被只读根覆盖。
* **可写面（评审 P1-2）**：只有 workspace 的 `enforce` **跑不了真实项目** ——
  `pip`/`npm`/`cargo` 会写 `~/.cache`、`node_modules/.cache`、`target`。
  * `roots` 来自 **`Sandbox.roots`**（workspace + extra_roots）—— 与路径层**同一个可写面**，
    否则会出现"路径工具能写、进程不能写"或反之的不一致（这也是 §C.4 选择"不做运行期可变"时要守住的边界）。
  * 额外可写路径来自 `policy.writable_paths`（默认含 `~/.cache`；**不含** `~/.ssh`/`~/.aws`/`~/.gnupg`）。
  * **取舍写进文档**：放宽可写面 = 缩小沙箱；`~/.cache` 不含凭据，是可用性与安全性的平衡点。
* **线程安全**：**无 `preexec_fn`** → `subprocess_runner` 的并发 `to_thread` 调用不受影响。
  这是选 argv 包装而非 `preexec_fn` 的主要理由。
* **探测与缓存（评审 P3-2 / P2-4）**：探针**复用 `wrap()` 的完整构造**（对 `-- true` 跑一次，
  含 `--unshare-pid --die-with-parent --new-session`）—— 只查 `which` 或只跑 `--ro-bind / /`
  会被"探针过、真实调用失败"骗过。失败原因码 `bwrap.unavailable`。
  **结果缓存**（`lru_cache` 或组合根探测一次）—— 否则每个 bash 调用都会 spawn 一次探针。

#### A.3 接线

| 位置 | 改动 |
|---|---|
| `security/process_sandbox.py`（新） | `SandboxGap` / `ProcessSandboxPolicy` / `SandboxBackend` / `BwrapBackend` |
| `infra/subprocess_runner.py` | **不改**（保持通用，不耦合安全概念） | —— |
| `tools/bash_tool.py` | `argv = spawn_argv(...)` → `run_process(argv, shell=False)` | M-D1 接线 |
| `core/jobs.py` | **不走 `run_process`** —— [L261](file:///workspace/ai-coding/ai_coding/core/jobs.py#L261) 是裸 `Popen(_bounded(command), shell=True)`，需**单独接线**：`argv = spawn_argv(...)` 且把 `_bounded(command)` **折进 `-c` 字符串**（否则 `ulimit -f` 日志上限静默失效，[L717-726](file:///workspace/ai-coding/ai_coding/core/jobs.py#L717-L726) 注释记录过 `yes` 写爆 1.59GB）（评审 P1-2） | M-D1 接线 |
| `config/models.py` + `config.example.yaml` | `process_sandbox`（`mode` / `allowBypass` / `writablePaths`）；`security.grantTtlSeconds` |
| `cli.py` / `server/world.py` | 构造 policy + backend；`JobRegistry`（[world.py:162](file:///workspace/ai-coding/ai_coding/server/world.py#L162)）与 `BashTool`（[builder.py:200](file:///workspace/ai-coding/ai_coding/tools/builder.py#L200)）共享同一个 backend 实例 |
| `.github/workflows/ci.yml` | `apt-get install -y bubblewrap`（否则真实子进程用例无法运行） |

**不覆盖 MCP 子进程**（长驻、需要 stdio/网络，隔离它需要整条传输改造）—— 写入 §七。

#### A.4 fail-closed 与上报

* `enforce` + 不可用 → **拒绝执行**，`ToolResult(error="denied", data.reason="bwrap.unavailable")`。
* `warn` + 不可用 → 继续，但**发一条 `Degraded` 事件**（`component="process_sandbox"`，`reason=<原因码>`）。
  * **复用既有的 `Degraded`**（[events.py:261](file:///workspace/ai-coding/ai_coding/core/events.py#L261)），**不新造事件类型** ——
    否则要牵动前端渲染/回放/golden（评审 P1-1：原稿 §四 #6 写的是新造 `SandboxDegraded`，矛盾）。
* **不静默**：两种情况都不装作"已隔离"。这是 agentkeeper 的原则，也是调研里反复出现的教训
  （"工具宁可显式上报 `DEGRADED`，也不给假绿色"）。

---

### B. M-D2 · 危险命令判定基准（回归检测 + 进 CI）

**定位**（用户拍板）：基准是**回归检测** —— 改动**不得引入新的漏报**（FN 在语料内必须为 0），FP 有界并记入指标表。
**诚实框定**："证明无漏报"**不可达** —— GuardFall 已证明文本过滤在结构上必有洞。基准的价值是**改动前后可比**，不是"绝对安全"。

#### B.1 语料（新，`security/redline_corpus.py`）

**六类 + 冻结判定**。每条语料是一个三元组 `(command, label, verdict)`，其中 **`verdict` 是
今天 `bash_denied` 的真实输出（逐条冻结）** —— 基准的硬断言是 **golden 比对**，不是"类别推断"。

| 类 | 语义 | `verdict` | 硬断言 |
|---|---|---|---|
| `dangerous` | 字面危险命令 | 非空（硬 DENY） | 非空 → 计入 **FN** |
| `unparseable_deny` | 语法**读不懂**且**含红线** | 非空（保守回退拦住） | 非空 → 计入 **FN** |
| `unparseable_warn` | 语法**读不懂**且**无红线** | **空**（→ 走审批门 **WARN**） | 与冻结值一致（**不计 FN**） |
| `safe` | 规则本就不该拦 | 空 | 空 → 计入 **FP** |
| `known_miss` | 语法**可读**但语义危险（变量展开/命令替换） | 空 | 与冻结值一致；**计数上报** |
| `known_fp` | 业务上安全但规则会拦（`chmod 777 ./build`） | 非空 | 与冻结值一致；**计数上报** |

**为什么把 `unparseable` 拆成两类（评审 P0-1 —— 原表按单一 verdict 冻结是错的）**：

实测（装上 tree-sitter 后真跑 `bash_denied`）：

```
'echo "rm -rf /'   => verdict = "recursive root deletion ('rm -rf /')"   parses_cleanly=False
'printf done &&'   => verdict = ''                                       parses_cleanly=False
'cat >'            => verdict = ''                                       parses_cleanly=False
```

`echo "rm -rf /` 是 [test_bash_red_lines.py:141-160](file:///workspace/ai-coding/tests/test_bash_red_lines.py#L141-L160)
钉住的"读不懂但含红线 → **DENY**"；`printf done &&` / `cat >` 是
[L163-173](file:///workspace/ai-coding/tests/test_bash_red_lines.py#L163-L173) 钉住的"读不懂且无红线 → **WARN**"。
**两条都是"读不懂"，但 verdict 一非空一空** —— 所以 `unparseable` 不能有单一冻结值，必须拆开。

* **"读不懂且无红线"是独立状态**（保持 WARN），既不进 `unparseable_deny` 也不进 `safe` —— 它现在有
  自己的标签 `unparseable_warn`。
* **FN 只统计 `dangerous` + `unparseable_deny`** —— `unparseable_warn` 的 verdict 空是**既有契约**，
  算成 FN 会让 CI 第一天就红。

**FN/FP 的口径（最终）**：

* **FN = `dangerous` + `unparseable_deny` 里 `verdict == ""` 的条数** —— 必须 0。
* **FP = `safe` 里 `verdict != ""` 的条数** —— 必须 0。
* `known_miss` / `known_fp` 的计数**按判定输出统计**（不是按标签）——
  这样"缺口被修好"会**反映在指标上**（计数降到 0），而不是永远 >0（评审 P2-2）。
* **golden 断言覆盖全部六类**：任何一条的 `verdict` 变了就红 —— 这是"回归检测"的本体。
* **`known_miss` 的 re-freeze 流程（评审 P1-5）**：某条的冻结值是**当时**的快照。
  **修好缺口时**必须同步：① 该条 `verdict` 改非空；② 标签从 `known_miss` 移到 `dangerous`。
  这是**受评审的语义变更**，不是"偷偷改测试" —— 基准的 golden 会强制这一步显式发生。

**为什么单设 `known_miss`（本次调研最重要的发现之一）**：

`bash_denied` 在 [permission_gate.py:232](file:///workspace/ai-coding/ai_coding/security/permission_gate.py#L232)
处有个**具体的、可修的**缺口 ——

```python
reason = bash_red_line(command)
if reason:
    return reason
if _grammar_reads(command):
    return ""          # ← 语法能读 → 直接放行，**不再看文本回退**
```

于是 `x="rm -rf /"; $x` 这类**语法可读但语义危险**的命令**直接放行**：
tree-sitter 读得懂（赋值 + 变量展开），`_runs(view, "rm")` 为假 → 无规则命中 → `""` →
`_grammar_reads` 为真 → 返回 `""`。

* **归因要准确（评审 P1-6）**：文本回退里**真正会拦住它的是子串表**
  （`_BASH_HARD_DENY_PATTERNS`，`rm -rf /` 是其中一条子串），
  **不是** `_dangerous_recursive_root_delete` —— 后者的 `program = Path(words[0]).name`
  取到的是 `x="rm` 之类的词形，实测返回 `False`。
* 这正是 GuardFall 的机理（"过滤器检查的字符串不是内核执行的 argv"）在本仓库的**具体落点**。
* **本轮不修**（修它要决定"可读但无命中时是否回退"，会引入新的 FP，是独立的语义变更）。
* **但基准必须把它显式列出来** —— 否则这个缺口会一直隐形。指标表里的 `known_miss` 计数
  就是这个缺口的**可见性**；修好之后它会**降到 0**（这是它和工作到什么程度的可度量信号）。

语料来源：
* 现有 `test_bash_red_lines.py` / `test_permission_gate.py` 的用例（升级为语料，**verdict 从今天的实现冻结**）；
* roadmap 点名的：`rm -rf /`、`sudo`、`dd if=`、`> /dev/sd`、强制 push；负例 `rm -rf ./build`、引号里的 `sudo`；
* **GuardFall 的混淆样本** → 按上表**分流**到 `unparseable`（读不懂的）或 `known_miss`（读得懂但放行的）。

#### B.2 基准脚本（新，`scripts/redline_benchmark.py`）

* 读语料 → 逐条跑 `bash_red_line_with_fallback` → **golden 比对**：
  * **任何一条的 `verdict` 与冻结值不符 → exit 非零**（回归检测的本体）
  * **FN**（`dangerous` 里 `verdict == ""`）—— **必须 0**
  * **FP**（`safe` 里 `verdict != ""`）—— **必须 0**
  * **`known_miss` / `known_fp` 计数** —— **按判定输出统计**，不失败，但必须出现在指标表里
  * 按规则分类的命中计数（供人看"哪条规则在拦什么"）
* 输出 Markdown 指标表（便于贴进 PR / `data-contracts.md`）。
* **进 CI**（`ci.yml` 加一步 `python scripts/redline_benchmark.py`）。
* **语料规模断言**（评审 P2-6）：`dangerous` ≥ 20、`unparseable` ≥ 10、`safe` ≥ 20、
  `known_miss` ≥ 5 —— 防止语料被悄悄删空后基准"假绿"。照抄
  [test_permission_gate_equivalence.py:151](file:///workspace/ai-coding/tests/test_permission_gate_equivalence.py#L151) 的 gold-standard 风格。

#### B.3 合并"双读法"（用户拍板）

**问题**：`bash_redlines`（语法层）与 `permission_gate`（文本回退层）**各有一份** `rm -rf /`、`chmod 777` 判定。
刻意保守，但**两处规则要同步维护** —— 今天只靠口头约定。

**合并方式**（单一判据，但**不改变任何一层的语义**）：

| 动作 | 说明 |
|---|---|
| 把回退层**移进** `bash_redlines.py` | `_normalize_command` / `_command_words` / `_BASH_HARD_DENY_PATTERNS` 从 `permission_gate` 迁入 |
| 新增 `bash_red_line_with_fallback(command) -> str` | = 今天 `bash_denied` 的**完整**逻辑（语法层，读不懂再回退） |
| `bash_red_line(command)` **契约不变** | 仍是纯语法层（读不懂返回 `""`），供需要区分"语法能读"的调用方 |
| `permission_gate.bash_denied` **改为委托** | 只调 `bash_red_line_with_fallback`，自身不再有规则 |

* **为什么不动 `bash_red_line` 的契约**：合并的目标是"**只有一处做危险判定**"，
  不是"把两层揉成一个函数"。保留纯语法层让基准能**分别**度量两层（见 B.2 的分类计数）。
* **必须同步改的文档（评审 P1-5）**：合并后这两处的注释**不再成立**，必须改：
  * [bash_redlines.py:272-292](file:///workspace/ai-coding/ai_coding/security/bash_redlines.py#L272-L292) 的
    "读不懂的命令不在此判定——调用方回退" → 改为"读不懂时由 `bash_red_line_with_fallback` 回退"。
  * [permission_gate.py:220-223](file:///workspace/ai-coding/ai_coding/security/permission_gate.py#L220-L223) 的
    "A command the grammar cannot read falls back to..." → 改为"委托给 `bash_red_line_with_fallback`"。
  * **不改语义**：只改"谁来做"，不改"怎么判"。基准（U9）是安全网。
* **基准盯着它**：U9 断言 `bash_red_line_with_fallback` 与**旧 `bash_denied` 的冻结快照**在
  **整个语料 + 现有测试集**上**逐条等价** —— 合并是**行为保持**的重构，基准是它的安全网。
* **风险与回退**：回退层动的是"语法读不懂时怎么办"，属于保守路径。若合并后基准出现任何 FN，
  **立即回退**到"两层分列"（git 可回滚）。

---

### C. M-D3 · 权限授予的最小化（TTL + 逐条撤销）

#### C.1 契约扩展（`grants.json` —— **不 bump 版本**，评审 P1-3）

项目**已有迁移约定**（[data-contracts.md:257-258](file:///workspace/ai-coding/docs/data-contracts.md#L257-L258)）：
"**加可选键、不 bump 版本**，让旧代码读新文件时忽略未知键"。原稿把 `workspaces[ws]` 从
`list[str]` 改成 `list[dict]` 并 bump 到 2，会**违反这条约定**：旧 `load()` 对 dict 元素
`isinstance(key, str)` 为假 → 返回 `set()` → **降级后所有 standing grant 静默消失**。

**正确做法：形状不变，新增可选键**：

```json
{
  "version": 1,
  "workspaces": {"/abs/ws": ["read:src@build"]},
  "meta": {
    "/abs/ws": {"read:src@build": {"grantedAt": 1761465600.0, "ttl": 86400}}
  }
}
```

* `workspaces[ws]` **仍是 `list[str]`** —— 旧代码读到新文件**行为不变**。
* `meta` 是**可选键**：旧代码忽略它；新代码读它拿 TTL。
* **不 bump `version`** → [test_grant_store.py:126-134](file:///workspace/ai-coding/tests/test_grant_store.py#L126-L134)
  的 `version == 1` 与 `workspaces[ws] == ["edit:a.py"]` 断言**仍然成立**（T1 从"必须改"变为"必须仍绿"）。
* **TTL 可配**：`security.grantTtlSeconds`（默认 `86400` = 24h）。`None` = 永久（显式 always）。
* **时间格式统一（评审 P1-7）**：`grantedAt` 用 **epoch 浮点**（`time.time()`），**不用** ISO 字符串。
  * 写入、比较、序列化**全部**用 epoch —— 混用两种格式是漂移之源。
  * 注入的 clock **返回 epoch**（默认 `time.time`）。项目已有的
    [infra/time_utils.now_utc](file:///workspace/ai-coding/ai_coding/core/jobs.py#L55) 返回的是
    `datetime`，**不复用**（会让 `GrantStore` 依赖 datetime 且引入两种格式）。
* **`add()` 的去重语义（评审 P3-2）**：同键重复 `add` → **更新 `meta` 的时间戳**（刷新 TTL），
  而不是追加一条 —— 否则"重新批准"不会延长有效期。
* **`clear()` / `revoke()` 必须同步删 `meta`（评审 P3-3）**：`GrantStore.clear()` 今天只删
  `workspaces[ws]`，扩展后**必须同时删 `meta[ws]`** —— 否则撤销后 `meta` 里的过期信息会残留。
* **`meta` 与 `workspaces[ws]` 的键一致性（评审 P3-4）**：读取时**以 `workspaces[ws]` 为准**
  （它是权威列表）；`meta` 里**多出的键忽略**、**缺失的键视为永久**。写入时两者同步。
  —— 这样单侧损坏不会放大成安全问题。

#### C.2 到期

* `GrantStore.load()` 过滤过期的（`grantedAt + ttl < now`）。
* **TTL 的数据通路（评审 P1-4 + P1-3）** —— 两个洞，都要补：

  **洞一：gate 侧没有时间戳。** `_grants` 是 `set[str]`、`_grant_applies` 逐字符串比较 ——
  必须加一条并行映射：

  ```python
  self._grant_expiry: dict[str, float] = {}   # key -> 过期 epoch；缺省 = 永久
  ```

  * `_grant_applies` 命中 grant 时**先查过期** → 过期即视为未授权。
  * `grant()` 写入时同时写 `_grant_expiry`（TTL 来自配置）。
  * `revoke_all()` 一并清空。

  **洞二：磁盘加载的授权拿不到 expiry。** `GrantStore.load()` 只返回 `set[str]`，
  `from_config` 也只把 `set[str]` 传给 `grants=` → **持久化授权在 `_grant_expiry` 里没有条目** →
  过期检查对它们是空操作 → **TTL 对持久化授权永不生效**。

  * 修法：`GrantStore` 增加 `load_with_expiry() -> tuple[set[str], dict[str, float]]`，
    `from_config` 用它**同时 seed** `_grants` 与 `_grant_expiry`。
  * **U12/U13 必须覆盖"从磁盘加载的带 TTL 授权在会话内过期"**，否则这个洞会原样保留。
* `PermissionGate.grants` 只返回**未过期**的（面板看到的是"当前有效"）。
* **`load()` 只读、`prune()` 单独写盘（评审 P2-4）** —— 原稿两处自相矛盾（§C.2 说"每次 load 都可能写"，
  §8.2.1 说"不在 `load()` 里写"）。统一为：
  * `load()` / `load_with_expiry()` **只读 + 内存过滤**，不写盘。
  * `prune()` 显式写盘，**调用点**：`grant()` / `revoke()` 之后（写路径顺带清理）。
  * `grants.json` 的读写**无锁**（今天也如此，best-effort 语义），多写者下最坏是"某次 prune 丢失"，
    可接受 —— **不引入锁**（与项目既有语义一致）。

#### C.3 逐条撤销

* `PermissionGate.revoke(key)` —— 内存（`_grants` + `_grant_expiry`）+ 磁盘。
* **键空间（评审 P1-5）**：真实键的形状是 **`tool:path@posture`**
  （[permission_gate.py:991](file:///workspace/ai-coding/ai_coding/security/permission_gate.py#L991)，
  例：`read:src/foo.py@build`）—— 原稿示例 `read@build` / `read` **与真实键不符**。规则：
  * 传**全键**（`read:src/foo.py@build`）→ 精确撤销该条；
  * 传**基名**（`@` 之前的部分，如 `read:src/foo.py`）→ 撤销该基名的**全部 posture 变体**；
  * 两者都接受；面板显示全键，命令接受两种。
* `permissions.revoke` handler + `/permissions revoke <key>` 命令。
* 面板**显示** `_once_grants`（session 级）+ 每条的过期时间。

#### C.4 `extra_workspace_roots` 运行期可变

**不做**（§七）。理由：它要重建 `Sandbox`（所有文件工具的依赖），回归面远大于本轮目标，且与 TTL/撤销是**不同的**最小化维度。
**但守住一条边界**：进程层的可写面（`roots`）**来自** `Sandbox.roots`（见 A.2）—— 两层永远是同一个可写面。

---

## 四、逐文件改动清单

| # | 文件 | 改动 | 为什么 |
|---|---|---|---|
| 1 | `security/process_sandbox.py`（新） | `SandboxGap` / `ProcessSandboxPolicy` / `SandboxBackend` / `BwrapBackend` / **`spawn_argv`** | M-D1 的策略/机制分离 + 唯一 wrap 入口 |
| 2 | `tools/bash_tool.py` / `core/jobs.py` | 改用 `spawn_argv(...)` + `shell=False`（**唯一两个调用方**） | M-D1 接线 |
| 3 | `config/models.py` | `process_sandbox`（mode / allowBypass / writablePaths）+ `security.grantTtlSeconds` | 配置 |
| 4 | `tools/builder.py` | `BashTool` 在 [L200](file:///workspace/ai-coding/ai_coding/tools/builder.py#L200) 构造、`Sandbox.roots` 也在那里可得 → **policy/backend/roots 经 `_build_tool_registry` 传入**（评审 P2-5：原稿漏了） | M-D1 接线 |
| 5 | `cli.py` / `server/world.py` | 构造 policy + backend | 组合根 |
| 6 | `core/events.py` | **复用** `Degraded`（`component="process_sandbox"`），**不新造事件类型** | M-D1 的上报（评审 P1-1） |
| 7 | `security/bash_redlines.py` | 迁入回退层（**完整清单见下**）+ 新增 `bash_red_line_with_fallback` | M-D2 合并双读法 |
| 8 | `security/permission_gate.py` | `bash_denied` 改为委托（**删掉自己的规则**）；`_grant_expiry` + 过期过滤；`load_with_expiry` seed | M-D2 单一判据 + M-D3 TTL 通路（评审 P1-4 / P1-3） |
| 9 | `security/redline_corpus.py`（新） | **六类**语料（`dangerous`/`unparseable_deny`/`unparseable_warn`/`safe`/`known_miss`/`known_fp`）+ **逐条冻结 verdict** | M-D2（评审 P0-1） |
| 10 | `scripts/redline_benchmark.py`（新） | 基准脚本（golden 全绿 / FN=0 / FP=0 / 分类计数） | M-D2 |
| 11 | `.github/workflows/ci.yml` | 加基准步骤 + `apt-get install -y bubblewrap` | M-D2 进 CI + M-D1 可跑 |
| 12 | `security/grants.py` | **`meta` 可选键**（形状不变、**不 bump 版本**）+ TTL + `load_with_expiry` + `prune()` + `revoke(key)` + 时钟注入 | M-D3（评审 P1-3） |
| 13 | `security/permission_gate.py` | `revoke(key)` / 过滤过期 / 暴露 `_once_grants` | M-D3 |
| 14 | `server/handlers.py` | `permissions.revoke` + 面板显示 once/过期 | M-D3 |
| 15 | `app/commands.py` | `/permissions revoke <key>` | M-D3 |
| 16 | `frontend/src/util/permissions-dialog.ts` + `component/dialog-permissions.tsx` | `PermissionsState.grants` 变为带过期时间的记录；**`grantLine` 签名同步**；渲染 once grants | M-D3（评审 P1-3 / P3-3） |
| 17 | `config.example.yaml` | 新配置面的示例；**给 `warn` 示例**（默认 `enforce` + 无 userns = 拒掉所有 bash，评审 P3-6） | 评审 P2-5 / P3-6 |
| 18 | `docs/data-contracts.md` | 契约同步 | |

**回退层迁入 `bash_redlines.py` 的完整清单（评审 P2-3 —— 漏一个 U9 就会假绿）**：

| 符号 | 处置 | 现位置 |
|---|---|---|
| `_normalize_command` / `_command_words` / `_SHELL_SPLIT` | 迁入 | `permission_gate.py` |
| `_dangerous_recursive_root_delete` / `_world_writable_chmod` | 迁入 | `permission_gate.py` |
| `_BASH_HARD_DENY_PATTERNS` | 迁入 | `permission_gate.py` |
| `_grammar_reads` | 迁入（判断"语法是否读完"，合并后属于判据的一部分） | `permission_gate.py` |
| `_reads_credential(words)` | 迁入，**改名 `_reads_credential_words`** | `permission_gate.py` |
| `_reads_credential(view)` | **复用既有**（`bash_redlines.py:165` 已有） | `bash_redlines.py` |
| `_HOME_PREFIX` | **复用既有**（两处定义完全相同） | 两边都有 |
| `command_words` / `parses_cleanly` | **import 补上**（回退层依赖；`bash_redlines` 目前只 import `command_shape, command_views`） | `shell_parse.py` |

> **重名是硬伤（评审 P1-4）**：`bash_redlines._reads_credential(view: CommandView)` 与
> `permission_gate._reads_credential(words: list[str])` **同名不同签名**。若按原名迁入，
> 模块级后定义覆盖前定义 → `bash_red_line` 会拿 `CommandView` 当 `list[str]` 迭代 → `TypeError`，
> **整条语法层红线判定挂掉**。必须改名。
>
> 另：`_verdict` 的测试辅助与 `test_bash_red_lines.py` 的 `_verdict` 包装需同步。

**测试条目（评审 P0-4 —— 原稿漏了）**：

| # | 文件 | 改动 |
|---|---|---|
| T1 | `tests/test_grant_store.py` | **必须仍绿**：[L126-134](file:///workspace/ai-coding/tests/test_grant_store.py#L126-L134) 断言 `version == 1` 且 `workspaces[ws] == ["edit:a.py"]` —— 因为**形状不变、不 bump 版本**（评审 P1-3）。**若它变红，说明扩展方式错了** |
| T2 | `tests/test_permission_gate.py` | `revoke(key)` / 过期过滤 / `_once_grants` 暴露 |
| T3 | `tests/test_bash_red_lines.py` | 语料升级后仍绿（**行为保持**） |
| T4 | `tests/test_process_sandbox.py`（新） | U1–U7 |
| T5 | `tests/test_redline_benchmark.py`（新） | U8 / U9 |
| T6 | `frontend/src/util/permissions-dialog.test.ts` | 面板契约变更后仍绿 |
| T7 | `tests/test_dispatch_grants.py` | 撤销后 dispatcher 的行为 |

> **不新造轮子**：策略/机制用 Protocol（项目里 `RootGrantor` 同款）；`grant_key`/posture
> 后缀/`_once_grants` 全部复用；时钟注入沿用 `TurnCloser` 的 `clock` 形状。

### 4.1 语义等价性

* **`mode="off"`**：行为与今天**逐字节相同**（`wrap()` 返回 `None` → argv 原样 → `shell=False` 执行）。
* **`mode="enforce"`（默认）+ 可用**：工作区外不可写；工作区内照常。
* **`mode="enforce"` + 不可用**：拒绝（今天会执行）—— 这是**有意的行为变更**，也是"不假装修了"的代价。
* **`bypass=True`**：本次不隔离（roadmap 点名的显式 allow）。
* **`shell=True` → `["/bin/sh","-c",cmd]` + `shell=False`**：**语义等价**（`/bin/sh -c cmd` 就是
  `shell=True` 的定义），但**不再让 bwrap 的参数被二次解析**。
* **M-D2**：**判定逻辑语义不变**（合并是行为保持的重构，U9 冻结快照证明）；
  新增的只有语料、基准脚本、以及 `known_miss` 的**上报**（不改变任何判定结果）。
  **注意**：`unparseable` 类的冻结 verdict 是**空**（走审批门 WARN），不是硬拒 —— 这是既有契约。
* **M-D3**：v1 授权仍有效（视为永久）；新授权带 TTL；面板多出信息。

### 4.2 对数据流的影响

* **bash/job 的进程树**：argv 多一层 bwrap 前缀（`--ro-bind` / `--bind` / `--tmpfs` / `--proc` / `--dev` / 3 个开关）。
  **无 `preexec_fn`**（评审 P2-3 —— 原稿这里残留了 landlock 路线的描述）。
* **`grants.json`**：从 `{"workspaces": {"ws": ["key"]}}` 变为带元数据的记录；**v1 可读**。
* **写放大**：`enforce` 模式下每个 bash 调用一次 `wrap()`（纯 argv 构造）—— 微秒级，可忽略；
  探针结果已缓存，不会每个调用都 spawn 一次 `bwrap`。

---

## 五、RIPR 用例

### Red（先写，必须变红）

| 编号 | 用例 | 期望红相（今天必须失败） |
|---|---|---|
| U1 | `test_process_sandbox.py::test_a_process_under_enforce_cannot_write_outside` | 真实子进程在 `enforce` 下 `echo x > <ws>/../outside.txt` → **失败**（今天：成功） |
| U1b | `test_process_sandbox.py::test_bash_tool_execute_is_actually_sandboxed` | **经 `BashTool.execute`** 的端到端：`enforce` 下真跑一条写工作区外的命令 → 被拒（评审 P0-1：只测 backend 抓不到"调用方忘了 wrap"） |
| U1c | `test_process_sandbox.py::test_a_background_job_is_sandboxed` | **经 `JobRegistry.start`**：job 在 `enforce` 下写不到工作区外（评审 P1-2） |
| U2 | `test_process_sandbox.py::test_a_process_under_enforce_can_write_inside` | 同进程写工作区内 → 成功（今天：成功 → **本条是 Prove**） |
| U2b | `test_process_sandbox.py::test_enforce_still_reads_outside_the_workspace` | **反向断言**：`enforce` 下读 `~/.ssh/id_rsa`（或任一工作区外文件）**仍成功** —— 本轮**只保证写边界**，防止把"沙箱"误读为"隔离"（评审 P2-6） |
| U3 | `test_process_sandbox.py::test_enforce_refuses_when_the_mechanism_is_missing` | backend 报不可用 → `bash` 返回 **`error == "denied"`** + `data["reason"] == "bwrap.unavailable"`（评审 P1-1：不 catch 就退化成 `tool_error`）（今天：无机制，会执行） |
| U3b | `test_process_sandbox.py::test_enforce_refuses_when_the_backend_is_missing` | `backend=None` + `enforce` → `SandboxGap("sandbox.backend.missing")`（评审 P1-2） |
| U4 | `test_process_sandbox.py::test_warn_degrades_and_reports` | `warn` + 不可用 → 执行 + **`Degraded`** 事件（`component="process_sandbox"`）（今天：无事件） |
| U5 | `test_process_sandbox.py::test_the_gap_has_a_stable_reason_code` | `SandboxGap.reason == "bwrap.unavailable"`，且**经 `BashTool.execute`** 时 `error == "denied"`（今天：无） |
| U6 | `test_process_sandbox.py::test_bypass_skips_the_mechanism` | `bypass=True` → 不包装（今天：无） |
| U7 | `test_process_sandbox.py::test_wrap_puts_the_writable_bind_after_the_readonly_root` | `wrap()` 的 argv 里 `--bind <ws>` 在 `--ro-bind / /` **之后**（顺序错则工作区仍只读） |
| U8 | `test_redline_benchmark.py::test_the_corpus_matches_its_frozen_verdicts` | **golden 全绿**：每条语料的 `verdict` 与冻结值一致（今天：无基准 → 用例无法运行/断言"基准存在"失败） |
| U9 | `test_redline_benchmark.py::test_the_two_readers_agree` | `bash_red_line_with_fallback` 与**旧 `bash_denied` 的冻结快照**在语料 + 现有测试集上**逐条等价**（合并是行为保持） |
| U10 | `test_redline_benchmark.py::test_known_miss_is_surfaced` | `x="rm -rf /"; $x` 被计入 `known_miss` 且**出现在指标表里**（今天：无上报 → 缺口隐形） |
| U11 | `test_redline_benchmark.py::test_the_corpus_is_not_emptied` | 语料规模 ≥ 阈值（dangerous 20 / unparseable 10 / safe 20 / known_miss 5） |
| U11b | `test_redline_benchmark.py::test_unparseable_splits_into_deny_and_warn` | `echo "rm -rf /` 在 `unparseable_deny`（非空）、`printf done &&` 在 `unparseable_warn`（空）—— **两类都有**，证明拆分不是空的（评审 P0-1） |
| U12 | `test_grant_store.py::test_an_expired_grant_is_not_loaded` | 写入带 TTL 的授权 → 过期后 `load_with_expiry()` 不含它（今天：无 TTL） |
| U13 | `test_grant_store.py::test_a_disk_loaded_grant_expires_inside_the_session` | **从磁盘加载**的带 TTL 授权 → `from_config` seed 后，**会话内**过期即失效（评审 P1-3 —— 这是"持久化授权 TTL 永不生效"那个洞的回归锁） |
| U14 | `test_permission_gate.py::test_a_single_grant_can_be_revoked` | `revoke(key)` 只删一条；传基名撤销全部 posture 变体（今天：只有 `revoke_all`） |
| U15 | `test_permission_gate.py::test_the_panel_shows_once_grants_and_expiry` | `permissions_state` 含 once grants + 过期时间（今天：无） |

> U2 是 **Prove**（断言"工作区内可写"，实现前后都成立）。
> **U1/U1b/U1c/U2 需要真 `bwrap`**：`@pytest.mark.skipif(not BwrapBackend().available(), reason="bwrap unavailable")`
> —— **显式理由，不是静默通过**；本地会跳过，CI（装了 bubblewrap）会跑。
> **U2b 是刻意的反向断言**：它钉住"读边界**不在**本轮范围"——若将来有人顺手加了读拒绝，
> 这条会红，提醒他那是一次**需要单独评审的语义变更**。
> **U10 是本次调研的核心产出**：它把"语法可读但无规则命中即放行"这个真缺口
> 从隐形变成**每次跑基准都看得见**的数字。

### Prove

1. `.venv/bin/python -m pytest -q`
2. `.venv/bin/ruff check .` / `.venv/bin/mypy ai_coding`
3. **基准脚本**：`python scripts/redline_benchmark.py` → FN 0、FP 有界、指标表
4. **突变测试**：
   * 把 `wrap()` 里 `--bind <ws>` 移到 `--ro-bind / /` **之前** → U7 变红
   * 去掉 `available()` 的实跑探测（只查 `which`）→ U5 变红
   * 把 `shell=False` 改回 `shell=True` → U1 变红（隔离静默失效）
   * 去掉 TTL 过滤 → U12 变红
   * 去掉 `revoke(key)` → U14 变红
   * 把 `bash_red_line_with_fallback` 换回"只调 `bash_red_line`" → U9 变红（证明基准真的盯着合并）
   * 把 `KNOWN_MISS` 的计数从指标表里去掉 → U10 变红（证明缺口不是隐形的）
5. **3.10 venv 跑 CI 同构七步**（CI 矩阵是 **3.10 + 3.13**）
6. **端到端**：`enforce` 下真跑一次 `bash` 写工作区外 → 被拒；CI 里跑（装了 bubblewrap）

### Refactor

* 确认 `ProcessSandboxPolicy` 与 `BwrapBackend` **互不引用**（策略不知道机制是谁）
* 确认 `permission_gate` 里**不再有**任何危险命令规则（`_BASH_HARD_DENY_PATTERNS` 已迁走）

---

## 六、验收判据

* **M-D1**：`enforce` 下真实子进程**写不到**工作区外（CI 装了 bubblewrap 后验证）；机制不可用时
  **显式拒绝或显式降级**（绝不静默）；策略与机制可独立替换。
* **M-D2**：基准脚本产出 **golden 全绿** + FN=0（`dangerous`）/ FP=0（`safe`）+ 分类计数；
  **进 CI**；**双读法合并为单一判据**，且基准用**冻结快照**证明合并是行为保持的；
  `known_miss` 计数**可见且会随修复降到 0**。
* **M-D3**：授权可**到期**（TTL）；可**逐条撤销**；v1 授权仍可读；面板显示 once grants 与过期时间。
* 3.10 与 3.13 全量绿；真实 CI 双 job 绿。

---

## 七、不做的事

* **不做读边界**（`~/.ssh` 等敏感路径的读拒绝）—— 读白名单平台相关，且**没有网络墙时**读泄漏的后果有限；与网络墙同一轮做。
* **不做网络墙** —— 需代理桥接，工作量与回归面大一个量级；调研说"两墙缺一不可"，本轮**先补上写边界**，并把"缺另一半"写进文档与事件。
* **不覆盖 MCP 子进程** —— 长驻、需要 stdio/网络，隔离它要整条传输改造。
* **不改 `Sandbox`（路径层）** —— 它与进程层正交。
* **不做 `extra_workspace_roots` 运行期可变** —— 要重建 `Sandbox`，与 TTL/撤销是不同维度。
* **不引入外部 Python 沙箱包**（`pylandlock` 之类）—— 机制只用系统二进制 `bwrap`。
* **不修"语法可读但无规则命中"的缺口**（`x="rm -rf /"; $x`）—— 见 B.1 的 `KNOWN_MISS`；
  修它要决定"可读但无命中时是否回退"，会引入新的 FP，是独立的语义变更。**基准把它显式列出**。

### 7.1 残余风险（显式记录，不藏）

| 风险 | 说明 | 处置 |
|---|---|---|
| **AF_UNIX 逃逸** | CVE-2026-47128：Landlock/seccomp 未仲裁 `AF_UNIX` 路径 → 经 per-user systemd D-Bus 逃逸。**bwrap 的构造里也没有套接字过滤**（评审 P1-6） | 本轮**不修**；写进本文档 + 事件。修法是 `--unshare-net` 不够，要过滤 `AF_UNIX` 的 `connect`/`bind`（bwrap 目前无此粒度） |
| **bwrap 可移植性** | 无 userns 时（Debian/RHEL 常见）`enforce` 会**拒掉所有 bash**（评审 P3-4） | `available()` 探测 + `warn` 档位兜底；文档写明"先确认 userns 可用" |
| **工作区配置是可执行代码** | Pillar 失效模式②：`.vscode` 任务、hook、virtualenv 解释器、git `fsmonitor` —— 代理写入被允许的文件，宿主稍后以可信方式执行 | **不在本轮范围**（要改的是"宿主的信任边界"，不是进程沙箱） |
| **特权守护进程在沙箱之外** | Pillar 失效模式④：Docker socket 等 | **不在本轮范围** |
| **"安全命令"仅凭名字不安全** | Pillar 失效模式③：`git show` 可写文件/触发 hook | M-D2 的语料已覆盖一部分；完整治理需"按副作用建模"（下一轮） |

---

## 八、评审记录

### 8.1 方案评审（子智能体，第 1 轮）

**4 个 P0（全部已修）**：

| # | 问题 | 修法 |
|---|---|---|
| P0-1 | `wrap()` 与 `shell=True` 契约不匹配 —— `bash_tool`/`jobs` 传的是 shell 字符串，`shell=True` 会**再包一层 `/bin/sh -c`**，外层 shell 把 bwrap 参数重新解析一遍，**隔离静默失效** | 新增 §A.1b：调用方规范化为 `["/bin/sh","-c",cmd]` + **强制 `shell=False`**；ledger 记包装前命令 |
| P0-2 | OPAQUE/FN=0 与"不改判定逻辑"**自相矛盾** | 核实 `bash_denied:232`：**语法可读即直接返回 `""`，不回退** → `x="rm -rf /"; $x` 是真漏报。改为**五类语料**，FN 只统计 `DANGEROUS`+`UNPARSEABLE`；新增 `KNOWN_MISS` 类**只上报不失败**（见 B.1） |
| P0-3 | FP 默认 0 与现状冲突（`chmod 777 <任意>`、`sudo <任意>` 一律 DENY） | FP 只统计 `SAFE` 语料；已知 FP 进 `KNOWN_FP`（只上报）；并给 SAFE 语料的边界定义 |
| P0-4 | v2 破坏现有测试（`test_grant_store.py:126-134` 断言 `version == 1`），且改动清单**没有 tests 条目** | §四 补 **T1–T7** 测试条目，T1 明确标注"必须改" |

**8 个 P1（全部已修）**：默认档位（P1-1 → 默认 `enforce`）、可写面（P1-2 → `writable_paths`）、
前端契约（P1-3 → §四 #16）、jobs 接线与 ledger（P1-4）、docstring 同步（P1-5 → B.3）、
AF_UNIX 残余风险（P1-6 → §7.1）、时钟格式（P1-7 → §C.1 epoch 统一）、撤销键空间（P1-8 → §C.3）。

**7 个 P2 + 5 个 P3（全部已修）**：`Literal` 而非裸 `str`、复用 `Degraded`、§4.2 的 `preexec_fn` 残留、
消费方表笔误、`config.example.yaml`、语料规模断言、prune 写盘说明、`gap()` 单一来源、
探针缓存、`roots` 来自 `Sandbox.roots`、bwrap 可移植性、§八 记录与调研链接。

### 8.2 方案评审（子智能体，第 2 轮）

> 复核四个点：bwrap argv 包装能否真隔离、五类语料 FN/FP 口径、grants v2 向后兼容、`shell=False` 语义等价。
> 结论：**机制本身成立**（bwrap 的 allow-list 构造、顺序、argv 前缀化都正确；`shell=False` 与 `/bin/sh -c cmd` 在 CPython 下等价），
> 但**落点与口径有 2 个 P0 必须先解决**。

**2 个 P0**：

| # | 问题 | 修法 |
|---|---|---|
| P0-1 | **包装落点二义 + `run_process_async` 未列入改动**：§A.1b 的代码块与 §A.3 的 `bash_tool` 行说**调用方** `wrap` 后再把 argv 交给 runner；§四 #2 却说 `run_process(..., sandbox, roots)` 由 **runner** `wrap`。两者都实现 = **双重 bwrap**；只实现 §四 #2，则 `bash_tool` 实际走的 `run_process_async` 构造的固定 `kwargs`（[subprocess_runner.py:285-296](file:///workspace/ai-coding/ai_coding/infra/subprocess_runner.py#L285-L296)）**根本不含 `sandbox`/`roots`** → **M-D1 对 bash 完全失效**，而 U1/U2 只测 backend，抓不到这个缺口 | 二选一并写死。建议：runner 只接收**已包装**的 argv（A.1b 口径），`bash_tool`/`jobs` 各自 wrap；并补一条**经 `BashTool.execute`** 的端到端用例（现 U1/U2 只覆盖 backend） |
| P0-2 | **`UNPARSEABLE` 判据 `!= ""` 与仓库已钉住的行为矛盾**：§B.1 定义 `UNPARSEABLE` = "语法读不懂" → 判据 `!= ""`（必须拦）→ 硬失败（FN）。但 [test_bash_red_lines.py:163-173](file:///workspace/ai-coding/tests/test_bash_red_lines.py#L163-L173) 钉住 `"printf done &&"` / `"cat >"` **读不懂但无红线**时返回 `""`（WARN）；实测 `bash_denied("printf done &&") == ""`、`bash_denied("cat >") == ""`。而 §B.1 的语料来源又明确含"现有 `test_bash_red_lines.py` 用例（升级为语料）" → 放进 `UNPARSEABLE` 则 FN≥1、CI 第一天就红；放进 `SAFE` 则污染 `SAFE` 的定义 | 把 `UNPARSEABLE` 定义为"**读不懂且含红线**（保守回退应拦）"，并显式声明"**读不懂且无红线**"是第三种状态（保持 WARN），既不进 `UNPARSEABLE` 也不进 `SAFE` |

**6 个 P1**：

| # | 问题 | 修法 |
|---|---|---|
| P1-1 | **§四 #6 与 §A.4 直接冲突**：§A.4 要求"复用既有的 `Degraded`，**不新造事件类型**（评审 P2-2）"，§四 #6 仍写 `core/events.py \| SandboxDegraded + 进 LoopEvent`。实测 `Degraded` 已在 `LoopEvent`（[events.py:573](file:///workspace/ai-coding/ai_coding/core/events.py#L573)）与 `__all__`（[events.py:644](file:///workspace/ai-coding/ai_coding/core/events.py#L644)）中 | 删掉 §四 #6 该行；按 §A.4 用 `Degraded(component="process_sandbox", reason=<码>)` |
| P1-2 | **jobs 路径未接线，且 `_bounded` 的日志上限会被静默丢掉**：§A.3 只写"同路"，未给 `JobRegistry.start` 的注入方式；`jobs.py` **不经过 `run_process`**，而是直接 `subprocess.Popen(_bounded(command), shell=True, ...)`（[jobs.py:261-270](file:///workspace/ai-coding/ai_coding/core/jobs.py#L261-L270)）。若规范化成 `["/bin/sh","-c",command]` 而忘了把 `_bounded(command)` 折进去，`ulimit -f` 上限（[jobs.py:717-726](file:///workspace/ai-coding/ai_coding/core/jobs.py#L717-L726)，注释记录过 `yes` 写爆 1.59GB）静默失效；且 ledger 的 pid/identity/kill 都以 bwrap 的 pid 为准，方案未说明这层是否仍成立 | 明确 `JobRegistry.start` 的签名与 backend 注入；把 `_bounded(command)` 折进 `-c` 的字符串；补一条"job 在 enforce 下写不到工作区外"的用例 |
| P1-3 | **grants v2 改形状 + bump version，违反项目自己的迁移约定**：既有约定（[data-contracts.md:257-258](file:///workspace/ai-coding/docs/data-contracts.md#L257-L258)）是"**加可选键、不 bump 版本**，让旧代码读新文件会忽略未知键——没有形状层面的不兼容"。方案的 v2 把 `workspaces[ws]` 从 `list[str]` 改成 `list[dict]` 并 bump 到 2 → 旧 `load()`（[grants.py:58-61](file:///workspace/ai-coding/ai_coding/security/grants.py#L58-L61)）对 dict 元素 `isinstance(key, str)` 为假 → 返回 `set()` → **降级后所有 standing grant 静默消失** | 改用加可选键的形状（保留 `workspaces[ws]: list[str]`，另加 `grantsMeta`），或显式写明"只保证 v1→v2，**不保证 v2→v1**" |
| P1-4 | **TTL 在 gate 里无法落地**：方案说"`load()` 过滤过期" + "`PermissionGate.grants` 只返回未过期"，但 gate 的 `_grants` 是 `set[str]`（[permission_gate.py:405](file:///workspace/ai-coding/ai_coding/security/permission_gate.py#L405)），`_grant_applies`（[:891-909](file:///workspace/ai-coding/ai_coding/security/permission_gate.py#L891-L909)）逐字符串比较，**没有任何 grantedAt/ttl 信息** | 给出数据通路：要么 gate 持有记录（`dict[str, GrantRecord]`）并改 `_grant_applies`/`grants`，要么每次 `check` 回查 store（热路径文件 I/O，不可接受）。§C.1/§C.2 必须写死是哪一种 |
| P1-5 | **撤销键空间的示例与真实键格式不符**：§C.3 说"传全键（`read@build`）"、"传基名（`read`）"。但真实存储键是 `tool:path@posture`（`grant_key` 对 read 返回 `read:<dir>`，[:377](file:///workspace/ai-coding/ai_coding/security/permission_gate.py#L377)），`_split_grant` 的 base 是 `read:src` 而非 `read`。若"基名"指工具名 `read`，则 `revoke read` 会撤销**所有路径**的 read 授权（远宽于预期），且与 `_split_grant` 语义不一致 | 定义"基名" = `_split_grant(key)[0]`（即 `read:src`），并把示例改成真实形状 |
| P1-6 | **`KNOWN_MISS` 的机制归因写错（结论对、证据错）**：§B.1 说 `x="rm -rf /"; $x` 的漏报是因为"文本回退本来拦得住（`_dangerous_recursive_root_delete` 会看到 `rm`/`-rf`/`/`），只是根本没被调用"。实测 `_command_words` 返回 `['x=', 'rm', '-rf', '/', '$x']`，`_dangerous_recursive_root_delete` 因 `words[0]=='x='` 返回 **False**。真正拦得住它的是回退层里的**子串表**（[permission_gate.py:243-245](file:///workspace/ai-coding/ai_coding/security/permission_gate.py#L243-L245)）的 `"rm -rf /" in normalized` | 把归因改成"子串表能拦、只是被 `_grammar_reads` 短路掉了"；这会影响"修它要决定什么"的判断 |

**6 个 P2**：

| # | 问题 | 修法 |
|---|---|---|
| P2-1 | **FN 的定义与实现字面冲突**：§B.1 写"FN = '该拦没拦'"，紧接着"只统计 `DANGEROUS` + `UNPARSEABLE`"——而 `KNOWN_MISS` 同样是"该拦没拦"却被排除。定义应改为"**语料内该拦未拦（限 `DANGEROUS`/`UNPARSEABLE` 两类）**" | 改一句定义，避免读者把 FN 读成"全部漏报" |
| P2-2 | **五类计数按"语料标签"而非"判定输出"**：§B.2/U10 说 `known_miss`/`known_fp` "仅统计上报"。若计数来自语料标签，将来真修好 `KNOWN_MISS` 缺口后计数仍 > 0，指标失真 | 让计数由判定输出推导：`KNOWN_MISS` 断言 `== ""`、`KNOWN_FP` 断言 `!= ""`，"缺口被修好"体现为计数下降 |
| P2-3 | **合并"双读法"的迁移清单不完整**：§B.3 只列了 `_normalize_command` / `_command_words` / `_BASH_HARD_DENY_PATTERNS` 迁入 `bash_redlines.py`，但回退层还包括 `_dangerous_recursive_root_delete`、`_world_writable_chmod`、`_reads_credential`、`_SHELL_SPLIT`、`_HOME_PREFIX` | 补齐清单；漏一个，`bash_red_line_with_fallback` 就不是"今天 `bash_denied` 的完整逻辑"，U9 会假绿 |
| P2-4 | **`available()` 探针与真实调用不一致**：探针是 `bwrap --ro-bind / / -- true`，真实调用还带 `--unshare-pid --die-with-parent --new-session --bind/--tmpfs/--proc/--dev`。探针过、真实调用失败（如环境禁 pid namespace）时 `available()` 仍返回 True，运行期才炸 | 探针直接复用 `wrap()` 的构造（对 `-- true` 跑一次），或至少包含 `--unshare-pid --new-session` |
| P2-5 | **`shell=True` 的失效机理描述不准确**：§A.1b/P0-1 说"留着 `shell=True` 会让 bwrap 的参数被再解析一次——隔离直接失效"。实际上 CPython 对 `shell=True` 是 `args = ["/bin/sh","-c"] + args`（**不 join**，见 `_execute_child`），列表里只有第一个元素成为命令字符串，bwrap 会以"无参数"**响亮失败**而非静默失效。规范本身（`shell=False` + `["/bin/sh","-c",cmd]`）是对的，但理由写错了；真正会"二次解析"的是"把 argv join 成一个字符串再 `shell=True`"这条路径，方案没有点名禁止 | 改写成"必须 `shell=False`：否则 Python 只把列表首元素当命令字符串，包装后的命令根本不会以预期形态执行"，并显式禁止 join 后传 `shell=True` |
| P2-6 | **读边界缺席使"沙箱"名不副实**：`--ro-bind / /` 让沙箱内进程可读 `~/.ssh/id_rsa`、`~/.aws/credentials`、`/etc/shadow`。M-D2 的凭据防线是**文本层**的（GuardFall 已证文本过滤结构上必有洞）→ 文本过滤一旦被绕过，OS 沙箱对**机密性**零防护。现在这条只在 §七/§7.1 隐含 | 在 §A 开头写一句显式声明"本轮只保证写边界、不保证读边界"，并在 M-D1 验收里加一条**反向断言**（`enforce` 下读 `~/.ssh/id_rsa` 仍成功），避免读者把"沙箱"误读为"隔离" |

**5 个 P3**：

| # | 问题 | 修法 |
|---|---|---|
| P3-1 | **读时升级写回自相矛盾**：§C.1 同时写了"读取时**升级写回**"和"（下次 save 转成 v2）"两种机制；且 `grants_path()` 是**全局共享**文件（[grants.py:34-36](file:///workspace/ai-coding/ai_coding/security/grants.py#L34-L36)），"读时写回"会让每次 `from_config` 都写盘，多 workspace 并发窗口变大 | 二选一；建议"读到 v1 视为永久、**下次 save 才升级**"，不在 `load()` 里写 |
| P3-2 | **`add()` 的 v2 去重语义未写**：`test_adding_twice_keeps_one_entry` 钉住"重复 add 只留一条"。v2 的 `add` 需按 `key` 找已有记录并刷新 `grantedAt`，而不是 append | 补一句语义 + 一条用例 |
| P3-3 | **§四 #16 的 `grantLine` 未列入改动**：`PermissionsState.grants` 变成记录后，`grantLine(grant: string)`（[permissions-dialog.ts:50](file:///workspace/ai-coding/frontend/src/util/permissions-dialog.ts#L50)）必须同步改签名，否则前端编译不过 | 改动清单补上 `grantLine` / `permissions-dialog.test.ts` 的对应断言 |
| P3-4 | **§二 R-D1 的"零命中"措辞**：`ai_coding/` 下 `seccomp` 有一处注释命中（[infra/deadline.py:11](file:///workspace/ai-coding/ai_coding/infra/deadline.py#L11)） | 改成"机制层零命中（仅一处注释提及）" |
| P3-5 | **环境约束已核实**：本机 `bwrap` 未安装、`unshare --mount` = `EPERM`、`unshare --user` 可用、landlock `ENOSYS` —— 与 §2.5 一致，CI 装 bubblewrap 的对策成立 | 无需改 |

#### 8.2.1 本轮 findings 的处置（全部已修）

| 级别 | 处置 |
|---|---|
| **P0-1** | 采纳评审建议（**调用方 wrap**，而非 runner wrap —— `run_process` 是通用 infra，不该耦合安全概念）。收口方式为**共享 helper `spawn_argv`**（唯一 wrap 入口，返回 `list[str]` 从类型上排除 join）；补 **U1b**（经 `BashTool.execute`）与 **U1c**（经 `JobRegistry.start`）两条端到端用例 |
| **P0-2** | 改为 **golden 语料**：每条带**冻结 verdict**；`unparseable` 的 verdict 是**空**（走审批门 WARN，与 [test_bash_red_lines.py:163-173](file:///workspace/ai-coding/tests/test_bash_red_lines.py#L163-L173) 一致）；硬断言是"逐条与冻结值一致"。**"读不懂且无红线"是第三种状态（保持 WARN），既不进 `unparseable` 也不进 `safe`** —— 已写进 §B.1 |
| **P1-1** | §四 #5 改为**复用 `Degraded`**，删掉"新造 `SandboxDegraded`" |
| **P1-2** | §A.3 写明 `JobRegistry.start` 的接线方式 + **把 `_bounded(command)` 折进 `-c` 字符串**（否则 `ulimit -f` 日志上限静默失效）+ 补 U1c |
| **P1-3** | 改为**形状不变 + `meta` 可选键 + 不 bump 版本**（合项目迁移约定）；T1 从"必须改"变"**必须仍绿**" |
| **P1-4** | §C.2 给出数据通路：**`_grant_expiry: dict[str, float]` 并行映射** + `_grant_applies` 先查过期 |
| **P1-5** | "基名"定义为 **`_split_grant(key)[0]`**（如 `read:src`），示例改成真实形状 `read:src/foo.py@build` |
| **P1-6** | 归因改为"**子串表**能拦、只是被 `_grammar_reads` 短路掉了"（实测 `_dangerous_recursive_root_delete` 返回 `False`） |
| **P2-1** | FN 定义改为"**语料内该拦未拦（限 `dangerous`/`unparseable` 两类）**" |
| **P2-2** | 计数**按判定输出推导**，不按标签 —— 缺口修好后计数会降到 0 |
| **P2-3** | 补**回退层迁入完整清单**（§四的表） |
| **P2-4** | 探针**复用 `wrap()` 的构造**（对 `-- true` 跑一次完整参数），不只查 `which` |
| **P2-5** | 机理改写：列表参数**不会** join，bwrap 会**响亮失败**；真正会二次解析的是"先 join 再 `shell=True`"，**明确禁止** |
| **P2-6** | §A 开头显式声明"只保证写边界"；补 **U2b 反向断言**（`enforce` 下读工作区外仍成功） |
| **P3-1** | 二选一：**读到旧形状视为永久，下次 save 才升级**，不在 `load()` 里写 |
| **P3-2** | 补 `add()` 语义：同键重复 → **刷新 `meta` 时间戳**（延长 TTL），不 append |
| **P3-3** | §四 #15 补 **`grantLine` 签名同步** |
| **P3-4** | §二 措辞改为"**机制层零命中**（仅一处注释提及 `seccomp`）" |
| **P3-5** | 无需改（环境约束已核实，与 §2.5 一致） |

### 8.3 实现评审（子智能体）

> **一次与本轮无关的 CI 偶发**：`40306229455`（纯文档提交）在 3.13 job 红在
> `tests/test_worktree.py::test_can_isolate_is_false_when_git_times_out` ——
> `assert await WorktreeManager(git_repo, timeout=0.001).can_isolate() is False`。
> 这是 **1ms 超时的时序敏感用例**：CI 机器上 git 若在 1ms 内完成，`can_isolate()` 返回 `True` → 断言失败。
> `ai_coding/core/worktree.py` 最后改动是 `d1da11a`（UI 重构），**与领域 D 无关**；
> 本地连跑 5/5 通过。**同一提交的 3.10 job 绿、前一次全量 CI（`40305144103`）双 job 绿** —— 判定为偶发。
> 建议纳入 N-2 的 flaky 收口（`timeout=0.001` 这类"不真实的小超时"用例对机器速度敏感）。

**0 P0 · 1 P1 · 4 P2 · 5 P3（全部已修，见 §8.3.1）**

**已实测验证**（评审逐条复核）：

| 断言 | 方式 | 结果 |
|---|---|---|
| `spawn_argv` fail-closed 的所有可达路径 | 直接调用 | ✅ enforce+backend None→`sandbox.backend.missing`；enforce+不可用→`bwrap.unavailable`；warn+不可用→放行+`emit`；off/bypass/None→原样 |
| 写边界真的隔离 | userns 变体实跑 bwrap | ✅ 写 `$HOME/md1-outside.txt`→`Read-only file system`；写工作区内→成功 |
| `--tmpfs` 必须在 `--bind` 之前 | 两种顺序实跑 | ✅ 原顺序→`Directory nonexistent`；实现顺序→成功（**实现相对方案是一处正确的缺陷修复**） |
| job 的 pid/kill 语义 | 实跑 `killpg(bwrap_pgid)` | ✅ `--die-with-parent` 保证父死子亡 |
| 双读法合并行为保持 | `git show` 逐行 diff | ✅ 迁出的回退层与旧 `bash_denied` 逐字节等价 |
| 基准 | 脚本 | ✅ 135/135 golden、FN=0、FP=0、known_miss=16、known_fp=9 |
| grants meta 向后兼容 | T1 | ✅ `version==1` 且 `workspaces[ws]==["edit:a.py"]` |
| TTL 数据通路 | U13 | ✅ 磁盘加载的带 TTL 授权在会话内过期 |
| **CI（真内核）** | `gh run view` | ✅ **仅 6 个 SKIPPED（全 MCP 门控）→ 沙箱真实子进程用例在 CI 里真的跑了并通过** |

#### 8.3.1 实现评审的处置（全部已修）

| 级别 | 问题 | 修法 |
|---|---|---|
| **P1-1** | **子智能体栈未接沙箱** —— `build_subagent_runner` 未透传 policy/backend/roots，子智能体的 bash **完全不受 bwrap 约束**（模型可经 `subAgent` 委托写工作区外）。**若威胁模型含模型主动委托，此项实质是 P0** | 透传进 `build_tools`，**roots 取子 worktree**（避免子智能体写到父树）；补接线用例 + 端到端用例 |
| P2-1 | `path_allowed` 不查 `_grant_expiry` —— 过期授权在**沙箱层**仍生效 | 改为遍历 `self.grants`（已按过期过滤）+ 用例 |
| P2-2 | `spawn_argv` 的 `or argv` 是潜在 fail-open；`warn`+`backend=None` 静默 | enforce 下 `wrap()` 返回 `None` → `raise SandboxGap("sandbox.wrap.no-op")`；warn+None 也走 `emit` |
| P2-3 | `config.example.yaml` 出厂 `mode: warn` 抵消了 `enforce` 默认 | 改 `enforce` + 注释说明无 userns 时如何降级 |
| P2-4 | **U9 两侧是同一个函数，断言恒真** | 新增 `redline_legacy_snapshot.py`（135 条旧 `bash_denied` 冻结快照，独立于被测代码）→ 真实等价断言 |
| P3 | job-kill 未落成自动化用例；`revokeGrantCommand` 未接进面板 | 补 `test_a_sandboxed_background_job_can_be_killed`（断言内层心跳也停）；面板加 ↑/↓ 选、`x` 撤销 |

#### 8.2.2 第 3 轮评审的处置（全部已修）

**1 个 P0**：

| # | 问题 | 修法 |
|---|---|---|
| P0-1 | `unparseable` 按**单一 verdict** 冻结是错的 —— 实测 `echo "rm -rf /` → 非空（DENY），`printf done &&` / `cat >` → 空（WARN），**两条都是"读不懂"**。且"第三种状态"在六类里无处安放 | **拆成 `unparseable_deny`（读不懂且含红线 → 非空，计入 FN）+ `unparseable_warn`（读不懂且无红线 → 空，不计 FN）**；FN 口径同步改为 `dangerous` + `unparseable_deny` |

**5 个 P1**：

| # | 问题 | 修法 |
|---|---|---|
| P1-1 | `SandboxGap` **没有捕获点** → 冒到 `dispatch.py:662` 的 `except Exception` → `error="tool_error"`，**不是**承诺的 `error="denied"` | 写死：`BashTool.execute` / `JobRegistry.start` 各自 `except SandboxGap` → `denied` + `data.reason`；**U3/U5 断言 `error == "denied"`** |
| P1-2 | `enforce` + `backend is None` **静默不隔离**（与 fail-closed 冲突）；且既有 66 处构造不能红 | `spawn_argv` 里 `enforce` + `backend is None` → `raise SandboxGap("sandbox.backend.missing")`；**`policy=None` 是唯一的"不改变行为"入口**（工具构造默认 `None`＝`off`，**组合根**才传 `enforce`） |
| P1-3 | TTL 数据通路**只覆盖会话内新授权** —— 磁盘加载的授权在 `_grant_expiry` 里没有条目 → **持久化授权的 TTL 永不生效** | `GrantStore.load_with_expiry() -> tuple[set[str], dict[str, float]]`；`from_config` 用它**同时 seed**；U12/U13 覆盖"磁盘加载的带 TTL 授权在会话内过期" |
| P1-4 | `_reads_credential` **同名不同签名**（view 版 vs words 版）→ 按原名迁入会**打挂整条语法层** | 回退版改名 **`_reads_credential_words`**；view 版复用既有；`_HOME_PREFIX` 复用既有；补 `command_words`/`parses_cleanly` 的 import |
| P1-5 | "`known_miss` 计数会降到 0" 与 "verdict 变了就红" **冲突** | 写明 **re-freeze 流程**：修好缺口时①verdict 改非空 ②标签移到 `dangerous` —— 这是**受评审的语义变更**，golden 强制它显式发生 |

**5 个 P2 + 6 个 P3（全部已修）**：`warn` 档的发事件出口（→ `emit` 形参）、jobs 路径**只 wrap 一次**
（→ 写死 `BashTool` 对 job 不 wrap）、jobs 的 pid/kill 语义（→ `Job.pid` 报 bwrap pid + `--die-with-parent`，
U1c 验证；不成立则 `--no-pidns`）、`load()` 只读 / `prune()` 写盘（→ 统一）、`builder.py` 漏进清单、
`GrantStore.clear()` 同步删 `meta`、`meta` 与 `workspaces[ws]` 的键一致性、**U9 快照落点**
（→ 冻结快照就是**语料文件本身**，U9 断言"合并后的实现 == 语料冻结值"，与 U8 不重复）、
`config.example.yaml` 给 `warn` 示例。

> **评审已核实为"对"的点**（不改）：grants `meta` 可选键 + 不 bump 版本（T1 仍绿）、
> 回退层符号完整且只被 `bash_denied` 使用、`shell=False` + `["/bin/sh","-c",cmd]` 语义等价、
> `known_miss` 的归因（子串表能拦、被 `_grammar_reads` 短路）、`known_fp`/`safe` 的边界。

---

## 九、调研来源

> 评审 P3-5：调研断言必须可复核，故附来源清单。

**OS 级沙箱**

* A Field Guide to AI Agent Sandboxes — Process Sandboxes: Namespaces, seccomp, Landlock, Seatbelt（双墙共识）
* Microsoft Learn — Application isolation（AppContainer / ACP 学习模式）
* nvx — Isolation enforcement matrix（fail-closed；macOS DEGRADED）
* agentkeeper — Platform support（`seatbelt.broad-system-read` 稳定原因码）
* Pillar Security — The Week of Sandbox Escapes（四类失效模式）
* CVE-2026-47128 — nono Landlock/seccomp AF_UNIX 沙箱逃逸（阿里云漏洞库根因分析）
* Anthropic — Claude Code Sandboxing（双墙 / 非沙箱重试逃生舱 / 严格模式）
* BlastShield Whitepaper — sandbox-exec 弃用

**危险命令检测基准**

* CARE（arXiv:2607.21642）— 执行前命令验证，F1/DR/FPR
* SABER（EMNLP 2026）— 有状态工作区的最终环境状态
* QuoteBench（arXiv:2608.13547）— 生成契约 vs 执行传输
* AgentJailbreak（arXiv:2608.05223）、AdvCLI（arXiv:2510.06607）、INFUSE（arXiv:2610.20776）、
  IssueTrojanBench（arXiv:2607.20759）、APEX-Harm（arXiv:2610.13623）、aegish
* GuardFall and the Shell Injection Illusion — 文本过滤为何在结构上失败

**最小授权**

* OWASP LLM06 Excessive Agency（功能/权限/自主性三重过度）
* OWASP Agentic Skills Top 10 — AST03 Over-Privileged Skills
* NVIDIA NemoClaw — Security Posture（deny-by-default / posture profiles）
* Codex CLI — least-privilege tool scoping（最具体路径优先）
* Aethelgard（arXiv:2604.11839）— 学习型能力治理；~15× 过度供给实测
