#!/usr/bin/env bash
# PS Editor - Python geliştirme ortamı kurulumu (Linux/macOS)
# python/.venv sanal ortamını oluşturur ve bağımlılıkları kurar.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$PROJECT_ROOT/python/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "==> python/.venv oluşturuluyor..."
    PY=""
    for candidate in python3.11 python3.12 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PY="$candidate"
            break
        fi
    done
    if [ -z "$PY" ]; then
        echo "Hata: python3 bulunamadı. https://www.python.org/downloads/ adresinden kurun." >&2
        exit 1
    fi
    "$PY" -m venv "$VENV_DIR"
else
    echo "==> python/.venv zaten var."
fi

PYTHON="$VENV_DIR/bin/python"
echo "==> pip güncelleniyor..."
"$PYTHON" -m pip install --upgrade pip --quiet
echo "==> Bağımlılıklar kuruluyor (python/requirements.txt)..."
"$PYTHON" -m pip install -r "$PROJECT_ROOT/python/requirements.txt"

echo ""
echo "Python ortamı hazır: $PYTHON"
