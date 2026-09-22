$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $root 'backend'
$frontendDir = Join-Path $root 'frontend'
$runtimeEnvFile = Join-Path $backendDir '.env.runtime'
$providerEnvFile = Join-Path $backendDir '.env'
$redisComposeFile = Join-Path $root 'docker-compose.redis.yml'
$logDir = Join-Path $backendDir 'data\logs'
$workerPidFile = Join-Path $backendDir 'data\worker.pid'
$backendPidFile = Join-Path $backendDir 'data\backend.pid'
$backendPort = 3456
$frontendPort = 2199

function Get-DotEnvValue([string]$path, [string]$name) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return $null
    }
    $line = Get-Content -LiteralPath $path |
        Where-Object { $_ -match "^\s*$([regex]::Escape($name))\s*=" } |
        Select-Object -Last 1
    if ($null -eq $line) {
        return $null
    }
    return (($line -split '=', 2)[1]).Trim().Trim('"').Trim("'")
}

function Get-RuntimeValue([string]$name, [string]$defaultValue = '') {
    $value = Get-DotEnvValue -path $providerEnvFile -name $name
    if ([string]::IsNullOrWhiteSpace($value)) {
        $value = Get-DotEnvValue -path $runtimeEnvFile -name $name
    }
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $defaultValue
    }
    return $value
}

function Test-TcpEndpoint([string]$hostName, [int]$port, [int]$timeoutMs = 1000) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($hostName, $port)
        return $task.Wait($timeoutMs) -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Test-DockerEngine {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        return $false
    }
    & docker info --format '{{.ServerVersion}}' *> $null
    return $LASTEXITCODE -eq 0
}

