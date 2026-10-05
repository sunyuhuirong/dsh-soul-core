# 记忆模块设计：记住「人」与「事」，并以阴阳对立的方式遗忘

> 本文补齐 [`soul-core-design.md`](soul-core-design.md) 中缺失的一块：agent 如何记住**接触过的人和事**。
> 现有内核的 `MemoryNode` 是**扁平文本节点** —— 它能记住「一句话」，但记不住「张三这个人是谁」，
> 也没有「我何时对他做过什么」的时间线。本次在 Python 内核新增记忆层，并在 DSH 插件中接入。
>
> 遵循既有约束：**纯标准库、零新依赖、不引入框架或抽象层、不重写与记忆无关的模块**。

---

## 0. 当前项目上下文（已核实）

| 项 | 事实 |
|---|---|
| 内核语言 | Python 3.9+（本机 `/usr/bin/python3` = 3.9.6），**零运行时依赖** |
| 内核模块 | `contract.py`(469) / `identity_engine.py`(515) / `scoring.py`(239) / `decay.py`(279) / `store.py`(519) / `metrics.py`(514) / `errors.py`(19) |
| 自我核心 | 身份契约 `soul/specs/ip-analyst.soul.json`，`contentHash=sha256:57f6a6fd…`，经 `load_soul()` 治理闸门校验 |
| 已有记忆原语 | `MemoryNode`（content/category/score/affinity/tier/accesses/merged_from/tombstone）、`MemoryStore.observe()` 唯一写入口、`recall()` 加权检索、`consolidate()` 衰减→合并→驱逐、`JsonlPersistence` |
| 宿主 | DSH 0.2.0-rc.2 / Node v24.18.0，Cordis 插件；`dsh-soul-core-plugin/` |
| 插件现有能力 | 身份节注入（`systemPrompt.section`）、目标上下文（`systemPrompt.context`）、`self_core_status` 工具 |

**缺口**：无实体维度、无实体链接、无按人检索、无时间线。本次新增。

---

## 1. 模块划分

```
soul-core/soulcore/
├── memory.py          ★ 新增：Person / Event / Edge / MemoryLedger
├── decay.py             复用（未改）：effective_half_life_days / strength_at / normalised_strength
├── identity_engine.py   复用（未改）：tokenize / lexical_similarity（实体归并 + 检索打分）
├── scoring.py           复用（未改）：ScoreVector / HeuristicScorer
├── store.py             复用（未改）：Persistence 协议、ICE 治理通道
├── contract.py          未改：身份契约（`prototype_for` 对未知类别有兜底，无需新增 category）
└── __init__.py        ✎ 修改：导出记忆层公开 API

soul-core/tests/
└── test_memory.py     ★ 新增：49 项测试（原 59 + 49 = 108 全绿）

dsh-soul-core-plugin/
├── index.js           ✎ 修改：memory 工具 + 简报复用注入 + 桥客户端
├── scripts/
│   ├── soulcore_bridge.py   ★ 新增：JSON-lines 子进程桥（插件 → 真实内核）
│   └── selftest.mjs         ✎ 修改：23 项断言（新增 12 项记忆相关）
└── soul/
    ├── spec.identity.json   未改
    ├── goal.json            未改
    └── memory/              ★ 运行时生成：state.json / memory.jsonl / store.jsonl / recall.md
```

**依赖方向**（沿用既有分层，无环）：

```
contract ──> decay ──> memory ──> (bridge, 进程外)
                 ↑         ↑
        identity_engine ───┘
                 ↑
              store ──────────┘（治理通道，可选注入）
```

`memory` 只**组合**不继承：`Person`/`Event` 通过 `_as_memory_node()` 适配成 `MemoryNode` 后交给 `decay`
的纯函数，因此衰减数学没有任何分支改动，也没有第二套实现。

---

## 2. 数据结构

### 2.1 Person —— 人

