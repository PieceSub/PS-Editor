# PS Editor - Python sidecar'ı PyInstaller ile derler.
# Çıktı: src-tauri/binaries/python-sidecar-<target-triple>.exe
# Tauri'nin externalBin yapılandırması bu isimlendirmeyi bekler.
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

if ($Clean -and (Test-Path (Join-Path $outDir "python-sidecar-$triple.exe"))) {
    Remove-Item (Join-Path $outDir "python-sidecar-$triple.exe") -Force
}

Write-Host "==> PyInstaller ile derleniyor (target: $triple)..."
$pyiArgs = @(
    "-n", "python-sidecar",
    "--onefile",
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

$final = Join-Path $outDir "python-sidecar-$triple.exe"
if (Test-Path $final) {
    Remove-Item $final -Force
}
Rename-Item (Join-Path $outDir "python-sidecar.exe") -NewName "python-sidecar-$triple.exe"

Write-Host ""
Write-Host "Sidecar hazır: $final"
Write-Host "Bilinen sorun: src-tauri/target içindeki önbellek güncellenmezse eski binary paketlenebilir."
Write-Host "Emin olmak için: npm run tauri build öncesinde src-tauri\target\release\python-sidecar.exe dosyasını silin."
