# AI Agent 会话/记忆持久化与崩溃恢复设计：调研发现报告

> 调研日期：2026-10-26
> 调研范围：主流 Agent 框架的会话/记忆持久化机制、底层存储与事务机制、崩溃恢复与副作用一致性、并发控制、记忆压缩与可靠性。
> 方法：以官方文档（P0）、学术论文（P0/P1）、厂商工程博客与生产实践文章（P1/P2）为主，对核心结论做 ≥3 源交叉验证。

---

## 一、执行摘要

围绕"AI Agent 会话/记忆的持久化与崩溃恢复"这一主题，证据收敛出四条主结论：

1. **会话持久化在工程上已收敛为一套通用双层模型**：以"线程/会话 ID"为主键的**检查点（checkpoint）**负责短期、线程内的图状态；独立的**长期记忆存储（store / memory layer）**负责跨线程、跨会话的事实与偏好。二者职责分离、通常配合使用。[cite:2][cite:4][cite:8]

2. **崩溃恢复的本质不是"重试"，而是"持久化执行（durable execution）"**：状态随持久存储存活，进程崩溃后由运行时从最后检查点/事件历史精确重建并续跑，而不是从头再来。[cite:33][cite:34][cite:12]

3. **仅靠检查点不足以达成端到端可靠性**。副作用（工具调用、外部 API 写入）成功与"完成"状态落库之间存在"最危险的崩溃窗口"；在 at-least-once 投递下会重复副作用。需要**幂等键、唯一约束、租约/围栏令牌、事务性发件箱（Transactional Outbox）、对账与补偿**的组合。[cite:41][cite:39][cite:42]

4. **记忆的"压缩/遗忘"属于可靠性设计，而非单纯的优化细节**。上下文压缩（compaction）会静默丢弃策略性约束，导致"治理衰减"；记忆子系统本身也可能"静默死亡"。[cite:54][cite:50]

---

## 二、会话持久化的分层模型

### 2.1 短期记忆：检查点（Checkpointer）

在 LangGraph 的模型中，检查点在每个 **super-step**（超级步）保存一次图状态快照，并按 **thread** 组织；`thread_id` 是存储与检索检查点的主键，缺少它则无法保存状态或在中断后恢复。[cite:1][cite:2] 检查点同时支撑四类能力：人机协同（human-in-the-loop）、会话记忆、时间旅行（time travel）调试、以及容错恢复。[cite:1]

关键设计点在于"**检查点保存的是图状态，而不是产品元数据**"。生产实践普遍建议在检查点之上自建一张 thread 表，用其 ID 作为 LangGraph 的 `thread_id`，以承载业务侧的会话元数据与隔离。[cite:8]

### 2.2 长期记忆：Store 与独立记忆层

LangGraph 明确区分两套系统：**Checkpointer** 持久化线程内的图状态（短期、线程作用域），**Store** 持久化应用自定义的键值数据（长期、跨线程作用域），用于用户偏好、事实与共享知识。[cite:4][cite:2] 这一"短期/长期"二分也被其他框架以不同形式复现：

- **Letta（MemGPT）**：三层记忆——核心记忆块（core memory，常驻上下文，Postgres/SQLite）、归档记忆（archival memory，向量库 pgvector，按需检索）、回忆记忆（recall memory，对话历史，超限自动压缩）。[cite:13][cite:16]
- **Mem0**：框架无关的记忆层，以 `add / search / update / delete` 为 API，底层为向量库 + 关系库，支持多租户隔离与命名空间；其论文报告在 LOCOMO 基准上相比全上下文方案降低 91% 的 p95 延迟、节省 90%+ token 成本。[cite:18][cite:21]
- **CrewAI**：四类记忆——短期（ChromaDB + RAG）、长期（SQLite）、实体记忆（RAG）、上下文记忆（编排层）。默认后端为本地 LanceDB/ChromaDB + SQLite，**缺乏按用户隔离**，并发下还会出现 ChromaDB 锁错误。[cite:22][cite:23]
- **AutoGen**：`Memory` 协议（`add/query/update_context/clear`），内置 `ListMemory`（仅进程内）、`ChromaDBVectorMemory`、`RedisMemory`；**默认无跨会话记忆**，会话结束即丢失状态。[cite:24][cite:23]
- **LangChain**：`ConversationBufferMemory` / `ConversationSummaryMemory`，作用域限于单次 chain 运行，不做事实抽取、去重或跨会话持久化。[cite:23]

