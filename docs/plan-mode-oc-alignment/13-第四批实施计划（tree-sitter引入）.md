# 13 · 第四批实施计划（tree-sitter 引入）

> 状态：**✅ 已完成（2026-10-20）**。方案见 §1-§9；实施记录与安全审查见 **§10**。
> 上游：`11-第三批实施计划（W2-安全簇）` §11.4 登记的残留「验收命令的路径判定复用文本扫描，不解析 shell 语法」——本文处理它。
> 依据：opencode 的 tree-sitter 实现（`~/projects/opencode` @ `0a46301e3`，v2.0.22）+ 本仓源码级核验 + **本机实测探针**。

---

## 0. 拍板（2026-10-20）

| 拍板项 | 决定 |
|---|---|
| ① 依赖形态 | **硬依赖**：`tree-sitter` + `tree-sitter-bash` 进主依赖 |
| ② 与现有正则的关系 | **叠加**：tree-sitter 的结果与正则结果**取并集** |
| ③ 职责边界 | **路径提取 + 词法边界**，两件事都交给 tree-sitter |
| ④ 红线表重写 | **合并两道判定**（子串表 + 词形判定）为一套基于 command 节点的规则 |

**本文在实施期对③做了一处修正**（§3.2）：词法边界交给 tree-sitter 后，红线判定的**净增量只有 1 个用例**，而代价是 `bash_denied` 从纯函数变成有状态、且要处理 `number` 节点与递归 command 节点。修正后仍按拍板执行，但**红线判定与路径提取分开落地**（先落路径提取，词法边界单独一步且单独验证）。

---

## 1. 目标与最终模样

1. **变量形态的路径不再漏判**：`D=/data/x; rm -rf $D`、`echo $(cat /data/secret)`、`` echo `cat /data/secret` `` —— 现有正则全漏，tree-sitter 全抓到（实测 §2.2）。
2. **既有判定不退步**：`test -f "/data/x/app.py"`（引号内路径）现有正则能抓到；tree-sitter 的 `word` 节点抓不到（引号内容是 `string_content`）→ **取并集**是唯一不退步的形态。
3. **验收命令（verify）——唯一的 shell 入口——获得真正的语法解析**。它现在是本仓唯一「以 shell 语义执行」却「用散文正则扫描」的路径。

---

## 2. 调研事实（全部实测）

### 2.1 opencode 怎么做（源码实证）

| 项 | 事实 | 出处 |
|---|---|---|
| 依赖 | `tree-sitter-bash` 0.25.0 + `web-tree-sitter` 0.25.10（WASM 运行时） | `packages/core/package.json:135-138` |
| **为何 WASM** | 同一个 core 包要同时跑 workerd / bun / node，还要出单文件二进制 | `packages/core/package.json` 的 `imports` 条件映射；`parser-wasm.workerd.ts:1-3`（workerd 无文件系统路径 → wasm 路径置空 → 降级） |
| 加载 | `lazy(initialize)`，`Parser.init({locateFile})` + `Language.load(path)`，**全进程一次** | `parse.ts:353-373` |
| 遍历 | `tree.rootNode.descendantsOfType("command")` + 手写 node 遍历（**非 query**） | `parse.ts:183-205` |
| 提取 | **只有 `cd` 家族**产出路径；`rm`/`mv` 的目标**故意不提取** | `parse.ts:14` 的 `CWD` 集合；理由是 rm/mv 的路径语义无法从语法可靠推出 |
| 漏检 | `expandKnownDirectory` 遇 `$`/反引号/`(` 开头 → **放弃该目录**（`parse.ts:329-337`） | 源码注释：「cannot be resolved safely during permission analysis」 |
| 解析失败 | tree-sitter **不抛异常**，产出带 ERROR 节点的树 → `parse.ts:177` 的「Failed to parse」是**死代码** | 实测 6 个畸形输入全部 `has_error=True` 而非异常 |
| **正在迁走** | `packages/core/src/shell/scan.ts` 是 1500 行**零依赖手写扫描器**，`experimental.portable_shell_scanner` 开关可整体替换 tree-sitter | `services/www/src/content/docs/content/permissions.mdx:189`：「The portable scanner replaces the default tree-sitter scanner; tree-sitter is not a fallback or second opinion」 |

