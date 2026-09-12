$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $root 'logs'
$stdoutLog = Join-Path $logDir 'install_deps_stdout.log'
$stderrLog = Join-Path $logDir 'install_deps_stderr.log'

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

$pythonExe = $null
if (Test-Path 'C:\Users\Miko win\AppData\Local\Programs\Python\Python311\python.exe') {
    $pythonExe = 'C:\Users\Miko win\AppData\Local\Programs\Python\Python311\python.exe'
}
if (-not $pythonExe) {
    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd) { $pythonExe = $pyCmd.Source }
}
if (-not $pythonExe) {
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) { $pythonExe = $pythonCmd.Source }
}
if (-not $pythonExe) {
    Add-Content -Path $stderrLog -Value 'Python launcher not found.'
    throw 'Python launcher not found.'
}

Add-Content -Path $stdoutLog -Value "==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===="
Add-Content -Path $stdoutLog -Value "PYTHON=$pythonExe"

& $pythonExe -m pip install --upgrade pip *>> $stdoutLog
& $pythonExe -m pip install fastapi 'uvicorn[standard]' websockets pydantic pillow qrcode pywin32 requests cryptography *>> $stdoutLog

Write-Host 'Dependencies installed successfully.'
