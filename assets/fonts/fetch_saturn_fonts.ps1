# Fetches the Saturn display typeface (Unbounded) into assets/fonts as static TTFs.
#
#   powershell -ExecutionPolicy Bypass -File assets\fonts\fetch_saturn_fonts.ps1
#
# Saturn is local-first, so the face ships with the app instead of being pulled
# from fonts.googleapis.com at runtime. Run this once, then restart Saturn:
# core/theme_fonts.py registers whatever is in this folder at startup. If you
# would rather not run a script, drop the .ttf files in here yourself - the
# names below are what it looks for.
#
# Body and mono text use Segoe UI and Consolas, which ship with Windows and
# match the design, so only the display face is fetched here. Nothing else in
# the app touches the network for fonts, and if this folder is empty the shell
# keeps using Segoe UI for headings.

$ErrorActionPreference = "Stop"
$dest = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = "https://raw.githubusercontent.com/google/fonts/main/ofl/unbounded"

# Unbounded ships as a single variable font that carries every weight, so one
# file is enough - no need to fetch per-style static instances (they do not
# exist upstream). The brackets in the SOURCE filename must be URL-encoded
# (%5B %5D); we save to a bracket-free local name because PowerShell treats
# [] in an output path as a wildcard. core/theme_fonts.py registers by the
# font's reported family, not its filename, so the local name does not matter.
$targets = @(
    @{ out = "Unbounded.ttf"; urls = @(
        "$repo/Unbounded%5Bwght%5D.ttf"
    ) }
)

$got = 0
$failed = @()

foreach ($t in $targets) {
    $out = Join-Path $dest $t.out
    if (Test-Path $out) { Write-Host "have  $($t.out)"; $got++; continue }
    $done = $false
    foreach ($u in $t.urls) {
        try {
            Invoke-WebRequest -Uri $u -OutFile $out -UseBasicParsing -TimeoutSec 40
            if ((Get-Item $out).Length -gt 20kb) {
                Write-Host "got   $($t.out)  <- $u"
                $got++; $done = $true; break
            }
            Remove-Item $out -Force
        } catch { }
    }
    if (-not $done) { $failed += $t.out }
}

Write-Host ""
Write-Host "Saturn fonts: $got file(s) in assets\fonts"
if ($failed.Count) {
    Write-Host "MISSING: $($failed -join ', ')" -ForegroundColor Yellow
    Write-Host "Saturn will fall back to Segoe UI for headings until Unbounded lands." -ForegroundColor Yellow
} else {
    Write-Host "Restart Saturn and the shell picks up Unbounded for the display face."
}