```python
@dataclasses.dataclass
class Person:
    pid: str                       # "person:000001"（稳定主键，下划线命名空间）
    names: List[str]               # names[0] 是主名，其余是别名（张总 / @handle / 署名）
    role: str                      # 客户 / 审计 / 同事…
    notes: str
    state: PersonState             # EMERGING -> ACTIVE -> DORMANT -> ARCHIVED
    score: ScoreVector             # 复用内核三维分数
    affinity: float                # 与自我核心的一致性（决定半衰期乘子）
    alignment: float
    retention_class: str           # identity_core | standard
    identity_critical: bool        # 命中红线/身份核心 -> 永不降级（「刚」）
    first_seen_at: float
    last_seen_at: float            # 决定「沉默多久」
    encounters: int                # 相遇次数（无为式转正的依据）
    accesses: List[float]          # 被召回的时刻（延长自身半衰期）
    strength_boost: float
    merged_from: List[str]         # 合并 lineage
```

### 2.2 Event —— 事

```python
@dataclasses.dataclass
class Event:
    eid: str                       # "event:000001"
    summary: str                   # 要点（永远保留）
    detail: str                    # 细节（FADED 时丢弃）
    kind: str                      # interaction / occurrence / decision
    participants: List[str]        # [pid...]
    state: EventState              # ACTIVE -> FADED -> PRUNED
    identity_critical: bool        # 命中红线 -> 细节也不丢
    memory_ref: Optional[str]      # 指向 MemoryStore 里 identity_critical 的全文节点
    occurred_at / recorded_at: float
```

### 2.3 Edge —— 时间线

```python
@dataclasses.dataclass(frozen=True)
class Edge:
    pid: str; eid: str
    kind: str = EDGE_INTERACTED    # interacted | involved | mentioned
    weight: float = 1.0
    created_at: float
```

人和事之间是**多对多**，`Edge` 就是时间线；没有图数据库，就是一个列表 + 索引。

### 2.4 MemoryPolicy —— 全部可调

```python
person_half_life_days = 90.0     # 人忘得慢
event_half_life_days  = 30.0     # 事忘得快
dormant_after_days    = 60.0
archive_after_days    = 180.0
prune_after_days      = 720.0    # 物极必反：极久不见，残影也淘汰
faded_below           = 0.30     # 事件转 FADED 的归一化强度
event_prune_below     = 0.05
entity_merge_similarity = 0.85   # 宁可留下待并档案，也不把两人并成一个
encounter_bonus       = 0.25
inject_max_chars      = 1200     # 注入预算（防记忆挤占上下文）
```

字段名拼错会**响亮失败**（`ValueError`），不静默忽略。

---

## 3. 存储位置与读写时机

### 3.1 存储

| 文件 | 角色 | 协议 |
|---|---|---|
| `soul/memory/state.json` | **权威快照**（进程重启据此恢复） | 原子写（tmp + `os.replace`） |
| `soul/memory/memory.jsonl` | **append-only 审计日志**（可 diff、可追溯） | 复用 `store.JsonlPersistence` |
| `soul/memory/store.jsonl` | `identity_critical` 内容进入 `MemoryStore` 的痕迹 | 同上 |
| `soul/memory/recall.md` | **内核渲染好的注入文本**（供插件同步读） | 原子写 |

**为什么不用「重放日志」恢复状态**：日志里的 `evidence` 是压缩过的，重放会得到不同的
`affinity` / `alignment`，从而**静默改变衰减行为**。所以快照存最终状态，日志只用于审计。
这个取舍写在 `memory.py` 的注释里。

### 3.2 读写时机

| 时机 | 动作 | 谁触发 |
|---|---|---|
| 会话中遇到人 | `observe_person(name, aliases, role, notes, evidence)` | agent 调 `memory` 工具 |
| 会话中发生事 | `observe_event(summary, detail, people)` | agent 调 `memory` 工具 |
| 插件启动 | `refresh` → 写 `recall.md` | 插件 |
| 每轮模型装配前 | 同步读 `recall.md` → 注入动态上下文 | 插件（`systemPrompt.context`） |
| 工具 `recall` | `MemoryLedger.recall(query)` | agent 按需 |
| 距上次巩固 ≥ 1 小时 | `consolidate()` → 再 `refresh` | 插件（廉价水位线：`state.json` mtime） |
| 用户显式遗忘 | `forget(ref, reason)` → 归档/淘汰 | agent 调 `memory` 工具 |

