param([ValidateSet('Platform','Fault','All')][string]$Scope='Platform',[switch]$SkipBuild)
. "$PSScriptRoot/Common.ps1"
if (!(Test-Path -LiteralPath (Join-Path $ProjectRoot '.env'))) { & "$PSScriptRoot/Setup.ps1" }
if (!(Test-Path -LiteralPath $PythonExe)) { throw '请先运行scripts/local/Setup.ps1安装本机依赖。' }
Import-LocalSettings
Assert-Toolchain
& $PythonExe (Join-Path $ProjectRoot 'scripts/preflight.py') --scope $Scope
Assert-Native '资源和端口预检'
# Build before changing any experiment services.
if (!$SkipBuild) { Build-Workbench }
elseif (!(Test-Path -LiteralPath (Join-Path $ProjectRoot 'frontend/dist/index.html'))) { throw '页面尚未构建，请移除SkipBuild后重试。' }
Invoke-Compose 'platform' @('up','-d','--wait','--wait-timeout','120')
if ($Scope -eq 'All') { Invoke-Compose 'healthy' @('up','--build','-d','--wait','--wait-timeout','240') }
if ($Scope -in @('Fault','All')) { Invoke-Compose 'fault' @('up','--build','-d','--wait','--wait-timeout','240') }
$taskCurrent = $null
try { $taskCurrent = Invoke-RestMethod 'http://127.0.0.1:18190/api/v1/health' -TimeoutSec 2 } catch {}
if (!$taskCurrent -or $taskCurrent.delivery -ne '3.3' -or $taskCurrent.version -ne '0.5.0') { & "$PSScriptRoot/RestartPlatform.ps1" -SkipBuild }
& $PythonExe (Join-Path $ProjectRoot 'scripts/seed_knowledge.py')
Assert-Native '内置手册和关键词索引'
Write-Output "OpsLens 已就绪（$Scope）：http://127.0.0.1:18190/"
Write-Output '默认仅操作平台。实验实例需显式选择Fault或All；启动不调用模型。'