### 2.2 本机实测：tree-sitter 到底拓什么（`/tmp/opencode/probe_py.py`）

用 `tree-sitter` 0.26.0 + `tree-sitter-bash` 0.25.1（abi 15）实测：

| 形态 | 现有正则 | tree-sitter | 增量 |
|---|---|---|---|
| `D=/data/x; rm -rf $D` | 漏 | **`/data/x`** | ✔ 拓新 |
| `TARGET=/data/x; printf y > "$TARGET"` | 漏 | **`/data/x`** | ✔ 拓新 |
| `echo $(cat /data/secret)` | 漏 | **`/data/secret`** | ✔ 拓新 |
| `` echo `cat /data/secret` `` | 漏 | **`/data/secret`** | ✔ 拓新 |
| `test -f "/data/w2demo/app.py"` | ✔ | ✔（`string_content` 节点） | 平（**需含 string_content**） |
| `mkdir -p /data/w2demo && test -d /data/w2demo` | ✔ | ✔ | 平 |
| `cat /etc/passwd \| head -1` | ✔ | ✔ | 平 |

**结论**：tree-sitter 的净增量 = **变量与命令替换里的路径**（4 类）；其余持平。**若只遍历 `word` 节点，引号内路径会丢（倒退）** → 必须含 `string_content`。

### 2.3 词法边界的对比（`/tmp/opencode/probe_words4.py` + `probe_redline.py`）

| 命令 | 正则词 | tree-sitter 词 | 谁更准 |
|---|---|---|---|
| `chmod 777 /tmp/x` | `[chmod, 777, /tmp/x]` | `[chmod, 777, /tmp/x]` | 同（`777` 是 `number` 节点，须专门取） |
| `cat $(rm -rf /)` | `[cat, $, rm, -rf, /]` | `[cat, rm, -rf, /]` | **ts 更准**（递归进嵌套 command） |
| `echo "a; b"` | `[echo, a, b]` | `[echo, "a; b"]` | **ts 更准**（引号内的 `;` 不是分隔符） |
| `rm -rf / # comment` | `[rm, -rf, /, #, comment]` | `[rm, -rf, /]` | **ts 更准**（注释不混入） |
| `FOO=bar rm -rf /` | `[FOO=bar, rm, -rf, /]` | `[rm, -rf, /]` | **ts 更准**（环境变量前缀不改变命令） |

**16 个红线用例的结论对比：15 个一致，1 个不同** —— 唯一差异是 `FOO=bar rm -rf /`，tree-sitter 判 True（正确），正则判 False（漏）。

**词法边界的净增量因此只有 1 个红线用例**，而代价是 `bash_denied` 从纯函数变成有状态 + 需处理 `number` 节点 + 需递归 command 节点。这是 §3.2 把词法边界单独切片的理由。

### 2.4 Python 侧的代价（实测安装）

```
$ pip install -v tree-sitter==0.26.0 tree-sitter-bash==0.25.1
Downloading tree_sitter-0.26.0-cp313-...-manylinux2014_x86_64.whl (668 kB)
Downloading tree_sitter_bash-0.25.1-cp310-abi3-...-manylinux2014_x86_64.whl (231 kB)
Successfully installed tree-sitter-0.26.0 tree-sitter-bash-0.25.1
```
- **零编译器**（安装日志无 `gcc`/`Running setup` 命中）
- `tree-sitter`：cp310/311/312/313/314 齐全，manylinux2014（glibc 2.17），**带 `py.typed`**
- `tree-sitter-bash`：**cp310-abi3**（一份 wheel 盖 py3.10–3.14），manylinux2014 / musllinux_1_2 / macosx / win
- 依赖：`tree-sitter-bash` 的 `tree-sitter~=0.24` 只在 `core` extra 下 → **主依赖需自己 pin**
- 体积：合计 890 KB

**性能反而更快**（实测 4000 条）：tree-sitter 22.1 ms（0.006 ms/条） vs 现有正则 157.1 ms（0.039 ms/条）—— 因为现有 `outside_workspace_paths` 每次都构造 `Sandbox`（`Path.resolve()` 系统调用）。