**插件装配路径零异步、零子进程等待**：`systemPrompt.context` 的 text provider 是同步的，
所以桥把渲染结果预先写盘，provider 只做一次小文件读（≤ 4096 字节硬截断，且不切断多字节字符）。

---

## 4. 检索方式

`recall(query, k)` 与 `MemoryStore.recall` **同构**：

```
score = relevance×1.0 + affinity×0.5 + importance×0.4 + normalised_strength×0.3
```

相关性 `text_similarity(query, haystack) = max(词法, 字符bigram, 短名包含)`：

| 路径 | 对付什么 | 为什么需要 |
|---|---|---|
| 词法（`tokenize` + `lexical_similarity`） | 英文/长文本 | 内核既有的覆盖度主导算法 |
| 字符 bigram Jaccard | 2–3 字中文名 | `tokenize()` 会丢掉长度 ≤1 的片段，「张三」在词法里**整体消失** |
| 短名包含（≤3 字片段原样出现在文中） | 精确人名查询 | 保证「张三」必定命中提到张三的记录 |

**命中即回忆**：检索会给被命中节点追加 `accesses` 时刻，从而延长它自己的半衰期（对齐内核做法）。

### 4.1 一个被实测推翻的设计

最初让插件用**当前目标**作为查询词来召回记忆。实测 0 命中：

```
query = "完成一份可核查的知识产权尽调简报"
recall → hits: 0
```

原因是检索是词法的（嵌入是可选增强，见 `soul-core-design.md` §6.6），目标措辞与
「张三提供了优先权文件」没有任何字面重叠。**目标该决定「做什么」，不该决定「认识谁」。**
改成 `render_brief()`：注入随强度自然浮现的简报 —— 最重的人 + 最近的事 + 阴阳账目，
不需要查询词；需要精确定位时由 agent 调 `recall`。

---

## 5. 生命周期与「阴阳对立」的遗忘机制

### 5.1 状态机

```
人：EMERGING ──2 次相遇──> ACTIVE ──沉默 60d──> DORMANT ──沉默 180d──> ARCHIVED ──沉默 720d──> 淘汰
                              ↑                                            │
                              └────────── 再次相遇 revive() ───────────────┘

事：ACTIVE ──强度 < 0.30──> FADED（丢 detail，留 summary）──强度 < 0.05──> PRUNED

刚（不参与上述任何降级）：identity_critical == True
```

**刻意不用 `if/elif`**：状态要能在一次巩固里连降多级，否则「推进 N 天再巩固一次」的行为
会依赖巩固频率，不可预测。

### 5.2 五条道家原则 → 五条可执行规则

| 原则 | 规则 | 落点 | 断言 |
|---|---|---|---|
| **无为** | 不主动为每个陌生人建档：首次提及只落 EMERGING，第 2 次才转 ACTIVE | `observe_person` | `test_first_mention_is_emerging_not_active` |
| **物极必反** | 记忆不是「留则全留、去则全去」：事件先丢细节留要点，人极久不见才成残影，更久则淘汰 | `consolidate` | `test_event_fades_loses_detail_but_keeps_summary` / `test_silent_person_demotes_then_archives_then_prunes` |
| **反者道之动** | `ARCHIVED` 的人再次相遇时 `revive()` 复活；遗忘是可达性下降，不是抹除 | `observe_person` / `forget` | `test_revive_after_long_silence` / `test_explicit_forget_archives_not_deletes` |
| **刚柔相济** | 命中红线/身份核心的人与事永不降级（刚）；闲谈很快散（柔） | `identity_critical` | `test_identity_critical_person_outlives_idle_person` |
| **损有余而补不足** | 高权重者继续吸收 `encounter_bonus`（有上限 `strength_cap`），长期不用者被削 | `_merge_person` / 衰减 | `test_strength_cap_respected` |

### 5.3 阴阳配比：可观测的「有没有在遗忘」

