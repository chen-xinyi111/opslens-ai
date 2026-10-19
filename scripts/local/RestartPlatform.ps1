param([switch]$SkipBuild,[switch]$StopOnly)
. "$PSScriptRoot/Common.ps1"
Import-LocalSettings
if (!$SkipBuild -and !$StopOnly) { Build-Workbench }
$taskPidFile=Join-Path $RuntimeRoot 'platform.pid'
if (Test-Path -LiteralPath $taskPidFile) {
    $taskProcessId=[int](Get-Content -LiteralPath $taskPidFile)
    $taskProcess=Get-CimInstance Win32_Process -Filter "ProcessId=$taskProcessId" -ErrorAction SilentlyContinue
    if ($taskProcess) {
        if ($taskProcess.ExecutablePath -ne $PythonExe -or $taskProcess.CommandLine -notmatch 'opslens.api:app') { throw 'PID belongs to another process; refusing to stop.' }
        foreach ($taskChild in (Get-CimInstance Win32_Process -Filter "ParentProcessId=$taskProcessId")) {
            if ($taskChild.CommandLine -match 'opslens.api:app' -and $taskChild.ExecutablePath -match 'python.exe$') { Stop-Process -Id $taskChild.ProcessId -ErrorAction SilentlyContinue }
        }
        Stop-Process -Id $taskProcessId -ErrorAction SilentlyContinue
    }
}
for ($taskCloseAttempt=0;$taskCloseAttempt -lt 10;$taskCloseAttempt++) {
    $taskClosingProbe=[Net.Sockets.TcpClient]::new()
    try { $taskClosingProbe.Connect('127.0.0.1',18190) }
    catch [Net.Sockets.SocketException] { break }
    finally { $taskClosingProbe.Dispose() }
    Start-Sleep -Milliseconds 300
}
$taskProbe=[Net.Sockets.TcpClient]::new()
try { $taskProbe.Connect('127.0.0.1',18190); throw 'Another process occupies port 18190; refusing to start.' }
catch [Net.Sockets.SocketException] {} finally { $taskProbe.Dispose() }
if ($StopOnly) { Write-Output 'OpsLens platform paused; lab services were not changed.'; return }
$taskLogStamp=[DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fff')
$taskStdout=Join-Path $RuntimeRoot "platform-$taskLogStamp.stdout.log"
$taskStderr=Join-Path $RuntimeRoot "platform-$taskLogStamp.stderr.log"
[IO.File]::WriteAllText((Join-Path $RuntimeRoot 'platform-logs.json'), (@{stdout=$taskStdout;stderr=$taskStderr}|ConvertTo-Json), [Text.UTF8Encoding]::new($false))
$taskProcess=Start-Process -FilePath $PythonExe -ArgumentList @('-m','uvicorn','opslens.api:app','--host','127.0.0.1','--port','18190') -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput $taskStdout -RedirectStandardError $taskStderr -PassThru
[IO.File]::WriteAllText($taskPidFile,"$($taskProcess.Id)",[Text.UTF8Encoding]::new($false))
for ($taskAttempt=0;$taskAttempt -lt 40;$taskAttempt++) {
    try {
        $taskHealth=Invoke-RestMethod 'http://127.0.0.1:18190/api/v1/health' -TimeoutSec 2
        if ($taskHealth.delivery -eq '3.3' -and $taskHealth.version -eq '0.5.0') { Write-Output 'OpsLens ready: http://127.0.0.1:18190/'; return }
    } catch {}
    Start-Sleep -Milliseconds 500
}
throw "Platform restart failed; see $taskStderr"
