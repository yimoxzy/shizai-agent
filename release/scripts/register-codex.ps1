[CmdletBinding()]
param(
    [string]$McpPath = (Join-Path $PSScriptRoot 'shizai-agent-mcp.exe'),
    [string]$ConfigPath = (Join-Path $env:USERPROFILE '.codex\config.toml'),
    [switch]$SkipCli,
    [switch]$UseCli
)

$ErrorActionPreference = 'Stop'
$serverName = 'shizai-agent'
$resolvedMcp = [IO.Path]::GetFullPath($McpPath)

if (-not [IO.File]::Exists($resolvedMcp)) {
    throw "MCP executable not found: $resolvedMcp"
}

$configDirectory = Split-Path -Parent $ConfigPath
[IO.Directory]::CreateDirectory($configDirectory) | Out-Null

if ($UseCli -and -not $SkipCli) {
    $codex = Get-Command codex -ErrorAction SilentlyContinue
    if ($null -ne $codex) {
        & $codex.Source mcp remove $serverName 2>$null | Out-Null
        & $codex.Source mcp add $serverName -- $resolvedMcp | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Codex CLI failed to register $serverName"
        }
    }
}

$content = if ([IO.File]::Exists($ConfigPath)) {
    [IO.File]::ReadAllText($ConfigPath, [Text.Encoding]::UTF8)
} else {
    ''
}

if ($content) {
    [IO.File]::Copy($ConfigPath, "$ConfigPath.shizai-agent.bak", $true)
}

$escapedName = [Regex]::Escape($serverName)
$sectionPattern = "(?ms)^\[mcp_servers\.$escapedName\]\r?\n.*?(?=^\[|\z)"
$content = [Regex]::Replace($content, $sectionPattern, '').TrimEnd()
$tomlPath = $resolvedMcp.Replace("'", "''")
$section = @"

[mcp_servers.$serverName]
command = '$tomlPath'
args = []
startup_timeout_sec = 30
tool_timeout_sec = 120
omit_tools_from = ["deferred"]
"@

[IO.File]::WriteAllText($ConfigPath, ($content + $section + "`r`n"), [Text.UTF8Encoding]::new($false))

[pscustomobject]@{
    registered = $true
    server = $serverName
    command = $resolvedMcp
    config = $ConfigPath
    note = 'Codex may show Auth: Unsupported for local stdio MCP servers; Shizai authentication comes from the signed-in desktop designer.'
} | ConvertTo-Json -Depth 3