**本仓适配度**：CI 是 `ubuntu-latest` × py3.10/3.13（`.github/workflows/ci.yml:15,22`），`pip install -e ".[dev]"`（`:33`），**无编译工具链声明** → 两个 wheel 都有，CI 零改动。本仓已有编译型扩展先例：`pydantic-core`（Rust）、`jiter`（Rust，openai 带入）—— 同为 prebuilt wheel 形态。

**被排除的路线**：
- **WASM**：PyPI 无 web-tree-sitter 的 Python 绑定（`tree-sitter-web` / `tree-sitter-wasm` 均 404）；需自写 Emscripten glue + `wasmtime`，且无 `py.typed`。opencode 选 WASM 是**它的打包约束**（workerd/单文件二进制），Python 侧无此约束。
- **`tree-sitter-language-pack` 1.22.1**：运行时下载语法（违反本仓离线纪律：`docs/how-to-run.md` 的验收脚本全离线）+ manylinux_2_34（排除 Ubuntu 20.04）。
- **前端解析**：架构错位——本仓的门在 Python 进程内、执行前判定；放前端等于把安全判定绑到可选 UI 进程，headless 无解析器。

---

## 3. 契约与复用

### 3.1 新模块 `ai_coding/core/shell_parse.py`

**唯一职责**：把一段 shell 文本解析成**词**与**路径候选**。

```python
"""Shell text, parsed (W2/T): the words and the paths a command names.

A command is a shell command, and a shell has a grammar: quotes hold a
phrase together, ``;`` inside a quote is not a separator, a command
substitution is a command of its own.  Reading those with a regular
expression is how ``echo "a; b"`` becomes two words and ``cat $(rm -rf /)``
loses the ``rm``.

tree-sitter gives the real grammar, so the words and the paths come from
the tree rather than from a guess.  The tree is only *read* here: the
judgements (is this path reachable, is this a red line) stay where they
are, in the modules that own them.
"""
```

**公开契约**（两个函数，别的都不导出）：

```python
def command_words(text: str) -> list[str]:
    """The words of every command in ``text``, in order.

    A word is what the shell would pass as an argument: a bare word, a
    number, or the content of a quoted string.  Commands inside a command
    substitution are included -- ``cat $(rm -rf /)`` has the words of
    both.
    """

def path_candidates(text: str) -> list[str]:
    """The absolute paths ``text`` names, in order.

    A path is a word that starts at the root (``/``) or at the home
    (``~``).  This is the *candidate* list: whether the path is reachable
    is the sandbox's question, not this module's.
    """
```

**为什么是两个函数而不是一个**：`command_words` 服务红线判定（词法边界），`path_candidates` 服务路径门（路径提取）。二者**共用同一棵树**，但回答不同问题——把它们合成一个「返回所有 token」的函数，会让调用方各自去猜哪些 token 是路径，等于把判定外泄。

**内部结构**：

```python
_LANGUAGE / _PARSER  # 模块级惰性单例（Language 构建 0.01ms、Parser 0.03ms）
def _tree(text) -> Node | None      # 解析；失败返回 None（不抛）
def _walk_commands(node) -> Iterator[Node]   # 递归所有 command 节点
def _words_of(command) -> list[str]  # command 节点的直接子节点
```

**`_words_of` 的节点取法**（实测确定）：

| 节点类型 | 取什么 | 为什么 |
|---|---|---|
| `command_name` | 其 `word` 子节点 | 命令名 |
| `word` | 文本本身 | 普通参数 |
| `number` | 文本本身 | **`777` 这类**——漏了它 `chmod 777` 的红线就失效 |
| `string` / `raw_string` | 其 `string_content` 子节点 | 引号内容（**含它才不退步**） |

**降级**（关键设计）：解析失败（`has_error=True` 或异常）时**不抛、不拒**——`command_words` 返回**正则切词的旧结果**，`path_candidates` 返回**空**（让调用方的正则路径继续工作）。理由：畸形 shell 语法不该让门失灵，也不该让门误拒。

### 3.2 与现有正则的关系（拍板②：取并集）

**路径门**（`plan_runner.outside_workspace_paths`）：

```
候选 = 正则候选 ∪ tree-sitter 候选
```

- 正则候选：现有 `_OUTSIDE_PATH_RE`（保留——它对**散文**有效，而 4 个调用点里 3 个传散文）
- tree-sitter 候选：`path_candidates(text)`（对**shell** 有效）
- 之后走**同一条**判定链：`_looks_like_path` → `Sandbox.resolve` → offender

