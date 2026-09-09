[CmdletBinding()]
param(
    [string]$Version = '0.7.27',
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\ShizaiAgent'),
    [string]$PackagePath,
    [string]$ChecksumPath,
    [switch]$SkipRegister
)

$ErrorActionPreference = 'Stop'
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
    foreach ($required in @('shizai-agent.exe', 'shizai-agent-mcp.exe', 'scripts\register-codex.ps1', 'scripts\unregister-codex.ps1', 'README.txt')) {
        if (-not [IO.File]::Exists((Join-Path $payloadRoot $required))) {
            throw "Release package is missing $required"
        }
    }

    # Stop the installed executables before replacing them.  Windows keeps an
    # executable locked while the MCP stdio process is alive, so copying first
    # makes upgrades fail even though the archive and checksum are valid.
    $runningTargets = @(
        (Join-Path $InstallDir 'shizai-agent-mcp.exe'),
        (Join-Path $InstallDir 'shizai-agent.exe')
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
    $backupDir = Join-Path $temporaryRoot 'previous'
    if ([IO.Directory]::Exists($InstallDir)) {
        Move-Item -LiteralPath $InstallDir -Destination $backupDir -Force
    }
    try {
        [IO.Directory]::CreateDirectory($InstallDir) | Out-Null
        Copy-Item -Path (Join-Path $payloadRoot '*') -Destination $InstallDir -Recurse -Force
    } catch {
        if ([IO.Directory]::Exists($InstallDir)) {
            [IO.Directory]::Delete($InstallDir, $true)
        }
        if ([IO.Directory]::Exists($backupDir)) {
            Move-Item -LiteralPath $backupDir -Destination $InstallDir -Force
        }
        throw
    }
    $versionMetadata = [ordered]@{
        version = $Version
        asset = $assetName
        installedAt = [DateTime]::UtcNow.ToString('o')
    }
    $versionMetadata | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $InstallDir 'version.json') -Encoding UTF8

    $registration = $null
    if (-not $SkipRegister) {
        $registration = & (Join-Path $InstallDir 'scripts\register-codex.ps1') -McpPath (Join-Path $InstallDir 'shizai-agent-mcp.exe') | ConvertFrom-Json
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
        directory = [IO.Path]::GetFullPath($InstallDir)
        registered = [bool]$registration.registered
        designer_ready = [bool]$auth.ready
        next_step = 'Restart Codex, open /mcp, then ask Codex to use shizai-agent. Auth: Unsupported is normal for this local stdio server.'
    } | ConvertTo-Json -Depth 5
} finally {
    if ([IO.Directory]::Exists($temporaryRoot)) {
        [IO.Directory]::Delete($temporaryRoot, $true)
    }
}
