<#
.SYNOPSIS
    Run the frozen qlib/LightGBM candidate through the direct prediction entry point.

.DESCRIPTION
    Executes candidate 0b61155b01509d06732e -- alpha360, horizon 20, 252-day rolling
    train window, l2 early stopping -- across one or more seeds, and compares the
    resulting Rank IC against what the 2026-09-05 handover recorded.

    The parameters come from configs/experiments/selected_parameters-direct-run-*.json,
    exported from the search's own trial_plan.json, and the figures to compare
    against come from the matching *.expected.json beside it. Neither is retyped
    here, so there is no second place for either to drift.

    THIS IS NOT A BLIND TEST. The 44 evaluation dates (2026-02-17..2026-04-21) took
    part in all 119 runs that selected this candidate: 24 structure trials, 80
    parameter trials and 15 replication trials. The numbers it prints are development
    figures and do not represent tradable returns. See the handover README.

.PARAMETER Seeds
    Seeds to run. Defaults to the three the handover replicated, because a single
    seed says little: the parameter search champion fell from 0.156124 to 0.040591
    on a different seed, which was that round's most important methodological result.

.PARAMETER OutDir
    Host directory for the JSON results. Written from PowerShell rather than
    redirected inside the container, because /app/var is a named volume and a
    redirect there lands somewhere the repository cannot see.

.PARAMETER Local
    Run in the current shell instead of `docker compose exec`. Requires a reachable
    PostgreSQL, and a provider root that exists on this machine -- the candidate
    config names the container's, so pass -ProviderRoot as well.

.PARAMETER ProviderRoot
    Overrides the provider root the candidate config names. Needed with -Local,
    where the container path in the config does not exist.

.PARAMETER SkipProvider
    Skip the build-provider step. Building is already idempotent; this only saves
    the round trip when the provider is known to exist.

.EXAMPLE
    .\scripts\Invoke-FrozenCandidate.ps1

.EXAMPLE
    .\scripts\Invoke-FrozenCandidate.ps1 -Seeds 20260829 -TestRange '2026-02-17:2026-05-26'
