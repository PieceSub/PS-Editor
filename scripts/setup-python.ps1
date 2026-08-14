# PS Editor - Python geliştirme ortamı kurulumu
# python/.venv sanal ortamını oluşturur ve bağımlılıkları kurar.
param()
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvDir = Join-Path $projectRoot "python\.venv"

if (-not (Test-Path $venvDir)) {
    Write-Host "==> python/.venv oluşturuluyor..."
    py -3.11 -m venv $venvDir
    if (-not $?) {
        py -3 -m venv $venvDir
    }
    if (-not $?) {
        Write-Error "Python 3.11 bulunamadı. https://www.python.org/downloads/ adresinden kurun."
    }
} else {
    Write-Host "==> python/.venv zaten var."
}

$python = Join-Path $venvDir "Scripts\python.exe"
Write-Host "==> pip güncelleniyor..."
& $python -m pip install --upgrade pip --quiet
Write-Host "==> Bağımlılıklar kuruluyor (python\requirements.txt)..."
& $python -m pip install -r (Join-Path $projectRoot "python\requirements.txt")

Write-Host ""
Write-Host "Python ortamı hazır: $python"
