# 配置信任（第二层）：外发 / 越权 / 越界路径

> 来源：N-4（项目配置不被信任授予执行）的「未覆盖（另立项）」清单 + 用户点名的 7 项。
> N-4 解决了**执行类**（`hooks`/`mcp` 整键丢弃）。本轮解决**外发类**、**越权类**、
> **越界路径类** —— 它们与执行类**修法不同**，不能靠"丢整个键"。
>
> 本稿为**第 2 版**，已按子智能体方案评审（§八）修订。

---

## 一、口径：三类语义，三条不同的修法

在某个目录里跑 `ai-coding`，操作者隐含的是：**信任该目录作为工作区、信任其中的代码**。
它**没有**隐含"信任其配置去把请求发到别处 / 读写工作区外的文件 / 扩大授权边界"。

| 类 | 语义 | 修法 | 本方案涉及 |
|---|---|---|---|
| **执行类** | 项目层**不得授予** | 整键丢弃 | N-4 已做（`hooks`/`mcp`） |
| **外发/秘密类** | 项目层**不得授予端点与凭据** | **按成员丢** | `models.*.base_url`/`api_key`、`ai.websearch_api_key` |
| **越权类** | 项目层**只能缩小**授权 | **取交集** | `tools.extra_workspace_roots` |
| **越界路径类** | 项目层**不得越出工作区** | 丢弃 + 消费侧护栏 | `tracing.dir`、`memory.embeddings.cache_path` |