#>
[CmdletBinding()]
param(
    [int[]] $Seeds = @(20260829, 20260830, 20260831),
    [string] $Config = 'configs/experiments/selected_parameters-direct-run-0b61155b01509d06732e.json',
    [string] $OutDir = 'var/direct-runs',
    [string] $TestRange,
    [string] $ProviderRoot,
    [switch] $Local,
    [switch] $SkipProvider
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($Local -and -not $ProviderRoot) {
    throw "-Local needs -ProviderRoot: the candidate config names the container's path."
}

# Repository root: this script lives in backend/scripts.
$backend = Split-Path -Parent $PSScriptRoot
$repo = Split-Path -Parent $backend

$configPath = Join-Path $backend $Config
if (-not (Test-Path $configPath)) {
    throw "Candidate config not found: $configPath"
}

# What the handover recorded, read rather than restated. Absent file means the
# table simply has no column to compare against.
$expectedPath = [System.IO.Path]::ChangeExtension($configPath, $null) + 'expected.json'
$expected = @{}
if (Test-Path $expectedPath) {
    $recorded = (Get-Content -LiteralPath $expectedPath -Raw | ConvertFrom-Json).rank_ic_mean_by_seed
    foreach ($property in $recorded.PSObject.Properties) {
        $expected[[int]$property.Name] = [double]$property.Value
    }
}

function Invoke-Direct {
    param([string[]] $DirectArgs)

    if ($Local) {
        Push-Location $backend
        try {
            $output = & uv run python scripts/qlib_lightgbm_direct.py @DirectArgs
        } finally {
            Pop-Location
        }
    } else {
        Push-Location $repo
        try {
            $output = & docker compose exec -T backend uv run python scripts/qlib_lightgbm_direct.py @DirectArgs
        } finally {
            Pop-Location
        }
    }
    # The command prints one JSON document to stdout; diagnostics and the error
    # payload go to stderr, so a non-zero exit means $output is not a result.
    return [PSCustomObject]@{ ExitCode = $LASTEXITCODE; Output = ($output -join "`n") }
}

function Save-Utf8 {
    param([string] $Path, [string] $Text)

    # [System.IO.File] rather than Set-Content/Out-File: both of those write a BOM
    # for their own "utf8" in Windows PowerShell 5.1.
    $full = [System.IO.Path]::GetFullPath($Path)
    [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($full)) | Out-Null
    [System.IO.File]::WriteAllText($full, $Text, (New-Object System.Text.UTF8Encoding($false)))
    return $full
}

$candidate = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
Write-Host "Candidate : $($candidate.feature_set), horizon $($candidate.label_horizon), window $($candidate.train_window), stop_metric $($candidate.stop_metric)"
Write-Host "Snapshot  : $($candidate.snapshot_id)"
Write-Host "Segments  : train $($candidate.train) | valid $($candidate.valid) | test $($candidate.test)"
Write-Host "Seeds     : $($Seeds -join ', ')"
Write-Host ''

if (-not $SkipProvider) {
    Write-Host '=== build-provider ===' -ForegroundColor Cyan
    $root = if ($ProviderRoot) { $ProviderRoot } else { $candidate.provider_root }
    $built = Invoke-Direct @(
        'build-provider', '--snapshot-id', $candidate.snapshot_id, '--provider-root', $root
    )
    if ($built.ExitCode -ne 0) {
        throw "build-provider failed with exit code $($built.ExitCode); see the error above."
    }
    $manifest = $built.Output | ConvertFrom-Json
    Write-Host "  path=$($manifest.provider_path) created=$($manifest.created)"
    Write-Host ''
}

$results = @()
foreach ($seed in $Seeds) {
    Write-Host "=== predict, seed $seed ===" -ForegroundColor Cyan
    $directArgs = @('predict', '--config', $Config, '--seed', "$seed")
    if ($TestRange) { $directArgs += @('--test', $TestRange) }
    if ($ProviderRoot) { $directArgs += @('--provider-root', $ProviderRoot) }

    $started = Get-Date
    $run = Invoke-Direct $directArgs
    $elapsed = (Get-Date) - $started

    if ($run.ExitCode -ne 0) {
        # 2 = configuration/snapshot/provider/data, 1 = training. Keep going: one
        # seed failing is itself a finding, and the other seeds still inform it.
        Write-Warning "seed $seed failed with exit code $($run.ExitCode); skipping"
        $results += [PSCustomObject]@{ Seed = $seed; RankIC = $null; Folds = $null; ICDates = $null; Expected = $expected[$seed]; Delta = $null; Minutes = [math]::Round($elapsed.TotalMinutes, 1) }
        continue
    }

    $stem = [System.IO.Path]::GetFileNameWithoutExtension($configPath) -replace '^.*-direct-run-', ''
    $path = Save-Utf8 (Join-Path $backend "$OutDir/direct-$stem-seed$seed.json") $run.Output
    $payload = $run.Output | ConvertFrom-Json
    $rankIc = $payload.summary.test_rank_ic_mean
    $want = $expected[$seed]
    Write-Host "  saved $path"

    $results += [PSCustomObject]@{
        Seed     = $seed
        RankIC   = $rankIc
        Folds    = $payload.summary.fold_count
        ICDates  = $payload.summary.test_ic_dates
        Expected = $want
        Delta    = $(if ($null -ne $want -and $null -ne $rankIc) { [math]::Round($rankIc - $want, 6) } else { $null })
        Minutes  = [math]::Round($elapsed.TotalMinutes, 1)
    }
}

Write-Host ''
Write-Host '=== results ===' -ForegroundColor Cyan
$results | Format-Table -AutoSize

$measured = $results | Where-Object { $null -ne $_.RankIC }
if ($measured) {
    $mean = ($measured | Measure-Object -Property RankIC -Average).Average
    if ($expected.Count) {
        $want = ($expected.Values | Measure-Object -Average).Average
        Write-Host ("seed mean Rank IC: {0:N6}  (recorded {1:N6})" -f $mean, $want)
    } else {
        Write-Host ("seed mean Rank IC: {0:N6}" -f $mean)
    }
}

$drifted = $results | Where-Object { $null -ne $_.Delta -and [math]::Abs($_.Delta) -gt 1e-6 }
if ($drifted) {
    Write-Warning 'Rank IC differs from the handover. Both entry points now share one training seam, so a difference is worth investigating rather than averaging away.'
}

Write-Host ''
Write-Warning 'Development figures only. These 44 evaluation dates took part in selecting this candidate across 119 runs; this is not a blind test and these numbers are not tradable returns.'

# A run where nothing succeeded must not report success, the way
# Invoke-DbMigration.ps1 propagates what it ran.
if (-not $measured) {
    Write-Error 'No seed produced a result.'
    exit 1
}
exit 0
