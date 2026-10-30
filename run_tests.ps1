# =============================================================================
# PixelPerfect Screenshot API - Automated Test Suite Runner
# =============================================================================
# File:    run_tests.ps1
# Author:  OneTechly
# Created: October 2026
#
# Runs the pytest suite in tests/suite. No server, no internet, no Stripe and
# no real accounts are needed: the database is a throwaway SQLite file and
# Stripe, R2, SMTP and Playwright are replaced by fakes.
#
# USAGE (from the backend folder):
#   .\run_tests.ps1                  # the regular suite (~1.5 min)
#   .\run_tests.ps1 -Browser         # + real-Chromium integration tests
#   .\run_tests.ps1 -Mutation        # + bug-injection check (~15-25 min)
#   .\run_tests.ps1 -Coverage        # + line coverage report
#   .\run_tests.ps1 -Only billing    # only tests whose name matches "billing"
#   .\run_tests.ps1 -Install         # install test dependencies first
#
# READING THE RESULT
#   passed    behaviour is correct
#   xfailed   a KNOWN DEFECT (F-01..F-13, see TESTING.md) is still open
#   XPASS / "[XPASS(strict)]" FAILED
#             a known defect is now FIXED - delete its @defect(...) marker
#   failed    something broke: read the first failure
# =============================================================================

param(
    [switch]$Browser,
    [switch]$Mutation,
    [switch]$Coverage,
    [switch]$Install,
    [string]$Only = ""
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Which Python to use, in this order:
#   1. the virtual environment you have activated (.\venv\Scripts\Activate.ps1)
#   2. ..\venv  (PixelPerfectAPI\venv, next to the backend folder)
#   3. .\venv   (backend\venv)
#   4. whatever "python" is on PATH
# The first one that actually has FastAPI installed wins, so an empty venv
# left over in the repo is skipped.
$candidates = @()
if ($env:VIRTUAL_ENV) { $candidates += (Join-Path $env:VIRTUAL_ENV "Scripts\python.exe") }
$candidates += (Join-Path $PSScriptRoot "..\venv\Scripts\python.exe")
$candidates += (Join-Path $PSScriptRoot "venv\Scripts\python.exe")
$Py = $null
foreach ($c in $candidates) {
    if (Test-Path $c) {
        & $c -c "import fastapi" 2>$null
        if ($LASTEXITCODE -eq 0) { $Py = (Resolve-Path $c).Path; break }
    }
}
if (-not $Py) { $Py = "python" }
Write-Host "Python: $Py" -ForegroundColor DarkGray
& $Py --version

if ($Install) {
    & $Py -m pip install -r requirements.txt pytest-cov
    if ($Browser) { & $Py -m playwright install chromium }
}

& $Py -c "import pytest, pytest_asyncio, httpx" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "pytest is not installed in this Python. Run once:  .\run_tests.ps1 -Install" -ForegroundColor Yellow
    exit 1
}

$pytestArgs = @("-m", "pytest", "-p", "no:cacheprovider")
if ($Browser)  { $pytestArgs += "--browser" }
if ($Coverage) { $pytestArgs += @("--cov=.", "--cov-report=term-missing:skip-covered") }
if ($Only)     { $pytestArgs += @("-k", $Only) }

Write-Host ""
Write-Host "=== Regular test suite ===" -ForegroundColor Cyan
& $Py @pytestArgs
$suiteExit = $LASTEXITCODE

$mutationExit = 0
if ($Mutation) {
    Write-Host ""
    Write-Host "=== Bug-injection check ===" -ForegroundColor Cyan
    & $Py "tests\mutation\run_mutations.py"
    $mutationExit = $LASTEXITCODE
}

Write-Host ""
if ($suiteExit -eq 0 -and $mutationExit -eq 0) {
    Write-Host "ALL GREEN" -ForegroundColor Green
} else {
    Write-Host "SOMETHING FAILED (suite exit $suiteExit, mutation exit $mutationExit)" -ForegroundColor Red
}
exit [Math]::Max($suiteExit, $mutationExit)
