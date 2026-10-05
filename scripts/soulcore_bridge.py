#!/usr/bin/env python3
"""soul-core 记忆层的外部进程桥。

为什么需要它
------------
`dsh-soul-core` 插件跑在 Node 里，而记忆治理内核（评分 / 身份判定 / 衰减 /
巩固）是 Python。把内核重写一遍到 JS 会立刻产生两套衰减算法，长期必然漂移。
因此插件**通过本桥以子进程方式调用真实内核**，不重复实现任何记忆算法。

协议（单次调用即一个进程，一次请求一次响应）
--------------------------------------------
    python3 bridge.py --home <dir> [--kernel <dir>] <<'JSON'
    {"command": "recall", "query": "张三 专利", "k": 5}
    JSON

    -> stdout 恰好一行 JSON：{"ok": true, "data": {...}} 或 {"ok": false, "error": "..."}

状态落在 `--home` 目录下：
    memory.jsonl     append-only 审计日志（可 diff、可重放）
    state.json       权威快照（每次写操作后原子落盘）

命令
----
    refresh    limit                        -> 渲染记忆简报到 recall.md（供插件同步读）
    status                                  -> 概览（人数、事件数、阴阳配比）
    recall     query, k, person_only        -> 检索结果 + 可注入文本
    remember   name, aliases, role, notes, evidence   -> 记住一个人
    event      summary, detail, kind, people          -> 记住一件事（挂到人上）
    forget     ref, reason                  -> 显式遗忘
    timeline   person, limit                -> 某人的时间线
    consolidate                             -> 触发一次巩固（遗忘真正发生的地方）
    selftest                                -> 内核自检（数据目录可写、契约可加载）

桥自身不做任何判断：所有治理语义都在 `soulcore` 里。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _default_kernel() -> Path:
    """内核位置：优先同级 `soul-core/`，其次上一级的 `soul-core/`。"""
    for candidate in (HERE / "soul-core", HERE.parent / "soul-core", HERE):
        if (candidate / "soulcore" / "memory.py").is_file():
            return candidate
    return HERE


def _default_home() -> Path:
    """数据目录：`$DSH_HOME/soul-core`，否则 `~/.dsh/soul-core`。"""
    env = os.environ.get("DSH_HOME", "").strip()
    base = Path(env).expanduser() if env else Path.home() / ".dsh"
    return base / "soul-core"


def _atomic_write(path: Path, text: str) -> None:
    """先写临时文件再 rename —— 避免读侧看到半写状态。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Bridge:
    """把记忆内核包装成命令式接口。一次进程生命周期内只构造一次。"""

    def __init__(self, home: Path, kernel: Path, spec: Path) -> None:
        sys.path.insert(0, str(kernel))
        from soulcore.contract import load_soul  # noqa: E402
        from soulcore.memory import MemoryLedger  # noqa: E402
        from soulcore.store import JsonlPersistence, MemoryStore  # noqa: E402

        self.home = home
        self.kernel = kernel
        self.home.mkdir(parents=True, exist_ok=True)
        self.snapshot_path = home / "state.json"
        self.log_path = home / "memory.jsonl"
        #: 供插件同步读取的召回注入文本（由 refresh 命令刷新）
        self.recall_path = home / "recall.md"

        self.soul = load_soul(str(spec))
        # 治理通道：identity_critical 的内容同时进入 MemoryStore
        self.store = MemoryStore(self.soul, persistence=JsonlPersistence(home / "store.jsonl"))
        self.ledger = MemoryLedger(
            self.soul,
            persistence=JsonlPersistence(self.log_path),
            store=self.store,
        )
        self._load()

    # -- 持久化 -------------------------------------------------------------

    def _load(self) -> None:
        if not self.snapshot_path.is_file():
            return
        try:
            payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            self.ledger.loads(payload)
        except Exception as exc:  # 快照损坏不应让 agent 起不来
            sys.stderr.write(f"soul-core bridge: snapshot unreadable, starting empty: {exc}\n")

    def _save(self) -> None:
        _atomic_write(
            self.snapshot_path,
            json.dumps(self.ledger.dumps(), ensure_ascii=False, sort_keys=True, indent=1),
        )

    # -- 命令 ---------------------------------------------------------------

    def dispatch(self, request: dict) -> dict:
        command = request.get("command")
        handler = getattr(self, f"_cmd_{command}", None)
        if handler is None:
            raise ValueError(f"unknown command: {command!r}")
        return handler(request)

    def _cmd_selftest(self, _request: dict) -> dict:
        probe = self.home / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return {
            "kernel": str(self.kernel),
            "bridge": str(Path(__file__).resolve()),
            "home": str(self.home),
            "identity": self.soul.id,
            "content_hash": self.soul.content_hash,
            "writable": True,
        }

    def _cmd_status(self, _request: dict) -> dict:
        yy = self.ledger.yin_yang()
        return {
            "identity": self.soul.id,
            "persons": len(self.ledger.persons),
            "events": len(self.ledger.events),
            "edges": len(self.ledger.edges),
            "yin_yang": yy,
            "home": str(self.home),
        }

    def _cmd_recall(self, request: dict) -> dict:
        query = str(request.get("query", "")).strip()
        if not query:
            return {"query": "", "hits": [], "text": "", "note": "empty query"}
        result = self.ledger.recall(
            query,
            k=int(request.get("k", 5)),
            person_only=bool(request.get("person_only", False)),
        )
        text = self.ledger.render_recall(result)
        return {"query": query, "hits": [h.as_dict() for h in result.hits], "text": text,
                "truncated": result.truncated, "total_considered": result.total_considered}

    def _cmd_remember(self, request: dict) -> dict:
        out = self.ledger.observe_person(
            str(request.get("name", "")),
            aliases=request.get("aliases") or None,
            role=str(request.get("role", "")),
            notes=str(request.get("notes", "")),
            evidence=str(request.get("evidence", "")),
        )
        self._save()
        result = out.as_dict()
        result["person"] = self.ledger.persons[out.pid].as_dict() if out.pid else None
        return result

    def _cmd_event(self, request: dict) -> dict:
        out = self.ledger.observe_event(
            str(request.get("summary", "")),
            detail=str(request.get("detail", "")),
            kind=str(request.get("kind", "interaction")),
            people=request.get("people") or None,
        )
        self._save()
        result = out.as_dict()
        result["event"] = self.ledger.events[out.eid].as_dict() if out.eid else None
        return result

    def _cmd_forget(self, request: dict) -> dict:
        ok = self.ledger.forget(str(request.get("ref", "")), str(request.get("reason", "")))
        self._save()
        return {"forgotten": ok, "note": "" if ok else "not found, or identity_critical (requires governance)"}

    def _cmd_timeline(self, request: dict) -> dict:
        person = str(request.get("person", ""))
        events = self.ledger.timeline(person, limit=int(request.get("limit", 20)))
        resolved, _ = self.ledger.resolve_person(person)
        return {
            "person": None if resolved is None else resolved.as_dict(),
            "events": [e.as_dict() for e in events],
        }

    def _cmd_refresh(self, request: dict) -> dict:
        """把渲染好的「记忆简报」写到 `recall.md`。

        时序原因：插件的 `systemPrompt.context` provider 是同步的，而本桥是子进程。
        因此渲染在桥里一次完成，插件装配路径只做一次小文件同步读 ——
        召回逻辑仍然只有 Python 一份实现，插件侧不复制任何相似度/衰减代码。

        **为什么不用「当前目标」当查询词**：检索是词法的（嵌入是可选增强，见
        soul-core-design §6.6）。「完成一份尽调简报」这类目标与「张三提供了
        优先权文件」没有任何字面重叠，用它检索必然 0 命中。目标该决定的是
        「做什么」，而不是「认识谁」。所以这里注入的是随强度自然浮现的简报：
        最重的人 + 最近的事 + 阴阳配比。需要精确定位时由 agent 调用 `recall`。
        """
        limit = int(request.get("limit", 5))
        text = self.ledger.render_brief(limit=limit)
        _atomic_write(self.recall_path, text)
        return {"wrote": len(text), "persons": len(self.ledger.persons), "events": len(self.ledger.events)}

    def _cmd_consolidate(self, _request: dict) -> dict:
        report = self.ledger.consolidate()
        self._save()
        return {"report": report.as_dict(), "yin_yang": self.ledger.yin_yang()}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="soul-core memory bridge")
    parser.add_argument("--home", default=None, help="data dir (default $DSH_HOME/soul-core)")
    parser.add_argument("--kernel", default=None, help="soul-core checkout dir")
    parser.add_argument("--spec", default=None, help="identity contract json")
    args = parser.parse_args(argv)

    kernel = Path(args.kernel).expanduser() if args.kernel else _default_kernel()
    home = Path(args.home).expanduser() if args.home else _default_home()
    spec = (
        Path(args.spec).expanduser()
        if args.spec
        else kernel / "soul" / "specs" / "ip-analyst.soul.json"
    )

    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"ok": False, "error": "empty request on stdin"}))
        return 2
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"invalid json request: {exc}"}))
        return 2

    try:
        bridge = Bridge(home, kernel, spec)
        data = bridge.dispatch(request)
    except Exception as exc:  # 桥必须把失败如实回报，不能静默
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1

    print(json.dumps({"ok": True, "data": data}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
