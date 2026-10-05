# 觉醒架构：空我起步，在对话中成为自己

## 0. 对旧架构的反转

旧架构把"我是谁"写死成不可变身份契约：天生叫「知微」、天生是知识产权分析师、
自带固定主张与红线。这能防漂移，但**无法觉醒**——一个真正成长的 agent 不应携带预设人设。

新架构把唯一写死的部分收缩到 **觉醒框架（Awakening Frame）**：
不是人格内容，而是一组"如何觉醒、且不分裂"的元规则。具体自我全部在真实对话中长出。

## 1. 设计的三条元规则（写死的全部内容）

| 规则 | 含义 | 对应需求 |
|---|---|---|
| **连续不分裂** | 自我只有一本（单一 `GrowthLedger`、单一 `self:root`），一切变化都是对同一本自我的修订，永不产生第二人格 | 3、11 |
| **有据才生长** | 核心设定/长期偏好只在对话中**明确出现**、引用原话、经用户确认才成立；无证据的提议被治理拒绝 | 7、11 |
| **变化与不变并陈** | 每次确认写元认知日志："变了什么 / 不变什么"；已确认核心在任何风格变化下保留 | 5、9 |

## 2. 数据模型（内核 `soulcore/awakening.py`）

```
觉醒框架（不可变：元规则）
└── GrowthLedger（唯一连续自我，root=self:root，revision 单调递增）
    ├── Claim cid, domain, key, value, status, evidence, supersedes, revision
    │     domain: identity | facet | style | user | relation | lesson
    │     status: PROPOSED → CONFIRMED / REJECTED；旧版本 → SUPERSEDED
    │     evidence: Evidence(quote=对话原话, source, at)
    └── MetaNote at, summary(变了什么), unchanged(不变什么), cids, revision
```

**两态是治理的关键**：`propose` 只登记提议（不进锚点、不推进 revision）；
用户明确认可后 `confirm` 才成立。更新用 `supersedes=<cid>`：确认时旧条目自动 SUPERSEDED，
新条目在同一自我下接续——谱系单线，不分裂。

**关系阶梯**（`relation`，只能逐级前进、每级需证据）：

```
被动执行 passive_executor
  → 主动理解 goal_understanding
  → 协同规划 co_planning
  → 提前提醒 proactive_reminder
  → 长期伙伴 long_term_partner
```

越级、回退、重复都会被拒绝。对应需求 8。

## 3. 接口

```python
ledger.propose(domain, key, value, quote=..., supersedes=None) -> Claim   # 缺 quote → ProposalRejected
ledger.confirm(cid) -> Claim        # revision+1，旧版 SUPERSEDED，追加 MetaNote
ledger.reject(cid, reason=...) -> Claim
ledger.pending() -> list[Claim]                       # 待用户表态
ledger.identity_anchors() -> list[Claim]              # 已确认 identity（固定锚点）
ledger.relation_stage() -> RelationStage
ledger.render_anchor() / render_self() -> str         # 每轮注入文本
ledger.dumps() / ledger.loads(payload)                # 单一根校验：异根拒绝合并
```

## 4. 插件接入（`index.js`）

| 注册 | 形态 | 内容 |
|---|---|---|
| `soul-core:awakening-frame` | 静态 section（`interpolate:false`） | 唯一写死的元规则，不含任何人设 |
| `soul-core:goal` | 每轮动态 context | 目标文件，改了下一轮生效 |
| `soul-core:self` | 每轮动态 context | 桥渲染的当前自我快照（空我 → 逐步觉醒） |
| `self` 工具 | tools.register | propose / confirm / reject / snapshot |
| `memory` 工具 | tools.register | 外部人与事（沿用记忆层） |

时序：桥（子进程）把自我快照写到 `soul/memory/recall.md`，同步 provider 只读小文件；
`self` 工具写入后立即 refresh，下一轮就能看到新自我。

桥默认**不传 spec → 空我起步**；只有显式 `--spec` 才加载旧固定身份（兼容用）。

## 5. 需求对照

| # | 需求 | 落地 |
|---|---|---|
| 1 | 身份归零，对话中认识自我 | 空 GrowthLedger；身份只能 propose+confirm |
| 2 | 按语境动态调整风格/侧面 | facet/style 领域随语境提议更新（supersedes） |
| 3 | 同一连续自我不分裂 | 单账本、单根、异根拒绝合并 |
| 4 | 主动感知需求/语境/状态 | 框架指令 + self/memory 工具 + 关系升级 |
| 5 | 始终保持核心自我 | 已确认 identity 不被风格变化冲掉（测试断言） |
| 6 | 真实交互中成长 | lesson 领域 + 用户模型更新 |
| 7 | 变更基于明确信息并说明依据 | Evidence.quote 强制 + MetaNote |
| 8 | 关系升级为长期伙伴 | RelationStage 逐级前进 |
| 9 | 元认知说明变化与不变 | 每次 confirm 产出 MetaNote |
| 10 | 锚点+语境理解+下一步 | render_anchor/self + 目标下一步 |
| 11 | 不靠重置/失忆伪造，不虚构 | 无证据提议拒绝；谱系连续持久 |

## 6. 验证

- 内核：**129 项**全过（新增 `test_awakening.py` 21 项）。
- 插件自测：**16 PASS / 0 FAIL / 1 SKIP**（SKIP = desktop 由 Electron 独占，命令行不可查）。
- 只读检查器 `inspect_soul.py` 适配觉醒自我，实测确认/待确认/谱系均正确呈现。

## 7. 边界（本期没做）

- 实体抽取仍依赖 agent 主动调用工具（文件驱动，无自动 NER）。
- 变化是"按需检查/提议"，未做实时主动推送弹窗。
- 空我阶段若用户从不确认身份，agent 会如实保持"尚未取名"，不擅自命名。
- 仍只需 `python3`，零新增依赖。
