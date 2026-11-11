# Builds the Pacer app: dist\Pacer\Pacer.exe (folder) and, when Inno Setup is installed, dist\Pacer-Setup.exe.
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 [-Python C:\path\to\python.exe]
# Needs Python 3.11+ with tkinter. Build tools (PyInstaller, Pillow, pystray) go into packaging\.venv, nothing global.
param([string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe")
$ErrorActionPreference = "Continue"  # native tools log to stderr; failures are caught through $LASTEXITCODE
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

if (-not (Test-Path "$here\.venv\Scripts\python.exe")) {
    & $Python -m venv "$here\.venv"
}
$py = "$here\.venv\Scripts\python.exe"
& $py -m pip install --quiet --upgrade pip pyinstaller pillow pystray pg8000
if ($LASTEXITCODE) { throw "pip failed" }

& $py "$here\make_icon.py"
if ($LASTEXITCODE) { throw "icon failed" }

& $py -m PyInstaller --noconfirm --clean --distpath "$here\dist" --workpath "$here\build" "$here\pacer.spec"
if ($LASTEXITCODE) { throw "PyInstaller failed" }
Write-Host "built: $here\dist\Pacer\Pacer.exe"

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    & $iscc "$here\installer.iss"
    if ($LASTEXITCODE) { throw "Inno Setup failed" }
    Write-Host "installer: $here\dist\Pacer-Setup.exe"
} else {
    Write-Host "Inno Setup not found: installer skipped (winget install JRSoftware.InnoSetup)"
}
