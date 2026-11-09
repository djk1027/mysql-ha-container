#!/usr/bin/env bash
set -euo pipefail

# 프로젝트 루트로 이동
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

echo "[WARNING] MySQL HA 환경을 초기화 합니다."
echo "[WARNING] 모든 MySQL 컨테이너와 데이터 볼륨이 삭제됩니다."
if [[ "${1:-}" == "--all" ]]; then
echo "[WARNING] Python3 가상화 환경을 초기화 합니다."
fi
echo

read -r -p "초기화 하려면 \"Y\"를 입력하세요 : " confirm
echo

if [[ "$confirm" != "Y" ]]; then
    echo "[INFO] 초기화를 취소했습니다."
    exit 0
fi

echo "[INFO] Stopping containers and removing volumes..."

docker compose -p mysql-ha-container down --volumes --remove-orphans
echo "[OK] MySQL environment reset."

if [[ "${1:-}" == "--all" ]]; then
    rm -rf -- "$PROJECT_ROOT/.venv"
    echo "[OK] Python virtual environment removed."
fi

echo "---------------------------------------"
echo " MySQL HA environment reset completed."
echo "---------------------------------------"