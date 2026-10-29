# 批次 6 方案（R22·H10）`referenced_files` 不受压缩管辖

> 本轮只出方案，不改代码。依据：`docs/r22-context-audit.md` §2.1、§3（第 7 块）、§5 H10、H11；
> 真机证据：S3 场景（`/tmp/record/r22-s3.jsonl` 请求体、`turn-1.jsonl` 的 `ContextAssembled`）。

## 1. 数据流（先把这个块是什么说清楚）

**入口**：`RequestAssembler.assemble()` 每个迭代调用一次
[`_referenced_files_block(user_input)`](file:///workspace/ai-coding/ai_coding/core/request_assembly.py#L425-L490)，
`user_input` 是 `human_utterance(messages)`（最后一条**非 synthetic** 的 user 消息）。

**两条解析器**（都返回工作区相对路径、保序、去重，且都做"必须在工作区内"的校验）：

| 来源 | 函数 | 触发 | 例 |
| --- | --- | --- | --- |
| `@path` | `resolve_file_mentions`（`core/file_mentions.py:57`）| 显式 mention | `看 @docs/api.md` |
| 裸路径 | `resolve_bare_paths`（`core/file_mentions.py:84`）| 正文里出现"存在且扩展名在白名单里"的路径 | `data/big.txt has 6000 lines…` |

**逐路径分类**（顺序执行，`dict.fromkeys` 保序）：

| kind | 判定 | 产出 | 上限 |
| --- | --- | --- | --- |
| image | 后缀在 `IMAGE_MIME_BY_SUFFIX` | **跳过**（图片走 message part）| — |
| dir | `target.is_dir()` | `_directory_listing`：`<file type="dir">` + 名字列表 | `_DIR_ENTRIES = 200` 条 |
| file | 其余 | `_file_content`：`<file path="rel">` + **文件头 `limit` 个字符** + `[truncated: N chars total]` | 每文件 `_FILE_CHARS = 20_000`，且不超过剩余预算 |
| 无空间 | `len(<file path>) >= room` | 一行 `- rel: [no room left…read it with the read tool]` | — |

**预算**：`_FILES_BLOCK_BUDGET = 40_000` **字符常量**（`request_assembly.py:52`），与
`max_context_tokens`（`resolve_context_window()`，`config/models.py:854`）**没有任何关系**。

**出口**：作为第 7 个 volatile tail 块拼进请求（`request_assembly.py:358`），
`ContextAssembled.tail_blocks` 只记字符数。**压缩发生在它之前**（`assemble()` 先 compress 再 build_tail），
所以三层压缩（L3/L1/L2/L4）都碰不到它 —— 这是 H10 的本质。

## 2. 实测问题（S3，`max_context_tokens=6000`）

| 观测 | 数字 |
| --- | --- |
| 每次请求的 `referenced_files` | **40,138 字符**（≈ 10k tokens，是窗口的 **1.7 倍**）|
| 占整请求字符 | **96%**（40,138 / 41,777）|
| 11 次迭代 | 同一段内容**原样重发 11 次** |
| 压缩 3 次之后 | `estimated_tokens` 仍 16k（窗口 6k 的 2.7 倍）|
| 模型实际看到的 | 6000 行文件的**前 497 行**，末尾才写 `[truncated: 483691 chars total]` |

后果两条，都被真机证实：

1. **压缩空转**：反复压仍超预算，因为预算的 96% 在压缩之后才拼上去；
2. **误导**：模型据此认为"文件内容已经在上下文里"，重读 offset 0–2500（已读过），被检索门判"无新证据"强制收尾，
   最终只报 12/162 条 ERROR（H11 的摘要失真与它是同一条链）。

## 3. 候选方案

| 方案 | 做法 | 优点 | 代价 / 风险 |
| --- | --- | --- | --- |
| A 预算随窗口缩放 + 截断注记给行号 | `budget = clamp(0.15 × window_tokens × 4 字符, 2k, 40k)`；每文件 `limit = min(20k, room)`；注记从"共 N 字符"升级为"**已含第 1–K 行 / 共 L 行；续读：`read offset=K limit=…`**" | 一处改动同时解掉"压缩空转"和"模型以为读完了"；不改语义（文件仍不被丢弃，D5 保住）| 大文件在宽窗口下仍会占 40k（可接受：48k 窗口下 40k 字符 ≈ 10k tokens ≈ 20%）|
| B 只对 `@` 注入正文，裸路径只给一行指针 | 裸路径 → `- rel (N 行, M 字符)：用 read 读取` | 直接消灭"顺手提到一个大文件就被塞 40k" | 改变既有产品行为（"粘贴路径=要看内容"，opencode 也是这个语义）；现有测试 `test_a_file_over_the_remaining_budget_is_truncated_not_dropped` 的意图会被推翻 |
| C 只给提及点附近的行（grep 风格上下文窗）| 以提及处为中心给 ±N 行 | 信息密度最高 | 需要"命中行"这个概念，而裸路径没有命中行；对"读全文统计"类任务反而更差 |

**选定：A**（B/C 的优点可以以后叠加，A 是唯一不推翻既有语义又能止损的）。理由：
S3 的失败不是"给了文件"而是"**给了一个装成全集的头部，且没有告诉模型它只是头部**"；
A 同时改预算与注记，正好对准这两点。

## 4. 精确改动（落地时按此执行）

1. **窗口来源（修正）**：`RequestAssembler` 只拿到 `AIConfig`，直接调 `resolve_context_window()` 会恒返 48k
   （`resolve_context_window` 读的是 `config.ai.max_context_tokens` / `config.models`，`AIConfig` 两者都没有）。
   唯一可信来源是**压缩器正在用的那个数**：`ContextManager._max_context_tokens`
   （`context_compression.py:848-859`，`/model` 切换时会重设）。
   - 给 `ContextManager` 加只读属性 `max_context_tokens`（返回 `_context_tokens`，不复制规则）；
   - 给 `Compactor` 加只读属性 `context_window`：`getattr(self._context, "max_context_tokens", None)`；
   - 组装器：`window = getattr(self._compactor, "context_window", None) or DEFAULT_CONTEXT_TOKENS`
     （测试里的假 compactor 没有该属性 → 落回 48k，不报错）。
2. **预算按 token 算，再换算成字符（修正）**：
   - `_FILES_BLOCK_TOKENS = clamp(int(window * 0.15), _FILES_BLOCK_MIN_TOKENS, 10_000)`；
     `10_000` token 就是原来 40,000 字符的 token 等价值，保留旧上限；
     `_FILES_BLOCK_MIN_TOKENS = 150`（约 600 个 ASCII 字符）——原计划的 2,000 字符下限在 3k 窗口下会吃掉半窗；
   - 用现成的 `context_compression.estimate_tokens()`（CJK 按 ½ token/字）定位字符切点：
     `while estimate_tokens(text[:n]) <= room: n = …`（大文件用二分，单文件 ≤ 20k 字符，开销可忽略）。
3. **截断注记（修正）**：
   `[truncated: 483691 chars, 6000 lines total; this text is lines 1-497; read offset=497 limit=500 for the rest]`
   - 已含行数 = `text[:n].count("\n")`（**不二次 IO**，`text` 已在内存）；
   - `read` 的 `offset` 是 **0-based**，所以续读参数正好等于"已含行数"（前 497 行之后从第 498 行开始）；
   - 末行无换行的文件，`count("\n")` 少 1 → 加一个 `text.endswith("\n")` 的修正。
4. 不动：`_DIR_ENTRIES`、图片跳过、`@`/裸路径解析、块顺序、`ContextAssembled` 结构。
   （目录列举本身不做预算裁剪，只计进 `used`；这是现状，本轮不改。）

## 5. 测试（红→绿，全部离线）

| # | 用例 | 结果 |
| --- | --- | --- |
| 1 | 窗口 6k（假 compactor 报 `context_window=6000`）+ 6000 行文件 → 块字符数 ≤ 4,000，且 `estimate_tokens(块) ≤ 900`（15%×6k）| **现在红**（40,138）|
| 2 | 窗口 48k → 预算 ≈ 7,200 token；ASCII 文件块 ≤ 29,000 字符（**修正**：原计划写 40,000 是算错了）| **现在红**（40,138）|
| 3 | 窗口 200k → 仍封顶 ≤ 40,000 字符 | 现在绿（本来就有 40k 上限），保留作回归 |
| 4 | 6000 行文件 → 注记含 `lines 1-`、`read offset=<已含行数>` | **现在红** |
| 5 | `test_a_file_over_the_remaining_budget_is_truncated_not_dropped`（`tests/test_file_mentions.py:259-273`）**要改写**：三个 15k 文件在 28.8k 预算下，前两个有 MARKER，第三个是带行数/字符数与 `read` 指令的指针 —— 这是有意的行为变更（指针优于 3k 字符碎片），测试意图从"必须有正文"改为"必须告诉模型怎么拿到" | 现在绿 → 改后仍绿 |
| 6 | 没有路径 → 空串（不变）| 现在绿 |
| 7 | 目录 250 条 → 截断到 200 条（不变）| 现在绿 |


## 6. 风险与不做

- 风险 1：**窗口很小（6k）时一个大文件只给 ~600–900 字符**，信息量骤降。缓解：下限 150 token（≈600 字符）+ 注记直接给出
  `read offset=` 参数，模型一步就能续读（S3 里它本来就想这么做）。
- 风险 2：**默认 48k 窗口的预算从 40k 字符降到 ≈28.8k 字符**，粘贴两三个大文件时第三个会变指针。
  这是有意的：28.8k 字符 ≈7.2k token ≈ 窗口的 15%，而旧上限在 48k 窗口里会占到 20%+。
  对"三个 15k 文件"这类场景，指针 + 行数比 3k 字符的碎片更有用。
- 风险 3：`getattr` 链（`_compactor.context_window`）是鸭子类型；任何自造的 Compactor 桩都能跑，拿不到就落回 48k 默认值（与现状一致）。
- 不做：`budget` 块阈值、`environment` 时间行、`task`/`anchor` 三合一（审计里另外三条 P2，各自独立）；压缩阈值不动。

## 7. 复验（真机）

同一 S3 任务（r22-s3，6k 预算）复跑：
- `ContextAssembled` 的 `referenced_files` ≤ 4,200 字符、`estimate_tokens` ≤ 900；
- `estimated_tokens` 不再被尾块独占（窗口的 ~1.2 倍以内）；
- 压缩次数 ≤ 1；
- 答案按 grep 真值核对（162 条），不以"模型说完了"为准。

## 8. 方案评审处置（第一轮）

| 评审意见 | 处置 |
| --- | --- |
| **BLOCKER** 窗口来源错：`resolve_context_window()` 读 `config.ai` / `config.models`，而 `RequestAssembler` 手里只有 `AIConfig` → 恒返 48k，6k 场景静默失效 | **已改**：改用压缩器的 `context_window`（`ContextManager._max_context_tokens` 是 `/model` 也会重设的那个数），加只读属性 + `getattr` 兜底 |
| **BLOCKER** 测试 #2 算错：48k × 0.15 × 4 = 28,800，到不了 40,000 | **已改**：断言改为 ≤29,000，另加一条 200k 窗口仍封顶 40k 的回归 |
| **BLOCKER** 既有 D5 用例会红，方案里标了"—"（漏项）| **已改**：测试 #5 明确要改写，意图从"必须有正文"变为"必须给出可取回的指针" |
| MINOR `×4` 低估中文（CJK ½ token/字）| **已改**：预算先按 token 定义，再用 `estimate_tokens()` 定位字符切点 |
| MINOR 行号可零二次 IO；`read offset` 是 0-based | **已改**：`text[:n].count("\n")` + `endswith("\n")` 修正；`offset` 直接取已含行数 |
| MINOR `_directory_listing` 行号写错、目录不做预算裁剪、`floor=2000` 过大 | **已改**：行号按实际 492-500；目录列举现状不改并写明；下限降到 150 token |

---

## 完成记录（H10 已落地并真机复验）

| 项 | 改动 | 测试 |
| --- | --- | --- |
| 预算口径 | `_FILES_BLOCK_RATIO = 0.15`、`_FILES_BLOCK_TOKENS_CAP = 10_000`、`_FILES_BLOCK_MIN_TOKENS = 150`、`_FILE_TOKENS_CAP = 5_000`，全部换成 token 口径 | `tests/test_file_mentions.py` +4（6k/48k/200k 窗口、注记行号）|
| 窗口来源 | `ContextManager.max_context_tokens`（只读）+ `Compactor.context_window`；组装器 `getattr(..., None) or DEFAULT_CONTEXT_TOKENS` | 同上（假 compactor 的 `context_window` 三态：数值 / None / 无属性）|
| 字符切点 | `_chars_for_tokens()`：先按 4 字符/token 试，超了再按比例二分（CJK 不会被 4× 高估），切在换行边界 | 注记用例 |
| 注记 | `[truncated: 483691 chars, 6000 lines total; this text is lines 1-497; read offset=497 limit=500 for the rest]`；无换行文件退化成"前 N 字符"（不带 rec 的 offset=0）| 注记用例 + 单行边角 |
| 既有 D5 用例 | 改写为"不许静默丢弃 → 放不下就给带行数的指针"，并断言第三个文件以指针出现 | 通过 |

### 真机复验（同一 S3 任务，6k 窗口）

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| `tail_blocks.referenced_files` | **40,138** 字符 | **3,751 / 4,093 / 4,488** 字符（三次采样）|
| 单次请求 `estimated_tokens` 峰值 | **20,267**（窗口的 3.4 倍）| **6,972**（窗口的 1.16 倍）|
| 一轮内的压缩次数 | 3 | **2** |
| 尾块占整请求 | 96% | 约 65%（窗口 6k 下尾块仍是主要开销，但已在预算内）|

### 诚实结论：答案仍然不对，但原因换了

复跑后模型报 **8 条**（真值 162），与修复前一样错。这一次不是引用块的问题：
它确实做了 12 次分块 read，然后 L3 落盘（改写 12 条，266,884→140,127 tok）→ L1 剪断（→6,667）→ L2 占位 → L4 摘要（→6,844），
**摘要只保留了 8 个行号**，模型就照着摘要回答，并且说"摘要明确记录了全部 12 个 chunk 已完成"。

也就是说 H10 修好了"输入把窗口撑爆"，剩下的坑是 **H11：摘要对"计数型证据"是有损的，而模型会把它当成原始数据**。
两者独立，H11 需要单独方案（候选：计数/聚合结果写进 `<working-memory>` 的结构化槽位，而不是只留在自由文本摘要里）。

### 代码评审处置（第二轮，0 blocker / 7 minor）

| 评审意见 | 处置 |
| --- | --- |
| 二分 `hi=int(guess*ratio)+1` 不是真上界，实测少给约 17% 预算 | **已改**：换成 `context_compression.chars_for_tokens()`，单遍扫描、到预算即停，既精确又快（只扫它真正返回的那一段，不再反复扫 20–30 万字符）|
| 单文件 47–67ms 的开销与注释不符 | **已改**：同上，单遍 + 提前返回 |
| `boundary == 0`（文件以空行开头）时切点不在行边界，注记却写 `lines 1-1` | **已改**：`> 0` → `>= 0` |
| 行数用 `count("\n")`，`read` 用 `splitlines`（`\x0c`/`\u2028` 也算行），偏移会早一行 | **已改**：`total = len(text.splitlines())`、`shown = len(cut.splitlines())` |
| 一个大文件吃满预算后，后面 21 字节的文件也退化成指针 | **已改**：为后续每个文件预留 `_FILES_BLOCK_MIN_TOKENS`；新增用例 `test_a_tiny_file_after_a_huge_one_still_arrives` |
| 注释里"10,000 = 旧 40,000 字符"只对 ASCII 成立 | **已改**：注释写明是 ASCII 等价，CJK 旧口径约 20k token |
| 200k 窗口的 ≤40,000 断言是空转（单文件被 5,000 token 单文件上限卡住）| **已改**：该用例改成 4 个文件，真正压到 10k token 的块上限 |

### 最终真机数字（评审修复后复跑）

| 指标 | 修复前 | 第一版 | **最终版** |
| --- | --- | --- | --- |
| `referenced_files` 字符 | 40,138 | 3,751–4,488 | **3,751** |
| 请求 `estimated_tokens` 峰值 | 20,267 | 6,972 | **6,915**（窗口 6k 的 1.15 倍）|
| 一轮内压缩次数 | 3 | 2 | **1** |

答案仍是 **8 条**（真值 162），且这一版模型自己说出了原因：*"根据早期对话摘要中已完成的分块读取结果…总计为 8 行"*。
压缩明细：L1 剪断 14 条（136,824→3,350 tok）、L4 摘要（133,429→3,497 tok）。**输入侧已经干净，剩下的损失全在摘要**。