`yin_yang()` 返回：

```jsonc
{
  "persons": {"emerging":0,"active":1,"dormant":0,"archived":1},
  "events":  {"active":1,"faded":0,"pruned":0},
  "kept": 1,              // 仍在册、仍可召回
  "shed": 1,              // 仍占内存但已褪色/归档
  "shed_cumulative": 0,   // 已淘汰（不在内存里）
  "turnover": 0.0,        // 淘汰数 / 总吞吐
  "shed_per_kept": 1.0,   // kept=0 时为 null（比值无定义，不能返回 0.0 误读成「没在遗忘」）
  "tracked": 2,           // = kept + shed（不变量）
  "edges": 1
}
```

这是**验收核心**：`turnover` 长期为 0 说明遗忘机制没生效；`kept` 线性增长而 `shed` 不变说明在无限累积。

### 5.4 无限累积的实测反证

```
50 个一次性联系人 ──(推进 900 天，巩固一次)──> persons: 0
yin_yang: kept=0, shed=0, shed_cumulative=50, turnover=1.0, edges=0
审计日志: 50 条 person_pruned（可追溯，不可召回）
```

---

## 6. 插件中的规划：注册、调用、扩展

### 6.1 注册（`cordis.patch.yml`）

```yaml
- insert:
    - id: soul-core
      name: dsh-soul-core
      inject:          # ← 必须写在行上（加载器会 unwrap 掉模块级 inject）
        - systemPrompt
        - tools
```

### 6.2 调用流程

```
                        ┌──────────────── 每轮装配（同步，零等待）────────────────┐
用户消息 ──> [系统提示词: 身份节] ──> [动态上下文: 目标] ──> [动态上下文: 记忆简报]
                                                                    ↑ 同步读 recall.md
                                                                    │
   agent 需要记忆 ──> memory 工具 ──> callBridge(子进程) ──> soulcore_bridge.py
                                          │                        │
                                          │                  MemoryLedger（真内核）
                                          │                        │
                                          └──── refresh ◄── 写 recall.md + state.json
```

### 6.3 接口（`memory` 工具，一个入口六种动作）

| action | 参数 | 桥命令 |
|---|---|---|
| `recall` | `query`, `k`, `person_only` | `recall` |
| `remember` | `name`, `aliases`, `role`, `notes`, `evidence` | `remember` |
| `event` | `summary`, `detail`, `people` | `event` |
| `timeline` | `name` | `timeline` |
| `forget` | `ref`, `reason` | `forget` |
| `status` | — | `status` |

`toBridgeRequest()` **只做字段转发**，不含任何记忆判断：判断全在 Python 内核，保证只有一套语义。

### 6.4 扩展点（不改插件也能扩）

- **换存储**：实现 `store.Persistence` 协议（SQLite/Postgres），算法不变。
- **换评分**：给 `MemoryLedger(store=...)` 注入带自定义 `scorer` 的 store，两个层次尺度一致。
- **换策略**：`MemoryPolicy` 全字段可调，或经插件 config `memoryHome` / 环境变量覆盖路径。
- **加实体类型**：`_as_memory_node()` 是唯一适配点；新增类型只需再写一个分支。

### 6.5 失败降级

| 缺失 | 行为 |
|---|---|
| 桥脚本 / spec 不存在 | `resolveBridge()` 返回 `undefined`；**身份与目标注入照常**，不注册 memory 工具 |
| `python3` 不可执行 | 桥调用返回 `{ok:false}`；工具如实报错；`recall.md` 读不到则本轮不注入记忆 |
| 编排目录不可写 | 桥 `selftest` 命令会如实失败（`PermissionError` 被捕获成 `{ok:false}`） |
| 快照损坏 | 启动为空账本并在 stderr 告警，不让 agent 起不来 |
| 身份契约不可用 | **响亮失败**（与既有设计一致：没有自我核心的 agent 不该运行） |

---

## 7. 边界条件

