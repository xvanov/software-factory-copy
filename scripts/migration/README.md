# Migration scripts — software-factory + sacrifice

Two scripts to move both repos plus all preserved state (factory.db, Postgres data, secret env files) from one Linux machine to another.

## When to use

- You're switching dev machines and want to keep direction state, spend ledger, run history, goals, proofs, users — all of it.
- You want a one-shot reproducible setup on a brand-new box.

## Workflow

### On the OLD machine

```bash
~/software-factory/scripts/migration/bundle.sh
# → produces ~/factory-migration-bundle.tar.gz
```

The script:
- Verifies `sacrifice-db` is running (needed to `pg_dump`).
- Copies `software-factory/.env`, `software-factory/state/factory.db`, `sacrifice/.env`.
- Runs `pg_dump` inside the `sacrifice-db` container and gzips the output.
- Writes a `manifest.json` with SHA256s + source hostname + timestamp.
- Tars everything into a single `.tar.gz`.

Options:
- `--out PATH`        — bundle output path (default `~/factory-migration-bundle.tar.gz`).
- `--factory PATH`    — software-factory directory (default `~/software-factory`).
- `--sacrifice PATH`  — sacrifice directory (default `~/sacrifice`).
- `--db-container N`  — postgres container name (default `sacrifice-db`).

### Transfer

```bash
scp ~/factory-migration-bundle.tar.gz newhost:~/
```

(Any transfer mechanism works — USB stick, syncthing, rsync. It's a single file.)

### On the NEW machine

```bash
# 1. Clone the factory so you can run the bootstrap script.
git clone git@github.com:xvanov/software-factory.git ~/software-factory

# 2. Run bootstrap with the bundle path.
~/software-factory/scripts/migration/bootstrap.sh ~/factory-migration-bundle.tar.gz
```

The script auto-installs missing system packages on Ubuntu/Debian (`git`, `make`, `docker.io`, `nodejs`, `npm`, `postgresql-client`, `jq`, `curl`, `unzip`), installs `uv` via the official installer, clones both repos (skipping if present), sets up venvs, creates Docker containers for Postgres + Redis, restores the `.env` files + `factory.db` + Postgres dump, runs `alembic upgrade head`, and runs the factory test suite as a smoke check.

Idempotent — safe to re-run. Each phase checks state before acting.

Options:
- `--bundle PATH`     — bundle path (positional arg also works).
- `--no-bundle`       — fresh setup; you populate `.env` files manually afterwards.
- `--factory PATH`    — software-factory destination (default `~/software-factory`).
- `--sacrifice PATH`  — sacrifice destination (default `~/sacrifice`).

## What's preserved vs. recreated

| Item | How |
|---|---|
| Factory API keys (`software-factory/.env`) | Preserved from bundle |
| Factory state DB (spend ledger, story state, cursors) | Preserved from bundle |
| Sacrifice secrets (`sacrifice/.env`) | Preserved from bundle |
| Sacrifice Postgres data (users, goals, proofs, payments) | Preserved via `pg_dump`/restore |
| Sacrifice Redis data | Recreated empty (Celery broker; no durable state) |
| Python virtualenvs (factory, sacrifice backend) | Recreated via `uv sync` |
| Frontend `node_modules` | Recreated via `npm install` |
| Direction files, story files, code, context files | Come from git |

## Troubleshooting

- **`sacrifice-db` not running** when running `bundle.sh`: `make -C ~/sacrifice up-db` then re-run.
- **Postgres restore fails** on the new machine: the script uses `pg_dump --clean --if-exists`. If the target DB has incompatible schema, blow it away: `docker rm -fv sacrifice-db && docker volume rm sacrifice-postgres-data` then re-run bootstrap.
- **Docker permission denied**: add yourself to the docker group with `sudo usermod -aG docker $USER && newgrp docker`, then re-run.
- **Non-Ubuntu distro**: install `git make docker nodejs npm postgresql-client jq curl unzip` manually, then run `bootstrap.sh` — it skips the apt step if everything's already present.
- **SSH auth fails on `git clone`**: ensure your SSH key is set up on the new machine and registered with GitHub. The scripts use SSH URLs (`git@github.com:...`).
- **Node version too old**: bootstrap warns if `node -v` < 18. Install Node 20 with `curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs`.

## What this does NOT do

- It doesn't push or pull from GitHub. Your local commits should be pushed before bundling (verify with `git status` in both repos).
- It doesn't install or copy crontab entries. The factory README documents the cron lines for Ralph / Bug-Hunter / UX-Auditor / Security; install those manually if you used them.
- It doesn't copy `screenshots/`, `logs/`, `state/dry_run_scratch/`, or any other gitignored scratch content. Treat those as ephemeral.
- It doesn't handle GitHub Actions / CI secrets — those are managed in the GitHub UI per repo.