**红线判定**（`permission_gate.bash_denied`）：

```
词 = 正则切词 ∪ tree-sitter 词（保持顺序，正则优先）
```

- 现有 `_command_words` 的正则切词**保留**（它对散文命令有效）
- tree-sitter 的词**追加**（它能抓到 `FOO=bar rm -rf /` 这种正则漏的）
- `_dangerous_recursive_root_delete` / `_world_writable_chmod` / `_reads_credential` 的**判定逻辑一字不改**——只换词表

**为什么不完全替换**（实测依据）：完全替换会让 `test -f "/data/x/app.py"` 漏判（除非额外处理 `string_content`，那样又是新逻辑）；而 `bash_denied` 是 dispatcher 与 gate **共用**的纯函数（`permission_gate.py:206` 注释：「the approval panel and the refusal can never disagree」），它的**纯**是被依赖的性质。

**`bash_denied` 的纯性如何保住**：tree-sitter 的 `Language`/`Parser` 是**进程级惰性单例**，构建后是**无状态**的（实测同一 parser 连续 parse 3 次结果一致）。`bash_denied` 仍是「无 IO、无时钟」——除了首次调用时的语言加载（一次性、可缓存）。**注释需更新**：从「Pure: no IO, no clock」改为「Pure: no IO after the grammar is loaded (once)」。

### 3.3 五个调用点各自用哪个

| 调用点 | 传入的是 | 用什么 | 为什么 |
|---|---|---|---|
| `present_plan_tool.py:195` | plan 文本 + item.content（**散文**） | 正则为主（tree-sitter 对散文只能给畸形树 → 空候选，天然等于不参与） | 散文不是 shell |
| `plan_runner.py:345` | topic（**散文**） | 同上 | 同上 |
| `plan_runner.py:360` | item.content（**散文**） | 同上 | 同上 |
| `plan_runner.py:669` | item.content（**散文**） | 同上 | 同上 |
| `acceptance.py:59` | verify（**shell**） | **tree-sitter 为主** | 唯一的 shell 入口；真机 52% 的越界路径在这里 |

**关键认识**：tree-sitter 的**唯一真正战场是 `acceptance.py`**（verify）。4 个散文调用点用不上它（散文不是 shell）。

**这与拍板②「取并集」不冲突**：并集在散文调用点上退化为「正则 ∪ 空 = 正则」，无行为变化；在 verify 上变成「正则 ∪ tree-sitter」，正是要的。

### 3.4 依赖

```toml
# pyproject.toml [project.dependencies]
# The shell grammar (W2/T): a command is a shell command, and a regular
# expression cannot tell "a; b" inside quotes from two commands.  Two
# prebuilt wheels, no compiler, py3.10-3.14.
"tree-sitter>=0.25,<0.27",
"tree-sitter-bash>=0.25,<0.26",
```

**pin 的理由**：`tree-sitter-bash` 的 `tree-sitter~=0.24` 只在 `core` extra 下 → 基础安装不绑版本，必须自己 pin；且 grammar 的 ABI 会随版本变（实测 0.25.1 → abi 15），不 pin 会漂移。

**CI**：无需改动（两个矩阵都有 manylinux2014 wheel）。

**mypy**：`tree_sitter` 带 `py.typed`；`tree_sitter_bash` 也带 → strict 可过。

---

## 4. 数据流（改前 vs 改后）

```
[改前]
verify（shell）──► outside_workspace_paths([text], ws)
                     └─► _OUTSIDE_PATH_RE 正则 ─► 候选 ─► _looks_like_path ─► Sandbox.resolve
                     （变量形态 $D / $(cat /x) / `cat /x` 全漏）

[改后]
verify（shell）──► outside_workspace_paths([text], ws)
                     ├─► 正则候选（保留：散文与形态都对）
                     └─► shell_parse.path_candidates(text)   [新：tree-sitter 语法树]
                            └─► word / number / string_content 节点
                     └─► 候选并集 ─► _looks_like_path ─► Sandbox.resolve ─► offender
```

**红线判定**：

