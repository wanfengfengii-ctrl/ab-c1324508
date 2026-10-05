#!/usr/bin/env bash
# One-shot verification, as delivered in Docker.
#
#   docker compose run --rm scripts/verify.sh style flow is not available
#   for the host script itself, so this runs on the HOST with a working
#   docker + docker compose plugin:
#
#     scripts/verify.sh
#
# Exit code is non-zero if any of: image build, unit tests, smoke stage 1,
# API restart, or smoke stage 2 fails.
set -euo pipefail

cd "$(dirname "$0")/.."

if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  echo "ERROR: docker compose is required (or run scripts/verify_local.sh)" >&2
  exit 2
fi

export API_PORT="${API_PORT:-8080}"
trap '$DC --profile verify down -v >/dev/null 2>&1 || true' EXIT

echo "==> [1/5] building image"
$DC build

echo "==> [2/5] unit tests"
$DC --profile verify run --rm unit-tests

echo "==> [3/5] start API + smoke stage 1 (create, contention, complete)"
$DC up -d api
$DC --profile verify run --rm -e STAGE=smoke1 verify

echo "==> [4/5] restarting API container (persistence check)"
$DC restart api
$DC exec -T api true  # fail fast if the container did not come back

echo "==> [5/5] smoke stage 2 (state + replay after restart)"
$DC --profile verify run --rm -e STAGE=smoke2 verify

echo
echo "ALL VERIFICATION PASSED: build, unit tests, smoke1, restart, smoke2."
