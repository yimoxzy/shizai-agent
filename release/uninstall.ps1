[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\ShizaiAgent'),
    [switch]$KeepFiles
)

$ErrorActionPreference = 'Stop'
$unregister = Join-Path $InstallDir 'scripts\unregister-codex.ps1'
if ([IO.File]::Exists($unregister)) {
    & $unregister | Out-Null
} else {
    $codex = Get-Command codex -ErrorAction SilentlyContinue
    if ($null -ne $codex) {
        & $codex.Source mcp remove shizai-agent 2>$null | Out-Null
    }
}

if (-not $KeepFiles -and [IO.Directory]::Exists($InstallDir) -and $PSCmdlet.ShouldProcess($InstallDir, 'Remove Shizai Agent installation')) {
    [IO.Directory]::Delete([IO.Path]::GetFullPath($InstallDir), $true)
}

[pscustomobject]@{
    uninstalled = $true
    files_kept = [bool]$KeepFiles
    directory = [IO.Path]::GetFullPath($InstallDir)
} | ConvertTo-Json
