<#
.SYNOPSIS
    Sets up a virtual environment and runs the full analytics pipeline (Windows / PowerShell).

.DESCRIPTION
    1. Creates .venv in the repository root if it does not exist (requires Python >= 3.11 on PATH).
    2. Installs the pinned dependencies from requirements.txt.
    3. Runs python/run_pipeline.py (generate -> load -> SQL -> quality -> analysis -> dashboard).
    4. Optionally runs the test suite.

.EXAMPLE
    .\run.ps1
    .\run.ps1 -SkipGenerate -Test
    .\run.ps1 -Strict

.NOTES
    If script execution is blocked, run once per session:
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#>
[CmdletBinding()]
param(
    [switch]$SkipGenerate,   # reuse data/transacciones.db instead of regenerating the data
    [switch]$Strict,         # fail if a critical data-quality check exceeds its threshold
    [switch]$Test,           # run pytest after the pipeline
    [switch]$SkipInstall     # do not (re)install requirements
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$venv = Join-Path $root '.venv'
$python = Join-Path $venv 'Scripts\python.exe'

Push-Location $root
try {
    if (-not (Test-Path $python)) {
        Write-Host 'Creating virtual environment in .venv ...' -ForegroundColor Cyan
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($launcher) { & py -3 -m venv $venv } else { & python -m venv $venv }
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment (is Python >= 3.11 installed?).' }
    }

    if (-not $SkipInstall) {
        Write-Host 'Installing requirements ...' -ForegroundColor Cyan
        & $python -m pip install --quiet --upgrade pip
        & $python -m pip install --quiet -r (Join-Path $root 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }
    }

    $pipelineArgs = @()
    if ($SkipGenerate) { $pipelineArgs += '--skip-generate' }
    if ($Strict) { $pipelineArgs += '--strict' }

    Write-Host 'Running pipeline ...' -ForegroundColor Cyan
    & $python (Join-Path $root 'python\run_pipeline.py') @pipelineArgs
    if ($LASTEXITCODE -ne 0) { throw "Pipeline failed (exit code $LASTEXITCODE)." }

    if ($Test) {
        Write-Host 'Running tests ...' -ForegroundColor Cyan
        & $python -m pytest (Join-Path $root 'tests') -q
        if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
    }

    Write-Host "Done. Open reports\hallazgos.md for the findings." -ForegroundColor Green
}
finally {
    Pop-Location
}
