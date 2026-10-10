[CmdletBinding()]
param(
    [string]$Version = '0.7.32',
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\ShizaiAgent'),
    [switch]$WithBrowserExtension,
    [switch]$WithoutBrowserExtension
)

$ErrorActionPreference = 'Stop'
if (-not (Get-Command npx.cmd -ErrorAction SilentlyContinue)) {
    throw 'Node.js 18+ is required for the npm installer. Alternatively use release/install.ps1 from the source checkout.'
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid release version.' }
$previousInstallDirectory = $env:SHIZAI_AGENT_HOME
try {
    $env:SHIZAI_AGENT_HOME = $InstallDir
    $arguments = @('-y', "shizai-agent-mcp@$Version", 'install')
    if ($WithoutBrowserExtension) { $arguments += '--without-browser-extension' }
    & npx.cmd @arguments
    if ($LASTEXITCODE -ne 0) { throw "MCP installation failed with exit code $LASTEXITCODE" }
} finally {
    $env:SHIZAI_AGENT_HOME = $previousInstallDirectory
}
