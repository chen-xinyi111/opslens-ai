. "$PSScriptRoot/Common.ps1"
Import-LocalSettings
foreach ($taskTarget in @('platform','healthy','fault')) { Invoke-Compose $taskTarget @('ps','-a') }
try { Invoke-RestMethod 'http://127.0.0.1:18190/api/v1/health' -TimeoutSec 3 } catch { Write-Output 'Workbench is not running.' }

$taskLogs=Join-Path $RuntimeRoot 'platform-logs.json'
if(Test-Path -LiteralPath $taskLogs){Write-Output '本次平台日志路径（历史日志保留）：';Get-Content -LiteralPath $taskLogs}
