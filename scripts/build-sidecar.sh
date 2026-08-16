#!/usr/bin/env bash
# PS Editor - Python sidecar'ı PyInstaller ile derler (Linux/macOS).
# Çıktı: src-tauri/binaries/python-sidecar/ (onedir: exe + _internal/)
# Tauri bu dizini bundle.resources ile $RESOURCE/sidecar/ altına taşır.
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

# Eski tek dosya (onefile) kalıntılarını ve onedir çıktısını sıfırla.
rm -rf "$OUT_DIR/python-sidecar"
rm -f "$OUT_DIR/python-sidecar-$TRIPLE"

echo "==> PyInstaller ile derleniyor (target: $TRIPLE)..."
# Not: --add-data ayracı platforma göre değişir (Windows ';' , Linux/macOS ':')
# onedir: her çalıştırmada /tmp'e 2.8GB ayıklama yapmaz (onefile'ın _MEI*
# çöpü sorunu); Tauri tarafı dizini resource olarak paketler.
"$PYTHON" -m PyInstaller \
    -n python-sidecar \
    --onedir \
    --clean \
    --distpath "$OUT_DIR" \
    --workpath "$PROJECT_ROOT/python/build" \
    --specpath "$PROJECT_ROOT/python" \
    --collect-all manga_ocr \
    --collect-all unidic_lite \
    --add-data "$PROJECT_ROOT/python/fonts:fonts" \
    "$SCRIPT"

if [ ! -x "$OUT_DIR/python-sidecar/python-sidecar" ]; then
    echo "HATA: onedir çıktısı beklenen konumda değil: $OUT_DIR/python-sidecar/python-sidecar" >&2
    exit 1
fi

echo ""
echo "Sidecar hazır: $OUT_DIR/python-sidecar/"
echo "Kurulum dizini büyüktür (~ayıklanmış içerik), ama /tmp kullanımı SIFIR."

# Eski derleme notu: Tauri önbelleği güncellenmezse eski dosya paketlenebilir
# (tauri#15134). Eski externalBin kopyaları (target/<profil>/python-sidecar)
# artık kullanılmıyor; resource kopyaları (sidecar/ dizini) dokunulmaz.
find "$PROJECT_ROOT/src-tauri/target" -maxdepth 2 -type f -name "python-sidecar" -delete 2>/dev/null || true
if [ -e "$PROJECT_ROOT/src-tauri/target/debug/python-sidecar.exe" ]; then
    rm -f "$PROJECT_ROOT/src-tauri/target/debug/python-sidecar.exe"
fi
if [ -e "$PROJECT_ROOT/src-tauri/target/release/python-sidecar.exe" ]; then
    rm -f "$PROJECT_ROOT/src-tauri/target/release/python-sidecar.exe"
fi