一个横向问题是**可移植性与迁移成本**：各框架用自己的 schema 与后端存储记忆，跨框架无法共享用户上下文；切换框架意味着历史记忆从零开始。[cite:23] 这正是 Mem0 等"框架无关记忆层"试图解决的空白。[cite:20]

### 2.3 框架实现对比

| 框架 | 短期/会话状态 | 长期/跨会话记忆 | 默认持久化后端 | 崩溃恢复能力 |
|---|---|---|---|---|
| LangGraph | Checkpointer（按 super-step 快照，`thread_id` 主键） | Store（跨线程） | MemorySaver（内存）/ SQLite / Postgres / Redis | 依赖 durability mode，可从中断点恢复 [cite:1][cite:12] |
| Letta (MemGPT) | 核心记忆块 + 回忆历史 | 归档向量库 + 核心块 | Postgres / SQLite + pgvector | 有状态服务端，`.af` 可移植代理文件 [cite:13][cite:16] |
| Mem0 | 会话内上下文 | 结构化记忆 + 图记忆变体 | 向量库 + 关系库（可配置） | 记忆层持久化，非执行恢复 [cite:18][cite:21] |
| CrewAI | 短期记忆（单次 kickoff） | 长期记忆（跨运行） | 本地 LanceDB/ChromaDB + SQLite | 弱，机器绑定、无多用户隔离 [cite:22][cite:23] |
| AutoGen | 会话内消息列表 | 需外接（如 Hindsight） | 进程内 / ChromaDB / Redis | 弱，会话结束即丢 [cite:24] |
| OpenAI Agents SDK | Session（Conversations API / MemorySession） | 通过 Session 接口外接 | 托管 Conversations / 自定义 | 支持从 `RunState` 续跑 [cite:25][cite:26] |
| Anthropic（Claude） | 上下文窗口 + 上下文编辑 | Memory Tool（文件系统，客户端管理） | 客户端自管存储 | 客户端负责持久化 [cite:28][cite:29] |

OpenAI Agents SDK 提供了值得注意的抽象：`Session` 接口只需五个异步方法即可接入 Redis、DynamoDB、SQLite 等自定义后端；`Runner` 会自动取回历史、拼接下一轮输入、并在每轮结束后持久化，还支持从被中断的 `RunState` 恢复（resumable runs）。[cite:25] 在仅使用 Responses API 时，则通过 `previous_response_id` 串联请求以在多轮间保留上下文，无需创建完整会话资源。[cite:26][cite:27]

---

## 三、崩溃恢复：从检查点到持久化执行

### 3.1 检查点机制与"恢复起点"

LangGraph 的容错语义是：**当某个 super-step 中一个或多个节点失败时，可从上一个成功步骤重启图**；若节点在执行中失败，已成功节点的"挂起写入（pending writes）"会被保存，恢复时不会重跑这些成功节点。[cite:1] 这依赖检查点接口的 `put / put_writes / get_tuple / list` 等契约。[cite:2]

需要澄清一个常见误解：**恢复时代码并不会从停止的"同一行"继续**。LangGraph 的持久化执行文档明确指出，恢复时会先确定一个合适的"起点"，从该起点继续执行；因此工作流必须被设计为**确定性且幂等**，并把副作用/非确定性操作封装在 task 中，以便恢复时从持久层取回结果而非重复执行。[cite:3] 这与 Temporal 的"重放（replay）"语义一脉相承。

### 3.2 持久化模式（durability modes）：安全 vs 性能的旋钮

LangGraph 提供三档持久化模式，按持久性由弱到强：[cite:12][cite:7][cite:3]

| 模式 | 语义 | 崩溃窗口风险 | 适用场景 |
|---|---|---|---|
| `exit` | 仅在图执行退出（成功/报错/中断）时持久化 | 执行中崩溃不保存中间态，无法恢复 | 短任务、非关键实验 |
| `async`（默认） | 执行下一步的同时异步持久化 | 有"少量检查点可能未写入"的风险 | 长流程，丢失检查点意味着重跑而非副作用 |
| `sync` | 开始下一步之前同步持久化 | 最小，每步先落库再继续 | 丢失检查点会产生真实世界重复（邮件/支付/写 API） |

