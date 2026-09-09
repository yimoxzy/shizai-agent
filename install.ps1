[CmdletBinding()]
param(
    [string]$Version = '0.7.27',
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\ShizaiAgent')
)

$ErrorActionPreference = 'Stop'
$uri = "https://raw.githubusercontent.com/yimoxzy/shizai-agent/main/release/install.ps1"
$script = Invoke-RestMethod -Uri $uri
$temporaryScript = Join-Path ([IO.Path]::GetTempPath()) ("shizai-agent-install-" + [guid]::NewGuid().ToString('N') + '.ps1')
try {
    [IO.File]::WriteAllText($temporaryScript, $script, [Text.UTF8Encoding]::new($false))
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $temporaryScript -Version $Version -InstallDir $InstallDir
    exit $LASTEXITCODE
} finally {
    if ([IO.File]::Exists($temporaryScript)) { [IO.File]::Delete($temporaryScript) }
}
