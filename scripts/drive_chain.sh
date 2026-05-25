#!/usr/bin/env bash
# drive_chain.sh — drive ``factory tick`` for an app until the queue drains
# or daily-cap pressure forces a stop. Generic: works for any app under
# ``apps/<name>/``; the factory itself is stack- and app-agnostic.
#
# Usage:
#   scripts/drive_chain.sh <app>
#   DAILY_LIMIT_USD=80 scripts/drive_chain.sh <app>
#
# Exits 0 when story_created queue is empty, 2 when spend guard trips,
# and propagates errors from ``factory tick`` invocations.

set -euo pipefail

if [ "$#" -lt 1 ] || [ -z "${1:-}" ]; then
  echo "usage: $0 <app>" >&2
  echo "  (positional <app> required — drive the chain for that app's queue)" >&2
  exit 64
fi

APP="$1"
cd "$(dirname "$0")/.."

DAILY_LIMIT_USD="${DAILY_LIMIT_USD:-90}"  # default guard: 90% of $100 cap

emit() { printf '[drive %s app=%s] %s\n' "$(date -u +%H:%M:%S)" "$APP" "$*"; }

while :; do
  budget_out=$(uv run factory budget 2>/dev/null)
  spend=$(printf '%s\n' "$budget_out" | awk -F'│' '/today_spend_usd/ {gsub(/[^0-9.]/,"",$3); print $3}')
  hour=$(printf '%s\n' "$budget_out" | awk -F'│' '/hour_spend_usd/ {gsub(/[^0-9.]/,"",$3); print $3}')
  hour_cap=$(printf '%s\n' "$budget_out" | awk -F'│' '/hourly_cap_usd/ {gsub(/[^0-9.]/,"",$3); print $3}')
  emit "spend today=\$${spend} hour=\$${hour} hour_cap=\$${hour_cap}"

  if awk "BEGIN{exit !(${spend:-0} >= ${DAILY_LIMIT_USD})}"; then
    emit "STOP: daily spend \$${spend} >= guard \$${DAILY_LIMIT_USD}"
    exit 2
  fi

  remaining=$(uv run factory queue --app "$APP" 2>/dev/null | grep -c "story_created" || true)
  emit "story_created remaining=${remaining}"
  if [ "${remaining}" -eq 0 ]; then
    emit "DONE: no story_created rows left"
    break
  fi

  # Pause within 5% of the configured hourly cap; reads the cap live so
  # a factory_settings.yaml change takes effect on the next iteration.
  threshold=$(awk "BEGIN{printf \"%.4f\", ${hour_cap:-2} * 0.95}")
  if awk "BEGIN{exit !(${hour:-0} >= ${threshold})}"; then
    emit "hourly cap near/over (\$${hour} / cap \$${hour_cap}); sleeping 120s"
    sleep 120
    continue
  fi

  emit "ticking…"
  uv run factory tick --app "$APP" 2>&1 | tail -25 || emit "tick errored (non-fatal, will retry)"
  emit "tick complete; brief pause before next iteration"
  sleep 30
done
