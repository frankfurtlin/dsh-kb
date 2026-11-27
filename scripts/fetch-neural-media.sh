#!/usr/bin/env bash
# 从同机的 betterlife/app/domains/neural 复制环境视觉素材（水母 / 光球）。
# 素材版权归 neural-creator-dashboard 作者；PolyForm Noncommercial 1.0.0
# （0.1.0 另有 MIT-legacy）。仅个人非商业使用，故不入库。
set -euo pipefail
# 素材源在同机另一个项目里：默认按家目录推导，可用 BETTERLIFE_NEURAL_MEDIA 覆盖。
SRC="${1:-${BETTERLIFE_NEURAL_MEDIA:-$HOME/workspace/betterlife/app/domains/neural/public/media}}"
DST="$(cd "$(dirname "$0")/.." && pwd)/server/app/web/static/media"
mkdir -p "$DST"
for f in jellyfish.mp4 jellyfish.jpg orb.jpg; do
  cp "$SRC/$f" "$DST/$f"
done
echo "copied to $DST"
