#!/usr/bin/env bash
# bootstrap.sh — run on the NEW machine to set up software-factory + sacrifice.
#
# Restores state from a migration bundle produced by bundle.sh.
# Auto-installs missing prereqs on Ubuntu/Debian via apt-get.
#
# Usage:
#   bootstrap.sh ~/factory-migration-bundle.tar.gz
#   bootstrap.sh --bundle ~/bundle.tar.gz --factory ~/sf --sacrifice ~/sac
#   bootstrap.sh --no-bundle            # fresh setup; you fill .env files yourself
#
# What it does (in order):
#   1.  Verify distro is apt-based; offer to install missing system deps.
#   2.  Install uv (if missing) via the official installer.
#   3.  Clone software-factory + sacrifice (skip if already present).
#   4.  uv sync the factory; uv sync the sacrifice backend; npm install the frontend.
#   5.  Extract the bundle and verify checksums against manifest.json.
#   6.  Restore .env files, factory.db, Postgres dump.
#   7.  Create or start sacrifice-db + sacrifice-redis Docker containers.
#   8.  Run alembic upgrade head against the restored sacrifice DB.
#   9.  Smoke-test: pytest the factory suite.
#   10. Print final summary and next-step commands.
#
# Idempotent: safe to re-run. Each phase checks state before acting.
#
# Exit codes:
#   0  success
#   1  unrecoverable error (see message)
#   2  missing prerequisites the script refused to install

set -euo pipefail

# ---- defaults ---------------------------------------------------------
FACTORY_DIR="${FACTORY_DIR:-$HOME/software-factory}"
SACRIFICE_DIR="${SACRIFICE_DIR:-$HOME/sacrifice}"
FACTORY_REPO="git@github.com:xvanov/software-factory.git"
SACRIFICE_REPO="git@github.com:xvanov/sacrifice.git"
DB_CONTAINER="sacrifice-db"
REDIS_CONTAINER="sacrifice-redis"
POSTGRES_IMAGE="postgres:16"
REDIS_IMAGE="redis:7-alpine"
BUNDLE=""
NO_BUNDLE=0

# ---- arg parsing ------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle)      BUNDLE="$2"; shift 2 ;;
    --factory)     FACTORY_DIR="$2"; shift 2 ;;
    --sacrifice)   SACRIFICE_DIR="$2"; shift 2 ;;
    --no-bundle)   NO_BUNDLE=1; shift ;;
    -h|--help)
      sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    -*)
      echo "unknown arg: $1" >&2; exit 1 ;;
    *)
      if [[ -z "$BUNDLE" ]]; then BUNDLE="$1"; shift
      else echo "unexpected positional: $1" >&2; exit 1
      fi
      ;;
  esac
done

if [[ "$NO_BUNDLE" -eq 0 && -z "$BUNDLE" ]]; then
  echo "Usage: bootstrap.sh BUNDLE.tar.gz   (or --no-bundle for fresh setup)" >&2
  exit 1
fi

if [[ -n "$BUNDLE" && ! -f "$BUNDLE" ]]; then
  echo "bundle not found: $BUNDLE" >&2; exit 1
fi

