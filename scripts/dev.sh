#!/usr/bin/env bash
# PolyEcho — development helper script
# Make executable: chmod +x scripts/dev.sh  (or run as: bash scripts/dev.sh <command>)
#
# Usage:
#   scripts/dev.sh up          # Start all services (production compose)
#   scripts/dev.sh down        # Stop and remove containers
#   scripts/dev.sh dev         # Drop into the all-in-one dev container shell
#   scripts/dev.sh doctor      # Run `polyecho doctor` via the CLI container
#   scripts/dev.sh logs [svc]  # Tail logs (all services or a specific one)
#   scripts/dev.sh db          # Open a psql shell in the DB container
#   scripts/dev.sh migrate     # Apply infra/migrations/0000_init.sql into the running DB
#   scripts/dev.sh discovery   # Run discovery once in fixture mode (no network)
#   scripts/dev.sh psql        # Alias for `db` — open psql in the DB container
#   scripts/dev.sh build       # Rebuild all images
#   scripts/dev.sh ps          # Show running containers

set -euo pipefail

# Resolve repo root (works regardless of cwd)
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_PROD="$REPO_ROOT/infra/docker-compose.yml"
COMPOSE_DEV="$REPO_ROOT/infra/docker-compose.dev.yml"

# Helper: run compose against production config
compose_prod() {
  docker compose -f "$COMPOSE_PROD" "$@"
}

# Helper: run compose against prod + dev override
compose_dev() {
  docker compose -f "$COMPOSE_PROD" -f "$COMPOSE_DEV" "$@"
}

CMD="${1:-help}"
shift 2>/dev/null || true

case "$CMD" in
  up)
    echo "Starting PolyEcho services..."
    compose_prod up -d
    echo "Services started. Dashboard: http://localhost:3000"
    ;;

  down)
    echo "Stopping PolyEcho services..."
    compose_prod down
    ;;

  dev)
    echo "Starting dev container (uv + pnpm, repo bind-mounted)..."
    compose_dev run --rm --profile dev dev bash
    ;;

  doctor)
    echo "Running polyecho doctor..."
    compose_prod run --rm --profile tools cli polyecho doctor "$@"
    ;;

  logs)
    SVC="${1:-}"
    if [ -n "$SVC" ]; then
      compose_prod logs -f "$SVC"
    else
      compose_prod logs -f
    fi
    ;;

  db|psql)
    echo "Opening psql shell in DB container..."
    compose_prod exec db psql -U polyecho -d polyecho
    ;;

  migrate)
    MIGRATION="$REPO_ROOT/infra/migrations/0000_init.sql"
    echo "Applying $MIGRATION into the running DB container..."
    # Pipe the SQL file through docker compose exec so no host psql needed
    compose_prod exec -T db \
      psql -U polyecho -d polyecho \
      < "$MIGRATION"
    echo "Migration applied successfully."
    ;;

  discovery)
    echo "Running discovery once in fixture mode (POLYECHO_DATASOURCE=fixture)..."
    compose_prod run --rm \
      -e POLYECHO_DATASOURCE=fixture \
      discovery \
      python -m polyecho.discovery --once
    ;;

  build)
    echo "Rebuilding all images..."
    compose_prod build "$@"
    ;;

  ps)
    compose_prod ps
    ;;

  config)
    echo "Validating docker-compose config..."
    compose_prod config
    ;;

  help|--help|-h)
    grep '^#' "$0" | sed 's/^#//'
    ;;

  *)
    echo "Unknown command: $CMD" >&2
    echo "Run: $0 help" >&2
    exit 1
    ;;
esac
