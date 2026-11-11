# Elevated WSL repair, run by scheduled task 'Paperclip WSL Repair' (highest privileges, S4U, no window).
# Triggered by watchdog.cjs when `wsl --shutdown` hangs / WSL stays unresponsive. Writes repair-wsl.done when finished.
$ErrorActionPreference = 'Continue'
$dir = Join-Path $env:USERPROFILE '.paperclip'
$log = Join-Path $dir 'watchdog.log'
function L($m) { Add-Content -Path $log -Value ("{0} repair-wsl: {1}" -f (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ'), $m) }
Remove-Item (Join-Path $dir 'repair-wsl.done') -ErrorAction SilentlyContinue
L 'start'
try {
    foreach ($n in 'wsl', 'wslhost', 'wslrelay', 'vmwp', 'wslservice') {
        Get-Process -Name $n -ErrorAction SilentlyContinue | ForEach-Object {
            try { Stop-Process -Id $_.Id -Force -ErrorAction Stop; L "killed $n $($_.Id)" } catch { L "cannot kill $n $($_.Id): $($_.Exception.Message)" }
        }
    }
    $svc = Get-Service WslService -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -ne 'Running') { Start-Service WslService -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 5
    $svc = Get-Service WslService -ErrorAction SilentlyContinue
    L "WslService: $($svc.Status)"
} catch { L "error: $_" }
Set-Content -Path (Join-Path $dir 'repair-wsl.done') -Value (Get-Date).ToString('o')
L 'done'
