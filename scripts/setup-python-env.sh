#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON="$PROJECT_ROOT/.venv/bin/python"

# 가상환경 생성
if [[ ! -x "$PYTHON" ]]; then
    echo "[INFO] Creating Python virtual environment..."
    python3 -m venv .venv
fi
echo "[OK] Python virtual environment ready."
echo "[OK] Python: $("$PYTHON" --version)"

# Python 패키지 설치
echo "[INFO] Installing Python dependencies..."
"$PYTHON" -m pip install -r requirements.txt

# python 패키지 확인
if ! "$PYTHON" -c "import pymysql; import dotenv" >/dev/null 2>&1; then
    echo "[ERROR] Python dependencies are unavailable."
    exit 1
fi
echo "[OK] PyMySQL available."