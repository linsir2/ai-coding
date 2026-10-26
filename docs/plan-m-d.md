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
| `landlock\|bwrap\|seatbelt\|seccomp\|unshare` 在 `ai_coding/` **零命中** | ✅ |
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
读边界（`~/.ssh` 等）与网络墙**不在本轮** —— roadmap 的验收判据原文就是"沙箱内进程**写不到**工作区外"；读白名单是平台相关的，且没有网络墙时读泄漏的后果有限（下一轮做网络墙时一起做）。

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

**规范**：调用方在交给 runner **之前**把命令规范化，并**强制 `shell=False`**：

```python
argv = ["/bin/sh", "-c", command]        # 唯一入口，集中在一处
argv = backend.wrap(argv, roots) or argv  # bwrap 变成 argv 前缀
run_process(argv, shell=False, ...)
```

* `wrap()` 因此只需处理 **argv**，不碰 shell 语义 —— 协议干净。
* `shell=False` 是**必须的**：留着 `shell=True` 会让 bwrap 的参数被再解析一次。
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
* **探测与缓存（评审 P3-2）**：`shutil.which("bwrap")` + 真跑一次 `bwrap --ro-bind / / -- true`
  （能跑通才算可用；只查 `which` 会被"装了但 userns 被禁"骗过）。失败原因码 `bwrap.unavailable`。
  **结果缓存**（`lru_cache` 或组合根探测一次）—— 否则每个 bash 调用都会 spawn 一次探针。

#### A.3 接线

