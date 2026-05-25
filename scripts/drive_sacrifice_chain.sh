#!/usr/bin/env bash
# Drive the sacrifice chain until queue drains or daily-cap pressure forces a stop.
# Pauses politely while hourly_spend_cap_exceeded gates everything; exits when
# nothing in story_created remains.
set -euo pipefail
cd "$(dirname "$0")/.."

DAILY_LIMIT_USD="${DAILY_LIMIT_USD:-90}"  # surface to user at 90% of $100 cap

emit() { printf '[drive %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

while :; do
  spend=$(uv run factory budget 2>/dev/null | awk -F'│' '/today_spend_usd/ {gsub(/[^0-9.]/,"",$3); print $3}')
  hour=$(uv run factory budget 2>/dev/null | awk -F'│' '/hour_spend_usd/ {gsub(/[^0-9.]/,"",$3); print $3}')
  emit "spend today=\$${spend} hour=\$${hour}"

  if awk "BEGIN{exit !(${spend:-0} >= ${DAILY_LIMIT_USD})}"; then
    emit "STOP: daily spend \$${spend} >= guard \$${DAILY_LIMIT_USD}"
    exit 2
  fi

  remaining=$(uv run factory queue --app sacrifice 2>/dev/null | grep -c "story_created" || true)
  emit "story_created remaining=${remaining}"
  if [ "${remaining}" -eq 0 ]; then
    emit "DONE: no story_created rows left"
    break
  fi

  hour_cap=$(uv run factory budget 2>/dev/null | awk -F'│' '/hourly_cap_usd/ {gsub(/[^0-9.]/,"",$3); print $3}')
  # Pause when within 5% of the configured hourly cap. Reads the cap live
  # so a `factory_settings.yaml` change takes effect on the next iteration.
  threshold=$(awk "BEGIN{printf \"%.4f\", ${hour_cap:-2} * 0.95}")
  if awk "BEGIN{exit !(${hour:-0} >= ${threshold})}"; then
    emit "hourly cap near/over (\$${hour} / cap \$${hour_cap}); sleeping 120s"
    sleep 120
    continue
  fi

  emit "ticking…"
  uv run factory tick --app sacrifice 2>&1 | tail -25 || emit "tick errored (non-fatal, will retry)"
  emit "tick complete; brief pause before next iteration"
  sleep 30
done