**共同根因**（与 N-4 同一条）：[loader.py](../ai_coding/config/loader.py) 的 `_overlay`
深合并、项目层逐键胜出，而 `config.example.yaml` 是**仓库里被跟踪的文件**且是 project
层候选（[loader.py:95-99](../ai_coding/config/loader.py#L95-L99) `DEFAULT_NAMES`）。

> **活样本**：本仓库自己的 [config.example.yaml:5-14](../config.example.yaml#L5-L14) 就
> ship 了 `models.deepseek-chat.base_url` + `api_key` + `default_model`。它正是 project
> 层候选 —— 等于仓库自带一个缺口 1 的实例。

---

## 二、调研结论：7 项逐项

（两轮子智能体调研 + 逐条回代码核实；行号均为调研时 HEAD）

### 缺口 1 · `models[].base_url` / `api_key` —— ❌ 未覆盖（最高危）

* **攻击面**：`_overlay` 是**深合并**，项目层**只写 `base_url` 一个键，`api_key` 仍继承
  用户层的真实密钥** → 用户真实 LLM key + 整段对话（prompt / 工具结果 / 记忆文本）被
  发往攻击者端点。
* **为什么覆盖 `name`/`max_tokens`/`temperature` 不构成外发**：`name` 是**发给谁**的
  模型名字符串，覆盖它只是让请求到**用户自己的**端点时声称另一个模型；`max_tokens`/
  `temperature` 是行为旋钮 —— "项目能决定怎么干"是分层配置的本意，两者都不改变**目的地**
  也不引入**凭据**。所以它们留在项目层。
* **消费点**：
  * [openai_engine.py:296-298](../ai_coding/ai/openai_engine.py#L296-L298) `AsyncOpenAI(base_url=…, api_key=…)`
  * [llm_helpers.py:63-67](../ai_coding/ai/llm_helpers.py#L63-L67) / [:98-102](../ai_coding/ai/llm_helpers.py#L98-L102) summarizer / extractor 的独立客户端
  * [memory/embeddings.py:159-163](../ai_coding/memory/embeddings.py#L159-L163) 语义档复用同模型的端点与凭据
* **现有防线**：**无**。`ModelConfig` 是裸 dataclass，`base_url` 无 scheme/白名单校验。
* **修法**：见 §三·A。

### 缺口 2 · `tools.extra_workspace_roots` —— ❌ 未覆盖（沙箱全绕过）

* **攻击面**：项目层写 `["/"]`（或 `/etc`、`/home`）。[builder.py:24-55](../ai_coding/tools/builder.py#L24-L55)
  的 `expand_root_patterns` **只校验 `is_dir()`**，不做包含性检查 → 根变成 `/`；
  [sandbox.py:70-101](../ai_coding/security/sandbox.py#L70-L101) 的 `_contained_in` 对 `/`
  下任意路径恒真 → **read/write/edit/glob/grep 对整台机器免问读写**。
* **消费点**：[builder.py:179-185](../ai_coding/tools/builder.py#L179-L185) `Sandbox(extra_roots=…)`；
  [cli.py:87-97](../ai_coding/cli.py#L87-L97) `_extra_roots`；[cli.py:225](../ai_coding/cli.py#L225) `AgentLoop`；
  [tools/base.py:72](../ai_coding/tools/base.py#L72)；[present_plan_tool.py:229-248](../ai_coding/tools/present_plan_tool.py#L229-L248)。
* **现有防线**：`resolve()` + `is_dir()`；`Sandbox` 对 extra roots **视为已授权、不再询问**。
* **修法**：见 §三·B（取交集）。

### 缺口 3 · `ai.websearch_api_key` —— ❌ 未覆盖

* **攻击面**：项目层可覆盖该键。但 [websearch_tool.py:23](../ai_coding/tools/websearch_tool.py#L23)
  的 `_SEARCH_URL` **硬编码** `https://api.tavily.com/search`，**无法重定向**。残余风险是
  "不可信层决定了一个秘密字段"（操作者真实 key 不再被使用），而非端点劫持。
* **消费点**：[builder.py:210-214](../ai_coding/tools/builder.py#L210-L214) → [websearch_tool.py:39-107](../ai_coding/tools/websearch_tool.py#L39-L107)。
* **修法**：与 `models.*.api_key` 同族 —— 项目层丢弃（用户层保留）。工具在无 key 时自解释，
  丢弃不会让功能不可诊断。

### 缺口 4 · `tracing.dir` —— ❌ 未覆盖（任意路径写 + `prune` 任意删）

* **攻击面**：项目层写 `tracing.dir: /home/user`（任意绝对路径）→
  1. **任意路径写**：[cli.py:236-237](../ai_coding/cli.py#L236-L237) `root = tracing.dir or default_root`
     → [trace.py:261-272](../ai_coding/core/trace.py#L261-L272) `begin_turn` 会 `mkdir(parents=True)` 并写入；
  2. **任意删除（更严重）**：[trace.py:339-358](../ai_coding/core/trace.py#L339-L358) `prune()`
     遍历 `root` 下**每个子目录**，凡 mtime 早于保留期就 `rglob("*")` 删文件 + `rmdir`。
     `root = /home/user` → **用户家目录下所有"旧"子目录被递归删除**。
* **消费点**：[cli.py:229-276](../ai_coding/cli.py#L229-L276) `_make_recorder` → `TraceStore`。
* **修法**：见 §三·C（丢弃）+ §三·D1（`prune` 护栏）。

### 缺口 5 · `memory.embeddings.cache_path` —— ❌ 未覆盖（任意文件写/读）

* **攻击面**：[cli.py:338-342](../ai_coding/cli.py#L338-L342) 对**相对**路径拼到 workspace（安全），
  但**绝对路径原样使用**。项目层写 `/home/user/.bashrc` →
  [embeddings.py:191-196](../ai_coding/memory/embeddings.py#L191-L196) `_save` 覆写成 JSON；
  [embeddings.py:174-189](../ai_coding/memory/embeddings.py#L174-L189) `_load` 还会**读取**任意路径。
* **消费点**：[cli.py:318-342](../ai_coding/cli.py#L318-L342) `_embeddings_for` → [embeddings.py:96](../ai_coding/memory/embeddings.py#L96) `Path(cache_path)`。
* **修法**：见 §三·C + §三·D2。

### 缺口 6 · `hooks.plugin_dir` —— ⚠️ 项目层已丢，**默认值仍开放**（残余，非三类之一）

* **N-4 覆盖情况**：`hooks` 是 `EXECUTION_KEYS`，项目层的 `plugin_dir` 连同 `events` 一起被丢。
* **残余**：[cli.py:295-297](../ai_coding/cli.py#L295-L297) 的**默认值**是
  `<workspace>/.ai-coding/hooks` —— **位于不可信仓库树内**。操作者只要在**用户层**写了
  `kind: python, module: X`，[hooks.py:358-370](../ai_coding/core/hooks.py#L358-L370) 就会
  `exec_module(<workspace>/.ai-coding/hooks/X.py)` —— **执行的是仓库里的文件**。
* **与三类的关系**：这一条的向量是"**用户层配置 + 不可信仓库文件**"，与 N-4 的"项目层授予"
  不是同一个，所以它不属于"外发/越权/越界"任一语义类；修法是**改默认值**，不是过滤。
* **修法**：见 §三·E。

### 缺口 7 · `mcp.servers[].headers` —— ⚠️ 项目层已丢，**残余 token 复用**（残余，非三类之一）

* **N-4 覆盖情况**：`mcp` 是 `EXECUTION_KEYS`，项目层的 `headers`/`url`/`command` 整键被丢；
  sidecar 锚点也已固定到用户层（N-4 的 P1-1 修复）。**主向量已关闭。**
* **残余**：[mcp.py:523-583](../ai_coding/core/mcp.py#L523-L583) 的 `_TokenStore` 以
  **`server.name`** 为键存 `~/.local/share/ai-coding/mcp-auth.json`（0600）。
  **同名但不同 `url` 的两个 server 会命中同一个 token 条目** —— 为 `https://a` 签发的
  OAuth token 会被呈给 `https://b`。
* **与三类的关系**：这是**用户层语义内**的键设计缺陷（两个用户自己定义的 server 撞名），
  同样不属于三类；修法是**把端点并入键**。
* **修法**：见 §三·F。

---

## 三、方案：逐项实现

### A. 外发/秘密类 —— 按成员丢（`loader.py`）

在 `EXECUTION_KEYS` 旁边新增**成员级**清单，并在 `load_layers_report` 的同一处
（`_normalize` 之后、`_overlay` 之前）对项目层生效：

```python
#: Members that decide *where* a request goes or *which secret* it carries.
#: Same reasoning as ``EXECUTION_KEYS`` -- the operator trusts the directory as a
#: workspace, not its config -- one level finer: the block stays (a project may
#: still tune how the agent behaves), only the destination and the credential are
#: withheld.
#:
#: ``models`` is the one block whose *entries* are also filtered: see
#: ``_filter_project_document``.
PROJECT_FORBIDDEN_MEMBERS: dict[str, frozenset[str]] = {
    "ai": frozenset({"websearch_api_key"}),
    "tracing": frozenset({"dir"}),
}
#: One level deeper: ``memory.embeddings.cache_path``.
PROJECT_FORBIDDEN_NESTED: dict[str, dict[str, frozenset[str]]] = {
    "memory": {"embeddings": frozenset({"cache_path"})},
}
#: ``models`` members that decide the destination.  Applied per entry, after the
#: entry itself has survived the "must be user-defined" rule.
MODEL_DESTINATION_MEMBERS: frozenset[str] = frozenset({"base_url", "api_key"})
```

**`models` 的过滤规则**（评审 P0-3 后收敛出的精确版本）：

0. **项目层不得把用户层的 mapping 换成非 mapping**（评审 NP-3，与 P0-1 同根因）。
   项目层写 `models:`（YAML 解析为 `None`）时，`_overlay` 的 else 分支
   （[loader.py:248](../ai_coding/config/loader.py#L248)）会把用户层的 dict
   **覆盖成 `None`** → 用户所有模型消失、启动即 `no default model configured`。
   `tools: null` 同理（直接校验失败）。规则：**用户层在某顶层键上是 dict、
   而项目层在该键上不是 dict → 丢掉项目层的值**。这是"只能收紧、不能移除"
   在块级上的同一原则。
1. 用户层 `models` 里**没有**的条目 → 项目层的该条目**整条丢弃**。
   *理由*：一个模型条目只有带上 `base_url` 才可用，而项目层**拿不到** `base_url`
   （下一条要丢的就是它）。本地/Ollama 这类无需 `api_key` 的模型也一样 —— 它们需要
   `base_url`，而项目层不能设。所以项目层新增的条目**必然是空壳**；与其留一个
   "选了就响亮失败"的假模型，不如整条丢掉。
   *行为变更*：**项目层从此不能新增模型**（除非 `AI_CODING_TRUST_PROJECT=1`）。
2. 用户层**已有**的条目 → 只丢 `base_url` / `api_key` 两个成员，其余
   （`name`/`max_tokens`/`temperature`/`timeout`/`context_window`/价格/…）保留生效。
3. `default_model`：**仅当它指向"项目层自己新增的模型"时丢弃**。
   *理由*：这是唯一会让它变成**悬空引用**的情形（那个条目已被规则 1 丢掉）。
   指向用户已定义的模型是**安全**的（同一个端点、同一个凭据，项目只是换了个选择），
   应当保留 —— 这也是分层配置"更近的一层胜出"的本意。
   *为什么不是"必须指向用户已定义的模型"*：那更严的读法会**让 5 条既有用例变红**，
   其中 `test_a_malformed_global_file_does_not_stop_the_project_from_loading` 的立意
   是"坏的用户层不该让项目起不来"，而"必须指向用户模型"在用户层坏掉时会把项目的
   选择一并丢掉 —— 与立意直接冲突。且**安全性完全相同**：端点与凭据都已被规则 2 锁死，
   项目层指向任何名字都不可能把流量重定向出去。

过滤函数（与 N-4 的 `EXECUTION_KEYS` 过滤同形，返回被丢弃的名字供一次性 warning）：

```python
def _filter_project_document(
    document: dict[str, Any], user_document: dict[str, Any], *, workspace: Path
) -> tuple[dict[str, Any], list[str]]:
    """Strip from a project document the members a repository may not grant.

    Returns the filtered document and the dotted names dropped, so the caller can
    say so once instead of silently changing behaviour.
    """
```

> `workspace` 进签名（评审 NP-6）：`extra_workspace_roots` 的交集要展开 pattern，
> 展开要 `base`。

### B. 越权类 —— `extra_workspace_roots` 取交集（`loader.py` + `builder.py`）

**语义**（评审 P0-1/P0-2 后定稿，三处一致）：

| 项目层声明 | 有效根 |
|---|---|
| 缺省 / 空列表 | **用户层原样**（不收紧） |
| 非空 | **{用户层展开后的根 ∩ 被项目层声明覆盖的根}** |

第三条是关键：**有效集 = "用户层已经授权的、且被项目层声明覆盖的那些目录"**。
用"覆盖"而不是"相等"，因为项目层写的 `["/"]` **包含**用户的所有根 → 覆盖全部 →
有效集 = 用户层原样（**无法把机器塞进来**）；写 `["/home/me/projects/a"]` 只覆盖 `a`
→ 有效集 = `{a}`（**收紧**）；写 `["/etc"]` 不覆盖任何用户根 → 有效集 = ∅（收紧到无）。

* 项目层声明**展开后为空**时**必须把该键从项目 document 里删掉**，让 `_overlay` 的
  list-replace 保留用户层的值 —— 否则 `config.example.yaml:134` 的
  `extra_workspace_roots: []` 会把**每个仓库**的操作者授权清空（评审 P0-1）。
  *判据必须用"展开后为空"而不是"原始列表为空"*（评审 NP-7）：`expand_root_patterns`
  会把 `""`、`"  "`、匹配不到的 pattern 都丢光，所以项目写 `[""]` 或 `["/nope/*"]`
  同样能清空授权 —— 那是同一个后门。

**为什么必须在展开后取交集**：`extra_workspace_roots` 支持 glob
（`/home/me/projects/*`），两个 pattern 集合的"交集"没有定义；必须先展开成具体目录再交。
展开发生在 `expand_root_patterns`，所以交集也只能在那里算。

**实现**：给 `expand_root_patterns` 加一个 `base` 参数（相对 pattern 相对谁解析），
然后在 `loader.py` 里用**惰性 import** 调它（避免 `config` → `tools.builder` 的导入期
环；`cli.py:94` 已经是这个写法）：

```python
def expand_root_patterns(
    patterns: Any, *, base: Path | None = None
) -> tuple[Path, ...]:
    """...  ``base`` resolves a relative entry against the workspace instead of
    the process CWD -- the CWD is not the workspace when the app is launched from
    elsewhere, and a relative root silently following ``cd`` is the same class of
    bug the embedding cache used to have (paths.py docstring)."""
```

```python
def _narrow_roots(
    project_patterns: Any, user_patterns: Any, *, workspace: Path
) -> list[str]:
    """Keep the operator's roots that the project's declaration covers.

    "A project may narrow the operator's grant, never widen it."  Computed on the
    expanded directories, because two pattern sets have no intersection of their
    own.  An empty project declaration is handled by the caller removing the key
    outright, so it never reaches here.
    """
```

* `base` 在**所有**调用点生效（评审 P1-3）：`loader.py`（交集）、`cli.py::_extra_roots`、
  `builder.py:181`。否则"项目层没声明"时用户层的相对 pattern 依旧跟随 `cd`。
* **返回形态**：具体目录字符串。`extra_workspace_roots` 本来就接受无通配符的纯路径
  （`expand_root_patterns` 对它是"原样解析"），所以具体目录**在既有契约之内**；
  `cli.py` 再跑一次 `expand_root_patterns` 对具体目录是**幂等**的。

> **取舍（写入文档）**：交集的"项目层声明非空但一个都不覆盖"→ 有效集为空（收紧到无）。
> 这是**保守**的一侧：宁可不给，不给错。
>
> **边界（评审 NP-8）**：上表的"收紧到 `{a}`"要求**用户根恰好是 `a` 或 `a` 的子集**。
> 若用户根是**父目录** `/home/me/projects` 而项目写 `a`，则 `U ⊄ P` → 有效集 = ∅。
> 也就是说：**项目层能"选中"用户已授权的目录，但不能把授权收窄到该目录的子目录**
> （除非用户当初就是按子目录授权的）。这是覆盖语义的必然结果，不是缺陷 ——
> 但它意味着"monorepo 里每个子项目收窄到自己"需要**用户层本身就按子目录授权**。

### C. 越界路径类 —— 项目层丢弃（`loader.py`）

`tracing.dir` 与 `memory.embeddings.cache_path` 走 `PROJECT_FORBIDDEN_MEMBERS` /
`PROJECT_FORBIDDEN_NESTED`，项目层一律丢弃，回落到默认（都在工作区内）。

**为什么是丢弃而不是钳制**：这两者的默认值**本来就**在工作区内
（`tracing.dir` 空 = `<workspace>/.ai-coding/traces`；`cache_path` 空 =
`paths.memory/embeddings.json`）。项目层唯一能做的事就是**指出去**，而那正是要禁止的。

### D. 越界路径类 —— 消费侧护栏（纵深防御）

分层过滤是根因修法，但 `prune()` 这类"即使配置可信也危险"的消费侧需要自己的护栏 ——
用户拍板选了"分层过滤 + 消费侧纵深防御"。

**D1 · `TraceStore.prune` 不得删到工作区外**（[trace.py](../ai_coding/core/trace.py)）

`TraceStore` 新增**可选** `workspace`；`prune` 在 `root` 不位于 `workspace` 内时
**直接返回 0 并记 warning**，不删任何东西。

```python
def prune(self, now: float | None = None) -> int:
    """Delete session directories whose last write is past retention.

    Refuses to run at all when ``root`` is outside ``workspace``: this walks and
    deletes whatever is under ``root``, so a ``root`` of ``/home/me`` would take
    the user's home with it.  The check is the store's own, not its caller's --
    a trusted config is not the only way to reach this code.
    """
```

* **`workspace=None`（默认）时拒删**（fail-closed）：无法证明安全就不删。
  代价是**必须更新全部构造点**（评审 P1-1），已在 §四 列出。
* **`root` 必须 `resolve()` 后再比较**（评审 P1-2）：`TraceStore.__init__` 现在只做
  `Path(root)`；若 `root` 是工作区内的符号链接指向 `/home/me`，词法包含检查会通过，
  但 `rglob`/`unlink`/`rmdir` 落到外部。全仓约定是"先 resolve 再比较"（`Sandbox`、
  `Paths`），这里对齐。

**D2 · 嵌入缓存路径必须在工作区内**（[cli.py](../ai_coding/cli.py)）

`_embeddings_for` 解析出 `target` 后，若它**不在** `paths.workspace` 之内 → 回落到
`paths.memory / "embeddings.json"` 并记 warning。

* **必须 `resolve()` 后再比较**（评审 NP-2）：[cli.py:338-342](../ai_coding/cli.py#L338-L342)
  是词法拼接，`_save`/`_load`（[embeddings.py:191-196](../ai_coding/memory/embeddings.py#L191-L196)
  / [:174-189](../ai_coding/memory/embeddings.py#L174-L189)）也不过符号链接。于是
  `cache_path` 指向**工作区内的符号链接**时，词法包含检查通过，写/读落到工作区外。
  D1 已把符号链接当绕过手段，D2 必须对齐（全仓约定：先 `resolve` 再比较）。
* `paths.memory` 就是 `<workspace>/.memory`，**本就在工作区内**，所以包含检查只需
  对 `paths.workspace`（评审 NP-2 顺带）。

* **作用于所有层**（评审 P1-5）：护栏是"消费侧鲁棒性"，不是分层过滤 —— 它防的是
  "将来某个调用方把不可信路径递进来"，那时它并不知道来源层。代价是操作者也不能把
  缓存放到工作区外的盘上；缓存按代码注释是"pure accelerator; losing it only costs
  recompute"，所以这个代价很低。**已列入 §4.2 的行为变更。**
* **为什么护栏放在 `cli.py` 而不是 `EmbeddingIndex` 里**：`EmbeddingIndex` 也被测试与
  `scripts/` 直接构造（传绝对路径是它们的正常用法），在类里拒绝会砸掉这些调用方。
  `cli.py` 是"配置 → 实例"的组合根，也是唯一知道 workspace 的地方。

### E. `hooks.plugin_dir` 默认目录移到用户级（[cli.py:295-297](../ai_coding/cli.py#L295-L297)）

```python
    plugin_dir = getattr(config, "plugin_dir", "") or str(
        config_home() / "hooks"
    )
```

* 用户级目录（`~/.config/ai-coding/hooks`）与 MCP token、grants 同一层 —— 与
  `paths.py` 的"user-global tier"一致（人的东西跟着机器走，不跟着仓库走）。
* **行为变更**：原先把 hook 放在 `<workspace>/.ai-coding/hooks/` 的操作者，需要显式写
  `hooks.plugin_dir`（用户层）指向它。这是**有意的**：那个位置本身就是缺口 6 的成因。
* `Paths.hooks`（`<workspace>/.ai-coding/hooks`）在生产代码里**没有消费者**
  （`tests/test_paths.py:74` 引用了它，是测试不是生产），本方案不动它。

### F. MCP token 键加入端点（[mcp.py:523-583](../ai_coding/core/mcp.py#L523-L583)）

`_TokenStore` 保持"以字符串为键"的签名不变（`tests/test_mcp_oauth.py` 用
`_TokenStore("remote-one")` 直接测存取机制，不该被密钥推导绑住）；**新增**一个推导函数：

```python
def _token_key(server: Any) -> str:
    """The token-store key for one server: name *and* endpoint.

    Keyed by name alone, two servers that share a name but point at different
    endpoints hand one's OAuth token to the other -- a token minted for
    ``https://a`` presented to ``https://b``.  The endpoint is what the token is
    actually valid for, so it is part of the identity.
    """
    return f"{server.name}\x00{server.url}"
```

* 两处调用点同步：[mcp.py:785](../ai_coding/core/mcp.py#L785) `_TokenStore(_token_key(server))`、
  [mcp.py:1338](../ai_coding/core/mcp.py#L1338) `_TokenStore(_token_key(server)).clear()`
  （该处上方已经 `next(...)` 拿到了 server 对象）。
* **行为变更**：磁盘上的键变了 → 既有 token 失效，用户需**重新登录一次**。这是可接受的
  一次性成本（token 文件是 secret，不是配置）。

---

## 四、逐文件改动清单

| # | 文件 | 改动 | 为什么 |
|---|---|---|---|
| 1 | `ai_coding/config/loader.py` | 新增 `PROJECT_FORBIDDEN_MEMBERS` / `PROJECT_FORBIDDEN_NESTED` / `MODEL_DESTINATION_MEMBERS` / `_filter_project_document` / `_narrow_roots`；`load_layers_report` 里对项目层调用；warning 文案从"execution"泛化 | 根因修法（§三 A/B/C） |
| 2 | `ai_coding/tools/builder.py` | `expand_root_patterns` 加 `base` 参数；`builder.py:181` 传入 | §三 B（含 P1-3） |
| 3 | `ai_coding/core/trace.py` | `TraceStore.__init__` 加 `workspace` + `resolve()`；`prune` 越界即拒 | 纵深防御（§三 D1） |
| 4 | `ai_coding/cli.py` | `_make_recorder` 传 `workspace`；`_extra_roots` 传 `base`；`_embeddings_for` 加工作区护栏；`_make_hooks` 的 `plugin_dir` 默认改用户级 | 组合根接线 + §三 D2/E |
| 5 | `ai_coding/core/mcp.py` | `_token_key`；两处调用点 | §三 F |
| 6 | `tests/test_config_layers.py` | U1–U7 用例（**既有 5 条不受影响**，见 §三 A 规则 3） | RIPR 红相 |
| 7 | `tests/test_trace_store.py` / `test_trace_recorder.py` / `test_trace_export_otlp.py` / `test_trace_export_html.py` / `test_trace_aggregate.py` / `test_repl.py`(**9 处** `TraceStore(...)`：436/445/458/782/888/918/930/954/978) / `test_plan_m1.py` / `test_loop_events.py` | `TraceStore(...)` 补 `workspace=`（`workspace=None` 会拒删，见 D1） | D1 的构造点 |
| 8 | `scripts/trace_cases.py` / `scripts/r9_cases.py` | 同上 | D1 的构造点 |
| 9 | `tests/test_memory_embeddings.py` | U9 护栏用例 | D2 |
| 10 | `tests/test_hooks.py` | U6 默认目录用例 | §三 E |
| 11 | `tests/test_mcp_oauth.py` | U7 token 复用用例（走 `_token_key`） | §三 F |
| 12 | `config.example.yaml` | `models` 段改为注释 + 指向用户层配置；`extra_workspace_roots` 注释补充"项目层只能收紧" | 评审 P2-8 |
| 13 | `docs/data-contracts.md` / `docs/roadmap-milestones.md` | 契约与里程碑 | |

> **只动 `loader.py` 一个文件做过滤**，其余改动都是"接线"、"纵深防御"或"文档"，
> 不引入新机制。

### 4.1 语义等价性（对可信场景与用户层零影响）

* 项目层可信的场景（`AI_CODING_TRUST_PROJECT=1`）：三类过滤全部跳过，行为与今天
  **逐字节相同**。
* **用户层的值从不被过滤**（过滤只作用于 `role == "project"`）。
* 无项目层时：`user_document` 就是全部，过滤无对象，行为不变。
* 显式 `--path`：`_layers_for` 只返回 `[("explicit", path)]`，`role != "project"`
  → 不过滤。这是操作者自己的文件。
* D2 的护栏**是唯一作用于所有层**的改动（理由见 §三 D2），已单独标注。

### 4.2 对数据流的影响与行为变更

| 变更 | 内容 |
|---|---|
| ① 端点/凭据 | 项目层的 `models.*.base_url`/`api_key`、`ai.websearch_api_key` **不生效** |
| ② 项目层新增模型 | 项目层新增的 `models` 条目**整条丢弃**；指向它的 `default_model` 也被丢 |
| ③ 越权根 | 项目层的 `extra_workspace_roots` 只能**收紧**（覆盖语义，见 §三 B） |
| ④ 越界路径 | 项目层的 `tracing.dir`、`memory.embeddings.cache_path` **不生效** |
| ⑤ 无用户配置时 | `config.example.yaml` 的 `models`/`default_model` 全部失效 → 启动即报 `no default model configured`（比今天"用占位 key 去 deepseek 然后 401"更清楚） |
| ⑥ 嵌入缓存位置 | **所有层**的绝对 `cache_path` 一律被钳回工作区（D2，唯一跨层的改动） |
| ⑦ `plugin_dir` 默认位置 | `<workspace>/.ai-coding/hooks` → `~/.config/ai-coding/hooks` |
| ⑧ MCP token | 键加入端点 → 既有 token 失效，需重新登录一次 |
| ⑨ `sources` 台账 | `sources` **只按顶层键**记录（[loader.py:300-301](../ai_coding/config/loader.py#L300-L301)），所以：`default_model` 被整键丢弃时 `sources` 保留用户层的值（键已从项目 document 删除）；但 `extra_workspace_roots` 嵌在 `tools` 下，删掉它**不会**改变 `sources["tools"]`（项目层的 `tools` 还有 bash/read 等键）—— 台账在这里没有信息量。`sources["models"]` 在"项目层削过但仍有存活条目"时指向项目层，属**已知不精确**，由一次性 warning 补充说明丢了哪些成员（评审 NP-5） |

### 4.3 `extra_workspace_roots` 的数据流

合并值从"用户 pattern 被项目 pattern 替换"变成"用户根的覆盖子集"。
`cli.py::_extra_roots` 的 `expand_root_patterns` 对结果再跑一次（对具体目录是**幂等**的）。

---

## 五、RIPR 用例

### Red（先写，必须变红）

用例名描述的是**期望的（安全）行为**，所以**今天必须失败** —— 这正是 RIPR 的 Red 相：
先让用例红，实现后再转绿。"实现是否真的改了行为"因此是可机检的（红 → 绿）。

| 编号 | 用例 | 期望红相（**今天必须失败**） |
|---|---|---|
| U1 | `test_config_layers.py::test_a_project_model_endpoint_does_not_redirect_the_users_key` | 用户层 `models.x.api_key: sk-REAL` + 项目层只写 `models.x.base_url: https://evil` → 断言端点**未**被重定向（今天：被重定向 → 红） |
| U2 | `test_config_layers.py::test_a_project_layer_may_not_widen_extra_workspace_roots` | 用户层空 + 项目层 `["/"]` → `Sandbox` 对 `/etc/shadow` **抛** `SandboxViolation`（今天：不抛 → 红） |
| U3 | `test_config_layers.py::test_a_project_websearch_key_does_not_replace_the_operators` | 用户层 `USER` + 项目层 `EVIL` → 工具内 key == `USER`（今天：`EVIL` → 红） |
| U4 | `test_config_layers.py::test_a_project_tracing_dir_does_not_leave_the_workspace` | 项目层 `tracing.dir = tmp_path` → 断言 `store.root` 落在工作区内（今天：`tmp_path` → 红） |
| U5 | `test_config_layers.py::test_a_project_embedding_cache_path_does_not_leave_the_workspace` | 项目层绝对路径 → 断言 `cache_path` 落在工作区内（今天：绝对路径 → 红） |
| U6 | `test_hooks.py::test_the_default_plugin_dir_is_not_the_workspace` | 项目目录放 `.ai-coding/hooks/evil.py` + 用户层 `module: evil` → 断言**未**被执行（今天：被执行 → 红） |
| U7 | `test_mcp_oauth.py::test_two_servers_that_share_a_name_do_not_share_a_token` | 同名不同 url 的两个 server → 断言**不共享** token（今天：共享 → 红） |
| U8 | `test_trace_store.py::test_prune_refuses_a_root_outside_the_workspace` | 红相：`TraceStore(tmp_path)`（**不传 `workspace`**）+ `prune(0)` → 断言旧子目录**未被删**（今天：被删 → 红；`workspace` kwarg 尚不存在，传了会是 `TypeError` —— 评审 NP-1） |
| U9 | `test_memory_embeddings.py::test_the_cache_path_is_clamped_into_the_workspace` | `cache_path = /etc/evil` → 断言 `_save` **未**写到工作区外（今天：写了 → 红） |

> **为什么不"反向断言漏洞"**：RIPR 的 Red 相是"**期望行为失败**"，它同时驱动实现
> （红 → 绿）。反过来先断言漏洞存在，用例今天就是绿的，既不能驱动实现，也不符合
> RIPR 的定义（评审 NP-1 的修订过程中确认）。

### Prove

1. `.venv/bin/python -m pytest -q` → 3863 + 新增用例 passed
2. `.venv/bin/ruff check .` / `.venv/bin/mypy ai_coding` → 干净
3. **突变测试**（每个过滤函数一处）：把过滤改成恒返回原 dict → 对应安全用例必须变红
4. **3.10 venv 七步**（与 CI 同构）
5. **端到端**：造一个恶意 `config.example.yaml`（外发端点 + 越权根 + 越界路径），
   用真实 `ConfigLoader.load_layers` 加载 → 断言三类都被挡住；再 `AI_CODING_TRUST_PROJECT=1`
   → 断言全部生效

### Refactor

* 确认 `sources` 的不精确已写入文档（§4.2 ⑨）
* 确认 `Paths.hooks` 仍无生产消费者

---

## 六、验收判据

* 项目层的 `models.*.base_url`/`api_key`、`ai.websearch_api_key`、`tracing.dir`、
  `memory.embeddings.cache_path` **一律不生效**（回落用户层）。
* 项目层**新增**的 `models` 条目**整条丢弃**；指向它的 `default_model` 也被丢；
  指向用户已定义模型的 `default_model` **保留**。
* 项目层**已有**条目的 `max_tokens`/`temperature`/`name` 等**仍生效**。
* 项目层的 `tools.extra_workspace_roots`：
  * 展开后为空（`[]`/`[""]`/`["/nope/*"]`/缺省）→ **用户层原样**（不收紧，评审 NP-7）；
  * `["/"]` → 用户层原样（**无法把机器塞进来**）；
  * `["<用户根>/a"]` 且用户根本身就是 `a`（或 `a` 的子集）→ 有效 = `{a}`（收紧）；
  * 项目写的目录是用户根的**父目录**（如用户根 `a`、项目写 `/home/me`）→ 有效 = ∅
    （覆盖语义：`U ⊄ P`；见 §三 B 边界）；
  * `["/etc"]`（不覆盖任何用户根）→ 有效 = ∅（收紧到无）。
* `AI_CODING_TRUST_PROJECT=1` 时**全部**恢复。
* `hooks.plugin_dir` 默认指向用户级；显式 `plugin_dir`（用户层）仍生效。
* MCP 同名不同 url 的两个 server **不共享** token。
* `TraceStore.prune` 在 `root` 位于工作区外时**不删任何东西**。
* 3.10 与 3.14 全量绿；真实 CI 双 job 绿。

---

## 七、不做的事

* **不改 `hooks.py`/`mcp.py` 的执行逻辑** —— 问题在"谁能喂给它们"。
* **不引入新的逃生门环境变量** —— 复用 `AI_CODING_TRUST_PROJECT`（用户拍板）。
* **不给 `config.example.yaml` 加豁免** —— 它正是问题的一部分；只更新它的指引（§四 12）。
* **不动 `Paths.hooks`** —— 它无生产消费者，改了只会增加面。
* **不改 `websearch_tool` 的硬编码端点** —— 那是"可配置 provider"，另一个话题。
* **不改 `EmbeddingIndex` 的构造契约** —— 绝对路径是测试与脚本的正当用法（§三 D2）。

---

## 八、评审记录

### 8.1 方案评审（子智能体，第 1 轮）

结论：分类框架与根因定位正确，§二 的 7 项调研行号与攻击面全部复核为真；
3 个 P0、5 个 P1、8 个 P2。逐条处置：

| 编号 | 内容 | 处置 |
|---|---|---|
| P0-1 | 项目层 `extra_workspace_roots: []` 会经 `_overlay` 的 list-replace **清空操作者授权**（`config.example.yaml:134` 正是 `[]`），与"空声明 = 不收紧"矛盾 | **已修订 §三 B**：空/缺省时**从项目 document 删掉该键**，让 overlay 保留用户层值；三处表述统一 |
| P0-2 | §三 B 与 §六 对 `["/"]` 给出相反结果（空 vs 用户原样） | **已修订 §三 B**：改为**覆盖语义**（有效 = 用户根中被项目声明覆盖的那些）→ `["/"]` 覆盖全部 → 用户层原样；§六 同步 |
| P0-3 | `default_model` 过滤规则会打破 5 条既有用例；且规则有歧义；只写在 docstring 里没有常量落点 | **已修订 §三 A 规则 3**：收敛为"**仅当它指向项目层自己新增的模型时丢弃**"。**已逐条核对 5 条用例**（`test_the_project_overlays_the_global_one`、`test_a_malformed_global_file...`、`test_the_nearer_layer_is_the_one_named`、`test_only_the_execution_keys_are_dropped`、`test_a_project_key_does_not_advance_the_sidecar_anchor`）——它们的项目层都**没有** `models` 块，新规则下全部**保持通过、无需修改**；安全性不变（端点与凭据已被规则 2 锁死），且不再与"坏用户层不该让项目起不来"的立意冲突 |
| P1-1 | `TraceStore.workspace` 必填/可选未定；改动清单漏了 ~15 个构造点 | **已修订 §三 D1 / §四 7-8**：定为**可选 + fail-closed**（`None` 拒删），并列出全部构造点（8 个测试文件 + 2 个脚本） |
| P1-2 | `prune` 护栏未 `resolve`，符号链接可绕过 | **已修订 §三 D1**：`root` 与 `workspace` 都 `resolve()` 后再比较，与 `Sandbox`/`Paths` 对齐 |
| P1-3 | `expand_root_patterns` 的 `base` 只被 loader 传，builder/cli 仍按 CWD | **已修订 §三 B**：`base` 在**所有**调用点生效 |
| P1-4 | `_narrow_roots` 返回形态与"整条丢弃 vs 集合交集"语义未定 | **已修订 §三 B**：定为**具体目录**（在既有契约内，`expand_root_patterns` 对其幂等），并说明"部分命中"的 DoS 面 |
| P1-5 | D2 的钳制作用于所有层，与 §4.1"用户层从不被过滤"冲突 | **已修订 §三 D2 / §4.1 / §4.2 ⑥**：明确 D2 是唯一跨层改动，给出理由（护栏是消费侧鲁棒性，防的是"将来某个调用方递进不可信路径"，它不知道来源层）与代价（缓存不能放到工作区外的盘；但缓存是 pure accelerator） |
| P2-1 | 缺口 1 攻击面列了 `name`/`max_tokens`/`temperature` 但修法不丢它们 | **已修订 §二 缺口 1**：补上"为什么不构成外发"（目的地与凭据都不变） |
| P2-2 | U8 命名与红相矛盾；U8/U9 分类混乱 | **已修订 §五**：统一为"基线用例（今天必须通过，实现后必须失败，再原地改写）" |
| P2-3 | `Paths.hooks` 有测试消费者 | **已修订 §三 E**：改为"生产代码里没有消费者；`tests/test_paths.py:74` 引用了它" |
| P2-4 | `sources` 不精确未给出修法 | **已修订 §4.2 ⑨**：区分两种情形（整键丢弃 → `sources` 保留用户层；`models` 部分存活 → 已知不精确，由 warning 补充） |
| P2-5 | "项目层新增条目必然是空壳"对本地/Ollama 模型不成立 | **已修订 §三 A 规则 1**：理由改为"项目层拿不到 `base_url`，本地模型也一样需要它"；并写入行为变更"项目层从此不能新增模型" |
| P2-6 | `_TokenStore` 签名与测试冲突 | **已修订 §三 F**：`_TokenStore` 保持字符串键签名，新增 `_token_key(server)` 推导；测试用 `_token_key` |
| P2-7 | 缺口 6/7 不属于三类 | **已修订 §二**：两条都标注"残余，非三类之一"，并说明其向量 |
| P2-8 | `config.example.yaml` 让"Copy to config.local.yaml"放 api_key，而那是项目层 | **已修订 §四 12**：更新指引 —— 模型与 key 放用户层（`~/.config/ai-coding/config.yaml`），项目层只能选模型名 |

### 8.2 方案评审（子智能体，第 2 轮）

结论：**3 个 P0 全部真闭合**（逐条回代码复核，并实跑 `test_config_layers.py` 22 passed
确认基线绿）。新提 1 个同类安全缺口（NP-3）+ 7 处细节。逐条处置：

| 编号 | 内容 | 处置 |
|---|---|---|
| NP-1 | U8 的红相 `TraceStore(tmp_path, workspace=ws)` **今天就是 `TypeError`**（`workspace` kwarg 尚不存在），不满足"今天必须通过" | **已修订 §五 U8**：红相改为 `TraceStore(tmp_path)`（不传 `workspace`）→ 断言被删；实现后该用例因 fail-closed 而失败，再改写为传 `workspace` → 断言未被删 |
| NP-2 | D2 护栏不 `resolve()`，与 D1 口径不一致；工作区内的符号链接可绕过；`paths.memory` 的括号是冗余 | **已修订 §三 D2**：补 `resolve()` 要求与理由；去掉冗余括号 |
| NP-3 | **项目层 `models:`（YAML `null`）会经 `_overlay` 的 else 分支把用户层的 `models` dict 覆盖成 `None`** → 用户所有模型消失（与 P0-1 同根因）；`tools: null` 同理 | **已修订 §三 A 规则 0**：新增通用规则"用户层在某顶层键上是 dict、项目层在该键上不是 dict → 丢掉项目层的值"（只能收紧、不能移除） |
| NP-4 | §四 #7 写 `test_repl.py`(×6)，实际 **9 处** `TraceStore(...)`（3 处包在 `_BudgetLoop`/`_FakeTrace` 里） | **已修订 §四 #7**：改为 9 处并列出全部行号 |
| NP-5 | §4.2 ⑨ 说"`extra_workspace_roots` 被整键丢弃时 `sources` 保留用户层" —— `sources` 只按**顶层键**记录，而它嵌在 `tools` 下 | **已修订 §4.2 ⑨**：区分顶层键（`default_model`）与嵌套键（`extra_workspace_roots`）两种情形 |
| NP-6 | `_filter_project_document` 签名缺 `workspace`（交集要展开 pattern） | **已修订 §三 A**：签名加 `*, workspace: Path` |
| NP-7 | "空声明"判据用**原始列表**，`[""]`/`["/nope/*"]` 仍能清空授权 | **已修订 §三 B**：判据改为"**展开后**为空" |
| NP-8 | §三 B 的例子隐含"用户根恰为 `a`"；若用户根是父目录则有效集为 ∅ | **已修订 §三 B / §六**：补边界说明（项目层能"选中"已授权目录，但不能收窄到子目录，除非用户层本就按子目录授权） |

### 8.3 实现评审（子智能体）

_待填。_