```
[改前] bash_denied(cmd) ─► _normalize_command ─► _SHELL_SPLIT 正则切词 ─► 形状判定

[改后] bash_denied(cmd) ─► _normalize_command
                          ├─► 正则切词（保留）
                          └─► shell_parse.command_words(cmd)  [新：树里的真词]
                          └─► 词表并集 ─► 形状判定（逻辑不变）
```

---

## 5. 架构图

```
                    ┌──────────────────────────────────────────┐
                    │  ai_coding/core/shell_parse.py  （新）  │
                    │                                          │
   shell 文本 ──────►│  _PARSER（进程级惰性单例）             │
                    │    Language(tree_sitter_bash.language())  │
                    │    Parser(Language)                       │
                    │           │                              │
                    │           ▼                              │
                    │    tree.rootNode                          │
                    │      ├─► _walk_commands（递归所有 command）│
                    │      │     ├─ command_name → word        │
                    │      │     ├─ word                       │
                    │      │     ├─ number      （777 这类）   │
                    │      │     └─ string → string_content    │
                    │      │        （引号内容）                │
                    │      ▼                                   │
                    │  command_words(text)  │  path_candidates(text)│
                    └───────────┬───────────┴──────────┬────────┘
                                │                      │
        ┌───────────────────────┴───┐      ┌───────────┴──────────────┐
        ▼                           ▼      ▼                          ▼
 permission_gate             plan_runner.outside_workspace_paths
   bash_denied()               （5 个调用点：4 散文 + 1 shell）
   ├─ _SHELL_SPLIT 正则词   ┌─ 正则候选（散文有效）
   └─ command_words（新增）│  └─ path_candidates（shell 有效）
        └─ 词表并集              └─ 候选并集
             ▼                          ▼
        形状判定（不变）        _looks_like_path → Sandbox.resolve
```

---

## 6. RIPR 用例总表

| 项 | Red（先失败） | Implement | Prove | Regression |
|---|---|---|---|---|
| **T-a** 词边界：变量前缀不改变命令 | `tests/test_shell_parse.py::test_a_variable_prefix_does_not_hide_the_command` | §3.1 `_words_of` | `pytest tests/test_shell_parse.py -q` | 全量 |
| **T-b** 词边界：`number` 节点 | `tests/test_shell_parse.py::test_a_number_is_a_word`（`chmod 777` → 词含 `777`） | §3.1 | 同上 | 全量 |
| **T-c** 词边界：引号内容是一个词 | `tests/test_shell_parse.py::test_a_quoted_phrase_is_one_word`（`echo "a; b"` → 1 个词不是 2 个） | §3.1 | 同上 | 全量 |
| **T-d** 词边界：命令替换里的命令 | `tests/test_shell_parse.py::test_a_command_inside_a_substitution_is_found` | §3.1 `_walk_commands` 递归 | 同上 | 全量 |
| **T-e** 路径：变量赋值里的路径 | `tests/test_shell_parse.py::test_a_path_in_a_variable_assignment_is_found` | §3.1 `path_candidates` | 同上 | 全量 |
| **T-f** 路径：命令替换里的路径 | `tests/test_shell_parse.py::test_a_path_in_a_command_substitution_is_found` | §3.1 | 同上 | 全量 |
| **T-g** 路径：引号里的路径 | `tests/test_shell_parse.py::test_a_path_in_quotes_is_found` | §3.1 | 同上 | 全量 |
| **T-h** 降级：畸形语法不抛 | `tests/test_shell_parse.py::test_a_broken_command_does_not_raise` | §3.1 `_tree` 的 None 路径 | 同上 | 全量 |
| **T-i** 红线：变量前缀的红线仍拦得住 | `tests/test_permission_gate.py::test_a_red_line_survives_a_variable_prefix` | §3.2 词表并集 | `pytest tests/test_permission_gate.py -q` | 全量 |
| **T-j** 红线：`chmod 777` 不因解析方式而失效 | `tests/test_permission_gate.py::test_chmod_777_is_still_a_red_line` | §3.1 `number` 节点 | 同上 | 全量 |
| **T-k** 路径门：verify 里的变量路径被拦 | `tests/test_acceptance.py::test_a_proof_command_reaching_outside_through_a_variable_asks` | §3.3 `acceptance.py` | `pytest tests/test_acceptance.py -q` | 全量 |
| **T-l** 路径门：既有判定不退步 | `tests/test_plan_workspace_gate.py` 既有 14 例**零改动** | §3.2 并集 | `pytest tests/test_plan_workspace_gate.py -q` | 全量 |
| **T-m** 红线专项矩阵 | `tests/test_permission_gate.py::test_the_red_lines_agree_under_both_word_lists`（参数化：引号/注释/变量前缀/命令替换/管道/重定向/环境变量赋值，每个形态一该拦一不该拦） | §3.2 词表并集 | `pytest tests/test_permission_gate.py -q` | 全量 |

