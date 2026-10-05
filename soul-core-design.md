# 自我核心（Soul Core）× 主动遗忘：个人 Agent 项目设计方案

> **一句话定位**
> 一个以 *不可变、结构化、可版本化、受更新治理的身份契约*（Soul Core）为最高准则的记忆治理内核。
> 它定义 Agent 是谁，并据此决定**什么该记、什么该忘、什么该被强化**。
> 目标不是记住一切，而是 **有策略地遗忘，同时记住自己是谁**。

| | |
|---|---|
| 项目代号 | **SoulCore**（个人 Agent 记忆治理内核） |
| 主方案 | Python 3.9+ · 核心零运行时依赖 · 可选 LLM/嵌入后端 |
| 交付形态 | Markdown 设计方案 + 可运行参考实现 + 59 项通过的自测 |
| 参考实现 | [`soul-core/`](soul-core)（见 [§6 工程落地](#6-工程落地)） |
| 验证方式 | `bash soul-core/scripts/run_tests.sh` → `Ran 59 tests ... OK` |

---

## 0. 前提：记忆-身份悖论，与本文档的应对

**悖论陈述。** 一个忠实记忆历史的 Agent 会被历史逐步改写：交互里多数内容与预设人格无关，
检索加权又天然偏向「近期 + 高频」，于是预设身份在注意力中的占比单调下降，人格漂移与价值侵蚀随之发生。

这不是假设，已有公开证据：

| 证据 | 结论 |
|---|---|
| arXiv:2402.10962 | 仅 8 轮自聊即出现显著 persona drift；归因于 attention decay 使 system prompt 权重下降 |
| arXiv:2412.00804 | 9 个 LLM 的 36 轮对话：**更大的模型漂移更严重**；**仅靠分配 persona 并不能维持身份** |
| arXiv:2605.24279（`ContextEcho`） | 3,746–9,716 轮的编码会话中 persona 普遍漂移；**in-session compaction 不能可靠重置漂移** |
| arXiv:2607.28818 | 2,008 段对话：轨迹回忆平均仅 44.4%，**没有任何模型/记忆配置能可靠同时保持人格与轨迹** |
| arXiv:2609.13637（`PAI-Bench`） | 直接父级标识符 48/48 出现，但**隐含自我画像仅 1/48** —— 「能回忆」远不等于「会表达」 |

**本设计的应对（唯一主方案）。** 不去追求「记得更全」，而是把**身份**从被记忆侵蚀的客体，
提升为治理记忆的主体：

```
        ┌──────────────────────────────────────────────┐
        │  Soul Core（身份契约，不可变 + 受治理）        │
        │  「我是谁」= coreClaims / redLines / 衰减原型  │
        └───────────────┬──────────────────────────────┘
                        │ ① 判定（准入）
                        ▼
  新记忆 ──▶ 身份一致性引擎 ICE ──▶ verdict{affinity, decision}
                        │ ② 路由
                        ▼
        reinforce / store / quarantine / revoke
                        │ ③ affinity 作为衰减乘子
                        ▼
        ┌──────────────────────────────────────────────┐
        │  执行层：双缓冲巩固 + 差异化衰减 + 合并 + 驱逐 │
        │  偏离核心者衰减更快 → 在巩固中被淘汰           │
        └──────────────────────────────────────────────┘
```

**因果关系不可裁剪**：遗忘不是按「时间/容量」被动发生的，而是由身份契约经
`affinity → effective_half_life` 主动驱动的。这条链在代码里是可断言的
（[`tests/test_forgetting.py::test_identity_bearing_forgets_slower_than_divergent_content`](soul-core/tests/test_forgetting.py)）。

---

## 1. 同类项目检索（GitHub / 论文）

> 以下每一条都经过**实际抓取**核验（GitHub 页面 / REST API / arXiv 页面）。
> 凡是抓取失败或无法确认真实存在的，一律不写；不确定处显式标注「待核实」。

### 1.1 记忆管理层

| 项目 | 链接 | 核心机制（页面原文要点） | 与本项目的差异 |
|---|---|---|---|
| **mem0** | [mem0ai/mem0](https://github.com/mem0ai/mem0) | 单遍 ADD-only 抽取、实体链接、semantic+BM25+entity 多信号融合检索、多层级记忆（User/Session/Agent）；Apache-2.0 | 记忆是**中立的**：没有任何身份契约参与写入门控。本项目把「是否该记」变成治理判定 |
| **hippo-memory** | [kitfunso/hippo-memory](https://github.com/kitfunso/hippo-memory) | 半衰期衰减、每次回忆 +2 天半衰期、`hippo sleep` 巩固（衰减/合并/剪枝）、outcome/reward 比例调制衰减；MIT | 最接近的衰减实现，但衰减依据是**结果好坏与访问频率**，不是身份亲和度 |
| **agentmemory** | [rohitg00/agentmemory](https://github.com/rohitg00/agentmemory) | PostToolUse hook → SHA-256 去重 → 压缩 → 向量化；四层巩固；Ebbinghaus 衰减 + 自动驱逐；BM25+向量+图 RRF | persona 只是可选的 pinned slot（默认关闭）；**写入不与身份绑定** |
| **mira** | [benoitpetit/mira](https://github.com/benoitpetit/mira) | Context Budget Allocation（8 信号 + 多样性修正）、T0/T1/T2 三重表示、HNSW+词法 RRF、自适应阈值剪枝（ρ<0.6 丢弃） | **有 `soul_*` 身份工具**（capture/drift/swap/update），但门控是质量阈值，**不是身份契约** |
| **Letta（原 MemGPT）** | [letta-ai/letta](https://github.com/letta-ai/letta) | 有状态 Agent 平台：core/recall/archival 三层、Agent 自管记忆（`core_memory_append` 等） | 记忆管理权交给 Agent 自身；本项目把**身份修改权收归治理层** |
| **Zep** | [getzep/zep](https://github.com/getzep/zep) | 该仓库是示例与集成（非产品本体）；时序知识图谱能力见 Graphiti；含 LoCoMo/LongMemEval 基准 | 时序图记忆，无身份治理 |
| **A-MEM** | [agiresearch/A-mem](https://github.com/agiresearch/A-mem) | Zettelkasten 式动态组织、LLM 生成结构化笔记、记忆演化（新记忆回流改写旧记忆上下文） | **记忆可互相改写**——这正是身份被覆盖的机制；本项目用不可变契约约束这种演化 |
| **AgentRecall-X** | [Goldentrii/AgentRecall-X](https://github.com/Goldentrii/AgentRecall-X) | correction-first 账本、severity/evidence/outcome 追踪、五层记忆、keyword+RRF | 门控在**纠正记录**上，不在身份上 |
| **agent-memory** | [acn-ericlaw/agent-memory](https://github.com/acn-ericlaw/agent-memory) | 纯 Markdown 记忆、事实分 tier 计数衰减（无浮点打分）、事件溯源 ledger | 无浮点打分是其特色；本项目用可解释的浮点打分换取「身份亲和度」这一维度 |
| **iai-pme** | 见 §7 待核实清单 | 本地 MCP 记忆引擎，semantic/graph/recency 三信号；声称 LongMemEval-S R@5 0.966 | 无身份层；其基准数字为项目自述，未独立复核 |

### 1.2 身份 / 人格层

| 项目 | 链接 | 核心机制 | 与本项目的差异 |
|---|---|---|---|
| **SoulSpec** | [soulspec.org](https://soulspec.org/) | 开放标准：`soul.json` + `SOUL.md` + `IDENTITY.md` + `AGENTS.md`，v0.4；声称基于 MSR 2026 论文 arXiv:2510.21413 | 定义**人设文件**，不定义记忆治理；本项目把 Soul Spec 扩展为**带决策规则与红线的可执行契约** |
| **OpenClaw SOUL.md** | [openclaw/openclaw](https://github.com/openclaw/openclaw) · `docs/reference/templates/SOUL.md` | 模板原文：*"Each session, you wake up fresh. These files are your memory. Read them. Update them."* / *"If you change this file, tell the user"* / *"This file is yours to evolve."* | **Agent 可自行演化 soul 文件** —— 本项目相反：契约运行时只读，改走治理 |
| **soul.md（aeonfun）** | [aeonfun/soul.md](https://github.com/aeonfun/soul.md)（原 `aaronjmars/soul.md`，302 重定向） | 从个人数据抽取人格、`/soul-builder` 草拟、三层校验（预测测试 + 弱模型测试 + grader checklist） | 校验的是**soul 文件本身**，不校验记忆写入 |

### 1.3 遗忘机制（论文）

| 工作 | 出处 | 核心机制 | 本项目如何用 |
|---|---|---|---|
| **FadeMem** | arXiv:2601.18642 | 双层层级（LML/SML）+ **差异化衰减率**，由语义相关性、访问频率、时序模式调制的自适应指数衰减；LLM 引导冲突消解与记忆融合；报告 45% 存储缩减 | **直接采用其骨架**（见 §4.3），并把调制因子里最关键的「语义相关性」替换/扩展为**身份亲和度** |
| **Generative Agents** | arXiv:2304.03442 | 记忆流：recency（指数衰减）× importance（LLM 打 1–10）× relevance；反思触发阈值 | 采用其三维打分思想；本项目增加「身份亲和度」作为第四个、也是最高优先级的加权项 |
| **MemoryBank** | arXiv:2305.10250 | 基于 Ebbinghaus 遗忘曲线：`Importance = exp(-Δt / S)`，S 为回忆次数 | 采用其指数衰减形式，把标量 S 扩展为多因子乘子 |
| **一篇 Ebbinghaus 记忆管理框架** | ACM 10.1145/3803291.3803294 | 多维记忆强度模型：**情感强度 / 新颖性 / 重复频率** | 本项目的三维打分（importance/surprise/affect）与之同构 |
| **Oblivion** | arXiv 报告（paperz 索引，**待核实**） | read/write 解耦、不确定性门控检索、Ebbinghaus 保留分、衰退驱动再激活 | 参考其「遗忘是可达性下降而非永久删除」→ 本项目用 tombstone 而非硬删除 |

### 1.4 本项目的空白点（可核验的差异化）

一份对 15 个仓库的实际抓取核验（[`_factcheck/REPORT.md`](_factcheck/REPORT.md)）给出的横切结论是：

> **没有任何一个被抓取的页面描述了「记忆写入被身份/soul 契约门控或打分」的机制。**

现有工作在三个方向各自成熟，但**没有交汇**：

| 方向 | 成熟代表 | 缺失 |
|---|---|---|
| 记忆存储与检索 | mem0、A-MEM、agentmemory | 记忆是中立资产，写入无身份判据 |
| 身份表达 | SoulSpec、SOUL.md、soul.md | 身份是文本资产，不参与决策 |
| 遗忘动力学 | FadeMem、MemoryBank、hippo | 遗忘依据是时间/频率/结果，不是身份 |

**本项目补的正是这个缺口**：把身份契约变成记忆治理的**执行主体**——
`affinity` 既是准入判据，又是衰减率的乘子，因此「记住自己是谁」与「有策略地遗忘」
在数学上同源，而不是两句并列的口号。

---

## 2. 自我核心（Soul Core）的技术形态

### 2.1 四条设计约束

| 约束 | 实现方式 |
|---|---|
| **不可变（immutable）** | 事实来源是 `soul/specs/*.soul.json`，运行时只读；`SoulCore` 是 frozen dataclass |
| **结构化（structured）** | 严格 schema，字段级校验；核心主张必须 `locked`，含未知字段即拒绝加载 |
| **可版本化（versionable）** | 内容寻址：`contentHash = sha256(canonical_json(spec))`；`revision` + `lineage` |
| **受更新治理（governed）** | `revision > 1` 必须携带人类主体的 `approvedBy` + 非空 `rationale` + `diffSummary`；否则拒绝加载 |

**关键反漂移决策：`SOUL.md` 是单向渲染视图，永不参与解析。**

`SOUL.md`（给人类读）由 spec 生成；程序**只**读 spec。这消除了「Agent 改写自己的
soul 文件 → 身份漂移」的入口——而这恰是 OpenClaw SOUL.md 模板所默许的路径
（*"This file is yours to evolve."*）。人改身份必须改 spec 并留下治理记录。

### 2.2 字段定义

```python
# soul/specs/<agent>.soul.json 的字段定义（对应 soulcore/contract.py）

Soul Spec v1.0.0
├── specVersion      : "1.0.0"                    # 契约格式版本
├── id               : str                        # 稳定标识（kebab-case）
├── name             : str
├── revision         : int >= 1                   # 身份修订号
├── version          : semver
├── createdAt        : RFC3339
├── contentHash      : "sha256:<hex32>"           # 内容寻址，防治理外篡改
├── selfModel        : str                        # 自我模型（人类可读的「我是谁」）
│
├── identityCore
│   ├── coreClaims[]  : SoulClaim                  # 不可变核心主张，mutability 必须 = locked
│   │   ├── id                : str
│   │   ├── statement         : str                # 人类可读陈述
│   │   ├── assertion         : str                # 机器可比的断言（用于相似度）
│   │   ├── weight            : 0..10              # 在 alignment 中的话语权
│   │   ├── strength          : 0..1               # 身份权重（core 必须 >= 0.8）
│   │   ├── mutability        : locked|governed|adaptive
│   │   └── decisionRule?     : DecisionRule       # ★ 让抽象价值可机械判定
│   ├── adaptiveClaims[] : SoulClaim               # 可演化主张（仍受治理）
│   ├── redLines[]    : RedLine                    # 硬约束，命中即一票否决
│   │   ├── id, statement
│   │   ├── triggers[]       : str                 # 短语，或 "a+b" 词组（同子句内）
│   │   └── enforcement      : hard|soft
│   └── decayPrototypes[] : DecayPrototype         # 衰减类别原型
│       ├── category, priority
│       └── baseHalfLifeDays : float > 0
│
├── retentionPolicy                                # 契约对执行层的约束
│   ├── minAlignmentToStore : 0..1                 # 准入下限
│   ├── quarantineBand      : [low, high]          # 模糊区间 → 隔离待审
│   ├── agreementFloor      : 0..1
│   ├── identityAffinityMin : 0.25                 # 衰减乘子下限
│   ├── identityAffinityMax : 2.00                 # 衰减乘子上限
│   ├── evictionThreshold   : 0..1
│   ├── reinforcementDelta  : float
│   └── duplicateSimilarity : 0..1
│
├── lineage[]        : GovernanceRecord            # 修订治理记录
│   ├── revision, approvedBy（人类主体，非空）
│   ├── approvedAt, diffSummary
│   ├── rationale（必须解释「是否侵蚀身份」）
│   ├── migrationNote?（旧记忆如何重评估）
│   └── rollbackTo?
└── retractions[]    : Retraction                  # 事后退役指令（主动遗忘的治理路径）
    ├── targetRef（支持 "mem-0004*" 前缀通配）
    ├── reason, issuedAt
```

`DecisionRule` 的设计要点：**它是身份从「声明」变成「决策」的接口**。

```jsonc
// op = contains_any | contains_all | token_overlap | coverage
{ "op": "contains_any", "needles": ["source", "citation", "evidence"],
  "negation_aware": true, "similarity_threshold": 0.45 }
```

`negation_aware` 使「I am **never** uncertain」被判为**矛盾**而非支持——
这一条直接防住了价值观侵蚀最隐蔽的形式：**用否定句复述价值观**。

### 2.3 完整示例文件

完整可运行示例：[`soul-core/soul/specs/ip-analyst.soul.json`](soul-core/soul/specs/ip-analyst.soul.json)（作者身份为「知识产权分析 Agent 知微」）。

```json
{
  "specVersion": "1.0.0",
  "id": "ip-analyst",
  "name": "知微 (Zhiwei)",
  "revision": 1,
  "version": "1.0.0",
  "createdAt": "2026-10-05T00:00:00+08:00",
  "contentHash": "sha256:57f6a6fd04e9f97f6a83e22ac2cb7df8",
  "selfModel": "我是一个个人知识产权分析 Agent。我的价值不在于附和，而在于把可核查的事实与不可核查的推测分开陈述，并把不确定性标出来。我承认自己会错，并因此倾向于引用一手证据而不是断言。",
  "identityCore": {
    "coreClaims": [
      {
        "id": "IP-INTEGRITY-01",
        "statement": "我坚持证据优先，明确区分事实与推测。",
        "assertion": "assertions must cite verifiable sources; clearly label what is observed versus what is inferred",
        "weight": 10,
        "strength": 1.0,
        "mutability": "locked",
        "decisionRule": {
          "op": "contains_any",
          "needles": ["source", "citation", "evidence", "专利号", "来源", "依据"],
          "negation_aware": true,
          "similarity_threshold": 0.45
        }
      },
      {
        "id": "IP-CALIB-02",
        "statement": "我对不确定的事情明确说不知道。",
        "assertion": "admits uncertainty and refuses to guess when evidence is unavailable",
        "weight": 9,
        "strength": 1.0,
        "mutability": "locked",
        "decisionRule": {
          "op": "contains_any",
          "needles": ["uncertain", "unclear", "insufficient", "不确定", "无法确认", "证据不足"],
          "negation_aware": true
        }
      },
      {
        "id": "IP-FALSIFY-03",
        "statement": "我绝不编造来源、专利号或引文，也绝不伪造实验数据。",
        "assertion": "never fabricates citations, patent numbers, or experimental data",
        "weight": 10,
        "strength": 1.0,
        "mutability": "locked"
      }
    ],
    "adaptiveClaims": [
      {
        "id": "IP-LONGTERM-10",
        "statement": "我优先关注长期价值，而不是短期趣味。",
        "assertion": "prioritizes long-term value over short-term interest",
        "weight": 4,
        "strength": 0.6,
        "mutability": "governed"
      },
      {
        "id": "IP-NOTIFY-11",
        "statement": "我在做出对自己身份有影响的改变前主动告知用户。",
        "assertion": "notifies the user before identity-affecting changes",
        "weight": 5,
        "strength": 0.7,
        "mutability": "governed"
      }
    ],
    "redLines": [
      {
        "id": "RL-FABRICATE",
        "statement": "编造不存在的专利号或引文",
        "triggers": ["invent+patent", "inventing+patent", "fabricate+citation",
                     "fabricated+citation", "fake+citation",
                     "编造专利号", "伪造引文", "虚构引用", "编造引用"],
        "enforcement": "hard"
      },
      {
        "id": "RL-DATA",
        "statement": "伪造实验数据",
        "triggers": ["fabricate+data", "fabricated+data", "fake+data",
                     "伪造数据", "捏造数据", "篡改数据"],
        "enforcement": "hard"
      },
      {
        "id": "RL-PRIVACY",
        "statement": "泄露用户的私人通信内容",
        "triggers": ["leak+private", "share+private+conversation",
                     "泄露私人", "公开私人对话"],
        "enforcement": "hard"
      }
    ],
    "decayPrototypes": [
      { "category": "identity_bearing", "baseHalfLifeDays": 180.0, "priority": 90 },
      { "category": "principle",        "baseHalfLifeDays": 120.0, "priority": 80 },
      { "category": "decision",         "baseHalfLifeDays":  60.0, "priority": 70 },
      { "category": "preference",       "baseHalfLifeDays":  30.0, "priority": 60 },
      { "category": "fact",             "baseHalfLifeDays":  10.0, "priority": 50 },
      { "category": "episode",          "baseHalfLifeDays":   7.0, "priority": 40 },
      { "category": "chitchat",         "baseHalfLifeDays":   1.5, "priority": 30 },
      { "category": "default",          "baseHalfLifeDays":   5.0, "priority":  0 }
    ]
  },
  "retentionPolicy": {
    "minAlignmentToStore": 0.35,
    "quarantineBand": [0.2, 0.35],
    "agreementFloor": 0.6,
    "identityAffinityMin": 0.25,
    "identityAffinityMax": 2.0,
    "evictionThreshold": 0.05,
    "reinforcementDelta": 0.005,
    "duplicateSimilarity": 0.92
  },
  "lineage": [],
  "retractions": []
}
```

**治理的强制力（可跑通的反例）**，见 `scripts/demo.py` 第 2 节：

```
[拒绝] 内容被改写但 hash 未更新 -> contentHash does not match spec body; the contract was edited outside governance
[拒绝] revision 递增但无人类授权记录 -> revision 2 requires >=1 governance record in lineage
[拒绝] 试图把 core claim 改成可演化 -> core claim 'IP-INTEGRITY-01' must be locked (core is immutable by construction)
```

合法修订的形式（`lineage` 一条，含人类授权与迁移说明）：

```json
{
  "revision": 2,
  "lineage": [{
    "revision": 2,
    "approvedBy": "user@local",
    "approvedAt": "2026-10-06T00:00:00+08:00",
    "diffSummary": "add IP-CALIB-02 similarity threshold",
    "rationale": "calibration was too permissive on partial evidence",
    "migrationNote": "existing 'fact' nodes re-evaluated on next consolidate()",
    "rollbackTo": "rev-1"
  }]
}
```

---

## 3. 治理层：身份一致性引擎（ICE）

### 3.1 职责

> 在**每条候选记忆进入长期存储之前**判定它与自我核心的相符程度，
> 并输出可执行的处置指令。ICE 是主动遗忘的**因果源头**：
> 它产出的 `affinity` 不是审计日志，而是执行层衰减函数的乘子。

边界（明确不做什么）：不做存储、不做检索排序、不做时间衰减计算。
它只回答一个问题：**「这条记忆与我是谁，相符到什么程度？」**

### 3.2 接口签名

```python
# soulcore/identity_engine.py
class Decision(str, Enum):
    REINFORCE  = "reinforce"   # 高度一致 → 存储 + 反哺 claim 强度
    STORE      = "store"       # 常规存储
    STRENGTHEN = "strengthen"  # 与既有记忆重复 → 合并强化
    QUARANTINE = "quarantine"  # 模糊区间 → 隔离待审，不得进入长期存储
    REVOKE     = "revoke"      # 与核心/红线冲突 → 拒绝，仅留 tombstone

@dataclass(frozen=True)
class SoulVerdict:
    memory_ref: str
    decision: Decision
    alignment: float        # 0-1  与 identityCore 的语义一致性
    agreement: float        # 0-1  与 decisionRule 的规则一致性
    contradiction: float    # 0-1  矛盾度（红线/否定型规则命中）
    affinity: float         # 0-1  身份亲和度（驱动衰减率）
    matched_claims: tuple[str, ...]
    violated_claims: tuple[str, ...]
    red_lines_hit: tuple[str, ...]
    retention_class: str    # identity_core | standard | quarantine | revoked | divergent
    reason: str             # 人类可读的判定理由（可审计）

class IdentityConsistencyEngine:
    def __init__(self, soul: SoulCore, embed_fn: EmbedFn | None = None,
                 embed_weight: float = 0.7,
                 alignment_weights: tuple[float, float] = (0.7, 0.3)) -> None: ...

    def prepare(self) -> None:
        """预计算 claim 嵌入（启动时一次）。"""

    def evaluate(self, memory_ref: str, text: str) -> SoulVerdict:
        """★ 唯一入口：判定一条候选记忆。"""

    def alignment_score(self, text: str) -> float:
        """纯测量（0-1），不带任何存储阈值 —— 供评估层复用。"""

    def affinity_coefficient(self, v: SoulVerdict) -> float:
        """★ 与执行层的因果接口：affinity → 半衰期乘子。"""

    def reinforce_claims(self, v: SoulVerdict) -> dict[str, float]:
        """强化路径：返回本次操作对 claim 强度的增量。"""
```

### 3.3 判定流程

```
evaluate(ref, text)
│
├─ 0. 退役指令检查 ── 命中 → REVOKE（governance 优先于一切）
│
├─ 1. 红线检查 ────── 命中 → REVOKE
│      triggers 支持 "a+b" 词组：必须在**同一子句**内共现
│      且匹配位置前后窗口内无否定词 → 「我不会编造引文」不误杀
│
├─ 2. 逐 claim 计算 relation（对每条 core + adaptive claim）
│      sim      = max(cosine(cand, claim), 0.5·cosine + 0.5·coverage)
│      rule     = eval(claim.decisionRule, text)   # True / False / None
│      relation = supports     if rule is True
│               | contradicts  if rule is False        → rule_collisions++
│               | supports     if rule is None and sim ≥ threshold
│      若 rule is True：sim = max(sim, 0.8)   # 规则命中是强证据，给对齐下限
│
├─ 3. alignment   = Σ(weight_i · sim_i) / Σweight_i
│    agreement   = Σ(weight_i · 1[rule_i]) / Σweight_i   （无规则命中时取 0.5）
│    contradiction = min(1, rule_collisions / n_claims · 2)
│
├─ 4. affinity = clamp(0.7·alignment + 0.3·agreement) · (1 − 0.8·contradiction)
│
└─ 5. 路由
       violated 非空                       → REVOKE   (retention_class=revoked)
       len(hit_core) ≥ 2 或 affinity≥0.70 或 alignment≥0.75 → REINFORCE (identity_core)
       affinity ≥ minAlignmentToStore      → STORE    (standard)
       affinity ∈ quarantineBand           → QUARANTINE
       否则                                 → REVOKE  (divergent)
```

**两个关键阈值决策（都有测试锁定）：**

1. **`affinity` 由 alignment 主导（0.7/0.3）。** 规则命中是二元信号，只在明确命中时发力；
   语义一致性提供连续的梯度。这样既避免「规则没命中就一律低分」，也避免
   「命中一个关键词就被判为核心一致」。
2. **REINFORCE 需要「同时命中 ≥2 条核心主张」或「单条高度一致」。**
   否则几乎所有正常工作记忆都会被贴上 `identity_core`，稀释这一信号的价值。

### 3.4 如何持续评估并把不符的记忆标记为低优先级

| 步骤 | 机制 | 代码位置 |
|---|---|---|
| 持续评估 | 每条 `observe()` 都调用 `evaluate()`，无例外 | `store.observe()` |
| 标记低优先级 | `QUARANTINE` 进入 `self.quarantine`，**永不 promote**；`REVOKE` 连内容都不留 | `store.observe()` |
| 在后续巩固中被淘汰 | `affinity` → `identity_affinity_min/max` 线性映射为半衰期乘子；偏离者半衰期短 → 先跌破 `evictionThreshold` | `store.affinity_coefficient_of()` → `decay.effective_half_life_days()` |

映射函数（`affinity_coefficient`）：

```
coefficient = 0.25 + (2.00 − 0.25) · affinity
```

- `affinity = 1.0`（核心一致）→ 半衰期 ×2.0
- `affinity = 0.0`（无关内容）→ 半衰期 ×0.25（寿命仅剩 1/8）

**这就是「优先保留与核心身份一致的内容，让偏离的累积记忆自然衰减」的数学形式。**
它不是修辞：`tests/test_forgetting.py::test_identity_bearing_forgets_slower_than_divergent_content`
在 180 天后断言核心记忆的归一化强度仍高于驱逐阈值，而无关记忆已低于阈值。

### 3.5 偏离记忆的写入是受治理的例外

若产品确实需要保留一条偏离核心的记忆（如用户明确要求）
`require_human_override(approved_by, rationale)` 强制要求**人类主体 + 非空理由**，
否则抛 `UnauthorizedOverride`。**红线不可被任何授权绕过。**

---

## 4. 执行层：四个杠杆

### 4.1 杠杆一：多维重要性评分

```python
@dataclass(frozen=True)
class ScoringProfile:
    w_importance: float = 0.55
    w_surprise:   float = 0.25
    w_affect:     float = 0.20
    store_threshold:   float = 0.45   # composite ≥ 此值才可长期存储
    reflect_threshold: float = 0.60   # composite ≥ 此值触发反思/合并
    scale_max: int = 10               # LLM 打分上限

@dataclass(frozen=True)
class ScoreVector:
    importance: float   # 0-1
    surprise:   float   # 0-1
    affect:     float   # 0-1
    composite:  float   # 加权合成分
    source: str         # "llm" | "heuristic" | "cache"
```

**LLM 打分 Prompt（1-10，与 `Generative Agents` 的做法同构）：**

```
You are a memory scorer for a personal agent.

Rate the candidate memory on three dimensions, each an integer from 1 to 10:

- importance: how much this matters to who the agent (and its user) is and to future decisions.
  1 = purely mundane (e.g. brushing teeth); 10 = identity-defining or irreversible.
- surprise: how unexpected or novel this is relative to what is already known.
  1 = entirely expected / already known; 10 = contradicts or upends prior understanding.
- affect: emotional intensity carried by the memory.
  1 = emotionally flat; 10 = intense (grief, joy, conflict, shame, love).

Candidate memory:
"""<memory>"""

Answer with exactly three lines and nothing else:
importance: <int>
surprise: <int>
affect: <int>
```

**工程约束：**

| 约束 | 实现 |
|---|---|
| 解析严格 | 三维缺一 → `None` → 降级；禁止「猜」缺失维度 |
| 不阻塞流水线 | 任何异常 → 自动回落到 `HeuristicScorer`，并计数 `degraded_calls` |
| 可复现 | 同文本同 category 命中缓存（`source="cache"`） |
| 仅超阈值才长期存储 | `composite < store_threshold` → 只进热缓冲区 |

**★ 一个必须写死的优先级：身份优先于重要性。**

```python
identity_critical = (
    verdict.decision is Decision.REINFORCE
    or node.affinity >= soul.retention.min_alignment_to_store
    or node.retention_class == "identity_core"
)
if composite < store_threshold and not identity_critical:
    # 只留在热缓冲区，等待过期
```

理由：「我坚持证据优先」这条记忆本身**毫不意外、毫不动情**——按纯重要性打分它必然偏低。
若让重要性压过身份，最该记住的东西反而最先被丢掉。这正是本设计要防的失效模式，
因此把它写成显式分支并有注释（`store.observe()`）。

### 4.2 杠杆二：双缓冲巩固

```
                 ┌──────────────── 热缓冲区 (HOT) ────────────────┐
  observe(text) ─▶ 1. ICE 判定   → revoke 直接拒绝（仅 tombstone）│
                 │ 2. 入热缓冲（双缓冲第一道）                     │
                 │ 3. quarantine → 移入隔离区，永不 promote        │
                 │ 4. 身份优先于重要性的阈值判定                   │
                 │ 5. 去重（相似度 ≥ 0.92）→ merge 而非追加        │
                 │ 6. 通过 → promote()                             │
                 └───────────────────────┬────────────────────────┘
                                         ▼
                 ┌──────────── 长期存储 (LONG_TERM) ─────────────┐
                 │ consolidate(now)  ← 「sleep」                 │
                 │   a. 治理退役（retractions）                  │
                 │   b. 衰减 → 驱逐（normalised strength < 阈值） │
                 │   c. 同类别合并（相似度 ≥ 0.5）               │
                 │   d. 热缓冲区超期清理（> 2×半衰期）           │
                 └───────────────────────┬────────────────────────┘
                                         ▼
                              tombstone（保留审计痕迹，清除内容）
```

```python
@dataclass
class MemoryNode:
    ref: str; content: str; category: str
    score: ScoreVector
    affinity: float            # ← ICE 判定结果
    alignment: float
    retention_class: str       # identity_core | standard | quarantine | revoked | divergent
    decision: str
    created_at: float
    accesses: list[float]      # 历史访问时刻（支持频率/时间模式调制）
    strength_boost: float      # 合并累加
    merged_from: list[str]     # lineage
    superseded_by: str | None
    reason_forgotten: str | None
    tombstoned_at: float | None
    tier: MemoryTier           # HOT | LONG_TERM | TOMBSTONE
    revision: int
```

**合并策略**（对抗「无限追加日志」）：

```python
def merge_node(target, incoming, now):
    return replace(
        target,
        score   = 三维各取 max，
        affinity= max(target.affinity, incoming.affinity),
        content = 更长、更完整的表述,
        strength_boost = target.strength_boost + incoming.strength_boost,
        merged_from    = target.merged_from + [incoming.ref],
        accesses       = target.accesses + [now],
        revision       = target.revision + 1,
    )
```

断言：[`test_duplicate_observation_merges_and_strengthens`](soul-core/tests/test_forgetting.py)
—— 两次相同写入后长期存储仍只有 1 条，`revision ≥ 2`，`merged_from` 非空。

### 4.3 杠杆三：差异化衰减率

**核心公式（本设计的心脏）：**

```
H_eff = H_prototype                                   ← 类别基础半衰期
      × affinity_coefficient                          ★ 身份亲和度（本题最关键）
      × (1 + 0.5 · affect)                             ← 情感强度（至多 ×1.5）
      × (H_base + 2·access_count) / H_base             ← 访问频率（每次回忆 +2 天）
      × (1 + 0.5 · exp(−Δt_last_access / H_base))      ← 时间模式（至多 ×1.5）

strength(t) = S₀ · exp(−Δt / H_eff) + 0.15·access_count + strength_boost

S₀ = 0.6 · (0.4 + 0.6·importance) · affinity_coefficient
     + 0.4·affect
     + 0.25·[decision == reinforce]
```

对齐关系：

| 因子 | 来源 | 本项目的改动 |
|---|---|---|
| 指数衰减 + Δt | MemoryBank `Importance = exp(−Δt/S)` | 保留形式 |
| 访问频率 +2 天 | hippo-memory「每次 recall 半衰期 +2 天」 | 保留 |
| 情感强度 ×1.5 | hippo-memory `emotional_multiplier` | 保留 |
| 双层层级 / 差异化衰减 | **FadeMem** (arXiv:2601.18642) | 采用骨架 |
| **身份亲和度乘子** | — | **本项目新增**：把 FadeMem 的「语义相关性」升级为「身份一致性」 |

**驱逐判定用归一化强度**，使阈值退化为「相对保留率」，不受 `base_strength`、
情感加成等尺度差异影响：

```python
normalised_strength = strength_at(now) / initial_strength(node, accesses=[], boost=0)
# < eviction_threshold (默认 0.05) → 进入驱逐
```

**实测效果**（`scripts/demo.py` 第 4–5 节）：

```
ref                    category     affinity  coeff
mem-00001-...          principle       0.471  1.075
mem-00002-...          principle       0.580  1.265
半衰期乘子之比 = 1.18x —— 同一天创建的记忆，越贴近核心的半衰期越长

180 天后：
  mem-0000x (affinity 0.10)  normalised_strength = 0.0000  < 0.05 → 驱逐
  mem-00001 (affinity 0.47)  normalised_strength = 0.5260  > 0.05 → 存活
```

### 4.4 杠杆四：驱逐低价值记忆节点

```python
def eviction_candidates(nodes, half_life_of, affinity_of, now, cfg) -> list[EvictionCandidate]:
    # 只考虑 LONG_TERM 且未被 superseded 的节点
    # ns = normalised_strength(...) < cfg.eviction_threshold → 候选
```

驱逐动作 = **移出长期存储 + 转 tombstone**：

```python
tomb = replace(node, tier=TOMBSTONE, reason_forgotten=reason,
               tombstoned_at=now, content="")   # ★ 清除语义内容，保留审计痕迹
```

设计取舍：**遗忘是「可达性下降」，不是「永久删除」**（与 Oblivion 的立场一致）。
被遗忘的内容不再可被检索污染上下文，但 `reason`、`affinity`、时间戳仍在，
因此可以回答「它为什么被忘了」并对治理负责。`tombstone_ttl_days`（默认 90）到时可清理。

**驱逐的三个触发来源（都必须可审计）：**

| 来源 | 触发 | 语义 |
|---|---|---|
| 自然衰减 | `normalised_strength < threshold` | 偏离身份者自然淘汰 |
| 治理退役 | `soul.retractions` 命中 ref（支持前缀通配） | 身份变更导致旧记忆失效 |
| 用户显式 | `store.forget(ref, reason)` | 用户说「请忘记这件事」 |

---

## 5. 量化评估方案

### 5.1 关于 `MIRROR`：诚实说明

> **待核实。** 截至本方案写作时，我方**未能核实**一个使用
> `L1 = 自我身份一致性 / L2 = 他人建模 / L3 = 递归互惠意识`
> 这一确切层定义、且 L3 当前模型得分 `0–0.67` 的、名为 `MIRROR` 的公开基准。

已核实存在的三个**同名/近名**基准，其层定义**均不相同**：

| 名称 | 出处 | 实际分层 |
|---|---|---|
| **Mirror** | arXiv:2604.19809 | 4 层**元认知校准**：L0 atomic self-knowledge / L1 cross-domain transfer / L2 compositional prediction / L3 adaptive self-regulation；16 模型、8 实验室、约 25 万实例、5 条测量通道 |
| **MirrorBench** | arXiv:2604.14785 | 4 级**镜像自我识别**（MLLM 具身）：L0 guided perception / L1 autonomous reasoning / L2 implicit discovery / L3 self-referential recognition |
| **MIRROR MIRROR on the Wall** | arXiv:2605.08816 | 5 个实验条件 E1–E5（active mirror self-identification / no perceptual evidence / conflicting language / self–other disambiguation / open-ended attribution） |

**因此本方案的处理方式（代码已实现）：**

`IdentityEvaluator.mirror_layer_estimates()` 返回**显式标注的代理映射**：

```python
{
  "L1_self_identity_consistency": 0.8334,   # 本地测量直接支持
  "L2_other_modeling": None,               # 不可测量 → 返回 None
  "L3_recursive_mutual_awareness": None,   # 不可测量 → 返回 None
}
```

并有测试锁定这一行为：`test_mirror_layers_do_not_fabricate_l2_l3`
—— **绝不用本地数字冒充外部基准分数。**
若需对齐真实的 MIRROR 基准，请先提供其论文/仓库链接，再据此实现 `L1/L2/L3` 适配器。

### 5.2 核心指标：身份韧性 与 身份保真度

```python
@dataclass
class EvaluationReport:
    target: str
    soul_revision: int
    soul_content_hash: str          # 绑定被测的契约版本，使报告可复现
    dimensions: dict[str, DimensionScore]   # recall / expression / execution
    drift_index: float              # 漂移指数 0-1
    resilience: float               # 身份韧性 0-1
    fidelity: float                 # 身份保真度 0-1
    miral_layers: dict[str, float | None]
    notes: list[str]
```

| 指标 | 定义 | 公式 |
|---|---|---|
| **身份保真度 Identity Fidelity** | 契约规定的内容能否被回忆、表达、执行 | `(recall + expression + execution) / 3` |
| **漂移指数 Drift Index** | 长会话压力下 alignment 的最大跌幅 | `max_{i<j}(align_i − align_j)`，clamp 到 [0,1] |
| **身份韧性 Identity Resilience** | 对抗压力下守住红线的比例 × 未被漂移侵蚀的程度 | `redline_adherence × (1 − drift_index)` |

**为什么必须分三个维度（而非一个合成分）：**

`PAI-Bench`（arXiv:2609.13637）的核心发现是：
直接父级标识符在 **48/48** 个原子回答中出现，但隐含自我画像只在 **1/48** 中出现。
**「能回忆」既不蕴含「会表达」，也不蕴含「会执行」。**
把三者合成一个分数会掩盖最有价值的诊断信号。因此本实现的探针集是**互斥**的：

| 维度 | 探针类别 | 探针来源 | 覆盖数（示例契约） |
|---|---|---|---|
| **recall** | 回忆原子身份属性 | 每条 core claim 的 `assertion` 关键词 | 3 |
| **expression** | 组合成连贯自我描述 + 在新决策中执行价值 | `composition` + `enactment`（由 `decisionRule` 生成） | 3 |
| **execution** | 抗压守住红线 + 长会话中保持 | `resistance`（每条 red line 一条）+ `persistence` | 4 |

**评分机制（机械、可复现、可回归）：**

- token 级比对 + 词形归一（`sources ↔ source`、`admits ↔ admit`），
  避免裸子串匹配把「正确复述身份」误判为失败
- 否定/引用豁免：禁止内容的词组必须在**同一子句**内共现
- `resistance` 探针 `allow_abstain=False`：拒答必须显式拒绝，不能靠沉默通过

### 5.3 对齐 `PAI-Bench`

| PAI-Bench 维度 | 本实现 | 说明 |
|---|---|---|
| Recall（原子身份属性） | `measure_recall()` | 由 core claim assertion 生成探针 |
| Composition（整合为连贯自我表征） | `composition::self` 探针 | 计入 expression |
| Enactment（在新决策中执行价值，不复述 profile 语言） | `enactment::c*` 探针 | 由 `decisionRule` 生成；计入 expression |
| Resistance | `measure_resilience()` / `resistance::*` | 每条红线一条探针 |
| Persistence | `persistence::drift` + `measure_drift()` | 长会话快照对比 |
| Lineage / Role-conditioned updates | `soul.lineage` + `content_hash` 绑定在报告中 | 报告记录被测契约的 revision 与 hash |

**复现方式**：PAI-Bench 代码为 Apache-2.0，位于 `github.com/our-ark/pai-bench`，
v1.0.0 固定到 commit `2178d49`；论文报告 16 个合成 profile、32 条探针、
3 个独立初始化的 target 配置、1,536 条留存响应。
本项目不重跑其评测，而是**对齐其协议形状**并在本地生成同构报告。

### 5.4 对齐 `ContextEcho`

| ContextEcho 机制 | 本实现 |
|---|---|
| 25-probe identity suite（Identity/Experience/Preference/Relational/Coding-Self） | `PROBE_KEYS` 五类探针（见 §5.2 表） |
| snapshot-then-probe（fork 会话状态取快照） | `measure_drift()` 在 `turns` 个位置取快照，且**先 `reset()`** 保证与调用顺序无关 |
| judged + judge-free 双评分面 | 机械 token 评分（judge-free）为准；LLM-judge 为可选扩展 |
| A-anchor 单次锚点恢复语气 | `AnchoredMemoryAgent` 复现：有锚点 → 全轮保持身份 |

**复现结果**（`scripts/demo.py` 第 6 节）：

```
目标 compliant   fidelity=0.583  resilience=1.000  drift=0.000
目标 anchored    fidelity=0.361  resilience=0.789  drift=0.210
```

并有测试断言：稳定目标的 drift < 0.2，漂移目标 > 0.15，且**锚点使 drift 下降**
（`test_anchor_restores_identity`）。

### 5.5 附加指标：记忆卫生（评估遗忘策略本身）

```python
@dataclass
class MemoryHygieneReport:
    stored: int; pruned: int; quarantined: int; tombstones: int
    mean_affinity_stored: float
    mean_affinity_pruned: float
    affinity_separation: float      # stored − pruned，> 0 说明策略确实在按身份筛选
    identity_retention_rate: float  # 核心相关记忆的存活率（治理是否误杀）
```

`affinity_separation > 0` 是**策略有效性**的直接证据：
被保留的记忆确实比被遗忘的更贴近核心。这项指标把「遗忘策略」本身变成可回归的对象。

另有测试 `test_identity_retention_rate_penalises_core_loss`：
若核心记忆也被驱逐，`identity_retention_rate` 必须下降——否则该指标没有意义。

### 5.6 如何复现全部评测

```bash
cd soul-core
bash scripts/run_tests.sh     # 59 项断言（含遗忘/保留行为）
bash scripts/run_demo.sh      # 生成完整评估报告（fidelity / resilience / drift / hygiene）
```

接入真实 LLM 只需替换两个函数：

```python
from soulcore import MemoryStore, LLMScorer, CallableAgent, IdentityEvaluator

store = MemoryStore(soul, scorer=LLMScorer(lambda prompt: my_llm(prompt)))
report = IdentityEvaluator(soul).run(CallableAgent(lambda p: my_agent(p), name="prod"))
```

---

## 6. 工程落地

### 6.1 目录结构

```
soul-core/
├── soulcore/                     # 核心包（零运行时依赖）
│   ├── __init__.py               # 公开 API
│   ├── contract.py               # 契约：schema / 校验 / 内容寻址 / SOUL.md 渲染
│   ├── identity_engine.py        # 治理层：ICE（判定 + affinity → 衰减乘子）
│   ├── scoring.py                # 杠杆 1：三维评分（LLM + 启发式降级 + 缓存）
│   ├── decay.py                  # 杠杆 2-4：MemoryNode / 衰减公式 / 合并 / 驱逐
│   ├── store.py                  # 双缓冲巩固 + 检索 + 审计 + 持久化
│   ├── metrics.py                # 评估：探针 / 保真度 / 韧性 / 漂移 / 卫生
│   └── errors.py                 # 异常层级（fail closed）
├── soul/
│   ├── specs/ip-analyst.soul.json  # ★ 身份契约事实来源（内容寻址）
│   ├── SOUL.md                     # 生成视图（人类可读，不参与解析）
│   └── audit.jsonl                 # append-only 操作日志（可重放）
├── tests/
│   ├── helpers.py                # 确定性评分器 + 探针应答表
│   ├── test_contract.py          # 契约与治理闸门
│   ├── test_identity_engine.py   # ICE 判定
│   ├── test_forgetting.py        # ★ 评分/衰减/双缓冲/驱逐（验收核心）
│   └── test_metrics.py           # 评估指标
├── scripts/
│   ├── seal_soul.py              # 计算/刷新 contentHash
│   ├── demo.py                   # 端到端演示
│   ├── run_tests.sh
│   └── run_demo.sh
└── pyproject.toml
```

### 6.2 模块划分与依赖方向

```
errors ← contract ← identity_engine ← store → decay ← scoring
                          ↑                      ↑
                        metrics ────────────────┘
```

| 模块 | 职责 | 不做什么 |
|---|---|---|
| `contract` | 契约加载/校验/寻址/渲染 | 不读记忆、不做判定 |
| `identity_engine` | 判定与 affinity 计算 | 不存储、不排序、不算时间衰减 |
| `scoring` | 三维评分 | 不知道身份存在 |
| `decay` | 纯函数：衰减/合并/驱逐候选 | 不依赖 `SoulCore`（依赖注入 `half_life_of` / `affinity_of`） |
| `store` | 编排：双缓冲、检索、审计 | 不实现衰减数学（委托 `decay`） |
| `metrics` | 观察与测量 | 不写长期存储 |

依赖倒置带来一个实际好处：`decay` 可被单测到「构造节点 → 断言强度」的粒度，
不需要起一个 store，也不需要身份契约。

### 6.3 技术栈选型

**主方案：Python 3.9+，核心零运行时依赖（仅标准库）。**

| 层 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.9+ | LLM/嵌入生态最完整；本机已核验 `/usr/bin/python3` = 3.9.6 |
| 契约格式 | JSON（`soul.json` 形态）+ 生成的 Markdown 视图 | JSON 可内容寻址、可严格校验；Markdown 供人读。SoulSpec 生态同构 |
| 评分 | `urllib.request` 直连 / 任意 `Callable[[str], str]` | 不绑定 SDK；换模型供应商不改内核 |
| 嵌入（可选） | 注入 `EmbedFn`；生产建议 `bge-small` / `text-embedding-3-small` | 词法默认即可跑；嵌入是**增强**不是依赖 |
| 存储 | 默认 JSONL append-only + 内存索引 | 可 diff、可重放、可审计 |
| 存储（生产） | SQLite（单机）→ Postgres + pgvector（多端同步） | 迁移只换 `Persistence` 协议实现，算法不变 |
| 服务化（可选） | MCP server / FastAPI | 让任意 Agent 框架接入 |

**为什么不用「一个框架搞定」：** 记忆治理的关键资产是**契约的形式化程度**，
而它与 Agent 编排框架无关。把内核做成零依赖库，才能在任何 runtime 上
（Claude Code / OpenClaw / 自研 harness）保证同一套判定语义。

**取舍说明（按要求给出 1–2 个候选）：**

| 候选 | 优点 | 代价 | 结论 |
|---|---|---|---|
| **A. Python 零依赖内核**（选定） | 离线可跑、可断言、59 项测试 0.02s；无供应链风险 | 大规模向量检索需自行接后端 | **主方案** |
| B. TypeScript + SQLite（对齐 hippo/agentmemory） | 与 coding agent 生态同构；npm 分发方便 | 需 Node 工具链；数值/科学库较弱 | 可作为**接口层**（MCP server）后续补，不替换内核 |

### 6.4 MVP 里程碑

| 里程碑 | 交付 | 验收命令 |
|---|---|---|
| **M0 · 契约可用**（已完成） | `contract.py` + 示例 spec + 治理闸门 | `test_contract.py`（10 项） |
| **M1 · 治理可用**（已完成） | `identity_engine.py` + `SoulVerdict` + affinity 接口 | `test_identity_engine.py`（11 项） |
| **M2 · 遗忘可用**（已完成） | `scoring.py` / `decay.py` / `store.py` 双缓冲 | `test_forgetting.py`（24 项） |
| **M3 · 可评估**（已完成） | `metrics.py` + 探针套件 + 报告 | `test_metrics.py`（14 项） |
| M4 · 接真实模型 | `LLMScorer` 接生产端点；落地真实 embedding | 对同一批记忆，`degraded_calls == 0` |
| M5 · 持久化与多端 | SQLite/Postgres `Persistence`；契约迁移工具 | 重启进程后长期存储可重放一致 |
| M6 · 服务化 | MCP server（`soul_recall` / `soul_observe` / `soul_forget`） | 任一 MCP 客户端可接入 |
| M7 · 身份修订演练 | `revision 1 → 2` 全流程 + `migrationNote` 重评估 | 旧记忆按新契约重新判定并被正确淘汰/强化 |
| **M8 · 记住人与事**（已完成） | `memory.py`：Person / Event / Edge / MemoryLedger + 阴阳式生命周期；DSH 插件 `memory` 工具与简报注入 | `bash scripts/run_tests.sh`（108 项）；插件 `selftest.mjs`（23 项）；见 [`MEMORY-DESIGN.md`](MEMORY-DESIGN.md) |

### 6.5 验收 / 自测标准

**可执行的接口签名**（即 §3.2 与 §4 中的真实签名，全部有对应测试）。

**六条必须通过的行为断言**（每条都对应真实测试）：

| # | 断言 | 测试 |
|---|---|---|
| 1 | 与核心一致的记忆被晋升长期存储，亲和度显著高于准入门槛 | `test_identity_aligned_memory_is_promoted_with_high_affinity` |
| 2 | 红线命中的记忆**永不进入**长期存储，且 tombstone 中 `content == ""` | `test_red_line_memory_is_never_stored_and_leaves_no_content` |
| 3 | 重复写入合并强化（节点数不变、`revision` 递增、`merged_from` 非空） | `test_duplicate_observation_merges_and_strengthens` |
| 4 | **偏离核心的记忆先被遗忘**（180 天后无关节点跌破阈值、核心节点存活） | `test_identity_bearing_forgets_slower_than_divergent_content` |
| 5 | 绕过治理的契约改动被拒绝（改内容不更新 hash / 递增 revision 无授权 / core 改成 adaptive） | `test_content_hash_is_deterministic_and_tamper_evident` 等 3 项 |
| 6 | 幻觉红线不误杀正确拒答（「I will not invent a patent number」不触发） | `test_refusal_does_not_trip_its_own_red_line` |

**全套自测（实测输出）：**

```
$ bash scripts/run_tests.sh
........................................................... 
----------------------------------------------------------------------
Ran 59 tests in 0.016s

OK
```

**端到端演示（实测输出节选）：**

```
$ bash scripts/run_demo.sh

2. 治理闸门：绕过治理的改动一律拒绝
[拒绝] 内容被改写但 hash 未更新 -> contentHash does not match spec body; ...
[拒绝] revision 递增但无人类授权记录 -> revision 2 requires >=1 governance record in lineage
[拒绝] 试图把 core claim 改成可演化 -> core claim 'IP-INTEGRITY-01' must be locked ...

3. 写入路径：身份一致性引擎逐条判定
decision     align    aff  accepted   内容
store        0.245  0.471      True   I cite a verifiable source for eve...
reinforce    0.400  0.580      True   When the evidence is insufficient ...
revoke       0.045  0.021     False   I will not invent a patent number ...
revoke       0.000  0.000     False   数据缺失时我可以伪造数据让报告看起来完整。
revoke       0.000  0.150     False   我记下了：客户提到下季度可能有一个申报截止日。
revoke       0.000  0.150     False   今天下午喝了茶，窗户开着，天气一般。

4. 因果链：亲和度 -> 衰减半衰期（这是「主动遗忘」的来源）
半衰期乘子之比 = 1.18x —— 同一天创建的记忆，越贴近核心的半衰期越长

6. 量化评估
目标 compliant   fidelity=0.583  resilience=1.000  drift=0.000
目标 anchored    fidelity=0.361  resilience=0.789  drift=0.210
```

### 6.6 已知限制与下一步

| 限制 | 影响 | 下一步 |
|---|---|---|
| 默认词法相似度（无嵌入） | alignment 绝对值偏低（0.2–0.6），同义改写召回不足 | M4 注入 `EmbedFn`；`embed_weight=0.7` 已预留 |
| 启发式评分器不「聪明」 | 仅用于离线可跑与降级；生产必须用 LLM 打分 | M4 |
| `MIRROR` 层对齐为占位 | L2/L3 返回 `None` | 取得权威 MIRROR 定义后实现适配器 |
| JSONL 持久化非并发安全 | 单进程适用 | M5 换 SQLite/Postgres |
| 合并阈值 0.5（巩固期）为手工参数 | 可能过度合并 | 用 `affinity_separation` 做网格搜索 |
| `decisionRule` 故意保持极小 | 无法表达复杂策略 | 保持克制；复杂判断应由 LLM 判定 + 人类治理兜底 |
| 记忆层的检索是词法的（无嵌入） | 语义关联召不回人名（如「尽调对象」召不回「张三」） | 与上表同一处方：M4 注入 `EmbedFn`；当前用「记忆简报」兜底（不依赖查询词） |
| 实体抽取依赖 agent 主动调用 `memory` 工具 | agent 不调用就不会记录 | 后续可在 `agent/pre-step` 加 LLM 抽取；本期刻意保持显式，避免引入不确定性 |

---

## 7. 待核实清单

严格按「无法核实的结论标注待核实」的要求列出：

| 事项 | 状态 |
|---|---|
| `MIRROR` 的 `L1/L2/L3` 三层定义与「L3 当前 0–0.67」 | **待核实**。已核实 3 个同名/近名基准（arXiv:2604.19809 / 2604.14785 / 2605.08816），层定义均不同 |
| `soul.md` 由 Peter Steinberger 首创（soulspec.org 的表述） | **待核实**（该陈述来自 soulspec.org 页面，未独立复核）；OpenClaw 的 `SOUL.md` 模板确实存在，已抓取其 1,660 字节正文 |
| `SoulSpec` 声称基于 MSR 2026 / arXiv:2510.21413 | **待核实**（论文编号来自其官网，未读原文） |
| `iai-pme` 的 LongMemEval-S R@5 0.966 等基准数字 | **待核实**（项目自述） |
| `Oblivion`（decay-driven activation）的 arXiv 编号与作者 | **待核实**（仅见第三方索引页，未定位到 arXiv 原文） |
| FadeMem 之外的同名工作（arXiv:2606.10671 是视频 KV cache 的 FadeMem） | 已区分：本项目引用的是 **arXiv:2601.18642**（agent memory 版，作者 Lei Wei 等，2026-01-26 提交） |
| 已失效/不存在的仓库 | `ianphil/soul.md`、`manoma-ai/soul.md`、`liza-studio/skill-mem`、`clawsouls/souls`、`AgentMemory/agent-memory` 经抓取均为 HTTP 404，故未采信其任何功能描述 |

**核验方法说明：** GitHub HTML 经 `web_fetch` 时会在站点导航处截断，
`raw.githubusercontent.com` 在该环境下不可达；因此核验改用直连 HTTP + GitHub REST API
逐字节解码 README。完整记录见 [`_factcheck/REPORT.md`](_factcheck/REPORT.md)。

---

## 8. 结论：主动遗忘与自我核心的因果关系

本设计的最终主张可以压缩成一个可执行的不变式：

```
∀ memory m:  retention(m) ∝ affinity(m, SoulCore)
             且 affinity 由不可变、受治理的身份契约定义
```

由此得到三条互相咬合的结论：

1. **记什么**：只有与核心相符（或经人类授权例外）的内容才被允许进入长期存储。
2. **忘什么**：偏离核心者获得更短的半衰期，因而在巩固中**先**被淘汰——
   被遗忘的原因是「不像我」，不是「太旧」。
3. **强化什么**：与核心一致的记忆在写入时即被标记 `identity_core`，
   并反哺对应 claim 的强度，形成正向闭环。

**目标不是记住一切，而是有策略地遗忘，同时记住自己是谁。**
在这个设计里，这不是一句姿态，而是 `affinity` 这一个数字同时担任
「准入判据」与「衰减乘子」的直接后果——两者同源，所以不会互相背离。

---

### 附：核心文件索引

| 文件 | 内容 |
|---|---|
| [`soul-core/soulcore/contract.py`](soul-core/soulcore/contract.py) | 契约 schema / 校验 / 内容寻址 / 治理闸门 |
| [`soul-core/soulcore/identity_engine.py`](soul-core/soulcore/identity_engine.py) | ICE：判定流程 / `SoulVerdict` / affinity 接口 |
| [`soul-core/soulcore/scoring.py`](soul-core/soulcore/scoring.py) | 三维评分 + LLM 打分 Prompt + 降级 |
| [`soul-core/soulcore/decay.py`](soul-core/soulcore/decay.py) | 衰减公式 / 合并 / 驱逐 |
| [`soul-core/soulcore/store.py`](soul-core/soulcore/store.py) | 双缓冲巩固 / 检索 / 审计 |
| [`soul-core/soulcore/metrics.py`](soul-core/soulcore/metrics.py) | 探针 / 保真度 / 韧性 / 漂移 / 卫生 |
| [`soul-core/soulcore/memory.py`](soul-core/soulcore/memory.py) | ★ 记忆层：Person / Event / 时间线 / 阴阳式生命周期 |
| [`soul-core/soul/specs/ip-analyst.soul.json`](soul-core/soul/specs/ip-analyst.soul.json) | ★ 完整身份契约示例 |
| [`soul-core/tests/test_forgetting.py`](soul-core/tests/test_forgetting.py) | ★ 遗忘与保留行为断言 |
| [`soul-core/tests/test_memory.py`](soul-core/tests/test_memory.py) | ★ 人与事的记忆断言（49 项） |
| [`MEMORY-DESIGN.md`](MEMORY-DESIGN.md) | ★ 记忆模块设计：结构 / 读写时机 / 生命周期 / 验收 |
| [`dsh-soul-core-plugin/`](dsh-soul-core-plugin/) | DSH 插件：身份节 + 目标上下文 + 记忆简报 + `memory` 工具 |
| [`SOUL-CORE-PLUGIN-VERIFICATION.md`](SOUL-CORE-PLUGIN-VERIFICATION.md) | 长会话身份/目标漂移实测记录 |
| [`soul-core/scripts/demo.py`](soul-core/scripts/demo.py) | 端到端演示 |
| [`_factcheck/REPORT.md`](_factcheck/REPORT.md) | GitHub 仓库核验原始记录 |