# ---- helpers ----------------------------------------------------------
log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mXX\033[0m  %s\n' "$*" >&2; exit "${2:-1}"; }
have() { command -v "$1" >/dev/null 2>&1; }

SUDO=""
if [[ "$EUID" -ne 0 ]]; then
  if have sudo; then SUDO="sudo"
  else die "must be root or have sudo installed" 2
  fi
fi

apt_install() {
  log "apt-get install -y $*"
  $SUDO apt-get install -y "$@"
}

# ---- phase 1: distro + system deps ------------------------------------
log "Phase 1/10: distro check + system prereqs"

if ! have apt-get; then
  die "this script auto-installs via apt-get (Ubuntu/Debian). For other distros, install: git, make, docker.io, nodejs, npm, postgresql-client, jq, build-essential. Then re-run with the deps already present." 2
fi

$SUDO apt-get update -y -qq

NEED=()
have git              || NEED+=("git")
have make             || NEED+=("make")
have docker           || NEED+=("docker.io")
have node             || NEED+=("nodejs")
have npm              || NEED+=("npm")
have psql             || NEED+=("postgresql-client")
have jq               || NEED+=("jq")
have curl             || NEED+=("curl")
have unzip            || NEED+=("unzip")

if (( ${#NEED[@]} > 0 )); then
  apt_install "${NEED[@]}"
else
  log "  all base system packages present."
fi

# docker group membership — warn if user is missing it (don't fix; requires relog)
if ! groups | grep -qw docker; then
  warn "you are not in the 'docker' group. Add yourself with:"
  warn "    $SUDO usermod -aG docker $(whoami) && newgrp docker"
  warn "or use 'sudo' before every docker command."
fi

# uv — install via official curl-pipe if missing
if ! have uv; then
  log "  installing uv (Astral)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin by default — make available for this script
  export PATH="$HOME/.local/bin:$PATH"
  hash -r
  have uv || die "uv install failed; check ~/.local/bin/uv"
fi

# Node version sanity (Expo 54 needs >=18; 20+ recommended)
NODE_MAJOR=$(node -v | sed 's/^v//;s/\..*//')
if (( NODE_MAJOR < 18 )); then
  warn "Node $NODE_MAJOR detected; Expo 54 needs >=18 (20+ recommended). Consider:"
  warn "    curl -fsSL https://deb.nodesource.com/setup_20.x | $SUDO -E bash - && $SUDO apt-get install -y nodejs"
fi

# ---- phase 2: clone repos ---------------------------------------------
log "Phase 2/10: clone repos"

clone_if_missing() {
  local repo="$1" dest="$2"
  if [[ -d "$dest/.git" ]]; then
    log "  $dest already cloned; skipping (run 'git -C $dest pull' to update)"
  else
    log "  git clone $repo -> $dest"
    git clone "$repo" "$dest"
  fi
}

clone_if_missing "$FACTORY_REPO"  "$FACTORY_DIR"
clone_if_missing "$SACRIFICE_REPO" "$SACRIFICE_DIR"

# ---- phase 3: factory venv --------------------------------------------
log "Phase 3/10: factory venv (uv sync)"
( cd "$FACTORY_DIR" && uv sync --quiet )
log "  factory deps installed"

# ---- phase 4: sacrifice backend venv ----------------------------------
log "Phase 4/10: sacrifice backend venv (uv sync)"
( cd "$SACRIFICE_DIR/backend" && uv sync --quiet )
log "  sacrifice backend deps installed"

# ---- phase 5: sacrifice frontend deps ---------------------------------
log "Phase 5/10: sacrifice frontend deps (npm install)"
if [[ -d "$SACRIFICE_DIR/frontend/node_modules" ]]; then
  log "  node_modules present; running 'npm install' to reconcile lockfile"
fi
( cd "$SACRIFICE_DIR/frontend" && npm install --no-fund --no-audit )
log "  frontend deps installed"

# ---- phase 6: extract + verify bundle ---------------------------------
if [[ "$NO_BUNDLE" -eq 1 ]]; then
  log "Phase 6/10: bundle [SKIPPED — --no-bundle]"
  log "  populate .env files manually:"
  log "    cp $FACTORY_DIR/.env.example   $FACTORY_DIR/.env   && \$EDITOR $FACTORY_DIR/.env"
  log "    cp $SACRIFICE_DIR/.env.example $SACRIFICE_DIR/.env && \$EDITOR $SACRIFICE_DIR/.env"
else
  log "Phase 6/10: extract + verify bundle ($BUNDLE)"
  STAGE="$(mktemp -d -t bootstrap-XXXXXX)"
  trap 'rm -rf "$STAGE"' EXIT
  tar -C "$STAGE" -xzf "$BUNDLE"

  [[ -f "$STAGE/manifest.json" ]] || die "bundle missing manifest.json"

  # Verify each file's sha256 against the manifest.
  while IFS=$'\t' read -r fname expected; do
    [[ -f "$STAGE/$fname" ]] || die "bundle missing file: $fname"
    actual=$(sha256sum "$STAGE/$fname" | awk '{print $1}')
    if [[ "$actual" != "$expected" ]]; then
      die "checksum mismatch for $fname: expected $expected got $actual"
    fi
  done < <(jq -r '.files | to_entries[] | "\(.key)\t\(.value.sha256)"' "$STAGE/manifest.json")

  log "  all 4 files verified against manifest"
fi

# ---- phase 7: restore .env files + factory.db -------------------------
log "Phase 7/10: restore .env files + factory.db"

if [[ "$NO_BUNDLE" -eq 0 ]]; then
  mkdir -p "$FACTORY_DIR/state"
  cp "$STAGE/factory.env"   "$FACTORY_DIR/.env"
  cp "$STAGE/factory.db"    "$FACTORY_DIR/state/factory.db"
  cp "$STAGE/sacrifice.env" "$SACRIFICE_DIR/.env"
  log "  restored: $FACTORY_DIR/.env"
  log "  restored: $FACTORY_DIR/state/factory.db"
  log "  restored: $SACRIFICE_DIR/.env"
fi

# Parse DB connection params from sacrifice/.env so we create/restore correctly.
# Expected format: DATABASE_URL=postgresql+asyncpg://USER:PASS@HOST:PORT/DBNAME
if [[ -f "$SACRIFICE_DIR/.env" ]]; then
  DB_URL=$(grep -E '^DATABASE_URL=' "$SACRIFICE_DIR/.env" | head -1 | cut -d= -f2- || true)
  if [[ -n "$DB_URL" ]]; then
    DB_USER=$(echo "$DB_URL"  | sed -nE 's#^.*://([^:]+):.*#\1#p')
    DB_PASS=$(echo "$DB_URL"  | sed -nE 's#^.*://[^:]+:([^@]+)@.*#\1#p')
    DB_PORT=$(echo "$DB_URL"  | sed -nE 's#^.*@[^:]+:([0-9]+)/.*#\1#p')
    DB_NAME=$(echo "$DB_URL"  | sed -nE 's#^.*/([^?]+).*#\1#p')
  fi
fi
DB_USER="${DB_USER:-postgres}"
DB_PASS="${DB_PASS:-postgres}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-sacrifice}"
log "  postgres params: user=$DB_USER port=$DB_PORT db=$DB_NAME"

# ---- phase 8: docker containers ---------------------------------------
log "Phase 8/10: docker containers (postgres + redis)"

container_state() {
  docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null || echo "missing"
}

PG_STATE=$(container_state "$DB_CONTAINER")
case "$PG_STATE" in
  running)  log "  $DB_CONTAINER already running" ;;
  exited|paused)
            log "  starting existing $DB_CONTAINER"
            docker start "$DB_CONTAINER" >/dev/null ;;
  missing)
            log "  creating $DB_CONTAINER ($POSTGRES_IMAGE)"
            docker run -d \
              --name "$DB_CONTAINER" \
              --restart unless-stopped \
              -e POSTGRES_USER="$DB_USER" \
              -e POSTGRES_PASSWORD="$DB_PASS" \
              -e POSTGRES_DB="$DB_NAME" \
              -p "${DB_PORT}:5432" \
              -v sacrifice-postgres-data:/var/lib/postgresql/data \
              "$POSTGRES_IMAGE" >/dev/null
            ;;
  *)        die "unexpected $DB_CONTAINER state: $PG_STATE" ;;
