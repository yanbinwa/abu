#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
service_dir="$repo_dir/services/wecom_bot"

if command -v node >/dev/null 2>&1; then
  node_bin=$(command -v node)
else
  node_bin="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
fi

if [ ! -x "$node_bin" ]; then
  echo "Node.js 未找到，请安装 Node.js 18+ 或设置 PATH。" >&2
  exit 127
fi

cd "$service_dir"
exec "$node_bin" bot.mjs
