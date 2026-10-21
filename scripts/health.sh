#!/usr/bin/env bash
# 代码健康报告(只报告, 永远 exit 0, 不拦提交)。阈值同 docs/conventions.md「代码健康」。
# 工具经 uvx / npx 临时运行并固定版本: 不进 pyproject / uv.lock, 项目运行时仍零依赖。
# 用法: bash scripts/health.sh [--full]   (--full 输出全部明细, 默认只给计数 + 前几条)
cd "$(dirname "$0")/.." || exit 0
FULL=0; [ "${1:-}" = "--full" ] && FULL=1
SRC=(sniper.py dashboard.py salad_portal.py salad_login.py salad_autologin.py)
VULTURE="vulture==2.14"; RUFF="ruff==0.6.9"; RADON="radon==6.0.1"; JSCPD="jscpd@4.0.5"
top() { if [ "$FULL" = 1 ]; then cat; else head -n "$1"; fi; }
have() { command -v "$1" >/dev/null 2>&1; }

echo "== 单文件行数 > 400"
wc -l "${SRC[@]}" | awk '$2 != "total" && $1 > 400 {printf "  %-22s %5d\n", $2, $1}'

if ! have uvx; then echo "(未找到 uvx, 跳过 Python 工具: 装 uv 后重跑)"; else
  echo "== 死代码 vulture(置信度 ≥80)"
  out=$(uvx -q --from "$VULTURE" vulture "${SRC[@]}" --min-confidence 80 2>/dev/null)
  echo "  共 $(printf '%s' "$out" | grep -c .) 处"; printf '%s\n' "$out" | grep . | top 10 | sed 's/^/  /'

  echo "== 未用导入 / 变量 ruff F"
  out=$(uvx -q --from "$RUFF" ruff check --isolated --select F --output-format concise "${SRC[@]}" 2>/dev/null | grep -E '^[^ ].*:[0-9]+:[0-9]+:')
  echo "  共 $(printf '%s' "$out" | grep -c .) 处"; printf '%s\n' "$out" | grep . | top 10 | sed 's/^/  /'

  echo "== 圈复杂度 > 10(ruff C901)"
  n=$(uvx -q --from "$RUFF" ruff check --isolated --select C901 --config 'lint.mccabe.max-complexity=10' --output-format concise "${SRC[@]}" 2>/dev/null | grep -c C901)
  echo "  共 $n 个函数; 最复杂的(radon; 列: 复杂度 位置 函数):"
  uvx -q --from "$RADON" radon cc -n C -s "${SRC[@]}" 2>/dev/null \
    | awk '/^[^ ]/{f=$1} /^ +[FMC] /{gsub(/[()]/,"",$NF); split($2, p, ":"); print $NF+0, f":"p[1], $3}' \
    | sort -rn | top 10 | awk '{printf "  %-4s %-26s %s\n", $1, $2, $3}'
fi

echo "== 重复块 jscpd(≥8 行)"
if ! have npx; then echo "  (未找到 npx, 跳过)"; else
  tmp=$(mktemp -d)
  npx --yes "$JSCPD" --silent --min-lines 8 --max-size 2mb --max-lines 50000 --format python \
      --reporters json --output "$tmp" "${SRC[@]}" >/dev/null 2>&1
  python3 - "$tmp/jscpd-report.json" "$FULL" <<'EOF' 2>/dev/null || echo "  (jscpd 未产出报告)"
import json, sys
d = json.load(open(sys.argv[1])); s = d["statistics"]["total"]
print(f"  {s['clones']} 处, 重复 {s['duplicatedLines']} / {s['lines']} 行 ({s['percentage']:.1f}%)")
dups = sorted(d["duplicates"], key=lambda c: -c["lines"])
for c in (dups if sys.argv[2] == "1" else dups[:5]):
    a, b = c["firstFile"], c["secondFile"]
    print(f"  {c['lines']:3d} 行  {a['name']}:{a['start']} <-> {b['name']}:{b['start']}")
EOF
  rm -rf "$tmp"
fi
exit 0
