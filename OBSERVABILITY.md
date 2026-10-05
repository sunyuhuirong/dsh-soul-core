# 可观察方案：打破「自我核心 / 记忆黑盒」

## 1. 问题陈述（现状）

agent 的自我核心（身份契约）、当前目标、人与事记忆，对用户是**黑盒**。

需要澄清一个事实（先区分观察与推断）：数据**并非没有落盘**——`state.json`、`memory.jsonl`、
`goal.json`、身份契约都真实存在。黑盒的根因是**这些内容只对 agent 可见、不对人可见**：

| # | 黑盒成因 | 说明 |
|---|---|---|
| 1 | 路径藏得深 | 文件在 `.../node_modules/dsh-soul-core/soul(-core)/...`，普通用户不知道位置 |
| 2 | 是原始数据 | JSON / JSONL，不适合人直接阅读，字段名是内部命名 |
| 3 | 观察口面向 agent | `self_core_status`、`memory` 是**给模型调用的工具**；用户想看只能"请 agent 转述"，转述可能漏、可能改 |
| 4 | 没有面向人的入口 | 缺一条统一、只读、不需要模型与凭据的检查命令 |

后果：用户无法独立核实"agent 现在认为自己是谁、目标是什么、记住了谁、忘掉了什么"，
也无法在怀疑漂移时拿到一手证据。

## 2. 设计原则

1. **人优先于 agent 转述**：用户必须能拿到**未经模型转述**的一手盘上事实。
2. **只读**：观察通道绝不写入、绝不改变记忆，避免"检查本身污染被检查对象"。
3. **零依赖、零凭据**：只复用标准库与已捆绑内核，不调模型、不需网络。
4. **两种消费形态**：默认人可读 Markdown；`--json` 供工具/脚本/自动化。
5. **失败不静默**：找不到插件/契约时响亮报错，不假装"一切正常"。

## 3. 方案：三层可观察通道

```
        ① 只读检查器 inspect_soul.py（本次新增，核心）
   用户 ────────────────────────────────────────► 直接读盘上身份/目标/记忆
        （一条命令，不经模型、不需凭据，只读）

        ② 落盘即报告
        soul/memory/recall.md（已有的注入简报）+ 可扩展的完整快照

        ③ 统一入口
        命令行直接调用；后续可挂到 GUI / dsh 命令，不依赖模型在线
```

为什么不直接"让 agent 用 self_core_status 念一遍"：那仍是**二手信息**，模型可能选择性呈现。
检查器绕开模型，直接 `load_soul` + `MemoryLedger.loads(state.json)`，语义与运行时**完全同一份**。

## 4. 交付物：`scripts/inspect_soul.py`

一条命令直读盘、渲染五段报告：

| 段落 | 内容 |
|---|---|
| 1. 自我核心 | 身份 id/名称、契约版本、contentHash、锁定主张、自适应主张、红线、契约文件路径 |
| 2. 当前目标 | objective/status/nextStep/acceptance + 目标文件路径；缺失时明确"未设定" |
| 3. 认识的人 | 全部人员，按强度排序：名称/ref/状态/角色/相遇次数/亲和/身份关键标记/备注 |
| 4. 记住的事 | 全部事件：摘要/ref/状态/类型/参与者引用/详情 |
| 5. 阴阳账目 | kept / shed / shed_cumulative / tracked、人与事的状态分布、人↔事边数、记忆目录 |

关键实现点（保证"直读一手"且"只读"）：

```python
kernel = plugin_dir / "soul-core"
sys.path.insert(0, str(kernel))
from soulcore.contract import load_soul
from soulcore.memory import MemoryLedger
from soulcore.store import JsonlPersistence, MemoryStore

soul   = load_soul(identity_path)                 # 与运行时同一治理闸门
ledger = MemoryLedger(soul, persistence=JsonlPersistence(...), store=store)
ledger.loads(json.loads((mem_home/"state.json").read_text()))   # 直读权威快照
# —— 全程没有任何写操作；记忆运行时用的也是这几个类，语义一致 ——
```

真实属性以 `contract.SoulCore` 为准（扁平字段）：`soul.core_claims`、
`soul.adaptive_claims`、`soul.red_lines`；不是嵌套对象（开发中曾误写为
`soul.identity_core.*`，已按 `contract.py:121` 的真实定义修正）。

## 5. 使用方式

```bash
# 最常用：在插件安装目录内直接看
python3 scripts/inspect_soul.py

# 给脚本/自动化消费
python3 scripts/inspect_soul.py --json

# 连已归档/淘汰的历史也显示
python3 scripts/inspect_soul.py --include-archived

# 指定插件目录（自动定位失败时）
python3 scripts/inspect_soul.py --plugin /path/to/dsh-soul-core
```

退出码：`0` 成功渲染；`2` 找不到插件/身份契约（stderr 带说明）；`1` 其他异常。

## 6. 验证结果（本次实测）

| 场景 | 期望 | 实测 |
|---|---|---|
| 空账本（当前真实现状） | 五段渲染、人/事为"暂无" | ✅ 身份哈希 `sha256:57f6…`、目标完整、人/事 0 |
| 有数据 | 按强度列人、事件挂参与者 | ✅ 赵六(active)/孙七、事件参与者引用正确 |
| 显式遗忘后 | 默认隐藏归档；`--include-archived` 可见 | ✅ 默认 1 人；加参数 2 人，孙七标"已归档" |
| 账目守恒 | tracked = kept + shed | ✅ tracked=3 = kept2 + shed1 |
| `--json` | 结构完整、可被工具消费 | ✅ 9 个顶层键，people/events/yin_yang 齐全 |
| 只读 | 运行检查器不产生写入 | ✅ 仅读取 state.json/goal.json/契约 |

## 7. 明确边界（本次没做）

- 未做实时"变更推送/订阅"：当前是**按需检查**（pull），不是变化时主动弹窗（可作后续里程碑）。
- 未改 GUI：桌面端 profile 的可视化面板需在 DSH 前端侧实现，本次先保证命令行可观察。
- 检查器依赖"插件目录可被定位"；若未来安装路径再变，需要同步更新自动定位逻辑。
- 仍需 `python3` 可用；不引入任何新依赖。