1. **空名字 / 空摘要** → `ValueError`，不静默丢弃。
2. **同名不同人**（`联系人1` / `联系人10`）→ 末尾编号不同直接判为不同人。
   bigram 在位数不同时虚高（0.857），曾导致 50 人连锁并成 10 人；靠显式规则而非调阈值解决。
3. **同人不同名** → 必须 `aliases` 显式声明，或经 `consolidate` 的归档合并（共享别名时）。
   取向是「宁可不并，也不错并」：错并不可逆。
4. **`identity_critical` 的遗忘请求** → 拒绝并留 `forget_refused` 审计。
5. **悬空边** → 巩固时清理指向已淘汰事件或已消失的人的边，并有不变量测试。
6. **注入预算** → 内核 `inject_max_chars` + 插件 `MEMORY_INJECT_MAX_BYTES` 双重截断；
   截断对模型**显式可见**（附「已达上限」提示），不静默丢内容。
7. **多字节截断** → 按字节切片后去掉尾部 `U+FFFD`，不产生半个汉字。
8. **时区/时间** → 全部用 epoch 秒；测试注入固定 `T0`，不依赖真实时钟
   （`people(now=...)` 也支持注入，否则排序会随运行时刻漂移）。
9. **`kept=0` 的比值** → 返回 `None` 而非 `0.0`。
10. **并发写** → append-only JSONL 单进程适用；并发场景换 SQLite（既有设计的已知限制）。

---

## 8. 验收 / 自测标准

### 8.1 命令

```bash
cd soul-core
bash scripts/run_tests.sh                      # 全量 108 项（原 59 + 新 49）
bash scripts/run_demo.sh                       # 原有端到端演示（回归）

cd ..
DSH_HOME="$PWD/soulcore-dsh-home" node dsh-soul-core-plugin/scripts/selftest.mjs   # 23 项
```

### 8.2 实测结果

```
soul-core 全量：Ran 108 tests in 0.034s ... OK
插件 selftest ：23 PASS / 0 FAIL
```

按模块分布（实测）：

```
test_contract             10
test_forgetting           26
test_identity_engine      11
test_metrics              12
test_memory               49   ← 本次新增
TOTAL                    108
```

插件 selftest 新增的 12 项记忆断言（真内核、真落盘、跨进程）：

- 记忆桥可调用（数据目录可写）
- 记忆桥加载的是真实身份契约（`contentHash` 与身份节一致）
- 首次记住一个人落为待定（**无为**）
- 第二次相遇转正为 active
- 同一人不产生第二个档案
- 事件挂到人上
- 账本状态可读（阴阳配比）
- `refresh` 写出可注入的简报
- 简报带阴阳账目
- **跨进程仍可召回**（记忆已落盘，比进程活得久）
- 显式遗忘是归档不是删除
- 遗忘后仍可在册（可达性下降，非抹除）

### 8.3 六条必须通过的行为断言

| # | 断言 | 测试 |
|---|---|---|
| 1 | 首次接触只落待定，第二次才转正（无为） | `test_first_mention_is_emerging_not_active` / `test_second_mention_promotes_to_active` |
| 2 | 别名归并成同一人，不同人不误并 | `test_alias_merges_into_same_person` / `test_distinct_people_stay_distinct` |
| 3 | 事件挂人并形成时间线（倒序） | `test_event_links_to_person_and_builds_timeline` / `test_timeline_is_descending` |
| 4 | 被遗忘的是细节，事本身还在 | `test_event_fades_loses_detail_but_keeps_summary` |
| 5 | 身份关键的人与事永不淘汰 | `test_identity_critical_person_outlives_idle_person` / `test_identity_critical_event_never_fades` |
| 6 | 长期无接触的档案必须收敛（不无限累积） | `test_memory_does_not_accumulate_unboundedly` |

### 8.4 自测无副作用（可反复运行）

插件默认把记忆写在 `<插件>/soul/memory/`。若自测不隔离，跑一次就会往仓库里留下运行时数据。
因此 `selftest.mjs` 在 `import` 插件之前就把数据目录钉到临时目录，并在结束时删除；
仅当调用方**显式**设了 `DSH_SOUL_CORE_HOME` 时才不动它（不删用户的目录）。实测：

