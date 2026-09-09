[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $env:USERPROFILE '.codex\config.toml'),
    [switch]$SkipCli
)

$ErrorActionPreference = 'Stop'
$serverName = 'shizai-agent'

if (-not $SkipCli) {
    $codex = Get-Command codex -ErrorAction SilentlyContinue
    if ($null -ne $codex) {
        & $codex.Source mcp remove $serverName 2>$null | Out-Null
    }
}

if ([IO.File]::Exists($ConfigPath)) {
    $content = [IO.File]::ReadAllText($ConfigPath, [Text.Encoding]::UTF8)
    [IO.File]::Copy($ConfigPath, "$ConfigPath.shizai-agent.bak", $true)
    $escapedName = [Regex]::Escape($serverName)
    $sectionPattern = "(?ms)^\[mcp_servers\.$escapedName\]\r?\n.*?(?=^\[|\z)"
    $content = [Regex]::Replace($content, $sectionPattern, '').TrimEnd() + "`r`n"
    [IO.File]::WriteAllText($ConfigPath, $content, [Text.UTF8Encoding]::new($false))
}

[pscustomobject]@{
    registered = $false
    server = $serverName
    config = $ConfigPath
} | ConvertTo-Json -Depth 3
