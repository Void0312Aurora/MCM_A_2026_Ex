param(
	[switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-AdbDeviceTable {
	$out = & adb devices
	if ($LASTEXITCODE -ne 0) {
		throw "adb devices failed (exit=$LASTEXITCODE)"
	}
	$lines = $out | Select-Object -Skip 1 | ForEach-Object { $_.TrimEnd() } | Where-Object { $_.Trim() -ne "" }
	$devices = @()
	foreach ($ln in $lines) {
		$ln = $ln.Trim()
		$parts = $ln -split "\s+"
		if ($parts.Count -lt 2) { continue }
		$state = $parts[-1]
		$serial = $ln.Substring(0, $ln.Length - $state.Length).Trim()
		if ($serial -eq "") { continue }
		$devices += [pscustomobject]@{ Serial = $serial; State = $state }
	}
	return $devices
}

function Ensure-AdbHealthy {
	$devices = @(Get-AdbDeviceTable)
	$offline = @($devices | Where-Object { $_.State -eq "offline" })
	if ($offline.Count -gt 0) {
		foreach ($d in $offline) {
			Write-Host "[run_plan_cpu_only] disconnect offline: $($d.Serial)"
			& adb disconnect $d.Serial | Out-Null
		}
		& adb kill-server | Out-Null
		& adb start-server | Out-Null
		Start-Sleep -Seconds 2
	}

	$devices = @(Get-AdbDeviceTable)
	$ok = @($devices | Where-Object { $_.State -eq "device" })
	if ($ok.Count -le 0) {
		throw "No adb devices in 'device' state. Run 'adb devices -l' and fix pairing/authorization."
	}

	# Prefer ip:port serial if present; otherwise use the first device.
	$ip = $ok | Where-Object { $_.Serial -match "^\d{1,3}(?:\.\d{1,3}){3}:\d+$" } | Select-Object -First 1
	if ($null -ne $ip) {
		return $ip.Serial
	}
	return $ok[0].Serial
}

function Get-DeviceBrightness {
	param(
		[Parameter(Mandatory=$true)][string]$Serial
	)

	# NOTE: best-effort. Values are typically 0-255. Mode: 0=manual, 1=auto.
	$b = (& adb -s "$Serial" shell settings get system screen_brightness) 2>$null
	$m = (& adb -s "$Serial" shell settings get system screen_brightness_mode) 2>$null
	$t = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
	return [pscustomobject]@{
		Timestamp = $t
		Brightness = ($b | Out-String).Trim()
		Mode = ($m | Out-String).Trim()
	}
}

$PY = "D:/workshop/MP_power/.venv/Scripts/python.exe"
$SERIAL = Ensure-AdbHealthy
Write-Host "[run_plan_cpu_only] using serial: $SERIAL"

$BR = Get-DeviceBrightness -Serial $SERIAL
Write-Host "[run_plan_cpu_only] brightness_mode=$($BR.Mode) screen_brightness=$($BR.Brightness) (settings/system)"

# Persist a machine-readable note for downstream calculations.
$NOTE_PATH = "artifacts/run_plan_cpu_only_brightness_last.txt"
"timestamp=$($BR.Timestamp)`nserial=$SERIAL`nbrightness_mode=$($BR.Mode)`nscreen_brightness=$($BR.Brightness)" | Out-File -FilePath $NOTE_PATH -Encoding utf8

# Also append a comment block into this script file for audit trail.
try {
	$self = $MyInvocation.MyCommand.Path
	$block = @(
		"",
		"# ---- runtime note (auto-appended) ----",
		"# timestamp=$($BR.Timestamp)",
		"# serial=$SERIAL",
		"# brightness_mode=$($BR.Mode)  (0=manual,1=auto)",
		"# screen_brightness=$($BR.Brightness)  (typically 0-255)",
		"# source: adb shell settings get system screen_brightness(_mode)",
		"# -------------------------------------"
	) -join "`n"
	Add-Content -Path $self -Value $block -Encoding utf8
} catch {
	Write-Host "[run_plan_cpu_only] WARN: failed to append runtime note into script: $($_.Exception.Message)"
}

if ($DryRun) {
	Write-Host "[run_plan_cpu_only] DryRun: OK"
	exit 0
}

# CPU负载梯度：threads=1（若无线调试导致CPU load不稳定，至少不阻塞后续场景）
# plan_id=S3-GRAD
& $PY scripts/pipeline_run.py --serial "$SERIAL" --scenario S3_load_t1 --duration 540 --interval 2 --thermal --display --qc --cpu-load-threads 1 --cpu-load-best-effort

# CPU负载梯度：threads=2（同上）
# plan_id=S3-GRAD
& $PY scripts/pipeline_run.py --serial "$SERIAL" --scenario S3_load_t2 --duration 540 --interval 2 --thermal --display --qc --cpu-load-threads 2 --cpu-load-best-effort

# CPU负载梯度：threads=4（已有，可重复；同上）
# plan_id=S3-GRAD
& $PY scripts/pipeline_run.py --serial "$SERIAL" --scenario S3_load_t4 --duration 540 --interval 2 --thermal --display --qc --cpu-load-threads 4 --cpu-load-best-effort

# CPU负载梯度：threads=6（同上）
# plan_id=S3-GRAD
& $PY scripts/pipeline_run.py --serial "$SERIAL" --scenario S3_load_t6 --duration 540 --interval 2 --thermal --display --qc --cpu-load-threads 6 --cpu-load-best-effort

# CPU负载梯度：threads=8（同上）
# plan_id=S3-GRAD
& $PY scripts/pipeline_run.py --serial "$SERIAL" --scenario S3_load_t8 --duration 540 --interval 2 --thermal --display --qc --cpu-load-threads 8 --cpu-load-best-effort

# ---- runtime note (auto-appended) ----
# timestamp=2026-02-02 21:49:04
# serial=adb-4LQWV47DYXT4GQSW-j7FoVL (2)._adb-tls-connect._tcp
# brightness_mode=0  (0=manual,1=auto)
# screen_brightness=55  (typically 0-255)
# source: adb shell settings get system screen_brightness(_mode)
# -------------------------------------

# ---- runtime note (auto-appended) ----
# timestamp=2026-02-02 21:49:33
# serial=adb-4LQWV47DYXT4GQSW-j7FoVL (2)._adb-tls-connect._tcp
# brightness_mode=0  (0=manual,1=auto)
# screen_brightness=55  (typically 0-255)
# source: adb shell settings get system screen_brightness(_mode)
# -------------------------------------
