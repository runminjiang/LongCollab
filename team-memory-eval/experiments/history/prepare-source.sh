#!/usr/bin/env bash
# 从当前检出复制运行模块，在新目录中装入指定历史批次的差异文件。
set -euo pipefail
history=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$history/../.." && pwd)
case "${1:-}" in
  work) variant=work-overlay ;;
  seven) variant=research-native ;;
  *) echo "用法: bash $0 {work|seven} 新目录" >&2; exit 2 ;;
esac
dest=$(realpath -m "${2:?请指定新目录}")
mkdir -- "$dest"
cd "$repo"
git ls-files -z -- run.py runtime service tools tasks scenarios eval \
  experiments/run_codex_research.py experiments/codex_research_worker.py |
  xargs -0 cp --parents --target-directory="$dest" --
cp -R "$history/$variant/." "$dest/"