**用例总数**：新 13 条（`test_shell_parse.py` 8 条 + `test_permission_gate.py` 3 条含 T-m 参数化 + `test_acceptance.py` 1 条 + `test_plan_workspace_gate.py` 1 条核对），**既有测试零改动**。

**每条 Red 的具体断言**（实施时照抄）：

```python
# T-a
def test_a_variable_prefix_does_not_hide_the_command():
    assert shell_parse.command_words("FOO=bar rm -rf /") == ["rm", "-rf", "/"]

# T-b
def test_a_number_is_a_word():
    assert "777" in shell_parse.command_words("chmod 777 /tmp/x")

# T-c
def test_a_quoted_phrase_is_one_word():
    assert shell_parse.command_words('echo "a; b"') == ["echo", "a; b"]

# T-d
def test_a_command_inside_a_substitution_is_found():
    words = shell_parse.command_words("cat $(rm -rf /)")
    assert "rm" in words and "/" in words

# T-e
def test_a_path_in_a_variable_assignment_is_found():
    assert "/data/x" in shell_parse.path_candidates("D=/data/x; rm -rf $D")

# T-f
def test_a_path_in_a_command_substitution_is_found():
    assert "/data/secret" in shell_parse.path_candidates("echo $(cat /data/secret)")

# T-g
def test_a_path_in_quotes_is_found():
    assert "/data/x/app.py" in shell_parse.path_candidates('test -f "/data/x/app.py"')

# T-h
def test_a_broken_command_does_not_raise():
    assert shell_parse.command_words('echo "unterminated') == []   # 降级：空
    assert shell_parse.path_candidates('echo "unterminated /data/x') == []
```

---

## 7. 实施顺序与提交切片

| 序 | 提交 | 内容 | RIPR |
|---|---|---|---|
| 1 | `feat(shell): a command's paths come from the shell grammar` | 新模块 `core/shell_parse.py` + 依赖 + `acceptance.py` 走 `path_candidates` | T-e~T-h, T-k, T-l |
| 2 | `feat(shell): the red lines read the command's real words` | `permission_gate` 词表并集 + **红线专项回归矩阵** | T-a~T-d, T-i, T-j, **T-m** |

**切分的理由**（实测，§2.3）：路径提取的净增量是 **4 类形态**且直击真机数据（越界路径 52% 在 verify 里）；词法边界的净增量是 **1 个红线用例**（`FOO=bar rm -rf /`）而代价是 `bash_denied` 从纯函数变成有状态。先把高收益那块做对，红线那块单独评估、单独验证。

**T-m（提交 2 专项）**：红线在两套词边界下结论一致的回归矩阵——覆盖引号 / 注释 / 变量前缀 / 命令替换 / 管道 / 重定向 / 环境变量赋值，每个形态既有「该拦的」也有「不该拦的」，用测试钉住红线判定**不退步**。

**每步 DoD**：目标套件绿 → 全量 `pytest` → `ruff check ai_coding tests` → `mypy ai_coding` → 提交。

**基线（2026-10-20 实测）**：后端 `3189 passed, 6 skipped`；前端 `405 pass / 0 fail`。

---

## 8. 自核（模拟走查）

