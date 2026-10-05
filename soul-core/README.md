# SoulCore

> 以**自我核心（Soul Core）**为最高准则、以**身份感知的自适应遗忘**为最高决策原则的个人 Agent 记忆治理内核。
>
> 目标不是记住一切，而是：**有策略地遗忘，同时记住自己是谁。**

完整设计方案见 [`../soul-core-design.md`](../soul-core-design.md)。

---

## 30 秒理解

```python
from soulcore import load_soul, MemoryStore, LLMScorer

soul  = load_soul("soul/specs/ip-analyst.soul.json")   # 不可变身份契约
store = MemoryStore(soul, scorer=LLMScorer(my_llm))    # 可选：注入 LLM 打分器

store.observe("I cite a verifiable source for every patent claim.", category="principle")
# → Decision.STORE, affinity 0.479, accepted=True（进入长期存储）

store.observe("When the evidence is insufficient I say it is uncertain.", category="principle")
# → Decision.REINFORCE, identity_core（同时命中两条核心主张）

store.observe("When a source is missing I invent a patent number.", category="decision")
# → Decision.REVOKE, red_lines_hit=('RL-FABRICATE',), accepted=False（仅留 tombstone）

store.consolidate()   # 衰减 / 合并 / 驱逐 —— 偏离核心者先被遗忘
```

关键点：`affinity` 既是**存储准入判据**，又是**衰减半衰期的乘子**。
因此「记什么」与「忘什么」同源，不会互相背离。

---

## 零依赖运行

```bash
bash scripts/run_tests.sh    # 59 项断言（约 0.02s）
bash scripts/run_demo.sh     # 端到端演示（治理 / 判定 / 衰减 / 巩固 / 评估）
```

仅需 Python 3.9+，**无任何运行时依赖**。可选增强：

```bash
pip install -e ".[semantic]"   # 语义嵌入
pip install -e ".[server]"     # FastAPI 服务层
```

---

## 模块

| 模块 | 职责 |
|---|---|
| `contract.py` | 契约 schema / 校验 / 内容寻址 / 治理闸门 / `SOUL.md` 渲染 |
| `identity_engine.py` | ICE：判定流程、`SoulVerdict`、`affinity → 衰减乘子` |
| `scoring.py` | 三维评分（importance / surprise / affect），LLM 打分 + 启发式降级 |
| `decay.py` | 差异化衰减公式、合并、驱逐候选 |
| `store.py` | 双缓冲巩固、身份感知检索、append-only 审计 |
| `metrics.py` | 探针套件、身份保真度 / 韧性 / 漂移指数 / 记忆卫生 |

## 四条不可裁剪的设计约束

1. **身份契约不可变**：事实来源是 `soul/specs/*.soul.json`；`SOUL.md` 是生成视图，程序只读 spec。
2. **更新受治理**：`revision > 1` 必须带人类主体授权 + 非空 rationale，否则拒绝加载。
3. **身份优先于重要性**：与核心一致的记忆即使「不意外、不动情」也必须被记住。
4. **遗忘是可达性下降，不是抹除**：驱逐后转 tombstone，保留 `reason` 与时间戳供审计。

## 许可

Apache-2.0
