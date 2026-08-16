# PS Editor - Python sidecar'ı PyInstaller ile derler (Windows).
# Çıktı: src-tauri/binaries/python-sidecar/ (onedir: python-sidecar.exe + _internal/)
# Tauri bu dizini bundle.resources ile $RESOURCE/sidecar/ altına taşır.
param([switch]$Clean)
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot "python\.venv\Scripts\python.exe"
$script = Join-Path $projectRoot "python\sidecar.py"
$outDir = Join-Path $projectRoot "src-tauri\binaries"

if (-not (Test-Path $python)) {
    Write-Error "venv bulunamadı. Önce: npm run setup:python"
}

Write-Host "==> PyInstaller kuruluyor..."
& $python -m pip install --upgrade pyinstaller --quiet
if (-not $?) { exit 1 }

$triple = ((& rustc -Vv | Select-String "host:").ToString() -split " ")[1].Trim()
if (-not $triple) {
    $triple = "x86_64-pc-windows-msvc"
}

New-Item -ItemType Directory -Force -Path $outDir | Out-Null

# Eski tek dosya (onefile) kalıntılarını ve onedir çıktısını sıfırla.
if (Test-Path (Join-Path $outDir "python-sidecar")) {
    Remove-Item (Join-Path $outDir "python-sidecar") -Recurse -Force
}
if (Test-Path (Join-Path $outDir "python-sidecar-$triple.exe")) {
    Remove-Item (Join-Path $outDir "python-sidecar-$triple.exe") -Force
}

Write-Host "==> PyInstaller ile derleniyor (target: $triple)..."
# onedir: her çalıştırmada %TEMP%'e ayıklama yapmaz (onefile'ın _MEI*
# çöpü sorunu); Tauri tarafı dizini resource olarak paketler.
$pyiArgs = @(
    "-n", "python-sidecar",
    "--onedir",
    "--clean",
    "--distpath", $outDir,
    "--workpath", (Join-Path $projectRoot "python\build"),
    "--specpath", (Join-Path $projectRoot "python"),
    "--collect-all", "manga_ocr",
    "--collect-all", "unidic_lite",
    "--add-data", (Join-Path $projectRoot "python\fonts;fonts"),
    $script
)
& $python -m PyInstaller @pyiArgs
if (-not $?) { exit 1 }

if (-not (Test-Path (Join-Path $outDir "python-sidecar\python-sidecar.exe"))) {
    Write-Error "HATA: onedir çıktısı beklenen konumda değil: $outDir\python-sidecar\python-sidecar.exe"
}

Write-Host ""
Write-Host "Sidecar hazır: $outDir\python-sidecar\"
Write-Host "Kurulum dizini büyüktür (~ayıklanmış içerik), ama %TEMP% kullanımı SIFIR."

# Eski derleme notu: Tauri önbelleği güncellenmezse eski dosya paketlenebilir
# (tauri#15134). Eski externalBin kopyaları (target\<profil>\python-sidecar.exe)
# artık kullanılmıyor; resource kopyaları (sidecar\ dizini) dokunulmaz.
foreach ($profile in @("debug", "release")) {
    $stale = Join-Path (Join-Path $projectRoot "src-tauri\target\$profile") "python-sidecar.exe"
    if (Test-Path $stale) { Remove-Item $stale -Force }
}
