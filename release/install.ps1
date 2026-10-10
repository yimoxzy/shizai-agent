[CmdletBinding()]
param(
    [string]$Version = '0.7.33',
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\ShizaiAgent'),
    [string]$PackagePath,
    [string]$ChecksumPath,
    [switch]$SkipRegister,
    [switch]$WithBrowserExtension,
    [switch]$WithoutBrowserExtension
)

$ErrorActionPreference = 'Stop'
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
if ($InstallDir.TrimEnd('\') -eq [IO.Path]::GetPathRoot($InstallDir).TrimEnd('\') -or
    $InstallDir.TrimEnd('\') -eq $env:USERPROFILE.TrimEnd('\')) { throw 'Unsafe installation directory.' }
if ([IO.Directory]::Exists($InstallDir) -and @(Get-ChildItem -LiteralPath $InstallDir -Force).Count -gt 0 -and
    -not [IO.File]::Exists((Join-Path $InstallDir 'shizai-agent-mcp.exe'))) { throw 'Refusing to replace an unrelated directory.' }
$preserveBackup = $false
$installCommitted = $false
$backupDir = $null
$releaseBase = "https://github.com/yimoxzy/shizai-agent/releases/download/v$Version"
$assetName = "shizai-agent-v$Version-windows-x64.zip"
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("shizai-agent-install-" + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($temporaryRoot) | Out-Null

try {
    if (-not $PackagePath) {
        $PackagePath = Join-Path $temporaryRoot $assetName
        $ChecksumPath = "$PackagePath.sha256"
        Invoke-WebRequest -UseBasicParsing -Uri "$releaseBase/$assetName" -OutFile $PackagePath
        Invoke-WebRequest -UseBasicParsing -Uri "$releaseBase/$assetName.sha256" -OutFile $ChecksumPath
    }

    $resolvedPackage = [IO.Path]::GetFullPath($PackagePath)
    if (-not [IO.File]::Exists($resolvedPackage)) {
        throw "Release package not found: $resolvedPackage"
    }

    if ($ChecksumPath) {
        $expected = ([IO.File]::ReadAllText([IO.Path]::GetFullPath($ChecksumPath)) -split '\s+')[0].ToLowerInvariant()
        $actual = (Get-FileHash -LiteralPath $resolvedPackage -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($expected -ne $actual) {
            throw "SHA256 mismatch. Expected $expected but received $actual"
        }
    }

    $stage = Join-Path $temporaryRoot 'stage'
    Expand-Archive -LiteralPath $resolvedPackage -DestinationPath $stage -Force
    $payloadRoot = $stage
    $rootFiles = @(Get-ChildItem -LiteralPath $stage -File -Force)
    $rootDirectories = @(Get-ChildItem -LiteralPath $stage -Directory -Force)
    if ($rootFiles.Count -eq 0 -and $rootDirectories.Count -eq 1) {
        $payloadRoot = $rootDirectories[0].FullName
    }
    foreach ($required in @('shizai-agent.exe', 'shizai-agent-mcp.exe', 'shizai-agent-browser-host.exe', 'browser-extension\manifest.json', 'scripts\register-codex.ps1', 'scripts\unregister-codex.ps1', 'scripts\register-browser.ps1', 'scripts\unregister-browser.ps1', 'README.txt')) {
        if (-not [IO.File]::Exists((Join-Path $payloadRoot $required))) {
            throw "Release package is missing $required"
        }
    }

    $verification = & (Join-Path $payloadRoot 'scripts\verify-package.ps1') -PackageRoot $payloadRoot -Version $Version | ConvertFrom-Json
    $oldConfig = Join-Path $InstallDir 'browser-bridge.json'
    if ([IO.File]::Exists($oldConfig)) { Copy-Item -LiteralPath $oldConfig -Destination $payloadRoot }

    # Stop the installed executables before replacing them.  Windows keeps an
    # executable locked while the MCP stdio process is alive, so copying first
    # makes upgrades fail even though the archive and checksum are valid.
    $runningTargets = @(
        (Join-Path $InstallDir 'shizai-agent-mcp.exe'),
        (Join-Path $InstallDir 'shizai-agent.exe'),
        (Join-Path $InstallDir 'shizai-agent-browser-host.exe')
    ) | ForEach-Object { [IO.Path]::GetFullPath($_).ToLowerInvariant() }
    $running = @(Get-CimInstance Win32_Process | Where-Object {
        if (-not $_.ExecutablePath) { return $false }
        try { $runningTargets -contains [IO.Path]::GetFullPath([string]$_.ExecutablePath).ToLowerInvariant() } catch { $false }
    })
    foreach ($process in $running) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
    }
    # Move the old directory out of the way before copying.  This keeps the
    # replacement atomic and also handles a short-lived Windows file lock that
    # remains after the process exits.
    $backupDir = Join-Path ([IO.Path]::GetDirectoryName($InstallDir)) ('.shizai-agent-backup-' + [guid]::NewGuid().ToString('N'))
    if ([IO.Directory]::Exists($InstallDir)) {
        Move-Item -LiteralPath $InstallDir -Destination $backupDir -Force
    }
    try {
        [IO.Directory]::CreateDirectory($InstallDir) | Out-Null
        Copy-Item -Path (Join-Path $payloadRoot '*') -Destination $InstallDir -Recurse -Force
        $versionMetadata = [ordered]@{
            version = $Version
            asset = $assetName
            installedAt = [DateTime]::UtcNow.ToString('o')
            manifest_sha256 = $verification.manifest_sha256
            tools = $verification.tools
        }
        $versionMetadata | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $InstallDir 'version.json') -Encoding UTF8
        $registration = $null
        if (-not $SkipRegister) {
            $registration = & (Join-Path $InstallDir 'scripts\register-codex.ps1') -McpPath (Join-Path $InstallDir 'shizai-agent-mcp.exe') | ConvertFrom-Json
        }
        $browserRegistration = $null
        if (-not $WithoutBrowserExtension) {
            $browserRegistration = & (Join-Path $InstallDir 'scripts\register-browser.ps1') -InstallDir $InstallDir | ConvertFrom-Json
        }
        $installCommitted = $true
    } catch {
        $installFailure = $_
        try {
            if ([IO.Directory]::Exists($InstallDir)) { [IO.Directory]::Delete($InstallDir, $true) }
            if ([IO.Directory]::Exists($backupDir)) { Move-Item -LiteralPath $backupDir -Destination $InstallDir -Force }
        } catch {
            $preserveBackup = $true
            throw "Installation rollback failed; previous installation preserved at $backupDir"
        }
        throw $installFailure
    }

    $auth = $null
    try {
        $auth = & (Join-Path $InstallDir 'shizai-agent.exe') --json agent auth-status | ConvertFrom-Json
    } catch {
        $auth = [pscustomobject]@{ ready = $false; error = $_.Exception.Message }
    }

    [pscustomobject]@{
        installed = $true
        version = $Version
        verified = $verification.verified
        tools = $verification.tools
        directory = [IO.Path]::GetFullPath($InstallDir)
        registered = [bool]$registration.registered
        browser_bridge_registered = [bool]$browserRegistration.registered
        browser_extension_path = $browserRegistration.extension_path
        browser_extension_loaded = $null
        designer_ready = [bool]$auth.ready
        next_step = if (-not $WithoutBrowserExtension) { 'Load browser_extension_path once from chrome://extensions or edge://extensions; verify with shizai_browser_status. Restart the MCP client to load the new tool list.' } else { 'Restart your MCP client to load the new tool list.' }
    } | ConvertTo-Json -Depth 5
} finally {
    if ($installCommitted -and $backupDir -and [IO.Directory]::Exists($backupDir)) {
        $backupPrefix = [IO.Path]::GetDirectoryName($InstallDir).TrimEnd('\') + '\.shizai-agent-backup-'
        if (-not [IO.Path]::GetFullPath($backupDir).StartsWith($backupPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe backup cleanup path.' }
        [IO.Directory]::Delete($backupDir, $true)
    }
    if ([IO.Directory]::Exists($temporaryRoot) -and -not $preserveBackup) {
        $safePrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\shizai-agent-install-'
        if (-not [IO.Path]::GetFullPath($temporaryRoot).StartsWith($safePrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe cleanup path.' }
        [IO.Directory]::Delete($temporaryRoot, $true)
    }
}
