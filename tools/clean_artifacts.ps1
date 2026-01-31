param(
    [switch]$AllAndroid
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$artifacts = Join-Path $repoRoot 'artifacts'

if (-not (Test-Path $artifacts)) {
    Write-Host "No artifacts/ directory found: $artifacts"
    exit 0
}

function Remove-PathIfExists([string]$p) {
    if (Test-Path $p) {
        Write-Host "Removing: $p"
        Remove-Item -Force -Recurse $p -ErrorAction SilentlyContinue
    }
}

# Always remove disposable run outputs
Remove-PathIfExists (Join-Path $artifacts 'runs')
Remove-PathIfExists (Join-Path $artifacts 'reports')
Remove-PathIfExists (Join-Path $artifacts 'plots')
Remove-PathIfExists (Join-Path $artifacts 'raw')
Remove-PathIfExists (Join-Path $artifacts 'android\policy_probe')
Remove-PathIfExists (Join-Path $artifacts 'android\power_profile')

# Remove top-level perfetto traces if any
Get-ChildItem -Path $artifacts -Filter '*.pftrace' -File -ErrorAction SilentlyContinue |
    ForEach-Object {
        Write-Host "Removing: $($_.FullName)"
        Remove-Item -Force $_.FullName -ErrorAction SilentlyContinue
    }

# Optionally remove all pulled android configs/overlays (kept by default)
if ($AllAndroid) {
    Remove-PathIfExists (Join-Path $artifacts 'android')
}

Write-Host 'Done.'
