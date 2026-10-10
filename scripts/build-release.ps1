[CmdletBinding()]
param(
    [string]$Version = '',
    [string]$Python = 'python',
    [string]$OutputDir = ''
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $Version) {
    $Version = (& $Python -c "from shizai_agent import __version__; print(__version__)" | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read source version.' }
}
$OutputDir = if ($OutputDir) { $OutputDir } else { Join-Path $root 'release' }
$output = [IO.Path]::GetFullPath($OutputDir)
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("shizai-agent-build-{0}-{1}" -f $Version, [guid]::NewGuid().ToString('N'))
$workRoot = Join-Path $tempRoot 'work'
$distRoot = Join-Path $tempRoot 'dist'
$stageRoot = Join-Path $tempRoot 'package'
$archiveName = "shizai-agent-v$Version-windows-x64.zip"
$archivePath = Join-Path $output $archiveName
$checksumPath = "$archivePath.sha256"

function Invoke-Python {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE"
    }
}

try {
    Push-Location $root
    Invoke-Python -Arguments @('-m', 'shizai_agent.core.release_integrity', '--source-root', $root, '--version', $Version)
    New-Item -ItemType Directory -Force -Path $workRoot, $distRoot, $stageRoot, $output | Out-Null
    Invoke-Python -Arguments @(
        '-m', 'PyInstaller', '--noconfirm', '--clean',
        '--workpath', (Join-Path $workRoot 'mcp'),
        '--distpath', (Join-Path $distRoot 'mcp'),
        (Join-Path $root 'shizai-agent-mcp.spec')
    )
    Invoke-Python -Arguments @(
        '-m', 'PyInstaller', '--noconfirm', '--clean',
        '--workpath', (Join-Path $workRoot 'cli'),
        '--distpath', (Join-Path $distRoot 'cli'),
        (Join-Path $root 'shizai-agent.spec')
    )
    Invoke-Python -Arguments @(
        '-m', 'PyInstaller', '--noconfirm', '--clean',
        '--workpath', (Join-Path $workRoot 'browser-host'),
        '--distpath', (Join-Path $distRoot 'browser-host'),
        (Join-Path $root 'shizai-agent-browser-host.spec')
    )

    Copy-Item -LiteralPath (Join-Path $distRoot 'mcp\shizai-agent-mcp.exe') -Destination $stageRoot
    Copy-Item -LiteralPath (Join-Path $distRoot 'cli\shizai-agent.exe') -Destination $stageRoot
    Copy-Item -LiteralPath (Join-Path $distRoot 'browser-host\shizai-agent-browser-host.exe') -Destination $stageRoot
    foreach ($extensionFile in @('manifest.json', 'service_worker.js', 'content_script.js', 'src\core.js', 'reload.html', 'reload.js', 'README.md')) {
        $target = Join-Path $stageRoot "browser-extension\$extensionFile"
        [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
        Copy-Item -LiteralPath (Join-Path $root "browser_extension\$extensionFile") -Destination $target
    }
    Copy-Item -LiteralPath (Join-Path $root 'release\README.txt') -Destination $stageRoot
    Copy-Item -LiteralPath (Join-Path $root 'release\scripts') -Destination $stageRoot -Recurse

    # Validate the frozen binaries, not just the Python source or ZIP filename.
    Invoke-Python -Arguments @('-m', 'shizai_agent.core.release_integrity', '--source-root', $root,
        '--version', $Version, '--package-dir', $stageRoot)

    if (Test-Path -LiteralPath $archivePath) { Remove-Item -LiteralPath $archivePath -Force }
    if (Test-Path -LiteralPath $checksumPath) { Remove-Item -LiteralPath $checksumPath -Force }
    Compress-Archive -Path (Join-Path $stageRoot '*') -DestinationPath $archivePath -CompressionLevel Optimal
    $hash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $archiveName" | Set-Content -LiteralPath $checksumPath -Encoding ASCII

    [pscustomobject]@{
        version = $Version
        archive = $archivePath
        sha256 = $checksumPath
        size = (Get-Item -LiteralPath $archivePath).Length
        build_root = $tempRoot
    } | ConvertTo-Json
} finally {
    if ((Get-Location).Path -eq $root) {
        Pop-Location
    }
    if (Test-Path -LiteralPath $tempRoot) {
        $resolvedTemp = [IO.Path]::GetFullPath($tempRoot)
        $allowedTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\shizai-agent-build-'
        if (-not $resolvedTemp.StartsWith($allowedTemp, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe build cleanup path.' }
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}
