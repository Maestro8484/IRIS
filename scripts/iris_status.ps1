<#
.SYNOPSIS
    IRIS_STATUS.json generator. Run at session start or on demand.
    Writes structured machine-readable state to project root.
    Claude Code session_start.py reads this instead of parsing prose.

.USAGE
    From project root:
    .\scripts\iris_status.ps1
    .\scripts\iris_status.ps1 -SkipPi4    # use when Pi4 is offline
#>

param(
    [switch]$SkipPi4
)

$root    = Split-Path -Parent $PSScriptRoot
$outPath = Join-Path $root "IRIS_STATUS.json"

# --- Git state ---
$branch    = git -C $root branch --show-current 2>&1
$commitRaw = git -C $root log --oneline -1 2>&1
$hash      = ($commitRaw -split ' ')[0]
$msg       = ($commitRaw -split ' ', 2)[1]
$dirtyList = git -C $root status --porcelain 2>&1
$dirty     = ($dirtyList | Where-Object { $_ -match '\S' }).Count -gt 0
$untracked = @(git -C $root ls-files --others --exclude-standard 2>&1 |
               Where-Object { $_ -match '\S' })

# --- PlatformIO last build ---
$elfPath   = Join-Path $root ".pio\build\teensy41\firmware.elf"
$lastBuild = if (Test-Path $elfPath) {
    (Get-Item $elfPath).LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
} else { "unknown" }

# --- Pi4 service state via SSH ---
$pi4Status = @{}
if (-not $SkipPi4) {
    try {
        $sshOut = ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no `
                      pi@192.168.1.200 `
                      'systemctl is-active assistant; systemctl is-active iris_web; uptime -p' `
                      2>&1
        $lines = ($sshOut -split "`n") | Where-Object { $_ -match '\S' }
        $pi4Status = [ordered]@{
            reachable = $true
            assistant = if ($lines.Count -ge 1) { $lines[0].Trim() } else { "unknown" }
            iris_web  = if ($lines.Count -ge 2) { $lines[1].Trim() } else { "unknown" }
            uptime    = if ($lines.Count -ge 3) { $lines[2].Trim() } else { "unknown" }
        }
    } catch {
        $pi4Status = [ordered]@{
            reachable = $false
            error     = $_.Exception.Message
        }
    }
} else {
    $pi4Status = [ordered]@{ reachable = "skipped" }
}

# --- Assemble and write ---
$status = [ordered]@{
    generated = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    git       = [ordered]@{
        branch    = "$branch"
        commit    = "$hash"
        message   = "$msg"
        dirty     = $dirty
        untracked = $untracked
    }
    teensy    = [ordered]@{
        last_build = $lastBuild
    }
    pi4       = $pi4Status
}

$json = $status | ConvertTo-Json -Depth 4
Set-Content -Path $outPath -Value $json -Encoding UTF8

Write-Host "[IRIS] IRIS_STATUS.json written."
Write-Host "[IRIS] Branch: $branch | Commit: $hash | Dirty: $dirty"
if ($pi4Status.reachable -eq $true) {
    Write-Host "[IRIS] Pi4: assistant=$($pi4Status.assistant) iris_web=$($pi4Status.iris_web) uptime=$($pi4Status.uptime)"
} elseif ($pi4Status.reachable -eq "skipped") {
    Write-Host "[IRIS] Pi4: check skipped (-SkipPi4 flag)"
} else {
    Write-Host "[IRIS] Pi4: UNREACHABLE — $($pi4Status.error)"
}
