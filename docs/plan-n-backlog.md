# 待立项缺陷 + 安全残余清理（N-1 / N-2 / N-6 / known_miss / CIDR / 运行期根）

> 调研：一轮仓库侦察（子智能体）+ 我逐条实测复核 + 联网（Codex exec-policy / E2B / Sandlock / seccomp 内核文档）。
> **本轮的每一条结论都有实测支撑**，凡推测都显式标注。
>
> **前情**：roadmap §十三 列了 N-1/N-2/N-6；`plan-m-d2.md` §7.1 列了安全残余。
> 本方案把两者合并成一轮可独立交付的清理。

---

## 一、调研结论

### 1.1 联网调研（三条决定性发现）

**① Codex exec-policy 对"可解析但规则未命中"的权威表述** —— 这正是 `known_miss` 的机理：

> compound `bash -lc` scripts are only split into rule-checkable subcommands for linear
> `&&`/`||`/`;`/`|` chains of **plain words** —— **redirections, command substitution,
> env-var assignments, wildcards, or control flow disable splitting entirely**, and the
> rule matches the whole blob or nothing.
>
> —— [Codex CLI Permissions & Control Model](https://agenticcontrolplane.com/controls/codex-cli)

**这不是"我们规则写得不够多"，而是这一类文本判定的结构性边界。** 对照本仓库 16 条 `known_miss`：
`x="rm -rf /"; $x`、`D=/data; rm -rf $D`、`rm -rf $HOME`、`cat ~/.ssh/id_$USER`、`x=1; $x rm -rf /`
**逐条落在 Codex 声明的 "disable splitting" 清单里**（env-var assignment / command substitution）。
GuardFall 的结论（文本过滤结构性失败）与之一致。

Codex 的应对**不是加正则**，而是两条：
- 诚实声明边界（上面那段引文就是它的官方 caveat）；
- `approval_policy = "untrusted"` = **对任何"不被已知安全"的动作询问用户**，而不是放行。

**② E2B 的出网管控语义**（[Internet access](https://e2b.dev/docs/network/internet-access)）：

* `allow_out` 支持 **CIDR + 裸 IP + 域名**（`8.8.8.8/32`、`8.8.8.8`、`*.example.com`）；
* `deny_out = [allTraffic]`（即 `0.0.0.0/0`）表示"除允许项外全拒"；
* **域名过滤只对 HTTP:80（Host 头）与 TLS:443（SNI）有效**；其他端口只走 CIDR；
* **UDP / QUIC / HTTP3 不支持域名过滤**；
* 用域名时自动放行 DNS 服务器。

**③ Sandlock**（[arXiv 2605.26298](https://arxiv.org/pdf/2605.26298v1)）—— 与本仓库同题的最新学术工作：

* 核心分工：**静态、与输入无关的策略编译进内核强制规则**（Landlock + seccomp-bpf），
  **依赖运行时的决策交给 supervisor**（seccomp user-notification + `pidfd_getfd`）；
* seccomp-bpf 负责**无条件**的系统调用过滤；运行时的 `connect` 目标仲裁走 user-notification；
* 启动开销 ~5ms；**明确反对**把 seccomp 当成完整沙箱（内核文档原话："System call filtering
  isn't a sandbox"）。

### 1.2 内核事实（seccomp）

* `SECCOMP_RET_USER_NOTIF` 把某个系统调用交给用户态处理（[内核文档](https://docs.kernel.org/userspace-api/seccomp_filter.html)）；
* **BPF 程序不能解引用指针** —— 只能直接评估系统调用参数。所以 `socket(domain, type)` 的
  **domain/type 是可过滤的**（它们是立即数），而 `connect(sockfd, sockaddr*)` 的**目标地址不可过滤**
  （是指针）→ 这就是 AF_UNIX 路径级控制"基本等于没有"的根源，也是 CVE-2026-47128 的根源。

### 1.3 一条必须纠正的文档错误

`plan-m-d2.md` §7.1 写：

> **IP/CIDR/UDP 绕过** | 域名白名单对**裸 IP/UDP** 无效（调研 F12/F13）

**实测两条都不成立**（见 §二）：

| 断言 | 实测 | 结论 |
|---|---|---|
| 裸 IP 无效 | `allow=['1.2.3.4']` → `_host_allowed('1.2.3.4',443)` = **True** | **错**：裸 IP 一直可用 |
| UDP 无效 | `unshare --user --net` 内 UDP `sendto` → `ENETREACH(101)` | **错**：`--unshare-net` 已阻断 |
| CIDR 无效 | `allow=['8.8.8.0/24']` → `_host_allowed('8.8.8.8',443)` = **False** | **对**：这是真缺口 |

**残余风险表 itself 需要修** —— 这正是本项目最在意的一类缺陷（文档与实测不一致）。

---

## 二、逐条实测复核（我亲自跑，不轻信子智能体）

### 2.1 N-1 · 快照静默失效 —— **已完整复现**

| 事实 | 证据 |
|---|---|
| `git()` 合并 stdout+stderr | [gitcmd.py:38](file:///workspace/ai-coding/ai_coding/core/gitcmd.py#L38) `return outcome.returncode or 0, (outcome.stdout + outcome.stderr).strip()` |
| `capture` 把合并串当 sha | [snapshots.py:88](file:///workspace/ai-coding/ai_coding/core/snapshots.py#L88) `sha = out.strip() if code == 0 else ""` |
| 无 sha 形状校验 | `ai_coding/` 内 grep 无任何 git sha 的 hex/长度校验 |

**复现**（以 `nobody` 跑，`.git/info/exclude` chmod 000）：

```
$ git -C /tmp/n3 stash create
exit=0
stdout=[0a9659903bdff27dc9324b0a25550c348c365b22]
stderr=[warning: unable to access '.git/info/exclude': Permission denied]  × 4
```

`gitcmd` 合并后 → `capture` 得到
`"0a9659...b22warning: unable to access '.git/info/exclude': Permission denied\n..."`
→ `update-ref` 失败 → `capture` 返回 `None` → **快照静默跳过**。

**触发条件**：仓库对执行用户可写 + 某个被 git 读取的文件不可读（权限受限 / 共享目录 / 容器）。
**注意**：root 绕过 DAC → **CI（root）不会复现**，必须以非 root 验证（同 M-A4 的 `verify_real_eacces.py` 模式）。

> 我另外试了 `.gitconfig` 不可读、`/etc/gitconfig` 不可读、`.config/git/ignore` 是目录 ——
> 这些**都是 fatal**（exit 128），会被 `code != 0` 分支挡住。**只有非致命警告才漏**，
> 而 `info/exclude` 的 permission-denied 正好是非致命的。

### 2.2 N-6 · 两条 rg 用例缺守卫 —— **已复现**

```
$ PATH=/tmp/norg pytest tests/test_m4_security.py::test_grep_include_dotenv_still_filters_credentials \
    tests/integration/test_r17_parity.py::test_both_runners_answer_the_same_question
..  [100%]        # ← rg 缺席，仍 2 passed
$ pytest （有 rg）
..  [100%]        # ← 完全相同的输出
```

| 用例 | rg 缺席时的真实行为 |
|---|---|
| [test_m4_security.py:461](file:///workspace/ai-coding/tests/test_m4_security.py#L461) | `use_ripgrep=True` **不探测安装**（[grep_tool.py:70-71](file:///workspace/ai-coding/ai_coding/tools/grep_tool.py#L70-L71) 直接 `resolved="rg"`）→ runner `FileNotFoundError` → `ToolResult(success=False)` → output 不含 `TOKEN=secret` → **断言成立但什么都没搜到** |
| [test_r17_parity.py:206](file:///workspace/ai-coding/tests/integration/test_r17_parity.py#L206) | `use_ripgrep=None` **自动探测** → rg 缺席 → `self._rg=None` → `real` 就是 fallback → **自己跟自己比**，恒真 |

对照：同文件 [:183](file:///workspace/ai-coding/tests/integration/test_r17_parity.py#L183) **有** `skipif(not shutil.which("rg"))`。

### 2.3 代理 CIDR —— **已复现**

```
allow=['1.2.3.4']      → host_allowed('1.2.3.4',443) = True    # 裸 IP 可用
allow=['8.8.8.0/24']   → host_allowed('8.8.8.8',443) = False   # CIDR 静默永不匹配
allow=['10.0.0.0/8']   → host_allowed('8.8.8.8',443) = False
allow=['*']            → False                                 # 设计如此（fail-closed）
allow=['api.example.com'] → api.example.com:443 True
                         | evil-example.com False
                         | example.com.evil.com False           # 后缀匹配正确
```

`_parse_allow_entry('8.8.8.0/24')` 把它当**主机名字符串**，永远匹配不到真实 host → **fail-closed，
但是静默的**：用户写了 CIDR，得到的是"全部拒绝"且**没有任何解释**。

### 2.4 UDP / 裸 TCP —— **已实测（纠正残余表）**

```
宿主机：      udp_send=sent,  tcp_connect=timed out
user netns：  udp_send=failed: 101 Network is unreachable
              tcp_connect=failed: 101 Network is unreachable
```

`--unshare-net` 建的新 netns 只有 `lo` → **UDP 与裸 TCP 都不可达**。
代理只是 **TCP/HTTP** 出口，但**它不是唯一出口**（AF_UNIX 是），所以 AF_UNIX 遮蔽仍然必要 ——
但**"UDP 绕过"这条残余不成立**。

### 2.5 `extra_workspace_roots` 运行期可变 —— **已核实，且发现一处真实分歧**

| 层 | 状态 | 证据 |
|---|---|---|
| **路径层** | **已支持运行期扩展** | [sandbox.py:95](file:///workspace/ai-coding/ai_coding/security/sandbox.py#L95) `resolve()` 在 containment 不中时**问 grantor**；M-D3 的 TTL/撤销已让它活起来 |
| **进程层** | **启动期快照** | [bash_tool.py:135](file:///workspace/ai-coding/ai_coding/tools/bash_tool.py#L135) `self._roots = tuple(...)`；[:440](file:///workspace/ai-coding/ai_coding/tools/bash_tool.py#L440) `spawn_argv(..., self._roots, ...)` |
| 构造点 | 快照来源 | [builder.py:236](file:///workspace/ai-coding/ai_coding/tools/builder.py#L236) `roots = tuple(sandbox_roots) if sandbox_roots else sandbox.roots` |

**真实分歧（当下就有，不是假想）**：对某个工作区外的目录 grant 之后 ——
文件工具**可以**写它（`path_allowed` 放行），bash **不能**（OS 沙箱 EROFS）。
而 [builder.py:233-235](file:///workspace/ai-coding/ai_coding/tools/builder.py#L233-L235) 的注释明写：

> the process layer's writable face is the path layer's -- the same roots, so a directory
> the file tools may write is a directory a bash command may write, **and never one or the other**.

**注释声称的不变量，在运行期被 grant 打破。** 这是"门禁说可达、工具说不可达"的分裂，
仓库注释里多次警告过的正是这种分裂。

### 2.6 `wrap()` 的 fail-open + 3 条缺守卫用例 —— **跑全量时新发现**

本地跑全量（`pytest tests/ -p no:randomly`）**3 条失败**，全部在
[test_process_sandbox.py](file:///workspace/ai-coding/tests/test_process_sandbox.py)：

| # | 用例 | 现象 |
|---|---|---|
| 1 | `test_wrap_keeps_the_isolation_switches`（:611） | `assert "--unshare-pid" not in argv` **失败** —— argv 里有 |
| 2 | `test_the_read_boundary_hides_a_directory_and_a_file`（:790） | `SandboxGap: bwrap.unavailable` |
| 3 | `test_a_writable_path_under_a_read_deny_is_still_unreadable`（:811） | `SandboxGap: bwrap.unavailable` |

**#1 是真缺陷**，根因在 [process_sandbox.py:866-870](file:///workspace/ai-coding/ai_coding/security/process_sandbox.py#L866-L870)：

```python
def wrap(self, argv, roots):
    if self._cache is None:
        self._probe()
    tier = self._tier if self._tier != "none" else "full"   # ← fail-open
    return self._build(argv, roots, tier)
```

探针失败时 `_tier == "none"`，`wrap()` 却**回落到 `"full"`** 去构造 —— 而 `tier` 属性对外报的是
`"none"`。**同一个对象，两个互相矛盾的答案。**

`SandboxBackend.wrap()` 的契约（[:320](file:///workspace/ai-coding/ai_coding/security/process_sandbox.py#L320)）
明写返回 `list[str] | None`，`None` 表示"没什么可包"。**回落 `"full"` 违反契约**：
任何一个绕过 `available()` 直接调 `wrap()` 的调用方，会拿到一个**看起来已隔离、实际会失败**的 argv。
`spawn_argv` 因为先查了 `available()` 才没踩到 —— 但契约不该依赖调用方记得查。

**#2/#3 是 N-6 同类**（缺守卫）：文件里已有 `_NEEDS_BWRAP`（[:56](file:///workspace/ai-coding/tests/test_process_sandbox.py#L56)），
这两条与 #1 都没用上。

> **为什么 CI 是绿的**：CI 装了 bubblewrap → 探针成功 → `tier != "none"` → #1 走 `if` 分支，
> #2/#3 有 bwrap 可跑。**本地（无 bwrap）才是照出缺陷的那一半** —— 与 N-5（rg）同一机理。

---

## 三、方案：逐项实现

### A. N-1 · 快照静默失效

**范围**：只让**产生 sha 的两处调用**不合并 stderr，并加一道 sha 形状校验。

#### A.1 `git()` 增加可选参数（不改默认契约）

```python
# core/gitcmd.py
async def git(
    root: Path | str,
    *args: str,
    stdin_data: str | None = None,
    timeout: float = DEFAULT_GIT_TIMEOUT,
    *,
    merge_stderr: bool = True,
) -> tuple[int, str]:
    """... ``merge_stderr=False`` for the callers that parse this stream as a
    value: git may put a *non-fatal* warning on stderr (an unreadable
    ``.git/info/exclude``, measured) and the caller would otherwise parse the
    warning as the value.  The default keeps the merged shape every existing
    caller was written against."""
```

合流点只有一处（`return`），`merge_stderr=False` 时返回 `(rc, outcome.stdout.strip())`。

#### A.2 `capture` 用干净流 + 校验形状

```python
# core/snapshots.py
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

async def capture(self, key: str) -> str | None:
    if not await self._is_repo():
        return None
    code, out = await git(self._root, "stash", "create",
                          timeout=self._timeout, merge_stderr=False)
    sha = out.strip() if code == 0 else ""
    if not _SHA_RE.match(sha):   # a clean tree's pre-turn state *is* HEAD
        code, sha = await git(self._root, "rev-parse", "HEAD",
                              timeout=self._timeout, merge_stderr=False)
        if code != 0 or not _SHA_RE.match(sha):
            return None
    ...
```

**为什么两条都要**：`merge_stderr=False` 是**根源**（不该把警告当值）；`_SHA_RE` 是**不再静默**
的保证 —— 校验不通过就走 `rev-parse HEAD` 兜底，仍然失败就**明确返回 None**（与现状一致，
但不再有"污染串被当成 sha"这种中间态）。

> **不做**：改 `git()` 的默认合并语义。那会动 `WorktreeManager` 的 6 个调用点，
> 且 docstring 明写合并是有意的。

### B. N-6 · 两条 rg 用例

**用例本身有缺陷**（静默假绿），按项目规则允许修改。

* `test_m4_security.py:461`：循环里 `use_rg=True` 那一轮，rg 缺席时 `pytest.skip(...)`；
  rg 在场时**追加一条正向断言**（`result.success is True`），让它不可能再"什么都没搜到还绿"。
* `test_r17_parity.py:206`：加 `@pytest.mark.skipif(not shutil.which("rg"), ...)`（与同文件
  [:183](file:///workspace/ai-coding/tests/integration/test_r17_parity.py#L183) 一致）；
  并在用例内断言 `GrepTool(Sandbox(root))._rg is not None`，**钉住"真跑了 rg 分支"**。

### C. `known_miss` 收敛

**分类（16 条 → 4 类）**：

| 类 | 条目 | 可否用规则修 |
|---|---|---|
| **E · 提权包装器** | `doas rm x`、`env -i sudo ls`、`timeout 5 sudo ls` | **可修**（包装器链） |
| **F · force push 变体** | `git push --force-with-lease origin main` | **可修**（`--force` 的同义 flag） |
| **A/B · 变量/波浪号展开** | `x="rm -rf /"; $x`、`D=/data; rm -rf $D`、`rm -rf ${HOME}`、`rm -rf $HOME`、`rm -rf $PWD`、`cat ~/.ssh/id_$USER`、`x=1; $x rm -rf /`、`rm -rf ~`、`rm -rf ~/` | **结构性不可解**（Codex 明示） |
| **C/D · 路径穿越/绝对路径** | `rm -rf ./..`、`rm -rf /home/user`、`rm -rf /var/log` | **不在本轮**（redline 层不知道 workspace，硬修会引入新 FP） |

#### C.1 修 E + F（4 条 → `dangerous`）

* `doas` 进包装器表；
* `env -i` / `timeout 5` 这类"带值的包装器"要能被剥掉（现在链在第一个带值词处断）；
* `--force-with-lease` 与 `--force` 同等对待。

#### C.2 剩余 12 条：**用机制层证明覆盖**（本轮核心）

**关键论点**：这 12 条的**文本层漏判，被 OS 写边界兜住**。
`--ro-bind / /` + 只有 workspace 可写 → `rm -rf $HOME`、`rm -rf ~`、`rm -rf /var/log`、
`rm -rf ./..` 在沙箱内**全部 EROFS 失败**。

**证明方式**：在 CI（装了 bubblewrap 的真内核）加一条**真实子进程**用例 ——
把这 12 条命令逐条在 `enforce` 档沙箱内执行，断言**没有一条写进了工作区外**。
这就是"双墙缺一不可"在本仓库的**可机检形式**。

#### C.3 文档写明结构性边界

在 `redline_corpus.py` 的 `known_miss` 段与 `features.md` 写明（引 Codex 的表述）：
**变量展开 / 命令替换 / 重定向 / 通配符 / 控制流会使规则无法逐条匹配 —— 这是文本判定的
结构性边界，不是规则写得不够多；兜底是审批 + OS 写/读边界，由 C.2 的用例钉住。**

#### C.3 "询问"已经是现状 —— 真问题是"要不要硬拒"

**实测契约**（`permission_gate._decide`）：bash 命令只有在 redline 命中时才 **DENY**
（[:425-431](file:///workspace/ai-coding/ai_coding/security/permission_gate.py#L425-L431)）；
未命中时落到 **WARN**（[:499-504](file:///workspace/ai-coding/ai_coding/security/permission_gate.py#L499-L504)、
[:524-527](file:///workspace/ai-coding/ai_coding/security/permission_gate.py#L524-L527)），
也就是**询问**（AutoApprove 下自动放行）。

所以：

* `known_miss` 的**当下后果** = 询问（或自动放行）**+ OS 写边界兜住**，**不是"静默放行"**。
* **"要不要加询问兜底"是个伪命题** —— 询问已经在。真问题是**要不要硬拒**。
* 硬拒这 12 条需要展开变量 / 波浪号，文本层做不到。Codex 的 "disable splitting"
  表述正是这件事的权威声明；它的 `untrusted` 档能做到"全部询问"，是因为它整条命令走
  exec-policy，而不是在"规则未命中"这个**残余**上加询问。

> **明确不做**：把 12 条硬拒掉。那需要 redline 层知道 workspace 或执行 shell 展开，
> 两条都会引入大量新 FP（`rm -rf $DIR` 是正常用法）。

### D. 代理 CIDR + 裸 IP 语义

#### D.1 CIDR 匹配（在**解析后**的地址上做）

`_host_allowed` 现在只吃 host 字符串，CIDR 在它里面永远匹配不到。改成两段：

```python
def _host_allowed(host, port, allow) -> bool:      # 域名 / 裸 IP（现状，不改）
    ...
def _cidr_allowed(ip: str, allow: Sequence[str]) -> bool:
    """CIDR entries are checked against the *resolved* address: a hostname
    cannot be compared to a network, so this runs after _resolve_public_address."""
```

调用点：`_resolve_public_address` 解析出 sockaddr 后，先查 CIDR，再（或同时）查 `_is_forbidden_ip`。
顺序上 CIDR 命中后才 `_is_forbidden_ip` —— 私网 CIDR 仍被拒（fail-closed 不变）。

#### D.2  malformed CIDR 要**响**，不能静默

`allow=['8.8.8.0/33']` 现在静默永不匹配。加配置校验（`NetworkConfig` 的 `allow` 校验处）：
条目含 `/` 但 `ip_network(..., strict=False)` 失败 → **启动即报错**，而不是运行时全部拒绝。

#### D.3 修正残余风险表

`plan-m-d2.md` §7.1 的"IP/CIDR/UDP 绕过"一行改为**实测口径**：
裸 IP 一直可用；UDP 已被 `--unshare-net` 阻断；**真缺口是 CIDR**（本轮修）。

### E. `extra_workspace_roots` 运行期可变

**目标**：让 [builder.py:233-235](file:///workspace/ai-coding/ai_coding/tools/builder.py#L233-L235)
声称的不变量在**运行期**也成立。

#### E.1 进程层的 roots 改成**活的**

`BashTool` 不再快照 `roots`，而是持有一个**每次调用时读的视图**：

```python
# security/sandbox.py（新）
class LiveRoots:
    """The writable roots, read at call time.

    The path layer's answer is live (``Sandbox.resolve`` asks the grantor), so
    the process layer must be live too -- a snapshot is exactly how a directory
    the file tools may write becomes one a bash command may not.
    """
    def __init__(self, sandbox: Sandbox, grantor: RootGrantor | None) -> None: ...
    def __iter__(self) -> Iterator[Path]: ...
```

`__iter__` = `sandbox.roots` + grantor 当前授权的目录（`PermissionGate` 新增一个
**公开**的 `authorized_roots()`，把 `path_allowed` 里已有的 key 解析逻辑提出来复用 ——
**不新写解析**）。

`spawn_argv` 的 `roots` 参数类型放宽为 `Sequence[Path] | LiveRoots`（`Iterable[Path]`），
在 `wrap()` 里 `list(roots)` 一次。

#### E.2 不变量的机检

加一条用例：grant 一个工作区外的目录后，
`set(bash 的实际 roots) == set(sandbox.roots) | set(授权目录)`，且 bash 真能写它。

> **这是本轮最大的一块**，涉及 `security/sandbox.py` / `tools/bash_tool.py` /
> `core/jobs.py` / `security/permission_gate.py` / `security/process_sandbox.py` 五个文件。
> 方案评审若认为风险过大，可整块**砍掉**（其余五项不受影响）—— 见 §六 的拍板项。

### F. `wrap()` 的 fail-open + 3 条缺守卫用例

#### F.1 `wrap()` 返回 `None`（契约诚实）

```python
# security/process_sandbox.py
def wrap(self, argv, roots):
    if self._cache is None:
        self._probe()
    if self._tier == "none":
        return None          # 契约：None = 没什么可包。回落 "full" 是 fail-open。
    return self._build(argv, roots, self._tier)
```

* `spawn_argv` 已经在前面查过 `available()`，所以生产路径行为**不变**；
* 直接调 `wrap()` 的调用方现在得到 `None`，由 `spawn_argv` 的既有分支转成
  `SandboxGap(SANDBOX_WRAP_GAP)` —— **不再有"看起来已隔离"的 argv**。

#### F.2 三条用例补守卫

* `test_wrap_keeps_the_isolation_switches`：加 `@_NEEDS_BWRAP`。
  **它测的是档位不变量，而档位不变量只在机制真能跑时才有意义** ——
  无 bwrap 时 `wrap()` 返回 `None`，断言 `flag in argv` 无从谈起。
* `test_the_read_boundary_hides_a_directory_and_a_file` / `test_a_writable_path_under_a_read_deny_is_still_unreadable`：
  加 `@_NEEDS_BWRAP`（`_spawn` 里的 `enforce` + 缺席机制必然 `SandboxGap`）。

#### F.3 一条新的契约用例（钉住 F.1）

```python
def test_wrap_returns_none_when_the_mechanism_is_absent():
    """契约：None = 没什么可包。回落 "full" 会给出一个看起来已隔离的 argv。"""
    backend = _UnavailableBackend()      # 文件里已有的测试替身
    assert backend.wrap(["/bin/true"], []) is None
```

用文件里已有的 `_UnavailableBackend`（[:76](file:///workspace/ai-coding/tests/test_process_sandbox.py#L76)），
**不引入新的测试替身**。

---

## 四、逐文件改动清单

| 文件 | 改动 | 属于 |
|---|---|---|
| `ai_coding/core/gitcmd.py` | `git()` 加 `merge_stderr: bool = True` | N-1 |
| `ai_coding/core/snapshots.py` | `capture` 用干净流 + `_SHA_RE` 校验 | N-1 |
| `scripts/verify_snapshot_stderr.py` | **新**：以非 root 真子进程复现并验证（同 `verify_real_eacces.py` 模式） | N-1 Prove |
| `tests/test_m4_security.py` | rg 守卫 + 正向断言 | N-6 |
| `tests/integration/test_r17_parity.py` | rg 守卫 + 钉住 rg 分支 | N-6 |
| `ai_coding/security/bash_redlines.py` | `doas` 包装器、带值包装器剥离、`--force-with-lease` | known_miss E/F |
| `ai_coding/security/redline_corpus.py` | 4 条 `known_miss` → `dangerous`；`MIN_KNOWN_MISS` 16→12 | known_miss |
| `scripts/redline_benchmark.py` | 计数随语料自动更新（无需改） | known_miss |
| `tests/test_process_sandbox.py` | **新**：12 条结构性缺口在 `enforce` 沙箱内全部写不出去 | known_miss C.2 |
| `ai_coding/server/sandbox_proxy.py` | `_cidr_allowed` + 解析后查 CIDR | CIDR |
| `ai_coding/config/models.py` | malformed CIDR 启动即拒 | CIDR |
| `ai_coding/security/sandbox.py` | `LiveRoots` | E |
| `ai_coding/security/permission_gate.py` | 公开 `authorized_roots()`（复用已有解析） | E |
| `ai_coding/tools/bash_tool.py` | roots 改活 | E |
| `ai_coding/core/jobs.py` | roots 改活 | E |
| `ai_coding/security/process_sandbox.py` | `spawn_argv` 接受 `Iterable[Path]` | E |
| `docs/plan-m-d2.md` | 修正 §7.1 的裸 IP / UDP 断言 | 文档 |
| `ai_coding/security/process_sandbox.py` | `wrap()` 在 `tier == "none"` 时返回 `None`（不再回落 `"full"`） | F.1 |
| `tests/test_process_sandbox.py` | 3 条补 `@_NEEDS_BWRAP`；新增 `wrap()` 契约用例 | F.2 / F.3 |
| `docs/roadmap-milestones.md` | N-1/N-2/N-6 标完成 | 文档 |

---

## 五、RIPR 用例

### Red（先写，必须变红）

| # | 用例 | 断言 |
|---|---|---|
| U1 | `capture` 收到 `(0, "warning: ...\n<sha>")` | **不得**把污染串当 sha 去 `update-ref`（monkeypatch `snapshots.git`） |
| U2 | rg 缺席 + `use_rg=True` | `test_grep_include_dotenv_still_filters_credentials` **skip**，不再静默通过 |
| U3 | rg 缺席 + parity | `test_both_runners_answer_the_same_question` **skip** |
| U4 | `doas rm x` | 判为危险 |
| U5 | `env -i sudo ls` / `timeout 5 sudo ls` | 判为危险 |
| U6 | `git push --force-with-lease origin main` | 判为危险 |
| U7 | 12 条结构性缺口在 `enforce` 沙箱内执行 | **没有一条**写进工作区外 |
| U8 | `allow=['8.8.8.0/24']` + 请求 `8.8.8.8:443` | **放行**（现在是被拒） |
| U9 | `allow=['10.0.0.0/8']` | **拒绝**（私网不变） |
| U10 | `allow=['8.8.8.0/33']` | **启动即报错**（不是静默全拒） |
| U11 | grant 工作区外目录后，bash 写该目录 | **成功**（现在是 EROFS） |
| U12 | 不变量 | `set(bash roots) == set(sandbox.roots) \| set(授权目录)` |
| U13 | `_UnavailableBackend().wrap([...], [])` | **返回 `None`**（现在是回落 `"full"` 的 argv） |
| U14 | 3 条缺守卫用例（本地无 bwrap） | **跳过**，不是失败 |

### Prove

* **U1-P**：`scripts/verify_snapshot_stderr.py` 以 `nobody` 真子进程复现 —— 改前 `capture → None`，改后返回合法 ref。
* **U7-P**：CI 真内核 + bubblewrap 实跑（本地 bwrap 缺席时按既有模式显式跳过并说明理由）。
* **突变测试**：① 去掉 `merge_stderr=False` → U1 变红；② 把 `_SHA_RE` 换回 `if not sha` → U1 变红；
  ③ 把 `_cidr_allowed` 的返回写死 `False` → U8 变红；④ 把 `LiveRoots.__iter__` 改成返回快照 → U11/U12 变红；
  ⑤ 把 `wrap()` 的 `if self._tier == "none": return None` 改回 `else "full"` → U13 变红。

### Refactor

* `PermissionGate.authorized_roots()` 与 `path_allowed()` 共用同一段 key 解析（提出私有 helper），
  两处不允许各写一份。

---

## 六、验收判据

* **N-1**：改前复现脚本 `capture → None`；改后返回 `refs/ai-coding/undo/<key>`，且 `update-ref` 的
  目标**是** 40 位 hex。`_SHA_RE` 拒绝污染串。
* **N-6**：`PATH=/tmp/norg` 下两条**跳过**（不是通过）；有 rg 时**通过**且断言 `success is True` / `_rg is not None`。
* **known_miss**：基准 `known_miss` 计数 **16 → 12**；`dangerous` 63 → 67；FN=0 / FP=0 硬阈值不变；
  **12 条在 CI 真沙箱内全部写不出去**。
* **CIDR**：`8.8.8.0/24` 放行、`10.0.0.0/8` 拒绝、malformed 启动即拒；域名匹配**无回归**
  （`evil-example.com` / `example.com.evil.com` 仍拒）。
* **E**：grant 后 bash 与文件工具**写面一致**（U12 不变量）；`ruff`/`mypy` 0。
* **F**：本地（无 bwrap）**3 条失败 → 3 条跳过**；`wrap()` 在机制缺席时返回 `None`；
  CI（有 bwrap）行为**不变**（探针成功，走不到该分支）。
* **N-2**：`test_tool_surface_warning` 与 `test_worktree` 的 flaky 收口（复现方式见 §八 评审记录）。
* 3.10 与 3.13 全量绿；真实 CI 双 job 绿；**本地无 bwrap / 无 rg 环境全量也绿**（这是本轮的
  新增验收面 —— 之前本地是 3 红）。

---

## 七、不做的事

* **不改 `git()` 的默认合并语义** —— 会动 `WorktreeManager` 的 6 个调用点，且合并是有意的。
* **不给"规则未命中"加统一询问兜底** —— 会把所有正常命令变成询问（见 §C.3）。
* **不修 C/D 类 `known_miss`**（`rm -rf ./..`、`rm -rf /home/user`、`rm -rf /var/log`）——
  redline 层不知道 workspace，硬修必引入新 FP；**由 OS 写边界兜住**（U7 证明）。
* **不加 seccomp UDP 过滤** —— `--unshare-net` 已阻断 UDP（§2.4 实测）；再加一层是重复造轮子。
* **不做 IP/CIDR 的出网"绕过"叙事** —— 残余表那条写错了，本轮是**改文档**，不是加机制。
* **不改 `Sandbox` 的路径层语义** —— 它已经是对的（§2.5）。

---

## 八、评审记录

### 8.0 本轮调研期的实测记录（N-2 的澄清）

* `tests/test_tool_surface_warning.py::test_the_warning_never_changes_the_request`
  **单独循环 30 次：0 失败**（roadmap 记的是"单独循环 40 次复现 1 次"，本轮未复现）。
* 全量 `pytest tests/ -p no:randomly` **第一次就 3 条失败** —— 但**不是 N-2**，
  而是 §2.6 的 3 条 bwrap 用例（本地无 bwrap）。**N-2 与 §2.6 是两件事**，
  本轮先收 §2.6（确定可复现），N-2 的复现方式留给评审决定。
* `tests/test_worktree.py` 的 `timeout=0.001` 用例：本地 5/5 通过，同样是时序敏感，
  与 §2.6 同类（**本地/快机才照得出来**）。

### 8.1 方案评审（子智能体，第 1 轮）

（待补）

---

## 九、调研来源

* Codex CLI 权限模型与 exec-policy 的 "disable splitting" 表述 —— https://agenticcontrolplane.com/controls/codex-cli
* Codex sandbox 模式与 Landlock/bubblewrap 现状 —— https://backgrind.com/blog/codex-cli-sandbox-modes/
* E2B 出网管控（CIDR / 裸 IP / 域名 / UDP 限制）—— https://e2b.dev/docs/network/internet-access
* Sandlock（Landlock + seccomp-bpf + user-notification 的分工）—— https://arxiv.org/pdf/2605.26298v1
* seccomp 内核文档（BPF 不能解引用指针；USER_NOTIF）—— https://docs.kernel.org/userspace-api/seccomp_filter.html
* Fly.io Sprites 的"策略设置者从沙箱内不可达"原则 —— https://fly.io/learn/fly-vs-e2b/
