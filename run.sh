#!/usr/bin/env bash
# 一条命令启动：自动播种演示数据（含错户冲正样例）并启动本地 Web。
#   ./run.sh            端口 8000
#   ./run.sh 8010       指定端口
#   ./run.sh --reset    清空演示数据后重建
set -euo pipefail
cd "$(dirname "$0")"

PORT=8000
RESET=""
for arg in "$@"; do
  if [[ "$arg" =~ ^[0-9]+$ ]]; then PORT="$arg"; else RESET="$arg"; fi
done

exec python3 -m app.seed --serve $RESET "$PORT"
