#!/usr/bin/env python3
"""soul-core 黑盒检查器（觉醒架构）：让人**不经 agent 转述**直接看到它的成长自我。

为什么需要它
------------
觉醒后，agent 的身份、原则、角色侧面、风格、对你的偏好认知、关系阶段、经验教训，
都在对话中逐步长成。用户必须能独立、随时查看，而不是只能听 agent 自述。

本检查器是一条**只读、零依赖、独立于模型与凭据**的观察通道：
直接读盘上的成长账本（growth.json）与目标，渲染成人可读报告，绝不写入、不调模型。

用法
----
    python3 scripts/inspect_soul.py [--plugin <插件目录>] [--json] [--include-archived]

退出码：0 成功；2 找不到插件（带说明）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
DEFAULT_PLUGIN = HERE.parent


def _locate_kernel(plugin_dir: Path) -> Path:
    kernel = plugin_dir / "soul-core"
    if not (kernel / "soulcore").is_dir():
        raise FileNotFoundError(f"插件内未找到内核: {kernel}")
    return kernel


def _build_report(plugin_dir: Path, *, include_archived: bool = False) -> Dict[str, Any]:
    """读盘构造报告。全程只读。"""
    kernel = _locate_kernel(plugin_dir)
    sys.path.insert(0, str(kernel))
    from soulcore.awakening import (  # noqa: E402
        ClaimStatus,
        Domain,
        GrowthLedger,
        RELATION_LABEL,
    )
    from soulcore.store import JsonlPersistence  # noqa: E402

    mem_home = plugin_dir / "soul" / "memory"
    growth = GrowthLedger(persistence=JsonlPersistence(mem_home / "growth.jsonl"))
    growth_file = mem_home / "growth.json"
    warning: Optional[str] = None
    if growth_file.is_file():
        try:
            growth.loads(json.loads(growth_file.read_text(encoding="utf-8")))
        except Exception as exc:
            warning = f"成长快照无法读取，按空自我呈现: {exc}"

    goal_path = plugin_dir / "soul" / "goal.json"
    goal: Optional[Dict[str, Any]] = None
    if goal_path.is_file():
        try:
            goal = json.loads(goal_path.read_text(encoding="utf-8"))
        except Exception:
            goal = None

    def claims(domain: Domain) -> List[Dict[str, Any]]:
        out = []
        for c in growth.by_domain(domain, include_inactive=include_archived):
            if not include_archived and c.status not in (
                ClaimStatus.CONFIRMED,
                ClaimStatus.PROPOSED,
            ):
                continue
            out.append(c.as_dict())
        return out

    return {
        "plugin_dir": str(plugin_dir),
        "warning": warning,
        "self_revision": growth.revision,
        "identity": claims(Domain.IDENTITY),
        "facets": claims(Domain.FACET),
        "style": claims(Domain.STYLE),
        "user_model": claims(Domain.USER),
        "lessons": claims(Domain.LESSON),
        "relation": {
            "stage": growth.relation_stage().value,
            "label": RELATION_LABEL[growth.relation_stage()],
        },
        "pending": [c.as_dict() for c in growth.pending()],
        "journal": [n.as_dict() for n in growth.journal],
        "goal": goal,
        "memory_home": str(mem_home),
    }


# ── 人可读渲染 ──────────────────────────────────────────────────────────────


def render_markdown(r: Dict[str, Any]) -> str:
    out: List[str] = ["# 觉醒自我检查报告（只读直读盘，未经 agent 转述）", ""]
    if r.get("warning"):
        out.append(f"> ⚠️ {r['warning']}")
        out.append("")
    out.append(f"- 自我版本：**{r['self_revision']}**")
    out.append(f"- 插件目录：`{r['plugin_dir']}`")
    out.append("")

    out.append("## 1. 已确认身份与原则（固定锚点）")
    out.append("")
    if not r["identity"]:
        out.append("- （空我：尚未在对话中确立身份）")
    else:
        for c in r["identity"]:
            out.append(f"- {c['key']}：{_v(c)}｜状态 {c['status']}")
    out.append("")

    out.append("## 2. 角色侧面（同属一个自我）")
    out.append("")
    out.extend(_row_list(r["facets"]))

    out.append("## 3. 表达 / 协作风格（随语境流变）")
    out.append("")
    out.extend(_row_list(r["style"]))

    out.append("## 4. 对用户的认知（经用户确认，不臆测）")
    out.append("")
    out.extend(_row_list(r["user_model"]))

    out.append("## 5. 关系阶段")
    out.append("")
    out.append(f"- **{r['relation']['label']}**（{r['relation']['stage']}）")
    out.append("")

    out.append("## 6. 经验教训")
    out.append("")
    out.extend(_row_list(r["lessons"]))

    out.append("## 7. 待用户确认的自我提议")
    out.append("")
    if not r["pending"]:
        out.append("- （无）")
    else:
        for c in r["pending"]:
            out.append(f"- `{c['cid']}` {c['domain']}.{c['key']}={_v(c)}")
    out.append("")

    out.append("## 8. 觉醒谱系（元认知：变了什么 / 不变什么）")
    out.append("")
    if not r["journal"]:
        out.append("- （还没有确认过的变化）")
    else:
        for n in r["journal"]:
            out.append(f"- rev{n['revision']}：变了 → {n['summary']}｜不变 → {n['unchanged']}")
    out.append("")

    out.append("## 9. 当前目标")
    out.append("")
    g = r.get("goal")
    if not g:
        out.append("- 未设定")
    else:
        out.append(f"- {g.get('objective', '（缺 objective）')}｜状态 {g.get('status', '')}")
    out.append("")
    out.append(f"- 记忆目录：`{r['memory_home']}`")
    return "\n".join(out) + "\n"


def _row_list(rows: List[Dict[str, Any]]) -> List[str]:
    if not rows:
        return ["- （无）", ""]
    out = []
    for c in rows:
        out.append(f"- {c['key']}：{_v(c)}｜状态 {c['status']}")
    out.append("")
    return out


def _v(c: Dict[str, Any]) -> str:
    value = c.get("value")
    if isinstance(value, (dict, list)):
        return repr(value)
    return str(value)


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="soul-core 觉醒自我只读检查器")
    parser.add_argument("--plugin", default=None, help="插件目录（默认自动定位）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--include-archived", action="store_true", help="含已驳回/取代的历史")
    args = parser.parse_args(argv)

    plugin_dir = Path(args.plugin).expanduser().resolve() if args.plugin else DEFAULT_PLUGIN.resolve()
    try:
        report = _build_report(plugin_dir, include_archived=args.include_archived)
    except FileNotFoundError as exc:
        sys.stderr.write(f"inspect: {exc}\n")
        return 2
    except Exception as exc:
        sys.stderr.write(f"inspect: {type(exc).__name__}: {exc}\n")
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True))
    else:
        sys.stdout.write(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
