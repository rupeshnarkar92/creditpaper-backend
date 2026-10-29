param(
    [Parameter(Mandatory = $false)]
    [Guid]$Tenant,
    [switch]$AllTenants,
    [int]$Concurrency = 4,
    [int]$PerTenant = 4,
    [ValidateSet('all', 'ingestion', 'generation')]
    [string]$Lane = 'ingestion',
    [double]$IdleSleepSeconds = 2,
    [int]$MaxRestartDelaySeconds = 30
)

$ErrorActionPreference = 'Stop'

if (-not $Tenant -and -not $AllTenants) {
    throw 'Pass -Tenant <uuid> or -AllTenants.'
}
if ($Tenant -and $AllTenants) {
    throw 'Pass only one of -Tenant or -AllTenants.'
}
if ($Concurrency -lt 1 -or $PerTenant -lt 1 -or $IdleSleepSeconds -le 0) {
    throw 'Concurrency, PerTenant and IdleSleepSeconds must be positive.'
}
if ($PerTenant -gt $Concurrency) {
    throw 'PerTenant cannot exceed Concurrency.'
}

# Keep the worker inside the Supabase session budget. The Python entry point
# also enforces this invariant before opening its pool.
if (-not $env:CREDITPAPER_WORKER_DB_POOL_MAX) {
    $env:CREDITPAPER_WORKER_DB_POOL_MAX = '5'
}

$delay = 1
while ($true) {
    $arguments = @('run', 'python', 'scripts/process_jobs.py')
    if ($AllTenants) {
        $arguments += '--all-tenants'
    } else {
        $arguments += @('--tenant', $Tenant.ToString())
    }
    $arguments += @(
        '--concurrency', $Concurrency.ToString(),
        '--per-tenant', $PerTenant.ToString(),
        '--lane', $Lane,
        '--idle-sleep', $IdleSleepSeconds.ToString([Globalization.CultureInfo]::InvariantCulture)
    )

    & uv @arguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        exit 0
    }

    Write-Warning "Processing worker exited with code $exitCode; restarting in $delay second(s)."
    Start-Sleep -Seconds $delay
    $delay = [Math]::Min($delay * 2, $MaxRestartDelaySeconds)
}
