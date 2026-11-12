#!/usr/bin/env bash
# 本地手动跑一次签到（不需要 GitHub）。零依赖，直接执行即可。
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f accounts.json ] && [ -z "${ABLESCI_ACCOUNTS_JSON:-}" ] \
   && [ -z "${ABLESCI_ACCOUNTS:-}" ] && [ -z "${ABLESCI_COOKIE:-}" ]; then
  echo "还没有账号配置。" >&2
  echo "  cp accounts.example.json accounts.json  然后填写；或参考 .env.example 设置环境变量" >&2
  exit 1
fi

exec python3 ablesci_checkin.py "$@"