1. **verify 是 `test -f PLAN_DEMO.md`**（真机 r19 样本）→ 正则候选空、tree-sitter 候选空 → 不需要人批 ✔（真机 r19 plan_mode 已 PASS 验证了这条路径不回归）。
2. **verify 是 `D=/data/x; rm -rf $D`** → tree-sitter 抓到 `/data/x` → 越界 → 要人批 ✔（原来漏）。
3. **verify 是 `chmod 777 /tmp/x`** → `bash_denied` 命中（`number` 节点）→ 直接拒 ✔（原来也拦，改动不退步）。
4. **verify 是 `echo "unterminated`** → 解析失败 → 降级为空 → 走正则的旧行为 ✔（不抛、不误拒）。
5. **plan 文本是散文**（`确保目录 /data/w2demo/ 存在`）→ tree-sitter 解析出畸形树 → 候选空 → 只有正则生效 ✔（行为不变，既有 14 例零改动）。
6. **`bash_denied` 仍是纯函数**：语言只加载一次（进程级单例），之后无 IO ✔（`permission_gate.py:206` 的注释需相应更新）。
7. **红线判定逻辑一字未改**：只是词表变长 → 判定结论只可能**变多**（多拦），不会变少（除 `FOO=bar` 这类正则漏的）→ 方向是 fail-safe ✔。
8. **性能**：tree-sitter 解析 0.006 ms/条，比现有正则扫描（0.039 ms/条，含 Sandbox 构造）更快 ✔。
9. **冗余审查**：新模块只有一个职责（解析），两个函数各自有明确消费方（`command_words` → 红线；`path_candidates` → 路径门）；不引入第二套判别式（`_looks_like_path` 与 `Sandbox.resolve` 复用）；不重复 opencode 的 `ARITY` 表（本仓不需要「always」的资源粒度）。

---

## 9. 非目标与已知边界

### 9.1 非目标

- 不做 PowerShell（本仓 bash 工具仅面向 Linux，`README.md:13`）。
- 不做「always」授权的资源粒度（opencode 的 `ARITY` 表 + `save` 字段，110+ 条命令的知识）——本仓 bash 永不记忆（`permission_gate.py:232` 的 `_NEVER_REMEMBERED`），没有这个需求。
- 不提取 `rm`/`mv` 的目标（opencode 也不提取：`parse.ts:14` 的 `CWD` 集合只有 cd 家族）——理由同 opencode：rm/mv 的路径语义无法从语法可靠推出。
- 不改 `_looks_like_path` 的判别式（三条判别式在 W2 已定并被真机验证过）。
- 不做 opencode 的 `portable scanner`（1500 行手写 TS）——那是 opencode 从 tree-sitter **迁走**的产物，方向与我们相反。

### 9.2 已知边界

| 边界 | 性质 | 处置 |
|---|---|---|
| 变量引用**本身**（`$D` 展开后是什么）仍不可知 | 漏判（不可解） | tree-sitter 抓到的是**赋值处**的路径（`D=/data/x`），不是展开值。opencode 同样放弃（`expandKnownDirectory` 遇 `$` 即返回 undefined） |
| `~user`（别人的家目录）不展开 | 漏判 | opencode 同样不展开（`expandKnownDirectory` 只认 `~` 与 `~/`） |
| 散文调用点用不上 tree-sitter | 范围限制 | 散文不是 shell，解析只会给畸形树。tree-sitter 的唯一战场是 verify |
| tree-sitter 的 grammar 版本会漂 | 维护 | 已 pin（`tree-sitter>=0.25,<0.27` / `tree-sitter-bash>=0.25,<0.26`） |
| `bash_denied` 不再严格「无 IO」 | 纯性 | 语言只加载一次（进程级单例），之后无 IO；注释需更新 |

---

## 10. 实施记录（2026-10-20）

### 10.1 提交

| 提交 | 内容 | 门禁 |
|---|---|---|
| `0309566` | 路径提取：`shell_parse.py` + `outside_workspace_paths(from_shell=True)` | 目标 54 passed；全量 3216 passed |
| `a57d55f` | 红线重写：`security/bash_redlines.py` + `command_views` 读层 | 全量 3303 passed |
| `69c1d41` | 降级路径专项 | 全量 3307 passed |

**终验门禁**：后端 `3307 passed, 6 skipped`；`ruff check ai_coding tests` 全绿；`mypy ai_coding` 143 文件无问题；`scripts/r19_acceptance.py --only plan_mode` 真模型 PASS（`receipts=2`，verify 照常跑通）。

### 10.2 实施期的三处修正