function Start-DockerDesktop {
    if (Test-DockerEngine) {
        return $true
    }

    $candidates = @(
        (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'),
        (Join-Path $env:LOCALAPPDATA 'Docker\Docker Desktop.exe')
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and (Test-Path -LiteralPath $_ -PathType Leaf) }
    $dockerDesktop = $candidates | Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace($dockerDesktop)) {
        return $false
    }

    Write-Host '[Agent Team] Docker Engine is offline; starting Docker Desktop...'
    Start-Process -FilePath $dockerDesktop -WindowStyle Hidden | Out-Null
    for ($attempt = 1; $attempt -le 45; $attempt++) {
        if (Test-DockerEngine) {
            Write-Host '[OK] Docker Engine is ready.'
            return $true
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Ensure-Redis([string]$redisUrl) {
    if ([string]::IsNullOrWhiteSpace($redisUrl)) {
        Write-Warning 'Redis mode has no REDIS_URL configuration.'
        return $false
    }
    try {
        $uri = [Uri]$redisUrl
        $redisHost = $uri.Host
        $redisPort = if ($uri.Port -gt 0) { $uri.Port } else { 6379 }
    } catch {
        Write-Warning 'REDIS_URL is invalid. Expected a value such as redis://127.0.0.1:6379/0.'
        return $false
    }
    if (Test-TcpEndpoint -hostName $redisHost -port $redisPort) {
        Write-Host "[OK] Redis is available on ${redisHost}:$redisPort"
        return $true
    }
    if ($redisHost -notin @('127.0.0.1', 'localhost') -or $redisPort -ne 6379) {
        Write-Warning "Redis is unavailable on ${redisHost}:$redisPort. Automatic Docker startup only supports local port 6379."
        return $false
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Warning 'Redis is unavailable and Docker was not found.'
        return $false
    }
    if (-not (Test-Path -LiteralPath $redisComposeFile -PathType Leaf)) {
        Write-Warning "Redis Compose file was not found: $redisComposeFile"
        return $false
    }
    if (-not (Start-DockerDesktop)) {
        Write-Warning 'Docker Desktop did not become ready.'
        return $false
    }
    Write-Host '[Agent Team] Starting the local Redis coordination service...'
    $composeOutput = & docker compose -f $redisComposeFile up -d redis 2>&1
    if ($LASTEXITCODE -ne 0) {
        $detail = ($composeOutput | Select-Object -Last 1)
        Write-Warning "Docker could not start Redis. $detail"
        return $false
    }
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        if (Test-TcpEndpoint -hostName $redisHost -port $redisPort) {
            Write-Host '[OK] Redis coordination service is ready.'
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Warning 'Redis did not become ready within 15 seconds.'
    return $false
}

function Get-ProjectProcess([int]$processId) {
    return Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
}

function Get-ChildProcesses([int]$processId) {
    return @(
        Get-CimInstance Win32_Process -Filter "ParentProcessId = $processId" -ErrorAction SilentlyContinue
    )
}

function Test-AgentTeamBackend([int]$port) {
    try {
        $health = Invoke-RestMethod -UseBasicParsing -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 2
        return $null -ne $health.status -and $null -ne $health.executionMode
    } catch {
        return $false
    }
}

function Test-ProjectProcess([object]$process, [string]$projectRoot, [ValidateSet('backend', 'frontend')][string]$kind) {
    if ($null -eq $process) {
        return $false
    }

    $rootPattern = [regex]::Escape($projectRoot.TrimEnd('\'))
    $commandLine = [string]$process.CommandLine
    if ($commandLine -match $rootPattern) {
        return $true
    }

    if ($kind -eq 'backend') {
        return $commandLine -match 'app\.main:app'
    }

    return $commandLine -match 'vite|npm\.cmd run dev'
}

function Stop-ProcessTree([int]$processId) {
    # Uvicorn --reload opens the listening socket in the reloader process and
    # passes it to a multiprocessing child. If the reloader exits first,
    # Windows may keep reporting the departed parent PID for the live socket.
    # Walking ParentProcessId therefore remains necessary even when the parent
    # itself can no longer be returned by Get-Process/Get-CimInstance.
    $children = Get-ChildProcesses -processId $processId
    foreach ($child in $children) {
        Stop-ProcessTree -processId ([int]$child.ProcessId)
    }

    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
}

function Stop-PreviousBackend() {
    if (-not (Test-Path -LiteralPath $backendPidFile -PathType Leaf)) {
        return
    }

    $rawPid = (Get-Content -LiteralPath $backendPidFile -Raw).Trim()
    $backendPid = 0
    if ([int]::TryParse($rawPid, [ref]$backendPid)) {
        $process = Get-ProjectProcess -processId $backendPid
        $children = Get-ChildProcesses -processId $backendPid
        $isKnownBackend = $null -ne $process -and (Test-ProjectProcess -process $process -projectRoot $root -kind backend)
        $hasUvicornChild = @($children | Where-Object {
            $_.Name -match '^python(w)?\.exe$' -and [string]$_.CommandLine -match 'multiprocessing\.spawn|spawn_main'
        }).Count -gt 0

        if ($isKnownBackend -or ($hasUvicornChild -and (Test-AgentTeamBackend -port $backendPort))) {
            Stop-ProcessTree -processId $backendPid
            Write-Host "[OK] Stopped previous backend process tree (PID $backendPid)"
        }
    }

    Remove-Item -LiteralPath $backendPidFile -Force -ErrorAction SilentlyContinue
}

function Stop-PreviousWorker() {
    if (-not (Test-Path -LiteralPath $workerPidFile -PathType Leaf)) {
        return
    }
    $rawPid = (Get-Content -LiteralPath $workerPidFile -Raw).Trim()
    $workerPid = 0
    if ([int]::TryParse($rawPid, [ref]$workerPid)) {
        $process = Get-ProjectProcess -processId $workerPid
        if ($null -ne $process -and [string]$process.CommandLine -match 'app\.worker') {
            Stop-ProcessTree -processId $workerPid
            Write-Host "[OK] Stopped previous Redis Worker (PID $workerPid)"
        }
    }
    Remove-Item -LiteralPath $workerPidFile -Force -ErrorAction SilentlyContinue
}

function Release-ProjectPort([int]$port, [ValidateSet('backend', 'frontend')][string]$kind) {
    for ($attempt = 1; $attempt -le 8; $attempt++) {
        $processIds = @(
            Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique
        )

        if ($processIds.Count -eq 0) {
            return
        }

        foreach ($processId in $processIds) {
            $process = Get-ProjectProcess -processId ([int]$processId)
            if ($null -eq $process) {
                $children = Get-ChildProcesses -processId ([int]$processId)
                $hasUvicornChild = @($children | Where-Object {
                    $_.Name -match '^python(w)?\.exe$' -and [string]$_.CommandLine -match 'multiprocessing\.spawn|spawn_main'
                }).Count -gt 0

                if ($kind -eq 'backend' -and $hasUvicornChild -and (Test-AgentTeamBackend -port $port)) {
                    Stop-ProcessTree -processId ([int]$processId)
                    Write-Host "[OK] Released orphaned backend port $port (stale parent PID $processId)"
                } else {
                    Write-Host "[WARN] Port $port is occupied by PID $processId, but Windows did not expose a verifiable Agent-Team process tree."
                    try {
                        & taskkill.exe /PID $processId /T /F 2>$null | Out-Null
                    } catch {
                        # The verification below reports the actionable error.
                    }
                }
                continue
            }

            if (-not (Test-ProjectProcess -process $process -projectRoot $root -kind $kind)) {
                throw "Port $port belongs to an unrelated process (PID $processId); it was not stopped."
            }

            Stop-ProcessTree -processId ([int]$processId)
            Write-Host "[OK] Released $kind port $port (PID $processId)"
        }

        Start-Sleep -Milliseconds 500
    }

    $remaining = @(
        Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
    if ($remaining.Count -gt 0) {
        throw "Could not release port $port. Still listening PID(s): $($remaining -join ', ')"
    }
}

function Wait-ForBackend([int]$port) {
    $capabilitiesUri = "http://127.0.0.1:$port/api/provider/capabilities"
    $workflowUri = "http://127.0.0.1:$port/api/workflows/software-development"

    for ($attempt = 1; $attempt -le 30; $attempt++) {
        try {
            $capabilities = Invoke-WebRequest -UseBasicParsing -Uri $capabilitiesUri -TimeoutSec 2
            $capabilityBody = [string]$capabilities.Content

            if ($capabilities.StatusCode -ne 200 -or [string]::IsNullOrWhiteSpace($capabilityBody)) {
                throw "Backend capability endpoint returned an invalid response."
            }

            $workflow = Invoke-WebRequest -UseBasicParsing -Uri $workflowUri -TimeoutSec 2
            $workflowBody = [string]$workflow.Content
            if ($workflow.StatusCode -eq 200 -and -not [string]::IsNullOrWhiteSpace($workflowBody)) {
                Write-Host '[OK] Backend verification passed: capability and workflow endpoints are available.'
                return
            }

            throw "Backend responded, but the current workflow definition was not loaded."
        } catch {
            if ($_.Exception.Message -match 'invalid response|current workflow definition') {
                throw
            }
            Start-Sleep -Milliseconds 700
        }
    }

    throw "Backend did not become ready on port $port within 21 seconds."
}

function Wait-ForWorker([int]$port) {
    $healthUri = "http://127.0.0.1:$port/api/health"
    for ($attempt = 1; $attempt -le 40; $attempt++) {
        try {
            $response = Invoke-RestMethod -UseBasicParsing -Uri $healthUri -TimeoutSec 2
            if ($response.executionMode -eq 'redis' -and $response.redis.available -and $response.workers.active -ge 1) {
                Write-Host "[OK] Redis Worker is registered ($($response.workers.active) active)."
                return
            }
        } catch {
            # Keep waiting; worker stderr is reported if the timeout expires.
        }
        Start-Sleep -Milliseconds 500
    }
    $workerError = Join-Path $logDir 'worker-error.log'
    $detail = if (Test-Path -LiteralPath $workerError) { (Get-Content -LiteralPath $workerError -Tail 10) -join ' ' } else { '' }
    throw "Redis Worker did not register within 20 seconds. $detail"
}

function Wait-ForFrontend([int]$port) {
    $frontendUri = "http://127.0.0.1:$port/"
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $frontendUri -TimeoutSec 2
            if ($response.StatusCode -eq 200 -and -not [string]::IsNullOrWhiteSpace([string]$response.Content)) {
                Write-Host '[OK] Frontend verification passed.'
                return
            }
        } catch {
            # Vite may still be compiling; keep waiting until the bounded timeout.
        }
        Start-Sleep -Milliseconds 500
    }

    $frontendError = Join-Path $logDir 'frontend-error.log'
    $detail = if (Test-Path -LiteralPath $frontendError) { (Get-Content -LiteralPath $frontendError -Tail 10) -join ' ' } else { '' }
    throw "Frontend did not become ready on port $port within 15 seconds. $detail"
}

try {
    if (-not (Test-Path -LiteralPath $backendDir -PathType Container)) {
        throw "Backend directory was not found: $backendDir"
    }
    if (-not (Test-Path -LiteralPath $frontendDir -PathType Container)) {
        throw "Frontend directory was not found: $frontendDir"
    }

    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $executionMode = (Get-RuntimeValue -name 'AGENT_TEAM_EXECUTION_MODE' -defaultValue 'local').ToLowerInvariant()
    $redisUrl = Get-RuntimeValue -name 'REDIS_URL'
    if ($executionMode -eq 'redis') {
        $redisReady = Ensure-Redis -redisUrl $redisUrl
        if (-not $redisReady) {
            if ($env:AGENT_TEAM_REDIS_REQUIRED -eq '1') {
                throw 'Redis is required by AGENT_TEAM_REDIS_REQUIRED=1, but it is unavailable.'
            }
            $executionMode = 'local'
            $env:AGENT_TEAM_EXECUTION_MODE = 'local'
            $env:REDIS_URL = ''
            Write-Warning 'Redis is unavailable. This launch will use local execution with durable SQLite checkpoints.'
            Write-Warning 'Runs remain recoverable after restart, but multi-worker queues and cross-process SSE replay are disabled.'
        }
    }
    $env:AGENT_TEAM_EXECUTION_MODE = $executionMode
    if ($executionMode -eq 'redis') {
        $env:REDIS_URL = $redisUrl
    }

    Write-Host "[Agent Team] Releasing project ports $backendPort and $frontendPort..."
    Stop-PreviousWorker
    Stop-PreviousBackend
    Release-ProjectPort -port $backendPort -kind backend

    $backendPython = Join-Path $backendDir '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $backendPython -PathType Leaf)) {
        $backendPython = 'python'
    }

    if ($executionMode -eq 'redis') {
        & $backendPython -c 'import redis' 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host '[Agent Team] Installing the missing Redis Python client...'
            & $backendPython -m pip install -r (Join-Path $backendDir 'requirements.txt') | Out-Host
            if ($LASTEXITCODE -ne 0) {
                throw 'Python dependencies could not be installed.'
            }
        }
    }

    $backendProcess = Start-Process -FilePath $backendPython -WorkingDirectory $backendDir -ArgumentList @(
        '-m', 'uvicorn', 'app.main:app', '--reload', '--timeout-graceful-shutdown', '3', '--host', '127.0.0.1', '--port', "$backendPort"
    ) -RedirectStandardOutput (Join-Path $logDir 'backend.log') -RedirectStandardError (Join-Path $logDir 'backend-error.log') -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath $backendPidFile -Value $backendProcess.Id -Encoding ascii
    Write-Host "[OK] Backend is starting on http://127.0.0.1:$backendPort (PID $($backendProcess.Id))"
    Wait-ForBackend -port $backendPort

    if ($executionMode -eq 'redis') {
        $workerProcess = Start-Process -FilePath $backendPython -WorkingDirectory $backendDir -ArgumentList @(
            '-m', 'app.worker'
        ) -RedirectStandardOutput (Join-Path $logDir 'worker.log') -RedirectStandardError (Join-Path $logDir 'worker-error.log') -WindowStyle Hidden -PassThru
        Set-Content -LiteralPath $workerPidFile -Value $workerProcess.Id -Encoding ascii
        Write-Host "[OK] Redis Worker is starting (PID $($workerProcess.Id))"
        Wait-ForWorker -port $backendPort
    }

    $frontendAlreadyRunning = $false
    try {
        Release-ProjectPort -port $frontendPort -kind frontend
    } catch {
        if ($_.Exception.Message -notmatch "unrelated process") {
            throw
        }

        # Never terminate an unrelated service such as nginx. If it already
        # serves the frontend port successfully, keep it and continue; this
        # makes the launcher safe to use alongside an existing reverse proxy.
        try {
            $frontendProbe = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$frontendPort/" -TimeoutSec 3
            if ($frontendProbe.StatusCode -eq 200) {
                $frontendAlreadyRunning = $true
                Write-Host "[OK] Frontend port $frontendPort is already serving HTTP; existing service was kept."
            } else {
                throw "Frontend port $frontendPort is occupied by an unrelated process and did not return HTTP 200."
            }
        } catch {
            throw "Frontend port $frontendPort is occupied by an unrelated process; it was not stopped. Choose another port or stop that service manually."
        }
    }

    if (-not $frontendAlreadyRunning) {
        $frontendProcess = Start-Process -FilePath 'npm.cmd' -WorkingDirectory $frontendDir -ArgumentList @(
            'run', 'dev', '--', '--host', '127.0.0.1', '--port', "$frontendPort"
        ) -RedirectStandardOutput (Join-Path $logDir 'frontend.log') -RedirectStandardError (Join-Path $logDir 'frontend-error.log') -WindowStyle Hidden -PassThru
        Write-Host "[OK] Frontend is starting on http://127.0.0.1:$frontendPort (PID $($frontendProcess.Id))"
    }

    Wait-ForFrontend -port $frontendPort
    if ($env:AGENT_TEAM_NO_BROWSER -ne '1') {
        Start-Process "http://127.0.0.1:$frontendPort/" | Out-Null
    }
    Write-Host "[READY] Frontend: http://127.0.0.1:$frontendPort/"
    Write-Host "[READY] Backend:  http://127.0.0.1:$backendPort/"
    Write-Host "[READY] Logs:     $logDir"
    exit 0
} catch {
    Write-Host "[STOP] $($_.Exception.Message)"
    exit 2
}