选择依据应是**"丢失的后果"（consequence-of-loss）**而非直觉。[cite:12] 有研究（Crab，arXiv:2604.28138）指出超过 75% 的 Agent 轮次不产生与恢复相关的状态，因此"无脑全量检查点"大多是浪费，语义感知的检查点可将恢复正确率从 8% 提升到 100%，同时把检查点流量降低最多 87%。[cite:12]

### 3.3 底层存储：WAL 与事件溯源

**PostgreSQL 的 WAL（预写日志）** 是 Postgres 检查点后端可靠性的根基。其核心不变量是"**日志先于数据页落盘**"（write-ahead）：崩溃恢复时读取 `pg_control` 与最近的检查点记录，从其中的 redo 位置开始向前 REDO，从而把系统带回崩溃前的稳定状态。[cite:36][cite:37][cite:38] 检查点的作用是周期性把脏页刷盘并写入检查点记录，使 redo 起点前移、旧 WAL 段可回收，从而限制恢复时需要重放的日志量。[cite:36] 换言之，Agent 检查点后端（如 `PostgresSaver`）本身依托数据库的 WAL/检查点机制获得持久性，Agent 层的 super-step 检查点与数据库层的 WAL 是**两级**持久化。

**事件溯源（Event Sourcing）** 是另一条主线。Temporal 用**事件历史（Event History）**——一份完整、有序、持久化的工作流生命周期日志——作为一切事实来源；工作流代码在恢复时**从开头重放**，用记录的事件而非重新执行来重建状态。[cite:31][cite:32] 这要求工作流代码确定性，凡涉及外部世界的操作（API、DB、LLM、文件 I/O）必须放进 Activity，Activity 结果在重放时被复用而不重算。[cite:32] 官方将其概括为"持久化执行"：一旦工作流启动，就会运行到完成，进程崩溃时由 Temporal Service 把工作交给另一个进程重建状态并从中断处续跑。[cite:33]

**Temporal + LangGraph 插件**把二者结合：每个节点声明 `execute_in: activity | workflow`，Activity 获得超时/重试/心跳，Workflow 节点保持内联且必须确定性；官方文档与生产案例报告其可在约 200ms 内自动崩溃恢复、零丢失运行，而纯检查点方案需要人工恢复（案例中每次 6–11 分钟）并有丢失。[cite:34][cite:35] 需要注意：该数据来自厂商/个人博客的生产案例，属 P2 级证据，且伴随约 0.7s/运行的调度开销，尚未在独立基准中复现。[cite:34]

---

## 四、仅靠检查点不够：副作用一致性与"最危险的崩溃窗口"

### 4.1 双写问题与最危险的毫秒

检查点解决的是**状态**持久化，但 Agent 的可靠性瓶颈常在**副作用**。核心难题是经典分布式系统的**双写问题（dual-write problem）**：Agent 做决策时需要原子地完成"持久化新状态"和"通知其他服务"，而"先写库、后发消息"存在竞态——库写成功而发布失败，则状态已前进但无人知晓；发布成功而库写失败，则下游基于一个从未提交的决策行动。[cite:39]

更隐蔽的是**"最危险的崩溃窗口"**：副作用已成功、Worker 尚未保存 `COMPLETED` 就崩溃。队列看不见外部世界，只能按 at-least-once 重新投递，于是同一意图产生两次副作用（两次重启、两封邮件、两笔扣款）。日志可能显示一条漂亮的"失败→重试→成功"恢复链，但用户看到的是两次动作——**"任务状态成功"不等于"业务副作用只发生一次"**。[cite:41]

### 4.2 幂等键、租约、围栏令牌与对账

针对上述窗口，证据指向一组组合方案：[cite:41][cite:39]

- **稳定幂等键**：为"业务意图"生成，绑定租户/作用域、入口只生成一次的 `intent_id`/`task_id`、工具名/动作类型、规范化（排序/去空白/统一单位）后的参数、以及工具 schema 版本。
- **接收端唯一约束**：即使 Worker 崩溃重投，接收端用唯一约束阻断第二次副作用。
- **租约（lease）+ 围栏令牌（fencing token）**：租约到期后另一 Worker 可接管；围栏令牌使旧 Worker 的迟到的写被拒绝。
- **对账（reconciliation）与补偿（compensation）**：对无法幂等的接收端做补偿。
- **持久任务状态机**：以显式状态（RUNNING/ACCEPTED/COMPLETED）驱动，而非仅靠消息 ACK。

