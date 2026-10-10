[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\ShizaiAgent')
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$extensionId = 'onlmcbjbkkjllainhobbehhaohnkloem'
$hostName = 'com.shizai.agent.browser'
$installRoot = [IO.Path]::GetFullPath($InstallDir)
$hostPath = Join-Path $installRoot 'shizai-agent-browser-host.exe'
$extensionPath = Join-Path $installRoot 'browser-extension'
$configPath = Join-Path $installRoot 'browser-bridge.json'
$manifestPath = Join-Path $installRoot "$hostName.json"

foreach ($required in @($hostPath, (Join-Path $extensionPath 'manifest.json'))) {
    if (-not [IO.File]::Exists($required)) {
        throw "Browser bridge file is missing: $required"
    }
}

$existing = $null
if ([IO.File]::Exists($configPath)) {
    $existing = [IO.File]::ReadAllText($configPath) | ConvertFrom-Json
    # Windows PowerShell deserializes JSON numbers as Int64, while newer
    # hosts may expose Int32. Validate the numeric range after conversion so
    # an otherwise valid existing identity is not rejected during upgrades.
    $existingPort = 0
    $portParsed = [int]::TryParse(
        [string]$existing.bridgePort,
        [Globalization.NumberStyles]::Integer,
        [Globalization.CultureInfo]::InvariantCulture,
        [ref]$existingPort
    )
    if (-not ($existing.installToken -is [string]) -or -not $existing.installToken -or
        -not $portParsed -or $existingPort -lt 1 -or $existingPort -gt 65535) {
        throw 'Existing browser bridge configuration is invalid; refusing to silently replace its identity.'
    }
}
if ($existing) {
    $installToken = $existing.installToken
    $bridgePort = $existingPort
} else {
    $tokenBytes = New-Object byte[] 32
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($tokenBytes) } finally { $random.Dispose() }
    $installToken = [Convert]::ToBase64String($tokenBytes)
    $bridgePort = 18768
}
$utf8 = [Text.UTF8Encoding]::new($false)
$config = [ordered]@{
    allowedExtensionIds = @($extensionId)
    installToken = $installToken
    bridgePort = $bridgePort
    capabilities = @('status', 'list_tabs', 'activate_tab', 'capture_viewport', 'inspect_points', 'infer_repeated_collection', 'performAction')
}
[IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 5), $utf8)

$manifest = [ordered]@{
    name = $hostName
    description = 'Shizai MCP browser bridge bootstrap'
    path = $hostPath
    type = 'stdio'
    allowed_origins = @("chrome-extension://$extensionId/")
}
[IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 5), $utf8)

$registryPaths = @(
    "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$hostName",
    "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\$hostName"
)
foreach ($registryPath in $registryPaths) {
    New-Item -Path $registryPath -Force | Out-Null
    Set-Item -Path $registryPath -Value $manifestPath
}

[pscustomobject]@{
    registered = $true
    extension_id = $extensionId
    extension_path = $extensionPath
    native_host = $hostPath
    extension_loaded = $null
    connection_verified = $false
    token_preserved = [bool]$existing
    chrome_setup = 'Open chrome://extensions, enable Developer mode, choose Load unpacked, then select extension_path.'
    edge_setup = 'Open edge://extensions, enable Developer mode, choose Load unpacked, then select extension_path.'
    browser_restart_required = $true
} | ConvertTo-Json -Depth 5