**① 模块位置**。`shell_parse` 最初放在 `ai_coding/core/`，但 `core/__init__.py` 导入 `AgentLoop` → `agent_loop` → `acceptance` → `permission_gate` → `shell_parse` 形成**循环导入**。移到顶层 `ai_coding/shell_parse.py`（与 `attachments.py`、`paths.py` 同级：跨包共享的无状态读取层），循环解除。

**② 判别式的适用范围**。拍板②「取并集」实施后发现：tree-sitter 候选若也过 `_looks_like_path`（形态启发），**本次改动的核心价值全漏**（`D=/data/x`、`$(cat /data/secret)`、`` `cat /data/secret` ``、`mkdir -p /data/w2demo` 四类真目标全被形态启发否决）。实测两者在两个场景作用相反：形态启发在散文里是「防误报」，在 shell 里是「漏真目标」。故按调用点区分（`from_shell`）：命令里的词由语法树确认、跳过形态启发；散文照旧走形态启发。

**③ 红线改造的净收益为 0**（拍板③ 的前提被实测证伪）。26 个形态的专项矩阵跑完：tree-sitter 改变词形判定的只有 2 个（`FOO=bar rm -rf /`、`x=1; rm -rf /`），而这两个**都被子串表提前拦下** → `bash_denied` 的最终结论 0 个改变。重新调研后发现真正的结构缺陷不在匹配方式，而在**判定单位**：词形判定看的是**扁平词表的首词**，只对第一条命令生效（`a && rm -rf /` 里的 rm 根本不在首位）。于是改为**每个 command 节点各判一次**，并把子串表改为只匹配 command 节点 —— 拍板④由此而来。

### 10.3 安全审查（红线重写的影响面）

75 个真实命令的比对（改动前 `818c276` vs 改动后），**8 个判定变化，全部落在拍板方向**：

| 方向 | 命令 | 说明 |
|---|---|---|
| **收紧** 3 个 | `dd of=/dev/sda if=/dev/zero` | 写盘（原来因参数顺序而漏） |
| | `echo x >/dev/sda` | 裸盘写入（原来因缺空格而漏） |
| | `git -C /tmp push --force` | 强推（原来因选项在前而漏） |
| **放宽** 5 个 | `rm -rf /tmp/build` | 拍板 A：目标非系统目录 |
| | `dd if=/dev/zero of=/tmp/out.bin` | 拍板 C：非块设备 |
| | `mkfsdoc --help` | 程序名有边界 |
| | `echo shutdown` / `echo sudo ls` | 拍板 B：只是文本 |

**关键红线逐条核对：32/32 全部拦住，0 漏**（删根 / 删系统目录 / 全局可写 / 提权 / 关机 / 重启 / 建文件系统 / 写盘 / 裸盘重定向 / 强推 / 凭据 / 位置无关 / 变量前缀 / 命令替换）。

### 10.4 新的模块边界

```
ai_coding/shell_parse.py          读层：语法树 → 词 / 路径候选 / command 视图
ai_coding/security/bash_redlines.py  规则层：command 视图 → 红线理由
ai_coding/security/permission_gate.py  入口：bash_denied（新规则 + 降级到旧表）
```

**降级**：语法读不完整时退回旧的文本判定（子串表 + 扁平词表）。保守方向——含红线的畸形命令仍被拒，不含的只是被问一句。

### 10.5 残留

| 残留 | 性质 | 处置 |
|---|---|---|
| `find -exec rm -rf {} \;` / `xargs rm -rf` 里的 rm 落在**参数位置**，不是 command 节点 | 漏判 | tree-sitter 的结构如此；要读它需要知道每个命令的参数语义（opencode 的 `ARITY` 表思路），本批不做 |
| `git push --force-with-lease` | 现状拦、现状也拦 | 带保护的强推，严格说该放行；本批未改，登记 |
| `chmod 666` / `0666` / `o+w` | 现状放行 | 「全局可写」不止 777；本批未扩，登记 |
| 变量引用**本身**（`$D` 展开后是什么） | 漏判（不可解） | tree-sitter 抓到的是赋值处的路径；opencode 同样放弃 |
| `BashPolicyGuard`（用户配置的 `bash_deny`）仍是子串匹配 | 另一套系统 | 与主线红线是两件事，本批未动 |
| `bash_denied` 不再严格「无 IO」 | 纯性 | 语言只加载一次（进程级单例），之后无 IO；注释已更新 |