### 4.3 事务性发件箱（Transactional Outbox）与 Saga

**事务性发件箱**是双写问题的标准解法：在**同一原子事务**内同时写入业务状态变更与一条 outbox 记录（序列化事件），再由后台 **relay** 读取未处理记录转发到消息代理，成功后标记已处理。因为两次写在同一事务中，要么都提交要么都回滚；relay 只会看到已提交记录，发布失败则重试，relay 崩溃则下次接着来。[cite:39] relay 有两种实现：**轮询发布器**（简单、有延迟与数据库负载）与 **CDC**（消费 WAL/binlog/Streams，低延迟、运维更复杂）。[cite:39]

在 Agent 场景的典型应用包括：工单路由决策 + 通知入队、编排器排期任务 + 任务创建事件、退款审批 + 支付服务通知。[cite:39] 与之配套的 **Saga 模式**用于多步补偿逻辑。[cite:39] 一个把 Kafka + Temporal + LangGraph 组合起来的生产架构（反欺诈调查）即采用：Temporal 跑调查、LangGraph 跑在 Activity 内、确定性代码做最终决策、outbox 发布事件、并显式处理重复投递与证据缺失/降级。[cite:40]

### 4.4 语义事务：把提交边界提升到任务级

学术研究 **Cordon（arXiv:2606.17573）** 指出当前 Agent 运行时把工具暴露为**孤立的 RPC**，缺乏跨多步任务的提交/回滚/恢复/审计边界；它提出"**语义事务（semantic transaction）**"：把每个 Agent 任务表示为任务级执行边界，绑定工具意图与结果血缘，将可逆的本地变更放入影子状态、把对外动作放入 **effect outbox**、并记录恢复元数据，在提交前对整体执行流做校验。[cite:42] 这代表一个趋势：**恢复与一致性边界正从"单次工具调用"上移到"任务/意图"级**。

---

## 五、并发控制：双发（double-texting）与乐观并发

当多个请求/副本同时推进同一线程时，会出现竞态。证据显示几类并发的处理范式：

- **线程隔离**：每个 `thread_id` 的状态完全隔离，检查点按键作用域读写，线程间无共享状态。[cite:44]
- **乐观并发控制**：读取时记录版本号，写入时做 compare-and-swap；若他人已推进版本则拒绝并抛 `StaleStateError`（如 10xGraph/PgCheckpointer），从而避免丢失更新。[cite:44]
- **悲观锁 / 行级锁**：Google ADK 的 `SessionService` 对同一会话的 `append_event` 使用**进程内锁**串行化；对 PostgreSQL/MySQL/MariaDB 使用 `SELECT ... FOR UPDATE` 行级锁。[cite:43]
- **线程级分布式锁**：CopilotKit 运行时对每个 run 持有 **Redis 支持的线程锁**；若线程已有活动 run，新请求返回 **409 Conflict**，防止两个 run 在同一线程上交错写事件。[cite:45]
- **双发策略（double-texting）**：LangGraph 生产运行时提供 `enqueue`（默认，新输入排队等当前 run 完成）/`reject`（拒绝）/`interrupt`（中止当前 run、保留进度、从该状态处理新输入）三种语义。[cite:6]

选择依据是业务对"同一会话并发输入"的容忍度：默认排队最安全，`interrupt` 适合"用户改了主意"的场景。[cite:6]

---

## 六、记忆的压缩、遗忘与可靠性

### 6.1 上下文工程与压缩

上下文是**有限且边际收益递减**的资源；随 token 增长会出现"context rot"（准确召回率下降）。[cite:30] Anthropic 提出的上下文工程两大手段是**上下文编辑（context editing）**与**记忆工具（memory tool）**：前者在接近 token 上限时自动清除陈旧的工具调用与结果（服务端策略 `clear_tool_uses_20250919` / `clear_thinking_20251015`），后者让 Claude 通过**文件系统**在上下文窗口之外存取信息，且**完全在客户端运行、由开发者管理存储后端**。[cite:28][cite:29] 官方报告：记忆工具 + 上下文编辑相较基线提升 39%，仅上下文编辑提升 29%；在 100 轮网页搜索评测中，上下文编辑使原本会因上下文耗尽而失败的任务得以完成，同时降低 84% token 消耗。[cite:28]

