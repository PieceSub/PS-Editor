#!/usr/bin/env bash
# PS Editor - Python sidecar'ı PyInstaller ile derler (Linux/macOS).
# Çıktı: src-tauri/binaries/python-sidecar-<target-triple>
# Tauri'nin externalBin yapılandırması bu isimlendirmeyi bekler.
# Kullanım: ./scripts/build-sidecar.sh [--clean]
set -euo pipefail

CLEAN=0
for arg in "$@"; do
    case "$arg" in
        --clean|-c) CLEAN=1 ;;
        *) echo "Bilinmeyen argüman: $arg" >&2; exit 1 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON="$PROJECT_ROOT/python/.venv/bin/python"
SCRIPT="$PROJECT_ROOT/python/sidecar.py"
OUT_DIR="$PROJECT_ROOT/src-tauri/binaries"

if [ ! -x "$PYTHON" ]; then
    echo "venv bulunamadı. Önce: npm run setup:python" >&2
    exit 1
fi

echo "==> PyInstaller kuruluyor..."
"$PYTHON" -m pip install --upgrade pyinstaller --quiet

TRIPLE="$(rustc -Vv 2>/dev/null | awk -F': ' '/^host:/ {print $2}')"
if [ -z "$TRIPLE" ]; then
    TRIPLE="x86_64-unknown-linux-gnu"
fi

mkdir -p "$OUT_DIR"

FINAL="$OUT_DIR/python-sidecar-$TRIPLE"
if [ "$CLEAN" -eq 1 ] && [ -e "$FINAL" ]; then
    rm -f "$FINAL"
fi

echo "==> PyInstaller ile derleniyor (target: $TRIPLE)..."
# Not: --add-data ayracı platforma göre değişir (Windows ';' , Linux/macOS ':')
"$PYTHON" -m PyInstaller \
    -n python-sidecar \
    --onefile \
    --clean \
    --distpath "$OUT_DIR" \
    --workpath "$PROJECT_ROOT/python/build" \
    --specpath "$PROJECT_ROOT/python" \
    --collect-all manga_ocr \
    --collect-all unidic_lite \
    --add-data "$PROJECT_ROOT/python/fonts:fonts" \
    "$SCRIPT"

if [ -e "$FINAL" ]; then
    rm -f "$FINAL"
fi
mv "$OUT_DIR/python-sidecar" "$FINAL"

echo ""
echo "Sidecar hazır: $FINAL"
echo "Bilinen sorun: src-tauri/target içindeki önbellek güncellenmezse eski binary paketlenebilir."
echo "Emin olmak için: npm run tauri build öncesinde src-tauri/target/release/python-sidecar dosyasını silin."
