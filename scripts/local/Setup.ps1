param([string]$Python = '')
. "$PSScriptRoot/Common.ps1"
Assert-Toolchain
[IO.Directory]::CreateDirectory($RuntimeRoot) | Out-Null
if (!(Test-Path -LiteralPath $PythonExe)) {
    if (!$Python) {
        $taskBundled = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
        if (Test-Path -LiteralPath $taskBundled) { $Python = $taskBundled }
        else { $Python = (Get-Command python).Source }
    }
    & $Python -c "import sys; assert (3,12) <= sys.version_info[:2] < (3,15), 'Python 3.12-3.14 is required'"
    Assert-Native 'Python version'
    & $Python -m venv (Join-Path $ProjectRoot '.venv')
    Assert-Native 'Virtual environment'
}
& $PythonExe -m pip install -r (Join-Path $ProjectRoot 'requirements.lock')
Assert-Native 'Python dependencies'
$taskConfig = Join-Path $ProjectRoot '.env'
if (!(Test-Path -LiteralPath $taskConfig)) {
    function New-LocalSecret { return [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(24)).ToLowerInvariant() }
    $taskPlatformSecret = New-LocalSecret
    $taskHealthySecret = New-LocalSecret
    $taskFaultSecret = New-LocalSecret
    $taskHealthyKey = New-LocalSecret
    $taskFaultKey = New-LocalSecret
    $taskText = @"
PLATFORM_DB_PASSWORD=$taskPlatformSecret
HEALTHY_DB_PASSWORD=$taskHealthySecret
FAULT_DB_PASSWORD=$taskFaultSecret
HEALTHY_DIAGNOSTIC_KEY=$taskHealthyKey
FAULT_DIAGNOSTIC_KEY=$taskFaultKey
PLATFORM_DATABASE_URL=postgresql://opslens:$taskPlatformSecret@127.0.0.1:18193/opslens
"@
    [IO.File]::WriteAllText($taskConfig, $taskText, [Text.UTF8Encoding]::new($false))
}
Import-LocalSettings
foreach ($taskLab in @('healthy','fault')) {
    $taskSecret = [Environment]::GetEnvironmentVariable("$($taskLab.ToUpperInvariant())_DB_PASSWORD")
    $taskKey = [Environment]::GetEnvironmentVariable("$($taskLab.ToUpperInvariant())_DIAGNOSTIC_KEY")
    $taskPort = if ($taskLab -eq 'healthy') { 18181 } else { 18182 }
    $taskText = "LAB_INSTANCE=$taskLab`nLAB_DB_PASSWORD=$taskSecret`nLAB_DIAGNOSTIC_KEY=$taskKey`nLAB_HTTP_PORT=$taskPort`n"
    $taskLabPath = Join-Path $RuntimeRoot "$taskLab.env"
    if (!(Test-Path -LiteralPath $taskLabPath)) { [IO.File]::WriteAllText($taskLabPath, $taskText, [Text.UTF8Encoding]::new($false)) }
}
Write-Output 'OpsLens configuration ready; secrets remain local.'