**压缩（compaction）** 是把接近上限的对话摘要化并开启新窗口；在 Claude Code 中即为标准做法。[cite:30] 压缩/编辑**在服务端进行**，客户端仍保留完整未修改的历史，因此无需同步编辑后的版本。[cite:29]

### 6.2 遗忘即风险：治理衰减

压缩虽是必要手段，却可能**静默破坏策略连续性**。Governance Decay 研究（arXiv:2606.22528）在 1,323 个 episode、7 个模型上测得：策略在上下文中可见时违规率为 0%，**单次压缩后升至 30%**，DeepSeek-V4 与 Kimi-K2.5 达 59%；在生产框架中更严重——LangGraph 65%、LangMem 95%、AutoGen（recency 驱逐策略）达 **100%** 违规率。[cite:54] 结论被概括为"**治理 Agent 需要治理它们如何遗忘**"：必须绑定的策略应放在 Agent 可编辑面之外，在网关层强制执行，而非放在可能被"说服"的上下文窗口里。[cite:54]

### 6.3 记忆子系统的可靠性工程

**Memory as Infrastructure（arXiv:2609.05510）** 记录了长达数月、78,933 次 hook 调用的生产运行，把 Agent 记忆失败归纳为四类：知识丢失（压缩抹去"为什么"的连接组织）、重复劳动（重新提出已存在的方案）、上下文退化（注入更多反而更差），以及**"静默记忆死亡"**——记忆系统（索引、embedding 服务、hook 管道）本身降级或死亡却无人察觉，所有人仍在信任它。[cite:50] 作者主张为记忆子系统补上 SRE 式的可靠性工程：会话启动健康门、可区分的失败模式、心跳遥测（使任何枚举的失败模式都不会无记录地通过）、以及告警疲劳预算。[cite:50]

### 6.4 记忆维护是后台任务

多条证据强调"记忆维护"应作为**后台作业**而非同步逻辑：Letta 的 **sleeptime agent** 在对话间隙异步整合碎片记忆、识别跨对话模式、去重与归档（默认每 5 条消息触发一次，可用 `sleeptime_agent_frequency` 配置），从而让前台 Agent 不承担记忆治理的延迟。[cite:13][cite:14] Letta 社区给出的过期策略示例：会话上下文 30 天（被引用 3+ 次则提升）、决策永不过期、调试/错误 14 天（除非标记为根因）、偏好永不过期、TODO 90 天。[cite:14]

---

## 七、失败模式与研究前沿

综合学术与工程证据，Agent 记忆/会话系统的失败可归为几类：

- **静默失败分类学（arXiv:2606.08162）** 提出五类静默失败，其中与本主题直接相关的是 **L2 认知框架滞后（记忆）** 与 **L3 数据一致性衰减（执行）**；并给出"P8：token 预算式记忆剪枝不可恢复"——被剪枝算法丢弃的信息**永久丢失**。[cite:53]
- **跨会话知识碎片化**：压缩把长期弧线的连接组织摘要掉，Agent"流畅但悄然失联"。[cite:50]
- **多智能体一致性**：Zylos 综述引用 Cemri 等（2025）指出 **36.9% 的多智能体系统失败源于智能体间不一致**（coherence/consistency），并指出没有任何现有框架开箱即用解决该问题；分布式记忆是"先是一个分布式系统问题，其次才是 AI 问题"。[cite:55]
- **新分类学**：`Memory in the Age of AI Agents`（arXiv:2512.13564）主张短/长期二分过于粗糙，提出 **持久性（persistence）/ 粒度（granularity）/ 操作（operation）** 三维分类，识别出传感缓冲、工作记忆、情节记忆等七类。[cite:51]

这些发现共同指向：**记忆的写入路径过滤、矛盾处理、延迟预算与隐私治理**是工程现实中的关键难点。[cite:52]

---

## 八、设计建议清单

基于上述证据，可提炼出可操作的设计准则：

