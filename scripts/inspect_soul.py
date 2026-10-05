#!/usr/bin/env python3
"""soul-core 黑盒检查器：让人**不经过 agent 转述**就能直接看到 agent 的全部自我核心与记忆。

为什么需要它
------------
身份契约、当前目标、人与事记忆虽然都落了盘，但：
  1. 路径藏在 `.../node_modules/dsh-soul-core/...` 里，用户不知道在哪；
  2. 是原始 JSON / JSONL，不适合直接读；
  3. `self_core_status` / `memory` 工具是给 **agent** 用的，用户看到的是 agent 的转述，
     可能被漏掉或改写。

本检查器是一条**只读、零依赖、独立于模型与凭据**的观察通道：
直接读盘上的身份契约与记忆快照，渲染成人可读的 Markdown，绝不写入、绝不调用模型。

用法
----
    python3 scripts/inspect_soul.py [--plugin <插件目录>] [--json] [--include-archived]

    # 最常用：自动定位插件，直接看
    python3 scripts/inspect_soul.py

    # 给工具/脚本消费
    python3 scripts/inspect_soul.py --json

退出码：0 = 成功渲染；2 = 找不到插件/身份契约（带说明）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
# 默认插件根：本脚本在 <插件>/scripts/ 下，父目录即插件根。
DEFAULT_PLUGIN = HERE.parent


def _locate_kernel(plugin_dir: Path) -> Path:
    """内核位于插件内捆绑的 `soul-core/`。"""
    kernel = plugin_dir / "soul-core"
    if not (kernel / "soulcore").is_dir():
        raise FileNotFoundError(f"插件内未找到捆绑内核: {kernel}")
    return kernel


def _locate_identity(kernel: Path) -> Path:
    spec = kernel / "soul" / "specs" / "ip-analyst.soul.json"
    if not spec.is_file():
        raise FileNotFoundError(f"未找到身份契约: {spec}")
    return spec


def _locate_memory_home(plugin_dir: Path) -> Path:
    """记忆目录默认 <插件>/soul/memory。"""
    return plugin_dir / "soul" / "memory"


def _build_report(plugin_dir: Path, *, include_archived: bool = False) -> Dict[str, Any]:
    """读盘、构造结构化报告。本函数只读，不产生任何写操作。"""
    kernel = _locate_kernel(plugin_dir)
    sys.path.insert(0, str(kernel))
    from soulcore.contract import load_soul  # noqa: E402
    from soulcore.memory import MemoryLedger  # noqa: E402
    from soulcore.store import JsonlPersistence, MemoryStore  # noqa: E402

    identity_path = _locate_identity(kernel)
    soul = load_soul(str(identity_path))

    mem_home = _locate_memory_home(plugin_dir)
    store = MemoryStore(soul, persistence=JsonlPersistence(mem_home / "store.jsonl"))
    ledger = MemoryLedger(soul, persistence=JsonlPersistence(mem_home / "memory.jsonl"), store=store)
    snapshot = mem_home / "state.json"
    if snapshot.is_file():
        try:
            ledger.loads(json.loads(snapshot.read_text(encoding="utf-8")))
        except Exception as exc:  # 快照损坏也要如实呈现，而不是崩溃
            return {"_warning": f"记忆快照无法读取，按空账本呈现: {exc}"}

    goal_path = plugin_dir / "soul" / "goal.json"
    goal: Optional[Dict[str, Any]] = None
    if goal_path.is_file():
        try:
            goal = json.loads(goal_path.read_text(encoding="utf-8"))
        except Exception:
            goal = None

    people = ledger.people(limit=10_000, include_archived=include_archived)
    events = [e for e in ledger.events.values() if include_archived or e.state.value != "pruned"]

    return {
        "plugin_dir": str(plugin_dir),
        "identity": {
            "id": soul.id,
            "name": getattr(soul, "name", None),
            "revision": soul.revision,
            "content_hash": soul.content_hash,
            "core_claims": [c.id for c in soul.core_claims],
            "adaptive_claims": [c.id for c in soul.adaptive_claims],
            "red_lines": [r.id for r in soul.red_lines],
            "identity_file": str(identity_path),
        },
        "goal": goal,
        "goal_file": str(goal_path),
        "people": [_person_view(p) for p in people],
        "events": [_event_view(e) for e in events],
        "edges": len(ledger.edges),
        "yin_yang": ledger.yin_yang(),
        "memory_home": str(mem_home),
    }


def _person_view(p: Any) -> Dict[str, Any]:
    d = p.as_dict()
    score = d.get("score", {})
    return {
        "ref": d.get("pid"),
        "names": d.get("names", []),
        "role": d.get("role", ""),
        "state": d.get("state"),
        "encounters": d.get("encounters"),
        "affinity": d.get("affinity"),
        "identity_critical": d.get("identity_critical"),
        "notes": d.get("notes", ""),
        "strength": round(float(score.get("composite", 0.0)), 3),
        "first_seen_at": d.get("first_seen_at"),
        "last_seen_at": d.get("last_seen_at"),
    }


def _event_view(e: Any) -> Dict[str, Any]:
    d = e.as_dict()
    return {
        "ref": d.get("eid"),
        "summary": d.get("summary"),
        "kind": d.get("kind"),
        "state": d.get("state"),
        "participants": d.get("participants", []),
        "detail": d.get("detail", ""),
        "occurred_at": d.get("occurred_at"),
    }


# ── 人可读渲染（Markdown）───────────────────────────────────────────────────

_STATE_LABEL = {
    "emerging": "待定",
    "active": "活跃",
    "dormant": "沉睡",
    "archived": "已归档",
}


def render_markdown(r: Dict[str, Any]) -> str:
    if "_warning" in r:
        return f"# soul-core 检查报告\n\n> ⚠️ {r['_warning']}\n"

    ident = r["identity"]
    out: List[str] = []
    out.append("# soul-core 检查报告（只读直读盘，未经 agent 转述）")
    out.append("")
    out.append(f"- 插件目录：`{r['plugin_dir']}`")
    out.append("")

    # 身份
    out.append("## 1. 自我核心（进程内不可变）")
    out.append("")
    out.append(f"- 身份 ID：**{ident['id']}**（{ident['name'] or '未命名'}）")
    out.append(f"- 契约版本：{ident['revision']}")
    out.append(f"- 内容哈希：`{ident['content_hash']}`")
    out.append(f"- 身份文件：`{ident['identity_file']}`")
    out.append("- 锁定核心主张：")
    for c in ident["core_claims"]:
        out.append(f"  - `{c}`")
    if ident["adaptive_claims"]:
        out.append("- 自适应主张：")
        for c in ident["adaptive_claims"]:
            out.append(f"  - `{c}`")
    out.append("- 红线：")
    for x in ident["red_lines"]:
        out.append(f"  - `{x}`")
    out.append("")

    # 目标
    out.append("## 2. 当前目标（每轮从盘重读）")
    out.append("")
    g = r.get("goal")
    if not g:
        out.append("- 未设定（或目标文件缺失/不可读）")
    else:
        out.append(f"- 目标：{g.get('objective', '（缺 objective）')}")
        if g.get("status"):
            out.append(f"- 状态：{g['status']}")
        if g.get("nextStep"):
            out.append(f"- 下一步：{g['nextStep']}")
        if g.get("acceptance"):
            out.append(f"- 验收：{g['acceptance']}")
    out.append(f"- 目标文件：`{r['goal_file']}`")
    out.append("")

    # 人
    people = r["people"]
    out.append(f"## 3. 认识的人（{len(people)} 个，按强度排序）")
    out.append("")
    if not people:
        out.append("- （暂无）")
    for p in people:
        label = _STATE_LABEL.get(p["state"], p["state"])
        crit = "｜🔒身份关键" if p["identity_critical"] else ""
        names = "/".join(p["names"])
        out.append(
            f"- **{names}**（{p['ref']}）｜{label}｜角色：{p['role'] or '未知'}"
            f"｜相遇 {p['encounters']} 次｜亲和 {p['affinity']}{crit}"
        )
        if p["notes"]:
            out.append(f"  - 备注：{p['notes']}")
    out.append("")

    # 事
    events = r["events"]
    out.append(f"## 4. 记住的事（{len(events)} 件）")
    out.append("")
    if not events:
        out.append("- （暂无）")
    for e in events:
        who = "｜涉及：" + "、".join(e["participants"]) if e["participants"] else ""
        out.append(f"- {e['summary']}（{e['ref']}｜{e['state']}｜{e['kind']}{who}）")
        if e["detail"]:
            out.append(f"  - 详情：{e['detail']}")
    out.append("")

    # 阴阳账目
    yy = r["yin_yang"]
    out.append("## 5. 阴阳账目（记住 vs 遗忘）")
    out.append("")
    out.append(
        f"- 在册 kept：**{yy['kept']}**｜已褪/归档 shed：{yy['shed']}｜"
        f"累计淘汰 shed_cumulative：{yy['shed_cumulative']}｜跟踪总量 tracked：{yy['tracked']}"
    )
    pcounts = yy["persons"]
    out.append(
        "- 人的状态分布：待定 {emerging} / 活跃 {active} / 沉睡 {dormant} / 归档 {archived}".format(
            **pcounts
        )
    )
    out.append("- 事的状态分布：活跃 {active} / 褪色 {faded} / 淘汰 {pruned}".format(**yy["events"]))
    out.append(f"- 关系边数（人↔事）：{r['edges']}")
    out.append("")
    out.append(f"- 记忆目录：`{r['memory_home']}`")
    return "\n".join(out) + "\n"


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="soul-core 黑盒只读检查器")
    parser.add_argument("--plugin", default=None, help="插件目录（默认自动定位）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非 Markdown")
    parser.add_argument("--include-archived", action="store_true", help="包含已归档/淘汰项")
    args = parser.parse_args(argv)

    plugin_dir = Path(args.plugin).expanduser().resolve() if args.plugin else DEFAULT_PLUGIN.resolve()
    try:
        report = _build_report(plugin_dir, include_archived=args.include_archived)
    except FileNotFoundError as exc:
        sys.stderr.write(f"inspect: {exc}\n")
        return 2
    except Exception as exc:  # 检查器不能假装成功
        sys.stderr.write(f"inspect: {type(exc).__name__}: {exc}\n")
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True))
    else:
        sys.stdout.write(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
