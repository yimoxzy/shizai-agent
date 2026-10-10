[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\ShizaiAgent')
)

$ErrorActionPreference = 'Stop'
$hostName = 'com.shizai.agent.browser'
$registryPaths = @(
    "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$hostName",
    "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\$hostName"
)
foreach ($registryPath in $registryPaths) {
    if (Test-Path -LiteralPath $registryPath) {
        Remove-Item -LiteralPath $registryPath -Recurse -Force
    }
}
foreach ($fileName in @('browser-bridge.json', "$hostName.json")) {
    $path = Join-Path ([IO.Path]::GetFullPath($InstallDir)) $fileName
    if ([IO.File]::Exists($path)) {
        [IO.File]::Delete($path)
    }
}

[pscustomobject]@{
    unregistered = $true
    native_host = $hostName
} | ConvertTo-Json
