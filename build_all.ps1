Write-Host "1. Building Uninstaller (Uninstall.exe)..." -ForegroundColor Cyan
.\.venv\Scripts\pyinstaller.exe installer\Uninstall.spec --noconfirm

if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to build uninstaller!" -ForegroundColor Red
    exit 1
}

Write-Host "2. Building Saturn Main Application..." -ForegroundColor Cyan
.\.venv\Scripts\pyinstaller.exe installer\Saturn.spec --noconfirm

if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to build main application!" -ForegroundColor Red
    exit 1
}

Write-Host "Bundling uninstaller into application bundle..." -ForegroundColor Cyan
if (Test-Path "dist\Uninstall.exe") {
    Copy-Item "dist\Uninstall.exe" -Destination "dist\Saturn\Uninstall.exe" -Force
}
if (Test-Path "installer\uninstall.cmd") {
    Copy-Item "installer\uninstall.cmd" -Destination "dist\Saturn\uninstall.cmd" -Force
}

Write-Host "3. Building Setup Wizard (Saturn_Setup.exe)..." -ForegroundColor Cyan
.\.venv\Scripts\pyinstaller.exe installer\Saturn_Setup.spec --noconfirm

if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to build setup wizard!" -ForegroundColor Red
    exit 1
}

Write-Host "============================================================" -ForegroundColor Green
Write-Host " Build complete! Setup is located at: dist\Saturn_Setup.exe" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
