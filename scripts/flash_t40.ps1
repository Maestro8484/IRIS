<#
.SYNOPSIS
    Build and flash Teensy 4.0 (servo + gesture) over LAN via Pi4 SSH.
    No USB cable move required — Pi4 holds the cable permanently.

.USAGE
    From project root:
        .\scripts\flash_t40.ps1
    Skip rebuild (flash existing hex):
        .\scripts\flash_t40.ps1 -SkipBuild

.NOTES
    Pi4 must be reachable at 192.168.1.200.
    teensy_loader_cli + 49-teensy.rules already installed on Pi4.
    -s flag triggers T40 bootloader via 1200-baud USB reset (no button press needed).
    --mcu=TEENSY40 prevents cross-flashing T41 even if both are connected.
#>

param([switch]$SkipBuild)

$PI4      = "pi@192.168.1.200"
$root     = Split-Path -Parent $PSScriptRoot
$t40dir   = Join-Path $root "servo_teensy40\teensy40_base_mount"
$hexLocal = Join-Path $t40dir ".pio\build\teensy40\firmware.hex"
$hexPi4   = "/tmp/servo.hex"

# 1 — Build
if (-not $SkipBuild) {
    Write-Host "[T40] Building env:teensy40..." -ForegroundColor Cyan
    Set-Location $t40dir
    pio run
    if ($LASTEXITCODE -ne 0) { Write-Host "[T40] Build FAILED." -ForegroundColor Red; exit 1 }
    Set-Location $root
}

if (-not (Test-Path $hexLocal)) {
    Write-Host "[T40] Hex not found: $hexLocal  (run without -SkipBuild)" -ForegroundColor Red
    exit 1
}

# 2 — Copy hex to Pi4
Write-Host "[T40] Copying hex to Pi4..." -ForegroundColor Cyan
scp -o StrictHostKeyChecking=no $hexLocal "${PI4}:${hexPi4}"
if ($LASTEXITCODE -ne 0) { Write-Host "[T40] scp FAILED." -ForegroundColor Red; exit 1 }

# 3 — Flash via Pi4
# Bootloader is triggered explicitly on /dev/ttyIRIS_SERVO (T40) at baud 134.
# -s is NOT used — it picks the first Teensy found and cross-flashes T41.
Write-Host "[T40] Flashing via Pi4 (explicit /dev/ttyIRIS_SERVO reset → upload → restart)..." -ForegroundColor Cyan
ssh -o StrictHostKeyChecking=no $PI4 @"
sudo systemctl stop assistant && \
sleep 1 && \
printf 'import serial,time\ns=serial.Serial(\042/dev/ttyIRIS_SERVO\042,134)\ntime.sleep(0.5)\ns.close()\n' | python3 && \
sleep 2 && \
sudo teensy_loader_cli --mcu=TEENSY40 -w -v $hexPi4 && \
sudo systemctl start assistant && \
echo '[T40] Flash complete. Verifying...' && \
sleep 5 && \
journalctl -u assistant -n 15 --no-pager | grep -E 'PAJ7620|Person|SERVO|Ready|ERROR' || true
"@

if ($LASTEXITCODE -eq 0) {
    Write-Host "[T40] Done. Check above for PAJ7620 init and Person Sensor lines." -ForegroundColor Green
} else {
    Write-Host "[T40] Flash may have failed -- check Pi4 journal." -ForegroundColor Yellow
}
