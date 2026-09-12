$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command python.exe -ErrorAction Stop).Source
$pyi = Join-Path (Split-Path $py) "Scripts\pyinstaller.exe"
if (-not (Test-Path $pyi)) {
  & $py -m pip install --disable-pip-version-check pyinstaller
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $pyi)) { throw "PyInstaller installation failed." }
}

# Verify delayed GUI dependencies before packaging. PyInstaller may otherwise
# omit imports that are only loaded while the application is running.
foreach ($dep in @("pystray", "PIL", "webview")) {
  & $py -c "import $dep" 2>$null
  if ($LASTEXITCODE -ne 0) {
    throw "Missing build dependency '$dep'. Install it first: $py -m pip install $dep"
  }
}
Write-Host "[IOTBOX] GUI dependencies OK (pystray, PIL, webview)"
$build = Join-Path $root "build"
if (Test-Path $build) { Remove-Item -LiteralPath $build -Recurse -Force }
New-Item -ItemType Directory -Path $build | Out-Null

& $pyi --noconfirm --clean --onedir --windowed --name gui_app `
  --icon "$root\assets\iotbox-icon.ico" `
  --add-data "$root\web;web" --add-data "$root\certs;certs" `
  --add-data "$root\runtime_config.json;." --collect-submodules pystray --hidden-import pystray._win32 --hidden-import webview `
  "$root\gui_app.py"

& $pyi --noconfirm --clean --onefile --windowed --name customer_display_app `
  --icon "$root\assets\iotbox-icon.ico" `
  "$root\customer_display_app.py"
if (-not (Test-Path (Join-Path $root "dist\customer_display_app.exe"))) {
  throw "Customer display build failed."
}
Copy-Item (Join-Path $root "dist\customer_display_app.exe") (Join-Path $root "dist\gui_app\customer_display_app.exe") -Force

# REDSYS runs as a separate local service.  Bundle its native bridge and
# resources so the GUI's card-terminal tab also works after installation.
& $pyi --noconfirm --clean "$root\redsys_service.spec"
if (-not (Test-Path (Join-Path $root "dist\redsys_service\redsys_service.exe"))) {
  throw "REDSYS service build failed."
}

# Only HTTPS is shipped: the plain-HTTP runtime was removed because starting it
# rewrote the shared runtime_config.json to plain_http and broke the pinned CA.
& $pyi --noconfirm --clean --onedir --console --name run_https `
  --icon "$root\assets\iotbox-icon.ico" `
  --add-data "$root\web;web" --add-data "$root\certs;certs" `
  --collect-all uvicorn --collect-all fastapi --collect-all cryptography `
  "$root\run_https.py"

$isccCandidates = @(
  "C:\Program Files\Inno Setup 6\ISCC.exe",
  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
)
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
  throw "Inno Setup 6 was not found. Install it, then run this script again."
}
& $iscc (Join-Path $root "installer\IOTBOX.iss")
Write-Host "Installer generated in $root\release\IOTBOX-SETUP.exe"