1. **分离两层存储**：用检查点（`thread_id` 主键）管短期/线程内图状态，用独立 store/记忆层管跨线程长期事实，两者配合而非互相替代。[cite:4][cite:2]
2. **按"丢失后果"选持久化档位**：默认 `async`，凡丢失会产生真实世界重复（写操作、支付、邮件）的流程用 `sync`；短任务可用 `exit` 省开销。[cite:12][cite:7]
3. **后端选型看部署形态**：单进程/原型用 MemorySaver/SQLite；多副本生产用 Postgres（依托 WAL 获得持久性）；Redis 适合低延迟线程级检查点（注意其 0.1.0 起要求 RedisJSON + RediSearch，需 Redis 8.0+/Stack）。[cite:36][cite:48][cite:49]
4. **恢复语义要"确定性 + 幂等"**：把副作用/非确定性操作封装为 task/activity，避免恢复时重复执行；接受"从起点重放"而非"从断点续行"的心智模型。[cite:3][cite:32]
5. **用幂等键 + 唯一约束 + 租约/围栏令牌封住崩溃窗口**：不要相信 broker 能提供端到端 exactly-once；对账与补偿兜底。[cite:41]
6. **用事务性发件箱原子化"决策 + 通知"**：业务状态与 outbox 记录同事务提交，relay 负责可靠投递，消费端按 `event_id` 去重。[cite:39]
7. **并发控制按容忍度选择**：默认线程隔离 + 排队（enqueue）；需要强一致时用乐观并发（版本 CAS）或行级锁；分布式部署用 Redis 线程锁 + 409。[cite:44][cite:43][cite:45][cite:6]
8. **把"遗忘"当可靠性设计**：绑定策略放上下文之外、网关层强制；对压缩导致的策略丢失做监控。[cite:54]
9. **给记忆子系统做 SRE**：健康门、心跳遥测、告警预算，防止"静默记忆死亡"。[cite:50]
10. **记忆治理异步化**：用 sleeptime/后台 agent 做整合、去重、归档与过期，避免阻塞前台延迟。[cite:13][cite:14]

---

## 九、结论

AI Agent 的会话/记忆持久化与崩溃恢复，本质上是把成熟的分布式系统范式（检查点、WAL、事件溯源、幂等、发件箱、租约/围栏、Saga）应用到"LLM 在循环中调用工具"这一新负载上。其核心张力有三：

- **状态 vs 执行**：检查点保存状态，但崩溃恢复需要运行时/编排层（如 Temporal）来检测失败、定位检查点、重新进入图。[cite:34][cite:12]
- **持久性 vs 性能**：durability mode、检查点频率、后端选型都是同一旋钮的不同档位。[cite:12][cite:7]
- **记忆 vs 治理**：记忆既要"记得住"，更要"忘得对"——压缩与遗忘直接关联策略连续性与合规。[cite:54][cite:50]

可迁移的通用模型是：**以会话 ID 为主键的分层检查点 + 独立长期记忆存储 + 确定性/幂等的恢复语义 + 副作用一致性协议（幂等键/发件箱/租约）+ 并发控制 + 记忆子系统的可观测性**。任何单一机制都不足以覆盖全部失败面，端到端可靠性来自这些机制的组合。

---

## 十、置信度与局限说明

- **高置信（≥3 源交叉验证）**：检查点按 super-step 快照并以 `thread_id` 为主键；checkpointer 与 store 的职责分离；durability 三档语义；WAL "日志先于数据"不变量；Temporal 事件历史 + 重放；双写问题与事务性发件箱；幂等键/租约/围栏令牌；线程隔离与乐观并发。
- **中等置信**：Temporal + LangGraph 的 200ms 恢复、零丢失、0.7s 开销等具体数字——来自厂商/个人博客生产案例（P2），未见独立基准复现。[cite:34][cite:35]
- **需注意的立场性偏差**：Mem0、Letta、Redis 相关文章由各自厂商撰写，对其自身产品的能力描述（如 Mem0 的 91% p95 延迟改善、26% 相对提升）应视为厂商基准而非中立结论。[cite:21][cite:18]
- **时效性**：部分来源为 2025–2026 年的预印本与博客，API/版本信息（如 LangGraph 1.2.x、Redis checkpoint 0.1.0、Letta 0.16.x）可能随后变化，落地前应以官方文档复核。

---

## 参考来源

