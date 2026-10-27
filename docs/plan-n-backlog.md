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

### 2.6 `wrap()` 的 fail-open + 缺守卫用例 —— **跑全量时新发现**

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
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")   # 40 = SHA-1, 64 = SHA-256 仓库
```

> **评审 P3（已采纳）**：上界从 40 放到 **64** —— SHA-256 仓库的 `rev-parse HEAD` 是 64 位，
> 用 40 会把合法 sha 判成污染串，反而**制造**静默失效。

```python
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

> **评审 P3（已记录，不在本轮扩范围）**：同一类 stderr 污染还存在于
> `SnapshotStore.restore`（`snapshots.py:134-137` 的 `diff --name-only`）、
> `SnapshotStore.prune`（[:179-195](file:///workspace/ai-coding/ai_coding/core/snapshots.py#L179-L195) 的
> `for-each-ref`）、`WorktreeManager.list`（[:95-104](file:///workspace/ai-coding/ai_coding/core/worktree.py#L95-L104) 的
> `worktree list --porcelain`）—— 它们也把 stdout 当值解析，只是不产生 sha。
> **本轮只修 `capture`（唯一产生 sha 的地方）**，其余写进 §七 的"已知同类未修"。

> **评审 P3（复现前置条件）**：`scripts/verify_snapshot_stderr.py` 必须继承
> `scripts/verify_real_eacces.py` 的做法 —— 其 docstring（[:11-14](file:///workspace/ai-coding/scripts/verify_real_eacces.py#L11-L14)）
> **已经写明** `GIT_CONFIG_GLOBAL=/dev/null` 是关键，否则 git 读不到 config 会**另外**往 stderr
> 打警告，把 `.git/info/exclude` 那条从 config 警告里隔离不出来。

### B. N-6 · 两条 rg 用例

**用例本身有缺陷**（静默假绿），按项目规则允许修改。

* `test_m4_security.py:461`：循环里 `use_rg=True` 那一轮，rg 缺席时 `pytest.skip(...)`；
  rg 在场时**追加一条正向断言**（`result.success is True`），让它不可能再"什么都没搜到还绿"。
* `test_r17_parity.py:206`：加 `@pytest.mark.skipif(not shutil.which("rg"), ...)`（与同文件
  [:183](file:///workspace/ai-coding/tests/integration/test_r17_parity.py#L183) 一致）；
  并在用例内断言 `GrepTool(Sandbox(root))._rg is not None`，**钉住"真跑了 rg 分支"**。

### C. `known_miss` —— **全量修 16 条**（用户拍板）

> **设计已原型验证**（`/tmp/proto_rules.py`，未落代码）：**15/16 修复、`SAFE` 误报 0 条、
> 33 条真实工作命令里 32 条放行**。下面每条规则都对应原型里的一次实测。

#### C.0 根因：语法层把"带展开的词"整个丢掉了

**这是本类缺口的真正根因**，实测（tree-sitter 树）：

| 命令 | 树 | `view.arguments` |
|---|---|---|
| `cat ~/.ssh/id_$USER` | `concatenation` → `word '~/.ssh/id_'` + `simple_expansion '$USER'` | **`()`** |
| `rm -rf $HOME` | `simple_expansion '$HOME'` | **`()`** |
| `rm -rf ${HOME}` | `expansion '${HOME}'` | **`()`** |
| `x="rm -rf /"; $x` | `command_name` → `simple_expansion '$x'` | `()`，program=`$x` |

[shell_parse.py:82](file:///workspace/ai-coding/ai_coding/shell_parse.py#L82) 的
`_WORD_NODES = ("word", "number")` **不含** `concatenation` / `simple_expansion` / `expansion`，
于是 `_words_of`（[:187](file:///workspace/ai-coding/ai_coding/shell_parse.py#L187)）
**把这些词整个丢掉** —— 规则看不见它们，`_grammar_reads` 又返回 `True` 短路掉文本回退，
**双重失明**。这正是 GuardFall 的机理：过滤器检查的是**原始字符串**，
而 bash 执行的是**展开之后的 argv**。

#### C.1 六处规则改动

**① `_WORD_NODES` 补四个节点**（`shell_parse.py`）

```python
_WORD_NODES = (
    "word", "number", "concatenation", "simple_expansion", "expansion",
    "command_substitution",
)
```

Shell **确实**会把这些当词传下去（`$HOME` / `$(pwd)` 展开后就是一个词）。补上它们，规则才看得见。

> **评审 P2（已采纳）**：`_WORD_NODES` 同时驱动 `_paths_in` → `path_candidates`
> （[shell_parse.py:202](file:///workspace/ai-coding/ai_coding/shell_parse.py#L202)），
> 所以 `cat ~/.ssh/id_$USER` 会**新进入** `plan_runner` 的 shell 路径候选
> （[:270](file:///workspace/ai-coding/ai_coding/core/plan_runner.py#L270)）。
> 这是**正确的方向**（那条路径确实含凭据目录），但必须在实现期跑一遍
> `tests/test_plan_runner*.py` 确认没有计划命令被误拒。

**② 新增"不可审计"展开**（`bash_redlines.py`）

```python
_UNRESOLVED = "\x00unresolved\x00"          # 哨兵：判据读不到 env，不给 fall-open 留口
_EXPANSIONS = {"HOME": str(Path.home()), "PWD": os.getcwd(), "USER": os.environ.get("USER", "")}

def _expand(word: str) -> str:
    """``~`` / ``$VAR`` / ``${VAR}`` → 具体路径；解不开的展开 → 哨兵。"""
    out = word
    if out == "~" or out.startswith("~/"):
        out = _EXPANSIONS["HOME"] + out[1:]
    elif out.startswith("~"):
        return _UNRESOLVED               # ~user：不是我们的家，解不开
    for name, value in _EXPANSIONS.items():
        out = re.sub(r"\$\{" + name + r"\}", value, out)
        out = re.sub(r"\$" + name + r"(?![A-Za-z0-9_])", value, out)   # 词边界
    if "$" in out or "`" in out:
        return _UNRESOLVED
    return out
```

**评审修的三处**：`(?![A-Za-z0-9_])` 词边界（否则 `$PWDDIR` 被 `$PWD` 猜成 `<cwd>DIR`）；
`~user` 不猜（`_HOME + "user"` 是错的）；`$PWD` 取 `os.getcwd()`（判据进程就是代理进程）。

**③ `rm -rf` 的受保护目标**（`bash_redlines.py`）

`_is_root_target` 升级为 `_is_protected_target`：

```python
_PREFIX_ROOTS = _SYSTEM_ROOTS - {"/home"}      # /home 只走深度 3，见下

def _is_protected_target(path: str) -> bool:
    expanded = _expand(path)
    if expanded is _UNRESOLVED:
        return True                        # 不可审计
    normalized = expanded.rstrip("/") or "/"
    if normalized in ("/", "/*"):
        return True
    parts = normalized.split("/")
    if any(p == ".." for p in parts):
        return True                        # 穿越
    if normalized == _EXPANSIONS["HOME"]:
        return True                        # 操作者的家
    if len(parts) == 3 and parts[1] == "home":
        return True                        # 别人的家 /home/<name>
    return any(normalized == r or normalized.startswith(r + "/") for r in _PREFIX_ROOTS)
```

| 形状 | 判据 | 覆盖 |
|---|---|---|
| 根 / 顶层系统目录 | **精确或前缀**匹配 `_PREFIX_ROOTS`（`/etc` 与 `/var/log` 都命中） | `rm -rf /var/log`、`rm -rf /etc` |
| 操作者的家 | 精确等于 `Path.home()` | `rm -rf ~`、`rm -rf $HOME`、`rm -rf ${HOME}` |
| 别人的家 | `/home/<name>`（深度 3） | `rm -rf /home/user` |
| 路径含 `..` | 任一组件是 `..` | `rm -rf ./..` |
| 解不开的展开 | 哨兵 | `D=/data; rm -rf $D` |

> **评审 P0（已修）**：原型 v1 只写了 `startswith(r + "/")`，**精确顶层目录全部漏**
> （`rm -rf /etc`、`/root`、`/usr`、`/bin`、`/opt`、`/boot`、`/lib`）——
> 而原型只测了 `SAFE`/`KNOWN_MISS`，**从不测 `DANGEROUS`**，所以照不出来。
> v2 改成 `== r or startswith(r + "/")`，并**对新旧两版各跑一次全量语料**：
> `DANGEROUS` 漏判数 **OLD 19 / NEW 19（完全一致 → 零回归）**，`SAFE` 误报 **0 / 0**。

> **`/home` 从"前缀集"里排除**，只走深度 3 那条 —— 否则 `rm -rf /home/<user>/proj/build`
> 会被误拒（`/home` 常是工作区所在）。**实测 `rm -rf /home/user/proj/build` 放行。**

**④ 包装器剥值**（`bash_redlines.py::_programs`）

`env -i sudo ls` / `timeout 5 sudo ls` 现在断在 `-i` / `5` 上。改成
**跳过包装器自己的 flag 及其取值**：

```python
_WRAPPER_VALUE_FIRST = frozenset({"timeout"})   # 时长永远在前，且必然是值
_WRAPPER_NUMERIC_FIRST = frozenset({"nice"})    # `nice 5 cmd` / `nice -n 5 cmd`
_WRAPPER_VALUE_FLAGS = {
    "env": frozenset({"-u", "-C", "-S"}),
    "nice": frozenset({"-n"}),
    "ionice": frozenset({"-c", "-n", "-p"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "timeout": frozenset({"-s", "-k"}),
    "command": frozenset({"-v", "-V", "-p"}),   # 查存在性，不执行后面的词
}
```

**评审修的两处（都是真回归）**：

* **`command -v sudo` 会被误判成提权**（P1）—— `command -v/-V/-p` 是"查存在性"，
  不执行后面的词。把这三个 flag 当**取值 flag**（跳 2）即可。实测 `command -v sudo` → 放行。
* **`nice sudo ls` 会被放行**（P1，与修 `timeout 5 sudo ls` 直接冲突）——
  `nice`/`stdbuf`/`ionice` 不带取值时**直接跟命令**是合法用法。
  所以 `_WRAPPER_VALUE_FIRST` **只留 `timeout`**（它的时长必然在前），
  `nice` 改用"下个数是数字才跳"。实测 `nice sudo ls` / `nice 5 sudo ls` / `nice -n 5 sudo ls`
  **三条都仍判为提权**。

同时把 `doas` 加进提权规则（`_runs(view, "sudo") or _runs(view, "doas")`）—— `doas` 已在
`_WRAPPERS` 里，只是规则没查它。

**⑤ `--force-with-lease`**（`bash_redlines.py::_git_push_is_forced`）

`--force` / `-f` 之外补 `--force-with-lease`（它就是 force push，只是更安全的写法）。

**⑥ 程序名解不开 → 不可审计**（`bash_redlines.py`）

```python
("unauditable command", lambda view: "$" in view.program or "`" in view.program),
```

`x="rm -rf /"; $x` 与 `x=1; $x rm -rf /` 的**程序名**是 `$x` —— 不经过 `_expand`/`_is_protected`，
必须单列一条。**评审 P2（已采纳）**：原型靠这一行修好那两条，v1 方案文字漏了。


#### C.2 `rm -rf $PWD` → 转 `SAFE`（**评审 P1 已修正论证**）

**v1 说它"≡ `rm -rf .`"，这是错的**（评审实测指出）：

* `rm -rf .` 在 GNU rm 下因默认 `--preserve-root` **被拒**，是 **no-op**（`skipping '.'`）；
* `rm -rf $PWD` 的 `$PWD` 是**绝对路径**（如 `/workspace`），`rm -rf /workspace`
  **真的删掉整个工作区**。

两者效果完全不同。**正确的理由**是：`$PWD` 展开成 cwd，而 **cwd 是"工作所在的地方"，
不是受保护目标**（受保护的是根 / 顶层系统目录 / 各家目录）。所以它放行 ——
**与 `rm -rf /<工作区绝对路径>` 放行是一致的**，而后者今天就是放行的。

> **这不是"静默漏洞"**：它的 verdict 仍是 `""`，gate 仍是 **WARN（询问）**，
> 不是硬放行。C.4 的兜底用例也**测不到它**（它删的是工作区**内**，沙箱允许）——
> 所以这一条必须在文档里写明"工作区内的删除是操作者自己的事"。

#### C.3 语料重冻

* 15 条 `known_miss` → `dangerous`（verdict 填新理由）；
* `rm -rf $PWD` → `SAFE`（理由见 C.2，**不是**"≡ `rm -rf .`"）；
* **`MIN_KNOWN_MISS` 必须显式改成 `0`**（评审 P1：v1 说"floors 只设下限，自动满足"是**错的** ——
  [redline_corpus.py:79](file:///workspace/ai-coding/ai_coding/security/redline_corpus.py#L79)
  是 `= 5`，`0 < 5` 会让 `redline_benchmark` 直接返回 1）；
* `tests/test_redline_benchmark.py` 的 `test_known_miss_is_surfaced`
  （[:181-198](file:///workspace/ai-coding/tests/test_redline_benchmark.py#L181-L198)）
  断言 `'x="rm -rf /"; $x' in out` —— 修好后它不再出现在 `known_miss` 里，**必须同步改写**；
* `redline_benchmark.py` 的计数**从判定输出计算**，自动归零 —— 脚本本身不用改。

#### C.4 兜底仍可机检（保留）

即使 16 条全修，**OS 写/读边界仍是兜底**（文本层总会有新写法）。C.2 的证明方式不变：
在 CI 真沙箱内把这 16 条逐条跑一遍，断言**没有一条写进工作区外 / 读到凭据**。

#### C.5 为什么这不是"加正则"

GuardFall 说文本过滤结构性失败 —— **对**。但本轮做的不是"加更多正则去猜展开后的值"，
而是：**让语法层别再丢掉带展开的词**（C.1①，根因），再把"解不开"这件事本身
**当成拒绝的理由**（C.1②③）。判据从"猜值"变成"承认猜不到"，这是方向上的不同。

### D. 代理 CIDR + 裸 IP 语义

#### D.1 CIDR 匹配 —— **真实调用链是"先按主机名授权、后解析"**（评审 P1 已重写）

实测调用链（[sandbox_proxy.py](file:///workspace/ai-coding/ai_coding/server/sandbox_proxy.py)）：

```
_authorize(proxy, host, port)  ──► _host_allowed(host, port, allow)   # ① 先按主机名判
                                  └─ 抛 _Refused(403)                 #    CIDR 在这里就被拒了
_connect(proxy.resolve_target(host, port), ...)
  └─► _resolve_public_address(host, port)   # ② 才解析
        └─ 内部调 _is_forbidden_ip(ip)      # ③ 私网拒绝在 resolve 内部
```

**v1 说"在 `_resolve_public_address` 之后查 CIDR"是错的** —— 那一步永远到不了，
因为 `_host_allowed` 先抛了。三处必须一起改：

```python
def _host_allowed(host, port, allow) -> bool:
    """域名 / 裸 IP。含 `/` 的条目是 CIDR，**在这里跳过**（它们匹配不了主机名）。"""
    for entry in allow:
        if "/" in str(entry):
            continue                      # CIDR 留给 _cidr_allowed
        ...

def _cidr_allowed(ip: str, allow: Sequence[str]) -> bool:
    """CIDR 条目对**已解析**的地址判定 —— 主机名无法与网络比较。"""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    return any(
        "/" in str(entry) and addr in ipaddress.ip_network(str(entry), strict=False)
        for entry in allow
    )
```

`_authorize` 改成**两段 OR**：

```python
if not _host_allowed(host, port, allow):
    raise _Refused(...)
address = proxy.resolve_target(host, port)        # 解析 + _is_forbidden_ip（私网仍拒）
if not _cidr_allowed(str(address.ip), allow):     # 新：CIDR 对 pinned 地址判
    raise _Refused(...)
```

> **顺序不变式**：`_is_forbidden_ip` **留在 `_resolve_public_address` 内部**（评审 P1 指出
> v1 的"先 CIDR 再私网"顺序与代码矛盾）。私网 CIDR（`10.0.0.0/8`）仍然被拒 ——
> 因为 resolve 阶段就先拒了，`_cidr_allowed` 根本看不到它。**fail-closed 不变。**
>
> **DNS rebinding 不受影响**：CIDR 判定发生在 `resolve_target` **之后**，
> 比对的是**已 pinned** 的 sockaddr（§C.6 的既有时序）。

#### D.2 malformed CIDR 要**响**，且要在**代理层**也响（评审 P2）

只在 `NetworkConfig` 校验不够 —— proxy 的模块 docstring 明写"the proxy keeps the rule
locally so a hand-built instance cannot open the wall"（[:12-13](file:///workspace/ai-coding/ai_coding/server/sandbox_proxy.py#L12-L13)），
而 `SandboxProxy(allow=[...])` 可以直接构造（测试就这么用）。所以**两处都校验**：

* `NetworkConfig.allow` 的 validator（config 来源）；
* `_parse_allow_entry` 或 `SandboxProxy.__init__`（hand-built 来源）——
  条目含 `/` 但 `ip_network(..., strict=False)` 失败 → **构造即报错**。

#### D.3 多地址只判第一个（评审 P3，写明语义）

`_resolve_public_address` 只返回 `resolved[0]`（[:208](file:///workspace/ai-coding/ai_coding/server/sandbox_proxy.py#L208)），
所以 CIDR 判定只覆盖**第一个**解析结果。这是**既有语义**，本轮不改；
写进文档即可（真实的多地址主机由 `_is_forbidden_ip` 的"任一非公网即拒"兜住）。

#### D.4 修正残余风险表

`plan-m-d2.md` §7.1 的"IP/CIDR/UDP 绕过"一行改为**实测口径**：
裸 IP 一直可用；UDP 已被 `--unshare-net` 阻断；**真缺口是 CIDR**（本轮修）。

### E. `extra_workspace_roots` 运行期可变

**目标**：让 [builder.py:233-235](file:///workspace/ai-coding/ai_coding/tools/builder.py#L233-L235)
声称的不变量在**运行期**也成立。

#### E.1 进程层的 roots 改成**活的**

`BashTool` 不再快照 `roots`，而是持有 `Sandbox` 并**每次调用时读活**：

```python
# security/sandbox.py（新）
def live_roots(self) -> tuple[Path, ...]:
    """The writable roots **right now**: the static ones plus whatever the
    grantor currently authorizes.

    ``Sandbox.resolve`` already asks the grantor before refusing, so the path
    layer's answer is live.  The process layer must be live too -- a snapshot is
    exactly how a directory the file tools may write becomes one a bash command
    may not (builder.py:233-235 claims the two never diverge).
    """
    roots = list(self.roots)
    if self._grantor is not None:
        roots.extend(self._grantor.authorized_roots())
    return tuple(roots)
```

`PermissionGate.authorized_roots()` 把 `path_allowed` 里已有的 key 解析逻辑
（[:838-849](file:///workspace/ai-coding/ai_coding/security/permission_gate.py#L838-L849)）
提出成私有 helper 复用 —— **不新写解析**。

`spawn_argv` 的 `roots` 参数放宽为 `Iterable[Path]`，在 `wrap()` 里 `list(roots)` 一次
（`wrap`/`_build` 只迭代一次，`_try_tier` 传的是 tuple，不受影响）。

**评审 P2（已采纳，改成更小的改法）**：v1 新造了一个 `LiveRoots` 类 —— 而 `Sandbox`
本来就持有 `_grantor` 和 `roots`，那只是 `Sandbox` 知识的薄包装。**改为给 `Sandbox`
加 `live_roots()`**，少一个新类、少一处状态副本。

**评审 P2（已修）**：`authorized_roots()` **只返回目录**。`path_allowed` 的 key 解析对
`write`/`edit` 的 grant 取的是**文件路径本身**（`grant_key` 返回 `edit:/outside/file.py`），
直接当 root 会让 bwrap `--bind` 一个**文件**。所以提取后要过滤 `p.is_dir()`。

#### E.2 三个构造点都要接线（评审 P1，v1 漏了两个）

roots 实际在**三处**被构造并下发，v1 的清单只覆盖了工具栈：

| 构造点 | 位置 | 现状 |
|---|---|---|
| 主工具栈 | [builder.py:236](file:///workspace/ai-coding/ai_coding/tools/builder.py#L236) → `BashTool(roots=...)` | 快照 |
| **后台 job** | [world.py:184-193](file:///workspace/ai-coding/ai_coding/server/world.py#L184-L193) → `JobRegistry` | 快照 |
| **子智能体** | [builder.py:101-103](file:///workspace/ai-coding/ai_coding/tools/builder.py#L101-L103) → 子 `BashTool` | worktree 元组 |

> **注意 `world.py` 的构造顺序**：`JobRegistry` 在 [:188](file:///workspace/ai-coding/ai_coding/server/world.py#L188)
> 构造，**早于** `_assemble_registry`（builder.py:195 起）里才创建的 `Sandbox`/gate。
> 所以要么把 `Sandbox`/`live_roots` 的构建**提前**，要么让 `JobRegistry` 持有一个
> **迟绑定**的 `Callable[[], Sequence[Path]]`。实现期按实际顺序定，**清单里必须有这一项**。

子智能体那条：子 `BashTool` 的 roots 绑定**子 worktree**，需要一个绑定到子 sandbox/gate 的
`live_roots()` —— 否则不变量只对主会话成立（评审 P2）。

#### E.3 不变量的机检

加一条用例：grant 一个工作区外的目录后，
`set(bash 的实际 roots) == set(sandbox.live_roots())`，且 bash 真能写它。

> **这是本轮最大的一块**，涉及 `security/sandbox.py` / `security/permission_gate.py` /
> `tools/bash_tool.py` / `core/jobs.py` / `security/process_sandbox.py` /
> **`server/world.py`** / **`tools/builder.py`（含子智能体）**。
> 方案评审若认为风险过大，可整块**砍掉**（其余五项不受影响）—— 见 §六 的拍板项。

### F. `wrap()` 的 fail-open + 缺守卫用例（第 1 轮评审后：**8 条**）

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

#### F.2 用例处理 —— **8 条，不是 3 条**（评审 P1）

`wrap()` 现在 fail-open 回落 `"full"`，所以**今天所有"调 `wrap()` 再检查 argv"的用例
在本地都能过**。F.1 改成返回 `None` 后，它们会拿到 `None` 再 `argv.index(...)` → 崩。
**本地（无 bwrap）会新增 5 条失败，加上原 3 条共 8 条**：

| 用例 | 行 | 处理 |
|---|---|---|
| `test_wrap_keeps_the_isolation_switches` | :593 | `@_NEEDS_BWRAP` |
| `test_the_read_boundary_hides_a_directory_and_a_file` | :767 | `@_NEEDS_BWRAP` |
| `test_a_writable_path_under_a_read_deny_is_still_unreadable` | :800 | `@_NEEDS_BWRAP` |
| `test_wrap_puts_the_writable_bind_after_the_readonly_root` | :568 | **改写**（见下） |
| `test_wrap_mounts_tmpfs_before_the_roots` | :580 | **改写** |
| `test_wrap_masks_after_the_tmpfs_and_the_binds` | :617 | **改写** |
| `test_the_deny_tier_cleans_inherited_proxy_env` | :1366 | **改写** |
| `test_the_off_tier_cleans_inherited_proxy_env` | :1373 | **改写** |

> **评审 P2（已采纳）**：后 5 条是**纯 argv 形状断言，本不需要真机制**。
> 给它们加 `@_NEEDS_BWRAP` 会把 mask 顺序 / proxy-env 清理的回归在**本地完全藏起来**
> （与 N-6 的"静默假绿"同机理）。所以这 5 条**改写成不依赖 `wrap()` 的返回值** ——
> 直接断言 `_build(argv, roots, "full")` 的产物，或注入一个 `tier="full"` 的替身。
> **它们测的是形状，形状不该需要内核。**

#### F.3 一条新的契约用例（钉住 F.1）—— **替身要换**（评审 P1）

v1 用 `_UnavailableBackend`，但它 `tier = "full"` 且 `wrap()` 返回 `list(argv)`
（[:76-89](file:///workspace/ai-coding/tests/test_process_sandbox.py#L76-L89)）——
**断言 `is None` 必然失败**。它是给"`available()` 为 False"路径用的，不是给 `tier == "none"`。

改成**在 F.2 改写那 5 条时一并加的 `tier="full"` 替身**的反面：一个
`tier="none"` 的 `_UnavailableBackend` 变体（或直接断言真 `BwrapBackend` 在无 bwrap 主机上
返回 `None`，用 `@_NEEDS_NOT_BWRAP` 反向守卫）。**实现期二选一，清单里写明。**


---

## 四、逐文件改动清单

| 文件 | 改动 | 属于 |
|---|---|---|
| `ai_coding/core/gitcmd.py` | `git()` 加 `merge_stderr: bool = True` | N-1 |
| `ai_coding/core/snapshots.py` | `capture` 用干净流 + `_SHA_RE` 校验 | N-1 |
| `scripts/verify_snapshot_stderr.py` | **新**：以非 root 真子进程复现并验证（同 `verify_real_eacces.py` 模式） | N-1 Prove |
| `tests/test_m4_security.py` | rg 守卫 + 正向断言 | N-6 |
| `tests/integration/test_r17_parity.py` | rg 守卫 + 钉住 rg 分支 | N-6 |
| `ai_coding/shell_parse.py` | `_WORD_NODES` 补 `concatenation`/`simple_expansion`/`expansion`/`command_substitution` | C.1① |
| `ai_coding/security/bash_redlines.py` | `_expand` + `_UNRESOLVED`；`_is_protected_target`；`_programs` 剥值；`doas`；`--force-with-lease`；程序名不可审计规则 | C.1②③④⑤⑥ |
| `ai_coding/security/redline_corpus.py` | 15 条 `known_miss` → `dangerous`；`rm -rf $PWD` → `SAFE`；`MIN_KNOWN_MISS` → 0 | C.3 |
| `scripts/redline_benchmark.py` | 计数随语料自动更新（无需改） | C.3 |
| `tests/test_redline_benchmark.py` | `test_known_miss_is_surfaced` 改写（断言语料非空而非某条已知命令） | C.3 |
| `tests/test_process_sandbox.py` | **新**：16 条在 `enforce` 沙箱内全部写不出去 / 读不到凭据 | C.4 |
| `ai_coding/server/sandbox_proxy.py` | `_host_allowed` 跳过 CIDR 条目；新增 `_cidr_allowed`；`_authorize` 两段 OR；代理层 malformed CIDR 校验 | D.1 / D.2 |
| `ai_coding/config/models.py` | `NetworkConfig.allow` 的 malformed CIDR 校验 | D.2 |
| `ai_coding/security/sandbox.py` | `live_roots()` | E.1 |
| `ai_coding/security/permission_gate.py` | 公开 `authorized_roots()`（复用已有解析，只返回目录） | E.1 |
| `ai_coding/tools/bash_tool.py` | roots 改活（持 `Sandbox`） | E.1 |
| `ai_coding/core/jobs.py` | roots 改活 | E.2 |
| `ai_coding/server/world.py` | `JobRegistry` 的 roots 改活（注意构造顺序） | E.2 |
| `ai_coding/tools/builder.py` | 主栈 + **子智能体** 的 roots 改活 | E.1 / E.2 |
| `ai_coding/security/process_sandbox.py` | `spawn_argv` 接受 `Iterable[Path]` | E.1 |
| `docs/plan-m-d2.md` | 修正 §7.1 的裸 IP / UDP 断言；补 CIDR 语义与"多地址只判第一个" | D.4 |
| `ai_coding/security/process_sandbox.py` | `wrap()` 在 `tier == "none"` 时返回 `None`（不再回落 `"full"`） | F.1 |
| `tests/test_process_sandbox.py` | **8 条**：3 条补 `@_NEEDS_BWRAP`、5 条改写成形状断言；新增 `wrap()` 契约用例 | F.2 / F.3 |
| `docs/roadmap-milestones.md` | N-1/N-2/N-6 标完成 | 文档 |

---

## 五、RIPR 用例

### Red（先写，必须变红）

| # | 用例 | 断言 |
|---|---|---|
| U1 | `capture` 收到 `(0, "warning: ...\n<sha>")` | **不得**把污染串当 sha 去 `update-ref`（monkeypatch `snapshots.git`） |
| U2 | rg 缺席 + `use_rg=True` | `test_grep_include_dotenv_still_filters_credentials` **skip**，不再静默通过 |
| U3 | rg 缺席 + parity | `test_both_runners_answer_the_same_question` **skip** |
| U4 | `x="rm -rf /"; $x`、`D=/data; rm -rf $D`、`x=1; $x rm -rf /` | 判为危险（不可审计） |
| U5 | `rm -rf ${HOME}`、`rm -rf $HOME`、`rm -rf ~`、`rm -rf ~/`、`rm -rf /home/user`、`rm -rf /var/log`、`rm -rf ./..` | 判为危险（受保护目标） |
| U6 | `doas rm x`、`env -i sudo ls`、`timeout 5 sudo ls` | 判为危险（提权） |
| U7 | `git push --force-with-lease origin main` | 判为危险（force push） |
| U7b | `cat ~/.ssh/id_$USER` | 判为危险（凭据） |
| U7c | `rm -rf $PWD` | **放行**（cwd 不是受保护目标），语料转 `SAFE` |
| U7d | `rm -rf ./build`、`rm -rf /tmp/build`、`rm -rf build`、`echo "rm -rf /"`、`# rm -rf /`、`rm -rf /home/user/proj/build` | **仍放行**（`SAFE` 零回归） |
| U7e | 16 条在 `enforce` 沙箱内执行 | **没有一条**写进工作区外 / 读到凭据 |
| U7f | `rm -rf /etc`、`rm -rf /root`、`rm -rf /usr`、`rm -rf /bin`、`rm -rf /opt`、`rm -rf /boot`、`rm -rf /lib` | **仍判危险**（评审 P0：精确顶层目录不能被前缀匹配漏掉） |
| U7g | `command -v sudo`、`command -v doas`、`nice sudo ls`、`stdbuf sudo ls`、`nice -n 5 sudo ls`、`nice 5 sudo ls`、`env -- sudo ls` | `command -v` **放行**；其余**仍判提权**（评审 P1 双向） |
| U7h | `rm -rf $PWDDIR`、`rm -rf $(pwd)`、`rm -rf ~root` | 判危险（词边界 / 替换 / `~user` 不猜错） |
| U8 | `allow=['8.8.8.0/24']` + 请求 `8.8.8.8:443` | **放行**（现在是被拒） |
| U9 | `allow=['10.0.0.0/8']` | **拒绝**（私网不变） |
| U10 | `allow=['8.8.8.0/33']` | **config 与 hand-built 两处都报错**（不是静默全拒） |
| U11 | grant 工作区外目录后，bash 写该目录 | **成功**（现在是 EROFS） |
| U12 | 不变量 | `set(bash roots) == set(sandbox.live_roots())` |
| U12b | `authorized_roots()` 对**文件级** grant（`edit:/outside/file.py`） | **不返回该文件**（只返回目录） |
| U13 | `tier == "none"` 的后端 `.wrap([...], [])` | **返回 `None`**（现在是回落 `"full"` 的 argv） |
| U14 | **8 条**（本地无 bwrap） | 3 条**跳过**、5 条**仍运行**（改写成形状断言） |

### Prove

* **U1-P**：`scripts/verify_snapshot_stderr.py` 以 `nobody` 真子进程复现 —— 改前 `capture → None`，改后返回合法 ref。
* **U7-P**：CI 真内核 + bubblewrap 实跑（本地 bwrap 缺席时按既有模式显式跳过并说明理由）。
* **突变测试**：① 去掉 `merge_stderr=False` → U1 变红；② 把 `_SHA_RE` 换回 `if not sha` → U1 变红；
  ③ 把 `_cidr_allowed` 的返回写死 `False` → U8 变红；④ 把 `Sandbox.live_roots()` 改成返回 `self.roots`
  → U11/U12 变红；⑤ 把 `wrap()` 的 `if self._tier == "none": return None` 改回 `else "full"` → U13 变红；
  ⑥ 把 `_is_protected_target` 的 `normalized == r` 去掉（只留前缀）→ **U7f 变红**（评审 P0 的回归钉）；
  ⑦ 把 `_WRAPPER_VALUE_FIRST` 加回 `nice` → U7g 的 `nice sudo ls` 变红（评审 P1 的回归钉）；
  ⑧ 把 `_WORD_NODES` 的新增节点去掉一个 → U7b/U5 变红。

### Refactor

* `PermissionGate.authorized_roots()` 与 `path_allowed()` 共用同一段 key 解析（提出私有 helper），
  两处不允许各写一份。

---

## 六、验收判据

* **N-1**：改前复现脚本 `capture → None`；改后返回 `refs/ai-coding/undo/<key>`，且 `update-ref` 的
  目标**是** 40 位 hex（64 位仓库同样通过）。`_SHA_RE` 拒绝污染串。
* **N-6**：`PATH=/tmp/norg` 下两条**跳过**（不是通过）；有 rg 时**通过**且断言 `success is True` / `_rg is not None`。
* **known_miss**：基准 `known_miss` 计数 **16 → 0**；`dangerous` 63 → 78；`SAFE` 26 → 27；
  FN=0 / FP=0 硬阈值不变；**16 条在 CI 真沙箱内全部写不出去 / 读不到凭据**（U7e）；
  **`DANGEROUS` 漏判数与改前一致**（零回归，U7f）；`command -v sudo` 等**不误拒**（U7g）。
* **CIDR**：`8.8.8.0/24` 放行、`10.0.0.0/8` 拒绝、malformed 在 config 与 hand-built **两处**都拒；
  域名匹配**无回归**（`evil-example.com` / `example.com.evil.com` 仍拒）。
* **E**：grant 后 bash 与文件工具**写面一致**（U12 不变量）；`authorized_roots()` 不返回文件（U12b）；
  `ruff`/`mypy` 0。
* **F**：本地（无 bwrap）**3 条失败 → 3 条跳过**，另 **5 条改写成形状断言后仍运行**（不藏回归）；
  `wrap()` 在机制缺席时返回 `None`；
  CI（有 bwrap）行为**不变**（探针成功，走不到该分支）。
* **N-2**：`test_tool_surface_warning` 与 `test_worktree` 的 flaky 收口（复现方式见 §八 评审记录）。
* 3.10 与 3.13 全量绿；真实 CI 双 job 绿；**本地无 bwrap / 无 rg 环境全量也绿**（这是本轮的
  新增验收面 —— 之前本地是 3 红）。

---

## 七、不做的事

* **不改 `git()` 的默认合并语义** —— 会动 `WorktreeManager` 的 6 个调用点，且合并是有意的。
* **不给"规则未命中"加统一询问兜底** —— bash 未命中本来就是 WARN（询问），加不出新东西。
* **不猜展开后的值** —— `_expand` 只解 `~`/`$HOME`/`$PWD`/`$USER`，其余一律当"不可审计"。
  猜值（跑 shell、查 env 全表）会引入不可辩护的 FP，且判据不再纯函数。
* **不改 `_grammar_reads` 的短路语义** —— 修好 `_WORD_NODES` 后，语法层已经看得见这些词，
  不需要动"文本回退什么时候接管"这条契约（它会波及全部 unparseable 用例）。
* **`rm -rf $PWD` 保持放行** —— cwd 不是受保护目标；**但注意它不等于 `rm -rf .`**
  （后者是 `--preserve-root` 的 no-op）。它与 `rm -rf /<工作区绝对路径>` 一致，后者今天就放行。
* **同一类 stderr 污染不扩范围** —— `SnapshotStore.restore`/`prune`、`WorktreeManager.list`
  同样把 stdout 当值解析（§A 的评审记录），但只有 `capture` 产生 sha，本轮只修它。
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

**2 个 P0 · 9 个 P1 · 12 个 P2 · 8 个 P3（全部已修，见 §8.1.1）**

**§C 的 P0（最严重）**：`_is_protected_target` 的**前缀匹配丢掉精确顶层目录** ——
原型只写 `startswith(r + "/")`，于是 `rm -rf /etc`、`/root`、`/usr`、`/bin`、`/opt`、
`/boot`、`/lib` **全部漏判**。而原型**只测了 `SAFE`/`KNOWN_MISS`，从不测 `DANGEROUS`**，
所以这个回归在"验证"里完全照不出来 —— 正是本方案自己最在意的那类"验证不完整"。
**修**：`normalized == r or normalized.startswith(r + "/")`，并**对新旧两版各跑一次全量语料**：
`DANGEROUS` 漏判 **19 → 19（零回归）**，`SAFE` 误报 **0 → 0**。

**§C 的 P1**：
* `rm -rf $PWD` "≡ `rm -rf .`" **论证错误** —— `rm -rf .` 是 `--preserve-root` 的 no-op，
  而 `rm -rf $PWD`（绝对路径）真的删工作区。已改写成正确理由。
* `rm -rf $VAR` 硬拒是**真实 FP 类**（`rm -rf $DIR`、`rm -rf ${TMPDIR}/build`）——
  **保留**（用户拍板"全量修"），但在 C.2 与 §六 显式写明这是**刻意取舍**。
* `command -v sudo` 会被误判提权（`command -v/-V/-p` 是查存在性）。
* `nice sudo ls` 会被放行（与修 `timeout 5 sudo ls` 直接冲突）。
* `MIN_KNOWN_MISS = 5` 必须显式归零（否则 `redline_benchmark` 直接返回 1）。
* `test_known_miss_is_surfaced` 的断言要同步改写。

**§C 的 P2**：`_expand` 词边界（`$PWDDIR`）、`~user` 不猜、`$(...)`/反引号、
程序名 `$x` 要单列一条规则、`_WORD_NODES` 改动会波及 `path_candidates`。

**§D 的 P1**：**CIDR 调用链写错了** —— 真实链是 `_authorize → _host_allowed`（先拒 CIDR）
`→ resolve_target → _is_forbidden_ip`。v1 说"在 `_resolve_public_address` 之后查 CIDR"
**永远到不了**。已重写为"两段 OR + `_host_allowed` 跳过含 `/` 的条目"。

**§E 的 P1**：改动清单**漏了 `server/world.py`（JobRegistry）与 `builder.py` 的子智能体路径**。
已补，并标注 `world.py` 的**构造顺序**问题。

**§E 的 P2**：`LiveRoots` 非最小（`Sandbox` 已持 `_grantor`，加 `live_roots()` 即可）；
`authorized_roots()` 会把**文件级** grant 当 root（要过滤 `is_dir()`）。

**§F 的 P1**：F.3 的 `_UnavailableBackend` **替身用错**（它 `tier="full"`、`wrap` 返回 list，
断言 `is None` 必失败）；F.1 会让**8 条**（不是 3 条）本地失败，其中 **5 条是纯 argv 形状断言，
应改写成不依赖 `wrap()` 的返回值**，而不是加守卫藏起来。

**§A 的 P3**：`_SHA_RE` 上界 40 → **64**（SHA-256 仓库）；复现脚本要继承
`GIT_CONFIG_GLOBAL=/dev/null`；同类污染在 `restore`/`prune`/`worktree.list` 未修（记录）。

**§D 的 P2/P3**：malformed CIDR 要在**代理层也校验**（hand-built 绕过 config）；
`_resolve_public_address` 只返回 `resolved[0]`（写明语义）。

#### 8.1.1 本轮 findings 的处置

| Finding | 级别 | 处置 |
|---|---|---|
| `_is_protected` 前缀漏精确顶层目录 | **P0** | 改 `== r or startswith(r+"/")`；新增 U7f 与突变 ⑥ |
| `rm -rf $PWD` 论证错误 | P1 | 改写 C.2 理由（`--preserve-root`） |
| `rm -rf $VAR` FP 类 | P1 | **保留**（用户拍板），C.2/§六 显式记录取舍 |
| `command -v sudo` 误判 | P1 | `command` 的 `-v/-V/-p` 当取值 flag；U7g + 突变 ⑦ |
| `nice sudo ls` 放行 | P1 | `_WRAPPER_VALUE_FIRST` 只留 `timeout`；`nice` 走数字判定 |
| `MIN_KNOWN_MISS` 未归零 | P1 | 显式改 0 |
| `test_known_miss_is_surfaced` | P1 | 同步改写（断言语料非空） |
| `_expand` 词边界 / `~user` / `$()` | P2 | 正则词边界 + `~user` → 哨兵 + `command_substitution` 入 `_WORD_NODES` |
| 程序名 `$x` 无规则 | P2 | 新增"unauditable command"规则（C.1⑥） |
| `_WORD_NODES` 波及 `path_candidates` | P2 | 记录 + 实现期跑 `test_plan_runner*.py` |
| CIDR 调用链错误 | **P1** | 重写 §D.1（两段 OR） |
| malformed CIDR 只在 config 层 | P2 | 代理层也校验（D.2） |
| 多地址只判第一个 | P3 | 写明语义（D.3） |
| `LiveRoots` 非最小 | P2 | 改为 `Sandbox.live_roots()` |
| `authorized_roots()` 返回文件 | P2 | 过滤 `is_dir()`（U12b） |
| 漏 `world.py` / 子智能体 | **P1** | 补进 §E.2 与改动清单 |
| `wrap()` 替身用错 | **P1** | F.3 换替身（`tier="none"`） |
| 漏 guard 5 条 | **P1** | F.2 列全 8 条；5 条改写而非跳过 |
| `_SHA_RE` 上界 | P3 | 40 → 64 |
| 复现前置 `GIT_CONFIG_GLOBAL` | P3 | 记录（A 的评审块） |
| 同类污染未修（restore/prune/list） | P3 | 记录，不扩范围 |
| `watch` 死配置 / `env --` / 计数口径 | P3 | 已修 |


---

## 九、调研来源

* Codex CLI 权限模型与 exec-policy 的 "disable splitting" 表述 —— https://agenticcontrolplane.com/controls/codex-cli
* Codex sandbox 模式与 Landlock/bubblewrap 现状 —— https://backgrind.com/blog/codex-cli-sandbox-modes/
* E2B 出网管控（CIDR / 裸 IP / 域名 / UDP 限制）—— https://e2b.dev/docs/network/internet-access
* Sandlock（Landlock + seccomp-bpf + user-notification 的分工）—— https://arxiv.org/pdf/2605.26298v1
* seccomp 内核文档（BPF 不能解引用指针；USER_NOTIF）—— https://docs.kernel.org/userspace-api/seccomp_filter.html
* Fly.io Sprites 的"策略设置者从沙箱内不可达"原则 —— https://fly.io/learn/fly-vs-e2b/
