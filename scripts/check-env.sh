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

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv/bin/python"

echo "[INFO] 실행 환경 사전점검..."

# Docker CLI 설치 확인
if ! command -v docker >/dev/null 2>&1; then
    echo "[ERROR] Docker CLI is not installed."
    echo
    echo "hint) Install Docker Engine (includes Docker CLI):"
    echo "      https://docs.docker.com/engine/install/"
    echo
    echo "hint) Verify installation:"
    echo "      # docker --version"
    exit 1
fi
echo "[OK] Docker: $(docker --version)"

# Docker Compose 확인
if ! docker compose version >/dev/null 2>&1; then
    echo "[ERROR] Docker Compose V2 is not installed or unavailable."
    echo
    echo "hint) Install Docker Compose plugin:"
    echo "      Ubuntu/Debian: sudo apt install docker-compose-plugin"
    echo "      RHEL/Rocky:    sudo dnf install docker-compose-plugin"
    echo
    echo "hint) Verify installation:"
    echo "      # docker compose version"
    echo
    echo "hint) Documentation:"
    echo "      https://docs.docker.com/compose/install/linux/"
    exit 1
fi
echo "[OK] Compose: $(docker compose version --short)"

# Docker daemon 접근 확인
if ! docker info >/dev/null 2>&1; then
    echo "[ERROR] Docker daemon is not running or accessible."
    echo
    echo "hint) Check Docker daemon status:"
    echo "      # sudo systemctl status docker"
    echo
    echo "      Start Docker daemon:"
    echo "      # sudo systemctl start docker"
    exit 1
fi
echo "[OK] Docker daemon accessible"

# Python 3 설치 확인
if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] Python 3 is not installed."
    exit 1
fi
echo "[OK] Python: $(python3 --version)"