#!/usr/bin/env bash
# bundle.sh — run on the OLD machine to package migration artifacts.
#
# Output: a single tarball containing everything bootstrap.sh needs to
# restore the factory + sacrifice stack on a new machine WITHOUT losing
# state.
#
# Contents of the produced tarball:
#   - factory.env                  (software-factory/.env)
#   - factory.db                   (software-factory/state/factory.db)
#   - sacrifice.env                (sacrifice/.env)
#   - sacrifice-postgres.sql.gz    (pg_dump of the running sacrifice-db)
#   - manifest.json                (timestamps + sha256 per file + source host)
#
# Usage:
#   ./bundle.sh                            # writes to ~/factory-migration-bundle.tar.gz
#   ./bundle.sh --out /tmp/bundle.tar.gz
#   ./bundle.sh --factory ~/sf --sacrifice ~/sac --out ~/bundle.tar.gz
#
# Exit codes:
#   0  bundle written successfully
#   1  missing prerequisite (sacrifice-db not running, etc.)
#   2  required source file missing

set -euo pipefail

# ---- defaults ---------------------------------------------------------
FACTORY_DIR="${FACTORY_DIR:-$HOME/software-factory}"
SACRIFICE_DIR="${SACRIFICE_DIR:-$HOME/sacrifice}"
OUT="${OUT:-$HOME/factory-migration-bundle.tar.gz}"
DB_CONTAINER="sacrifice-db"
DB_NAME="sacrifice"
DB_USER="postgres"

# ---- arg parsing ------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --factory)     FACTORY_DIR="$2"; shift 2 ;;
    --sacrifice)   SACRIFICE_DIR="$2"; shift 2 ;;
    --out)         OUT="$2"; shift 2 ;;
    --db-container) DB_CONTAINER="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 1
      ;;
  esac
done

# ---- helpers ----------------------------------------------------------
log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mXX\033[0m  %s\n' "$*" >&2; exit "${2:-1}"; }

sha256() { sha256sum "$1" | awk '{print $1}'; }

require_file() {
  [[ -f "$1" ]] || die "required file missing: $1" 2
}

# ---- preflight --------------------------------------------------------
log "Bundling migration artifacts."
log "  factory   = $FACTORY_DIR"
log "  sacrifice = $SACRIFICE_DIR"
log "  output    = $OUT"

require_file "$FACTORY_DIR/.env"
require_file "$FACTORY_DIR/state/factory.db"
require_file "$SACRIFICE_DIR/.env"

command -v docker >/dev/null || die "docker not on PATH"
command -v jq >/dev/null     || warn "jq not installed; manifest will be hand-written (still valid JSON)"

# Verify sacrifice-db container is up; pg_dump runs inside it.
if ! docker inspect -f '{{.State.Running}}' "$DB_CONTAINER" 2>/dev/null | grep -q true; then
  die "container '$DB_CONTAINER' is not running. Start it with 'make -C $SACRIFICE_DIR up-db' then re-run."
fi

# ---- stage ------------------------------------------------------------
STAGE="$(mktemp -d -t factory-bundle-XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT

log "Staging in $STAGE"

cp "$FACTORY_DIR/.env"               "$STAGE/factory.env"
cp "$FACTORY_DIR/state/factory.db"   "$STAGE/factory.db"
cp "$SACRIFICE_DIR/.env"             "$STAGE/sacrifice.env"

log "Dumping Postgres ($DB_NAME from $DB_CONTAINER)..."
docker exec "$DB_CONTAINER" pg_dump \
  --username="$DB_USER" \
  --dbname="$DB_NAME" \
  --clean --if-exists --no-owner --no-privileges \
  | gzip --best > "$STAGE/sacrifice-postgres.sql.gz"

DUMP_SIZE=$(stat -c%s "$STAGE/sacrifice-postgres.sql.gz")
log "  pg_dump.sql.gz = $(numfmt --to=iec --suffix=B "$DUMP_SIZE")"

# ---- manifest ---------------------------------------------------------
cat > "$STAGE/manifest.json" <<EOF
{
  "schema": 1,
  "produced_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "source_host": "$(hostname)",
  "source_user": "$(whoami)",
  "factory_dir": "$FACTORY_DIR",
  "sacrifice_dir": "$SACRIFICE_DIR",
  "db_container": "$DB_CONTAINER",
  "db_name": "$DB_NAME",
  "files": {
    "factory.env":                { "sha256": "$(sha256 "$STAGE/factory.env")",               "bytes": $(stat -c%s "$STAGE/factory.env") },
    "factory.db":                 { "sha256": "$(sha256 "$STAGE/factory.db")",                "bytes": $(stat -c%s "$STAGE/factory.db") },
    "sacrifice.env":              { "sha256": "$(sha256 "$STAGE/sacrifice.env")",             "bytes": $(stat -c%s "$STAGE/sacrifice.env") },
    "sacrifice-postgres.sql.gz":  { "sha256": "$(sha256 "$STAGE/sacrifice-postgres.sql.gz")", "bytes": $(stat -c%s "$STAGE/sacrifice-postgres.sql.gz") }
  }
}
EOF

# ---- tar --------------------------------------------------------------
log "Writing $OUT"
mkdir -p "$(dirname "$OUT")"
tar -C "$STAGE" -czf "$OUT" \
  manifest.json factory.env factory.db sacrifice.env sacrifice-postgres.sql.gz

OUT_SIZE=$(stat -c%s "$OUT")
OUT_SHA=$(sha256 "$OUT")

# ---- done -------------------------------------------------------------
log "Bundle written."
echo
echo "  path:   $OUT"
echo "  size:   $(numfmt --to=iec --suffix=B "$OUT_SIZE")"
echo "  sha256: $OUT_SHA"
echo
echo "Transfer to the new machine, e.g.:"
echo "  scp \"$OUT\" newhost:~/"
echo "Then on the new machine:"
echo "  git clone git@github.com:xvanov/software-factory.git ~/software-factory"
echo "  ~/software-factory/scripts/migration/bootstrap.sh ~/$(basename "$OUT")"
