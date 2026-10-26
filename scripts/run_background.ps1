param([switch]$Worker)

# 1. Использовать корень проекта и постоянные файлы состояния.
$ErrorActionPreference = 'Stop'
$workspacePath = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$logDirectory = Join-Path $workspacePath 'output/background'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$pidFile = Join-Path $logDirectory 'pid.json'
$statusFile = Join-Path $logDirectory 'status.json'

# 2. Запустить отдельный скрытый процесс, переживающий окончание чата.
if (-not $Worker) {
    if (Test-Path -LiteralPath $pidFile) {
        $saved = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
        if (Get-Process -Id $saved.worker_pid -ErrorAction SilentlyContinue) {
            Write-Output "Background workflow already running: PID $($saved.worker_pid)"
            exit 0
        }
    }
    $launchArguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        ('"{0}"' -f $PSCommandPath), '-Worker'
    )
    $process = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList $launchArguments -WorkingDirectory $workspacePath `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logDirectory 'all.stdout.log') `
        -RedirectStandardError (Join-Path $logDirectory 'all.stderr.log')
    @{ worker_pid = $process.Id; started_utc = [DateTime]::UtcNow.ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding UTF8
    Write-Output "Background workflow started: PID $($process.Id)"
    exit 0
}

# 3. Выполнить один последовательный, возобновляемый полный конвейер.
Set-Location -LiteralPath $workspacePath
$started = [DateTime]::UtcNow.ToString('o')
@{ state = 'running'; worker_pid = $PID; started_utc = $started } |
    ConvertTo-Json | Set-Content -LiteralPath $statusFile -Encoding UTF8
$workflowExitCode = 1
try {
    & uv --cache-dir .cache/uv-analysis run --locked python scripts/run_project.py all
    $workflowExitCode = $LASTEXITCODE
}
catch {
    [Console]::Error.WriteLine($_.Exception.ToString())
}
finally {
    $state = if ($workflowExitCode -eq 0) { 'computations_completed' } else { 'failed' }
    @{
        state = $state
        worker_pid = $PID
        started_utc = $started
        finished_utc = [DateTime]::UtcNow.ToString('o')
        exit_code = $workflowExitCode
        manual_pdf_review_required = $true
        resume_command = 'uv run --locked python scripts/run_project.py all'
    } | ConvertTo-Json | Set-Content -LiteralPath $statusFile -Encoding UTF8
}
exit $workflowExitCode
