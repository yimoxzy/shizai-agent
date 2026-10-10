[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PackageRoot,
    [Parameter(Mandatory = $true)][string]$Version
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
function Get-ShizaiFileHash([string]$LiteralPath) {
    $stream = [IO.File]::OpenRead($LiteralPath)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $hasher.Dispose() }
}
$root = [IO.Path]::GetFullPath($PackageRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
$manifestPath = Join-Path $root 'release-manifest.json'
if (-not [IO.File]::Exists($manifestPath)) { throw 'Release manifest missing; use a newly verified release package.' }
$manifest = [IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json
if ($manifest.format -ne 1 -or $manifest.version -ne $Version) { throw 'Release manifest format/version mismatch.' }
$required = @('shizai-agent.exe', 'shizai-agent-mcp.exe', 'shizai-agent-browser-host.exe',
    'browser-extension/manifest.json', 'browser-extension/service_worker.js',
    'browser-extension/content_script.js', 'browser-extension/src/core.js',
    'scripts/register-browser.ps1', 'scripts/unregister-browser.ps1',
    'scripts/register-codex.ps1', 'scripts/unregister-codex.ps1', 'scripts/verify-package.ps1', 'README.txt')
foreach ($name in $required) {
    if (-not $manifest.files.PSObject.Properties[$name]) { throw "Release manifest missing $name" }
}
foreach ($entry in $manifest.files.PSObject.Properties) {
    $target = [IO.Path]::GetFullPath((Join-Path $root $entry.Name))
    if (-not $target.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Release manifest contains an escaping path.'
    }
    if (-not [IO.File]::Exists($target)) { throw "Release file missing: $($entry.Name)" }
    if ((Get-ShizaiFileHash $target) -ne $entry.Value) {
        throw "Release file hash mismatch: $($entry.Name)"
    }
}
foreach ($profile in @('full', 'rpa-authoring')) {
    $tools = $manifest.profiles.$profile
    if ($null -eq $tools -or $tools.count -ne @($tools.names).Count -or
        'shizai_browser_status' -notin $tools.names -or 'shizai_rpa_add_step' -notin $tools.names) {
        throw "Release missing verified browser/authoring tool manifest: $profile"
    }
}
[pscustomobject]@{ verified = $true; version = $manifest.version; extension_version = $manifest.extension_version
    tools = @{ full = $manifest.profiles.full.count; 'rpa-authoring' = $manifest.profiles.'rpa-authoring'.count }
    manifest_sha256 = Get-ShizaiFileHash $manifestPath
} | ConvertTo-Json -Depth 5
