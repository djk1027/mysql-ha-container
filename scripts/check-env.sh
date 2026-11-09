#!/usr/bin/env bash
set -euo pipefail
source /etc/os-release

case "$ID" in
    ubuntu|debian)
        echo "[INFO] Debian-based Linux detected"
        ;;
    rhel|rocky|almalinux|centos|fedora)
        echo "[INFO] Red Hat-based Linux detected"
        ;;
    *)
        echo "[ERROR] Unsupported OS: $ID"
        exit 1
        ;;
esac

echo "[INFO] 실행 환경 사전점검..."

# Docker CLI 설치 확인
if ! command -v docker >/dev/null 2>&1; then
    echo "[ERROR] Docker CLI is not installed."
    echo "Install Docker Engine or Docker Desktop."
    exit 1
fi

# Docker Compose 확인
if ! docker compose version >/dev/null 2>&1; then
    echo "[ERROR] Docker Compose V2 is required."
    exit 1
fi

# Docker daemon 접근 확인
if ! docker info >/dev/null 2>&1; then
    echo "[ERROR] Docker daemon is not running or accessible."
    exit 1
fi

echo "[OK] Docker: $(docker --version)"
echo "[OK] Compose: $(docker compose version --short)"
echo "[OK] Docker daemon accessible"