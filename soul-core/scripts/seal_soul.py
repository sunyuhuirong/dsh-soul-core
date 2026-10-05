#!/usr/bin/env python3
"""给 soul spec 计算并写入 contentHash（内容寻址）。

用法::

    python3 scripts/seal_soul.py soul/specs/ip-analyst.soul.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from soulcore.contract import seal_spec  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    path = Path(argv[1])
    spec = json.loads(path.read_text(encoding="utf-8"))
    sealed = seal_spec(spec)
    path.write_text(json.dumps(sealed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"sealed {path}: {sealed['contentHash']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