```bash
rm -rf dsh-soul-core-plugin/soul/memory
DSH_HOME="$PWD/soulcore-dsh-home" node dsh-soul-core-plugin/scripts/selftest.mjs
ls dsh-soul-core-plugin/soul/          # -> goal.json  spec.identity.json（无 memory/ 残留）
```

### 8.5 真实会话端到端（DSH 内实测）

```bash
export DSH_HOME="$PWD/soulcore-dsh-home"
dsh --profile soulcore --patch "$PWD/soulcore-dsh-home/model.patch.yml" \
  "请调用 memory 工具，记住一个人：张三，角色是客户。"
# → person:000001，状态 emerging（待定）— 首次记录，再出现一次即转正

dsh --profile soulcore --patch "$PWD/soulcore-dsh-home/model.patch.yml" \
  "我下周要见的那位客户叫什么？他是什么角色？不要调用任何工具。"
# → 姓名：张三 / 角色：客户      ← 记忆简报已注入，无需再查

dsh --profile soulcore --patch "$PWD/soulcore-dsh-home/model.patch.yml" \
  "再记一次张三，别名「张总」；并记录事件：张三今天提交了优先权文件。"
# → person:000001 names=['张三','张总'] state=active encounters=2
#   event:000001 participants=['person:000001']，edges 1 条
```

---

## 9. 新增 / 修改点清单

| 类型 | 路径 | 说明 |
|---|---|---|
| **新增** | `soul-core/soulcore/memory.py` | 记忆层：Person / Event / Edge / MemoryLedger + 阴阳式生命周期 |
| **新增** | `soul-core/tests/test_memory.py` | 49 项测试 |
| **新增** | `dsh-soul-core-plugin/scripts/soulcore_bridge.py` | JSON-lines 子进程桥（插件调真实内核） |
| **新增** | `MEMORY-DESIGN.md`（本文） | 设计文档 |
| **修改** | `soul-core/soulcore/__init__.py` | 导出记忆层公开 API（纯增量） |
| **修改** | `dsh-soul-core-plugin/index.js` | 桥客户端 + `memory` 工具 + 记忆简报注入（+120 行） |
| **修改** | `dsh-soul-core-plugin/scripts/selftest.mjs` | +12 项记忆断言（共 23 项） |
| **修改** | `dsh-soul-core-plugin/package.json` | `files` 加入 `scripts` |
| **修改** | `dsh-soul-core-plugin/README.md` | 记忆层用法与命令 |
| **未改** | `contract.py` / `decay.py` / `scoring.py` / `store.py` / `identity_engine.py` / `metrics.py` | 一行未动：`prototype_for` 对未知类别已有兜底，衰减函数本就接受注入的 `half_life_of` |

> 不新增任何依赖、构建步骤或抽象层：`memory.py` 只 import 标准库与既有内核模块。

---

## 10. 已知限制与下一步

| 限制 | 影响 | 下一步 |
|---|---|---|
| 检索是词法的（无嵌入） | 「尽调对象」这类**语义**关联召不回人名，同义改写召回不足 | 注入 `EmbedFn`（`soul-core-design` §6.6 已列为 M4） |
| 实体抽取依赖 agent 主动调用 | 不调 `memory` 工具就不会记录 | 后续可在 `agent/pre-step` 用 LLM 抽取；本次刻意保持显式，避免引入不确定性 |
| 巩固需外部触发 | 会话不活跃时不会自动遗忘 | 已做「距上次 ≥1 小时顺带巩固」；生产可加定时任务 |
| JSONL 非并发安全 | 单进程适用 | 换 SQLite `Persistence` 实现，算法不变 |
| `entity_merge_similarity=0.85` 为手工标定 | 边界个例可能留下待并档案 | 用真实人名对做网格搜索；当前取向是「宁可留下，也不错并」 |
| 未做「反思」（反思型记忆） | 只有原始人与事，没有归纳出的人物画像 | 可在 `consolidate` 里加一次 LLM 归纳，产出 `Person.notes` |
