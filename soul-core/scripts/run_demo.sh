#!/usr/bin/env bash
# 端到端演示：身份契约 -> 写入判定 -> 衰减 -> 巩固 -> 评估
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/demo.py
