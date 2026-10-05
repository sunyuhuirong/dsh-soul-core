#!/usr/bin/env bash
# 运行全部测试（零依赖，仅需 Python 3.9+）
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -m unittest discover -s tests -t . -v
