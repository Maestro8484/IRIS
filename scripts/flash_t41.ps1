<#
.SYNOPSIS
    Build and flash Teensy 4.1 (eyes + mouth) over LAN via Pi4 SSH.
    No USB cable move required — Pi4 holds the cable permanently.

.USAGE
    From project root:
        .\scripts\flash_t41.ps1
    Skip rebuild (flash existing hex):
        .\scripts\flash_t41.ps1 -SkipBuild

.NOTES
    Pi4 must be reachable at 192.168.1.200.
    teensy_loader_cli + 49-teensy.rules already installed on Pi4.
    -s flag triggers T41 bootloader via 1200-baud USB reset (no button press needed).
    --mcu=TEENSY41 prevents cross-flashing T40 even if both are connected.
#>

param([switch]$SkipBuild)

$PI4      = "pi@192.168.1.200"
$root     = Split-Path -Parent $PSScriptRoot
$hexLocal = Join-Path $root ".pio\build\eyes\firmware.hex"
$hexPi4   = "/tmp/eyes.hex"

Set-Location $root

# 1 — Build
if (-not $SkipBuild) {
    Write-Host "[T41] Building env:eyes..." -ForegroundColor Cyan
    pio run -e eyes
    if ($LASTEXITCODE -ne 0) { Write-Host "[T41] Build FAILED." -ForegroundColor Red; exit 1 }
}

if (-not (Test-Path $hexLocal)) {
    Write-Host "[T41] Hex not found: $hexLocal  (run without -SkipBuild)" -ForegroundColor Red
    exit 1
}

# 2 — Copy hex to Pi4
Write-Host "[T41] Copying hex to Pi4..." -ForegroundColor Cyan
scp -o StrictHostKeyChecking=no $hexLocal "${PI4}:${hexPi4}"
if ($LASTEXITCODE -ne 0) { Write-Host "[T41] scp FAILED." -ForegroundColor Red; exit 1 }

# 3 — Flash via Pi4
# Bootloader is triggered explicitly on /dev/ttyIRIS_EYES (T41) at baud 134.
# -s is NOT used — it picks the first Teensy found and can cross-flash T40.
Write-Host "[T41] Flashing via Pi4 (explicit /dev/ttyIRIS_EYES reset → upload → restart)..." -ForegroundColor Cyan
ssh -o StrictHostKeyChecking=no $PI4 @"
sudo systemctl stop assistant && \
sleep 1 && \
printf 'import serial,time\ns=serial.Serial(\042/dev/ttyIRIS_EYES\042,134)\ntime.sleep(0.5)\ns.close()\n' | python3 && \
sleep 2 && \
sudo teensy_loader_cli --mcu=TEENSY41 -w -v $hexPi4 && \
sudo systemctl start assistant && \
echo '[T41] Flash complete. Verifying...' && \
sleep 4 && \
journalctl -u assistant -n 10 --no-pager | grep -E 'VER|Person|Ready|ERROR' || true
"@

if ($LASTEXITCODE -eq 0) {
    Write-Host "[T41] Done. Check above for [VER] and Person Sensor lines." -ForegroundColor Green
} else {
    Write-Host "[T41] Flash may have failed -- check Pi4 journal." -ForegroundColor Yellow
}
