# Runs on the Windows PC (nightly scheduled task): triggers a fresh backup on the Pi, then copies the
# newest one here, off the Pi. Keeps the newest $Keep copies. Folder defaults to ~\polymarket-backups.
#   Register once:  powershell -File tools\pull_backup.ps1 -Register
param([string]$PiHost = "sujayketh@raspberrypi", [string]$Dest = "$HOME\polymarket-backups",
      [int]$Keep = 14, [switch]$Register)
if ($Register) {
    $act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    $trg = New-ScheduledTaskTrigger -Daily -At 4:00am
    $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries
    Register-ScheduledTask -TaskName "PolymarketPiBackup" -Action $act -Trigger $trg -Settings $set -Force | Out-Null
    Write-Host "Registered scheduled task PolymarketPiBackup (daily 04:00, runs when the PC wakes if it missed it)."
    return
}
New-Item -ItemType Directory -Force $Dest | Out-Null
ssh $PiHost "~/polymarket-paper/tools/backup_db.sh"
if ($LASTEXITCODE -ne 0) { throw "backup on the Pi failed" }
$latest = (ssh $PiHost "ls -1t ~/polymarket-paper/data/backups/paper_us-*.db.gz | head -1").Trim()
scp "${PiHost}:$latest" "$Dest\"
if ($LASTEXITCODE -ne 0) { throw "scp failed" }
Get-ChildItem "$Dest\paper_us-*.db.gz" | Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep | Remove-Item
Write-Host "Backup copied to $Dest"
