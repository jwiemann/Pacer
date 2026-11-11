# One-time move of the Pacer code from C:\Users\<user>\.paperclip to X:\Pacer. Run ONCE in an ELEVATED PowerShell:
#   powershell -ExecutionPolicy Bypass -File X:\Pacer\packaging\migrate-from-paperclip-dir.ps1
# - stops the running (old) Pacer tray/dashboard/keepalive
# - re-points the 'Paperclip WSL Keepalive' task (created elevated, so only an elevated shell may change it) to X:\Pacer
# - removes the dead 'Paperclip Tray' task (it pointed at a tray.pyw that no longer exists); the tray starts through
#   the HKCU Run key instead (the same entry the Pacer's own "start with Windows" switch writes)
# - deletes the old code from the data dir (settings, logs and state stay; the WSL-side scripts are re-copied by the Pacer)
# - starts the Pacer from X:\Pacer
$ErrorActionPreference = 'Stop'
$code = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)   # X:\Pacer
$data = Join-Path $env:USERPROFILE '.paperclip'
$pyw  = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\pythonw.exe'
if (-not (Test-Path $pyw)) { throw "pythonw.exe not found at $pyw" }

foreach ($p in Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'") {
    if ($p.CommandLine -match '\.paperclip[\\/]|[\\/]Pacer[\\/](pacer|launch|wsl-keepalive)') {
        Stop-Process -Id $p.ProcessId -Force
    }
}

$keep = New-ScheduledTaskAction -Execute $pyw -Argument "`"$code\wsl-keepalive.pyw`"" -WorkingDirectory $code
Set-ScheduledTask -TaskName 'Paperclip WSL Keepalive' -Action $keep | Out-Null
Unregister-ScheduledTask -TaskName 'Paperclip Tray' -Confirm:$false -ErrorAction SilentlyContinue

Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'Pacer' -Value "$pyw `"$code\pacer.py`" tray"

$old = 'pacer.py','pacer_config.py','paperclip_common.py','governor.py','watchdog.py','updater.py','tray.py','tray_icon.py',
       'dashboard.py','live_chart.py','live_tokens.py','copilot_monitor.py','autostart.py','i18n.py','launch.pyw',
       'start-paperclip-wsl.pyw','wsl-keepalive.pyw','claude-governor.cjs','pacer-config.cjs','watchdog.cjs',
       'update-paperclip.cjs','refresh-claude-auth.cjs','paperclip.ico'
foreach ($f in $old) { Remove-Item (Join-Path $data $f) -Force -ErrorAction SilentlyContinue }
foreach ($d in '__pycache__','patches','packaging','%TEMP%pf') { Remove-Item (Join-Path $data $d) -Recurse -Force -ErrorAction SilentlyContinue }

Start-Process -FilePath $pyw -ArgumentList "`"$code\wsl-keepalive.pyw`"" -WorkingDirectory $code
Start-Process -FilePath $pyw -ArgumentList "`"$code\pacer.py`" launch" -WorkingDirectory $code
Write-Host "Pacer now runs from $code. Data stays in $data."