esac

REDIS_STATE=$(container_state "$REDIS_CONTAINER")
case "$REDIS_STATE" in
  running)  log "  $REDIS_CONTAINER already running" ;;
  exited|paused)
            log "  starting existing $REDIS_CONTAINER"
            docker start "$REDIS_CONTAINER" >/dev/null ;;
  missing)
            log "  creating $REDIS_CONTAINER ($REDIS_IMAGE)"
            docker run -d \
              --name "$REDIS_CONTAINER" \
              --restart unless-stopped \
              -p 6379:6379 \
              "$REDIS_IMAGE" >/dev/null
            ;;
  *)        die "unexpected $REDIS_CONTAINER state: $REDIS_STATE" ;;
esac

# Wait for postgres to accept connections.
log "  waiting for postgres to be ready"
for i in {1..30}; do
  if docker exec "$DB_CONTAINER" pg_isready -U "$DB_USER" -d "$DB_NAME" -q 2>/dev/null; then
    break
  fi
  sleep 1
  if (( i == 30 )); then die "postgres did not become ready in 30s"; fi
done
log "  postgres ready"

# ---- phase 9: restore Postgres dump + alembic upgrade -----------------
log "Phase 9/10: restore postgres dump + alembic upgrade head"

if [[ "$NO_BUNDLE" -eq 0 ]]; then
  log "  restoring sacrifice-postgres.sql.gz into $DB_NAME"
  gunzip -c "$STAGE/sacrifice-postgres.sql.gz" | docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -q >/dev/null
  log "  dump restored"
fi

log "  running alembic upgrade head"
( cd "$SACRIFICE_DIR/backend" && uv run alembic upgrade head ) || warn "alembic upgrade returned non-zero — verify manually"

# ---- phase 10: smoke test + summary -----------------------------------
log "Phase 10/10: factory pytest smoke"
if ( cd "$FACTORY_DIR" && uv run pytest -q --no-header 2>&1 | tail -5 ); then
  log "  factory tests OK"
else
  warn "  factory tests reported failures — review above"
fi

cat <<EOF

\033[1;32mBootstrap complete.\033[0m

Next steps:
  cd $SACRIFICE_DIR && make up          # start backend + frontend + db
  cd $FACTORY_DIR  && uv run factory inbox
  cd $FACTORY_DIR  && uv run factory pm-sync --app sacrifice

State preserved:
  factory.db    -> $FACTORY_DIR/state/factory.db
  factory .env  -> $FACTORY_DIR/.env
  sacrifice .env-> $SACRIFICE_DIR/.env
  postgres dump -> restored into '$DB_CONTAINER' as '$DB_NAME'

If anything went wrong:
  - Re-run this script; it is idempotent.
  - Check '$DB_CONTAINER' logs:  docker logs $DB_CONTAINER
  - Verify .env values:          \$EDITOR $FACTORY_DIR/.env $SACRIFICE_DIR/.env
  - Reset postgres data:         docker rm -fv $DB_CONTAINER && docker volume rm sacrifice-postgres-data
                                 then re-run this script.
EOF
