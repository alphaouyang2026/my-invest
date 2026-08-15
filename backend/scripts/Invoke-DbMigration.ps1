<#
.SYNOPSIS
Applies Alembic migrations from Windows PowerShell to the project's Docker
PostgreSQL service exposed on localhost.

.EXAMPLE
.\scripts\Invoke-DbMigration.ps1

.EXAMPLE
.\scripts\Invoke-DbMigration.ps1 -Test

.EXAMPLE
.\scripts\Invoke-DbMigration.ps1 -RevisionMessage 'add jquants market data sync'
#>
[CmdletBinding()]
param(
    [switch]$Test,
    [string]$RevisionMessage
)

$ErrorActionPreference = 'Stop'

$backendRoot = Split-Path -Parent $PSScriptRoot
$repoRoot = Split-Path -Parent $backendRoot
$envFile = Join-Path $repoRoot '.env'
$alembic = Join-Path $backendRoot '.venv\Scripts\alembic.exe'

if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Missing $envFile. Copy .env.example to .env and configure PostgreSQL first."
}
if (-not (Test-Path -LiteralPath $alembic)) {
    throw "Missing $alembic. Install the backend dependencies first."
}

# Read only the database credentials needed for the connection. Values supplied
# by this script override POSTGRES_HOST/PORT because `db` is Docker-internal
# and cannot be resolved from Windows.
$values = @{}
foreach ($line in Get-Content -LiteralPath $envFile) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
        $values[$matches[1]] = $matches[2].Trim('"').Trim("'")
    }
}

foreach ($name in @('POSTGRES_USER', 'POSTGRES_PASSWORD', 'POSTGRES_DB')) {
    if (-not $values.ContainsKey($name) -or [string]::IsNullOrWhiteSpace($values[$name])) {
        throw "Missing $name in $envFile."
    }
}

$env:POSTGRES_USER = $values['POSTGRES_USER']
$env:POSTGRES_PASSWORD = $values['POSTGRES_PASSWORD']
$env:POSTGRES_HOST = '127.0.0.1'

if ($Test) {
    if (-not $values.ContainsKey('TEST_POSTGRES_DB') -or [string]::IsNullOrWhiteSpace($values['TEST_POSTGRES_DB'])) {
        throw "Missing TEST_POSTGRES_DB in $envFile."
    }
    $env:POSTGRES_DB = $values['TEST_POSTGRES_DB']
    $env:POSTGRES_PORT = '5433'
} else {
    $env:POSTGRES_DB = $values['POSTGRES_DB']
    $env:POSTGRES_PORT = '5432'
}

Write-Host "Migrating postgresql+psycopg://$($env:POSTGRES_USER)@${env:POSTGRES_HOST}:$($env:POSTGRES_PORT)/$($env:POSTGRES_DB)"

Push-Location $backendRoot
try {
    & $alembic upgrade head
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    if ($RevisionMessage) {
        & $alembic revision --autogenerate -m $RevisionMessage
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
} finally {
    Pop-Location
}