[cite:1] LangGraph — Checkpointers（Python 官方文档）. https://docs.langchain.com/oss/python/langgraph/checkpointers
[cite:2] LangGraph — Checkpointers（JavaScript 官方文档）. https://docs.langchain.com/oss/javascript/langgraph/checkpointers
[cite:3] LangGraph — 持久化执行（Durable Execution）官方文档. https://docs.langchain.org.cn/oss/python/langgraph/durable-execution
[cite:4] LangGraph — Persistence（Checkpointer vs Store）官方文档. https://docs.langchain.com/oss/javascript/langgraph/persistence
[cite:5] LangGraph — Interrupts 官方文档. https://docs.langchain.com/oss/javascript/langgraph/interrupts
[cite:6] LangChain — The runtime behind production deep agents（Deep Agents 概念指南）. https://www.langchain.com/conceptual-guides/runtime-behind-production-deep-agents
[cite:7] LangGraph — Pregel 参考（durability 参数）. https://reference.langchain.com/python/langgraph/pregel/
[cite:8] LangGraph persistence: Threads, checkpoints and Postgres（生产指南）. https://www.learnwithparam.com/blog/langgraph-persistence-thread-models-production
[cite:9] LangGraph + PostgreSQL: Durable Checkpoints for AI Agents. https://markaicode.com/integrate/langgraph-with-postgresql/
[cite:10] SqliteSaver: persistent checkpointing across restarts. https://theneuralbase.com/langgraph/learn/intermediate/sqlitesaver-persistent-checkpointing-across-restarts/
[cite:11] LangGraph Checkpointers in Production: Durable, Resumable Agents. https://www.callsphere.ai/blog/langgraph-checkpointer-durable-resumable-agents
[cite:12] Durable Execution in LangGraph: Agents That Survive Failure. https://www.vadim.blog/durable-execution-agents-that-survive-failure-and-resume-where-they-left-off/
[cite:13] Letta — Stateful Agent Server with Memory Blocks & Sleep-Time Compute. https://hivebook.wiki/wiki/letta-stateful-agent-server-with-memory-blocks-sleep-time-compute
[cite:14] Sleeptime Agents for Memory Consolidation: Best Practices（Letta 社区）. https://forum.letta.com/t/sleeptime-agents-for-memory-consolidation-best-practices-guide/154
[cite:15] Rearchitecting Letta's Agent Loop: Lessons from ReAct, MemGPT, & Claude Code. https://www.letta.com/blog/letta-v1-agent/
[cite:16] Letta (MemGPT) — The Memory-Native Agent Framework. https://chatforest.com/reviews/letta-memgpt-stateful-agent-framework/
[cite:17] The AI agents stack（Letta）. https://www.letta.com/blog/ai-agents-stack
[cite:18] Open-Source AI Agents: Built-in Memory & Persistence（Mem0）. https://mem0.ai/blog/open-source-ai-agents-with-built-in-memory
[cite:19] Building AI Chatbot With Persistent Memory（Mem0）. https://mem0.ai/blog/ai-chatbot-development-with-persistent-memory
[cite:20] AI agent frameworks and how to choose a memory strategy（Mem0）. https://mem0.ai/library/integrations/ai-agent-frameworks-and-how-to-choose-a-memory-strategy
[cite:21] Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory（arXiv:2504.19413）. https://arxiv.org/pdf/2504.19413v1.pdf
[cite:22] How to Fix CrewAI Memory in Production with Mem0. https://mem0.ai/blog/crewai-memory-production-setup-with-mem0
[cite:23] Memory Layer for Open Source Agent Frameworks（Mem0）. https://mem0.ai/blog/memory-layer-for-open-source-agent-frameworks
[cite:24] Persistent Memory for AutoGen Agents with Hindsight. https://hindsight.vectorize.io/blog/2026/04/06/autogen-persistent-memory
[cite:25] Sessions | OpenAI Agents SDK（官方文档）. https://openai.github.io/openai-agents-js/guides/sessions/
[cite:26] 运行智能体 | OpenAI Agents SDK（previousResponseId）. https://openai.github.io/openai-agents-js/zh/guides/running-agents/
[cite:27] Use the Responses API in WebSocket mode（Azure OpenAI）. https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/websockets
[cite:28] Managing context on the Claude Developer Platform（Anthropic 官方公告）. https://www.anthropic.com/news/context-management
[cite:29] Context editing — Claude Platform Docs（官方文档）. https://platform.claude.com/docs/en/build-with-claude/context-editing
[cite:30] Effective context engineering for AI agents（Anthropic Engineering）. https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
[cite:31] Event History | Temporal Documentation（官方文档）. https://docs.temporal.io/encyclopedia/event-history
[cite:32] Temporal Workflow（官方文档，Replay 语义）. https://docs.temporal.io/workflows.md
[cite:33] Understanding Temporal（官方文档，Durable Execution）. https://docs.temporal.io/evaluate/understanding-temporal
[cite:34] Temporal + LangGraph: Crash-Proof Agents That Resume in 200ms. https://dailyaiworld.com/workflow/temporal-langgraph-durable-execution-crash-proof-blueprint
[cite:35] Durable LangGraph Agents on Temporal: Crash Recovery at Scale. https://dailyaiworld.com/public/workflow/build-durable-langgraph-temporal-workflow-survive-crash-human-loop
[cite:36] PostgreSQL 16 — 30.5. WAL Configuration（官方文档）. https://www.postgresql.org/docs/16/wal-configuration.html
[cite:37] PostgreSQL 18 — 28.6. WAL Internals（官方文档）. https://www.postgresql.org/docs/current/wal-internals.html
[cite:38] Write-Ahead Logging (WAL): The Internals of Reliability and Recovery（Percona/PGConf.EU 2023）. https://www.postgresql.eu/events/pgconfeu2023/sessions/session/4896/slides/429/
[cite:39] Transactional Outbox and Saga Patterns for AI Agent Orchestration. https://zylos.ai/research/2026-06-06-transactional-outbox-saga-patterns-ai-agent-orchestration/
[cite:40] Building reliable event sourced agentic system with Kafka, Temporal and LangGraph. https://saumitra.me/2026/2026-06-13-building-reliable-agentic-systems/
[cite:41] AI Agent 明明只执行一次，为什么服务器却重启了两遍？（幂等/租约/围栏令牌实验）. https://blog.margrop.net/post/ai-agent-worker-crash-idempotency/
[cite:42] Cordon: Semantic Transactions for Tool-Using LLM Agents（arXiv:2606.17573）. https://arxiv.org/pdf/2606.17573v1.pdf
[cite:43] Google ADK — Session: Tracking individual conversations（并发与锁）. https://raw.githubusercontent.com/google/adk-docs/refs/heads/main/docs/sessions/session/index.md
[cite:44] 10xGraph — Durability and concurrency（线程隔离、乐观并发、StaleStateError）. https://agentflow.10xscale.ai/docs/guides/durability-and-concurrency.md
[cite:45] CopilotKit — Threads & Persistence Architecture（线程锁、409）. https://docs.copilotkit.ai/premium/threads-explained
[cite:46] LangGraph & Redis: Build smarter AI agents with memory & persistence. https://redis.io/blog/langgraph-redis-build-smarter-ai-agents-with-memory-persistence.md
[cite:47] Redis Agent Memory Server（PyPI）. https://pypi.org/project/agent-memory-server/
[cite:48] LangGraph Redis Checkpoint 0.1.0: From "Make it work" to "Make it fast". https://redis.io/blog/langgraph-redis-checkpoint-010.md
[cite:49] LangGraph + Redis + PostgreSQL Agent Stack: Production Guide. https://markaicode.com/stack/langgraph-redis-postgresql-agent-stack/
[cite:50] Memory as Infrastructure: Reliability Engineering for Persistent Agent Memory（arXiv:2609.05510）. https://arxiv.org/html/2609.05510
[cite:51] Agent Memory in 2026: A Research Survey Breakdown for Practitioners. https://niteagent.com/blog/agent-memory-research-2026/
[cite:52] Memory for Autonomous LLM Agents: Mechanisms, Evaluation, and Emerging Frontiers（arXiv:2603.07670）. https://arxiv.org/pdf/2603.07670.pdf
[cite:53] Silent Failure in LLM Agent Systems: The Entropy Principle（arXiv:2606.08162）. https://arxiv.org/html/2606.08162
[cite:54] Agent Memory Design: Three Failure Modes and the Enforcement Floor（Governance Decay, arXiv:2606.22528）. https://www.ideabosque.com/library/agent-memory-design-patterns-persistent-context/
[cite:55] Federated and Distributed AI Agent Memory Systems. https://zylos.ai/research/2026-04-29-federated-distributed-ai-agent-memory-systems/