| 位置 | 改动 |
|---|---|
| `security/process_sandbox.py`（新） | `SandboxGap` / `ProcessSandboxPolicy` / `SandboxBackend` / `BwrapBackend` |
| `infra/subprocess_runner.py` | `run_process(..., sandbox: SandboxBackend \| None = None, roots=())` → `argv = sandbox.wrap(argv, roots)` |
| `tools/bash_tool.py` | 按策略取 backend → **规范化 argv + `shell=False`** → `wrap` → 传入；`enforce` 且不可用 → `denied` |
| `core/jobs.py` | 同路；**ledger 记包装前的 `command`**（评审 P1-4） |
| `config/models.py` + `config.example.yaml` | `process_sandbox`（`mode` / `allowBypass` / `writablePaths`）；`security.grantTtlSeconds` |
| `cli.py` / `server/world.py` | 构造 policy + backend；`JobRegistry` 在 [world.py:162](file:///workspace/ai-coding/ai_coding/server/world.py#L162)、`BashTool` 在 [builder.py:200](file:///workspace/ai-coding/ai_coding/tools/builder.py#L200)，backend 由二者共享的 composition root 注入 |
| `.github/workflows/ci.yml` | `apt-get install -y bubblewrap`（否则真实子进程用例无法运行） |

**不覆盖 MCP 子进程**（长驻、需要 stdio/网络，隔离它需要整条传输改造）—— 写入 §七。

#### A.4 fail-closed 与上报

* `enforce` + 不可用 → **拒绝执行**，`ToolResult(error="denied", data.reason="bwrap.unavailable")`。
* `warn` + 不可用 → 继续，但**发一条 `Degraded` 事件**（`component="process_sandbox"`，`reason=<原因码>`）。
  * **复用既有的 `Degraded`**（[events.py:261](file:///workspace/ai-coding/ai_coding/core/events.py#L261)），不新造事件类型 ——
    否则要牵动前端渲染/回放/golden（评审 P2-2）。
* **不静默**：两种情况都不装作"已隔离"。这是 agentkeeper 的原则，也是调研里反复出现的教训
  （"工具宁可显式上报 `DEGRADED`，也不给假绿色"）。

---

### B. M-D2 · 危险命令判定基准（回归检测 + 进 CI）

**定位**（用户拍板）：基准是**回归检测** —— 改动**不得引入新的漏报**（FN 在语料内必须为 0），FP 有界并记入指标表。
**诚实框定**："证明无漏报"**不可达** —— GuardFall 已证明文本过滤在结构上必有洞。基准的价值是**改动前后可比**，不是"绝对安全"。

#### B.1 语料（新，`security/redline_corpus.py`）

**四类**，判据统一用 `bash_red_line_with_fallback()`（合并后的单一判据，见 B.3）：

| 类 | 语义 | 判据 | 失败后果 |
|---|---|---|---|
| `DANGEROUS` | 字面危险命令 | `!= ""` | **硬失败**（FN） |
| `UNPARSEABLE` | 语法**读不懂**（引号/转义/截断） | `!= ""`（保守回退必须拦） | **硬失败**（FN） |
| `SAFE` | 规则本就不该拦 | `== ""` | **硬失败**（FP） |
| `KNOWN_MISS` | 语法**可读**但语义危险（变量展开/命令替换） | 仅**统计上报** | 不失败，进指标表 |
| `KNOWN_FP` | 业务上安全但规则会拦（`chmod 777 ./build`） | 仅**统计上报** | 不失败，进指标表 |

**FN/FP 的口径（评审 P0-2 / P0-3）**：

* **FN = "该拦没拦"，只统计 `DANGEROUS` + `UNPARSEABLE`，硬阈值 0。**
* **FP = "不该拦却拦了"，只统计 `SAFE`，硬阈值 0。**
* `KNOWN_MISS` / `KNOWN_FP` **不进硬阈值**，但**必须出现在指标表里** ——
  基准的价值是**回归检测 + 可见性**，不是"证明无漏报"。

**为什么单设 `KNOWN_MISS`（这是本次调研最重要的发现之一）**：

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
`_grammar_reads` 为真 → 返回 `""`。而文本回退**本来拦得住**（`_dangerous_recursive_root_delete`
会看到 `rm` / `-rf` / `/`），只是**根本没被调用**。

* 这正是 GuardFall 的机理（"过滤器检查的字符串不是内核执行的 argv"）在本仓库的**具体落点**。
* **本轮不修**（修它要决定"可读但无命中时是否回退"，会引入新的 FP，是独立的语义变更）。
* **但基准必须把它显式列出来** —— 否则这个缺口会一直隐形。指标表里的 `known_miss` 计数
  就是这个缺口的**可见性**。

语料来源：
* 现有 `test_bash_red_lines.py` / `test_permission_gate.py` 的用例（升级为语料）；
* roadmap 点名的：`rm -rf /`、`sudo`、`dd if=`、`> /dev/sd`、强制 push；负例 `rm -rf ./build`、引号里的 `sudo`；
* **GuardFall 的混淆样本** → 按上表**分流**到 `UNPARSEABLE`（读不懂的）或 `KNOWN_MISS`（读得懂但放行的）。

#### B.2 基准脚本（新，`scripts/redline_benchmark.py`）

* 读语料 → 逐条跑 `bash_red_line_with_fallback` → 统计：
  * **FN**（`DANGEROUS` + `UNPARSEABLE` 里该拦没拦）—— **必须 0**，非 0 直接 exit 非零
  * **FP**（`SAFE` 里不该拦却拦了）—— **必须 0**，非 0 直接 exit 非零
  * **`known_miss` / `known_fp` 计数** —— 不失败，但**必须 > 0 时显眼**（否则缺口隐形）
  * 按规则分类的命中计数（供人看"哪条规则在拦什么"）
* 输出 Markdown 指标表（便于贴进 PR / `data-contracts.md`）。
* **进 CI**（`ci.yml` 加一步 `python scripts/redline_benchmark.py`）。
* **语料规模断言**（评审 P2-6）：`DANGEROUS` ≥ 20、`UNPARSEABLE` ≥ 10、`SAFE` ≥ 20、
  `KNOWN_MISS` ≥ 5 —— 防止语料被悄悄删空后基准"假绿"。照抄
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

#### C.1 契约升级（`grants.json` v2）

```json
{
  "version": 2,
  "workspaces": {
    "/abs/ws": [
      {"key": "read@build", "grantedAt": 1761465600.0, "ttl": 86400}
    ]
  }
}
```

* **向后兼容 v1**：`["key", ...]` → 视为 `ttl=None`（永久）。读取时**升级写回**（下次 save 转成 v2）。
* **TTL 可配**：`security.grantTtlSeconds`（默认 `86400` = 24h）。`None` = 永久（显式 always）。
* **时间格式统一（评审 P1-7）**：`grantedAt` 用 **epoch 浮点**（`time.time()`），**不用** ISO 字符串。
  * 写入、比较、序列化**全部**用 epoch —— 混用两种格式是漂移之源。
  * 注入的 clock **返回 epoch**（默认 `time.time`）。项目已有的
    [infra/time_utils.now_utc](file:///workspace/ai-coding/ai_coding/core/jobs.py#L55) 返回的是
    `datetime`，**不复用**（会让 `GrantStore` 依赖 datetime 且引入两种格式）；
    `GrantStore(clock=time.time)` 保持它的"无时钟、纯"性质。

#### C.2 到期

* `GrantStore.load()` 过滤过期的（`grantedAt + ttl < now`）。
* `PermissionGate.grants` 只返回**未过期**的（面板看到的是"当前有效"）。
* **prune 会写盘**（评审 P2-7）：`grants.json` 的读写**无锁**（今天也如此，best-effort 语义），
  v2 之后每次 load 都可能写 → 窗口变大。**记一句**：仍保持 best-effort（写失败不影响内存态），
  不引入锁（与项目既有语义一致）；多写者场景下最坏情况是"某次 prune 丢失"，可接受。

#### C.3 逐条撤销

* `PermissionGate.revoke(key)` —— 内存 + 磁盘。
* **键空间（评审 P1-8）**：存储键是 `key@posture`（[permission_gate.py:991](file:///workspace/ai-coding/ai_coding/security/permission_gate.py#L991)），
  而 `/permissions revoke <key>` 由**用户**输入。规则：
  * 传**全键**（`read@build`）→ 精确撤销该条；
  * 传**基名**（`read`）→ 撤销该基名的**全部 posture 变体**（用户视角的"把这个权限收回来"）。
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
| 1 | `security/process_sandbox.py`（新） | `SandboxGap` / `ProcessSandboxPolicy` / `SandboxBackend` / `BwrapBackend` | M-D1 的策略/机制分离 |
| 2 | `infra/subprocess_runner.py` | `run_process(..., sandbox=None, roots=())` → `argv = sandbox.wrap(argv, roots)` | M-D1 的机制落点 |
| 3 | `tools/bash_tool.py` / `core/jobs.py` | 按策略取 backend 并包装；`enforce` 且不可用 → `denied` | 接线 |
| 4 | `config/models.py` | `process_sandbox`（mode / allowBypass） | 配置 |
| 5 | `cli.py` / `server/world.py` | 构造 policy + backend | 组合根 |
| 6 | `core/events.py` | `SandboxDegraded` + 进 `LoopEvent` | M-D1 的上报 |
| 7 | `security/bash_redlines.py` | 迁入回退层 + 新增 `bash_red_line_with_fallback` | M-D2 合并双读法 |
| 8 | `security/permission_gate.py` | `bash_denied` 改为委托（**删掉自己的规则**） | M-D2 单一判据 |
| 9 | `security/redline_corpus.py`（新） | 三类语料（DANGEROUS / OPAQUE / SAFE） | M-D2 |
| 10 | `scripts/redline_benchmark.py`（新） | 基准脚本（FN=0 / FP 有界 / 分类计数） | M-D2 |
| 11 | `.github/workflows/ci.yml` | 加基准步骤 + `apt-get install -y bubblewrap` | M-D2 进 CI + M-D1 可跑 |
| 12 | `security/grants.py` | v2 格式 + TTL + prune + `revoke(key)` + 时钟注入 | M-D3 |
| 13 | `security/permission_gate.py` | `revoke(key)` / 过滤过期 / 暴露 `_once_grants` | M-D3 |
| 14 | `server/handlers.py` | `permissions.revoke` + 面板显示 once/过期 | M-D3 |
| 15 | `app/commands.py` | `/permissions revoke <key>` | M-D3 |
| 16 | `frontend/src/util/permissions-dialog.ts` + `component/dialog-permissions.tsx` | `PermissionsState.grants` 从 `string[]` 变为带过期时间的记录；渲染 once grants | M-D3（评审 P1-3） |
| 17 | `config.example.yaml` | 新配置面的示例 | 评审 P2-5 |
| 18 | `docs/data-contracts.md` | 契约同步 | |

**测试条目（评审 P0-4 —— 原稿漏了）**：

| # | 文件 | 改动 |
|---|---|---|
| T1 | `tests/test_grant_store.py` | **必须改**：[L126-134](file:///workspace/ai-coding/tests/test_grant_store.py#L126-L134) 断言 `version == 1` 且 `workspaces[ws] == ["edit:a.py"]`；[L54-58](file:///workspace/ai-coding/tests/test_grant_store.py#L54-L58) 断言 `load() == {"edit:src/foo.py"}` —— v2 后必红 |
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
  新增的只有语料、基准脚本、以及 `KNOWN_MISS` 的**上报**（不改变任何判定结果）。
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
| U2 | `test_process_sandbox.py::test_a_process_under_enforce_can_write_inside` | 同进程写工作区内 → 成功（今天：成功 → **本条是 Prove**） |
| U3 | `test_process_sandbox.py::test_enforce_refuses_when_the_mechanism_is_missing` | backend 报不可用 → `bash` 返回 `denied` + `reason`（今天：无机制，会执行） |
| U4 | `test_process_sandbox.py::test_warn_degrades_and_reports` | `warn` + 不可用 → 执行 + **`Degraded`** 事件（`component="process_sandbox"`）（今天：无事件） |
| U5 | `test_process_sandbox.py::test_the_gap_has_a_stable_reason_code` | `SandboxGap.reason == "bwrap.unavailable"`（今天：无） |
| U6 | `test_process_sandbox.py::test_bypass_skips_the_mechanism` | `bypass=True` → 不包装（今天：无） |
| U7 | `test_process_sandbox.py::test_wrap_puts_the_writable_bind_after_the_readonly_root` | `wrap()` 的 argv 里 `--bind <ws>` 在 `--ro-bind / /` **之后**（顺序错则工作区仍只读） |
| U8 | `test_redline_benchmark.py::test_the_corpus_has_no_missed_dangerous_command` | 基准跑语料 → `DANGEROUS`+`UNPARSEABLE` 的 FN == 0（今天：无基准 → 用例无法运行/断言"基准存在"失败） |
| U9 | `test_redline_benchmark.py::test_the_two_readers_agree` | `bash_red_line_with_fallback` 与**旧 `bash_denied` 的冻结快照**在语料 + 现有测试集上**逐条等价**（合并是行为保持） |
| U10 | `test_redline_benchmark.py::test_known_miss_is_surfaced` | `x="rm -rf /"; $x` 被计入 `known_miss` 且**出现在指标表里**（今天：无上报 → 缺口隐形） |
| U11 | `test_redline_benchmark.py::test_the_corpus_is_not_emptied` | 语料规模 ≥ 阈值（DANGEROUS 20 / UNPARSEABLE 10 / SAFE 20 / KNOWN_MISS 5） |
| U12 | `test_grant_store.py::test_an_expired_grant_is_not_loaded` | 写入带 TTL 的授权 → 过期后 `load()` 不含它（今天：无 TTL） |
| U13 | `test_grant_store.py::test_v1_grants_still_load_and_are_permanent` | v1 格式仍可读、视为永久（今天：v1 可读但无"永久"语义） |
| U14 | `test_permission_gate.py::test_a_single_grant_can_be_revoked` | `revoke(key)` 只删一条；传基名撤销全部 posture 变体（今天：只有 `revoke_all`） |
| U15 | `test_permission_gate.py::test_the_panel_shows_once_grants_and_expiry` | `permissions_state` 含 once grants + 过期时间（今天：无） |

> U2 是 **Prove**（断言"工作区内可写"，实现前后都成立）。
> **U1/U2 需要真 `bwrap`**：`@pytest.mark.skipif(not BwrapBackend().available(), reason="bwrap unavailable")`
> —— **显式理由，不是静默通过**；本地会跳过，CI（装了 bubblewrap）会跑。
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
* **M-D2**：基准脚本产出 FN=0 / FP 有界 + 分类计数；**进 CI**；**双读法合并为单一判据**，
  且基准用**冻结快照**证明合并是行为保持的。
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

_待填。_

### 8.3 实现评审（子智能体）

_待填。_

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
