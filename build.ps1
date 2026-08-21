<#
.SYNOPSIS
    Build the native engine and package the NVDA add-on.

.DESCRIPTION
    One command from a clean checkout to an installable .nvda-addon:

      1. cargo build --release           (produces server.exe)
      2. cargo test --release            (protobuf encoder vs real capture)
      3. python tests/test_driver.py     (driver logic, no NVDA needed)
      4. python -m compileall             (add-on syntax check)
      5. copy server.exe into the add-on tree
      6. zip the add-on tree to dist/googleTtsRs-<version>.nvda-addon

.PARAMETER SkipTests
    Skip steps 2-4. Useful for a quick repackage; not for a release.

.PARAMETER Clean
    Remove dist/ and the bundled server.exe before building.
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot
$addon = Join-Path $repo "addon"
$engine = Join-Path $repo "engine"
$nativeDir = Join-Path $addon "synthDrivers\googleTtsRs\native"
$dist = Join-Path $repo "dist"

function Write-Step($n, $text) { Write-Host "[$n] $text" -ForegroundColor Cyan }

$manifest = Join-Path $addon "manifest.ini"
$version = (Select-String -Path $manifest -Pattern '^\s*version\s*=\s*(.+)$').Matches[0].Groups[1].Value.Trim()
if (-not $version) { throw "Could not read version from $manifest" }
Write-Host "Google TTS RS - version $version" -ForegroundColor Green

if ($Clean) {
    Write-Step 0 "Cleaning"
    Remove-Item -Recurse -Force $dist -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $nativeDir -ErrorAction SilentlyContinue
}

Write-Step 1 "Building native engine (cargo build --release)"
Push-Location $engine
try {
    cargo build --release --bin server
    if ($LASTEXITCODE -ne 0) { throw "cargo build failed" }

    if (-not $SkipTests) {
        Write-Step 2 "Engine tests (cargo test --release)"
        cargo test --release
        if ($LASTEXITCODE -ne 0) { throw "cargo test failed" }
    }
} finally {
    Pop-Location
}

if (-not $SkipTests) {
    Write-Step 3 "Driver tests (no NVDA required)"
    python (Join-Path $repo "tests\test_driver.py")
    if ($LASTEXITCODE -ne 0) { throw "driver tests failed" }

    Write-Step 4 "Add-on syntax check"
    python -m compileall -q $addon
    if ($LASTEXITCODE -ne 0) { throw "add-on syntax check failed" }
    Get-ChildItem $addon -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
}

Write-Step 5 "Bundling server.exe into the add-on"
New-Item -ItemType Directory -Path $nativeDir -Force | Out-Null
Copy-Item (Join-Path $engine "target\release\server.exe") (Join-Path $nativeDir "server.exe") -Force

$stray = Get-ChildItem $addon -Recurse -File -Filter "*.zvoice"
if ($stray) { throw "Voice data found in the add-on tree: $($stray.FullName -join ', ')" }

Write-Step 6 "Packaging"
New-Item -ItemType Directory -Path $dist -Force | Out-Null
$output = Join-Path $dist "googleTtsRs-$version.nvda-addon"
$tempZip = Join-Path $dist "temp_build.zip"
Remove-Item -Force $output, $tempZip -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $addon "*") -DestinationPath $tempZip -Force
Move-Item -Force $tempZip $output

$size = (Get-Item $output).Length
Write-Host ""
Write-Host "Built $output" -ForegroundColor Green
Write-Host ("      {0:N0} bytes" -f $size)
