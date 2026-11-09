#!/usr/bin/env bash
set -euo pipefail

# 프로젝트 루트로 이동
cd "$(dirname "$0")"

echo "=============================="
echo " 1. Checking environment"
echo "=============================="
bash scripts/check-env.sh
echo

echo "=============================="
echo " 2. Starting MySQL containers"
echo "=============================="
docker compose up -d --wait
echo

echo "=============================="
echo " 3. Configuring replication"
echo "=============================="
bash scripts/setup-replication.sh
echo

echo "------------------------------"
echo " MySQL HA setup completed! "
echo "------------------------------"
docker compose ps