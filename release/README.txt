Shizai Agent 0.7.32 Windows release

Files
-----
shizai-agent-mcp.exe  MCP stdio server for Codex and other MCP clients.
shizai-agent.exe      Optional command-line client for diagnostics and setup.
shizai-agent-browser-host.exe  Optional Native Messaging bootstrap host.
browser-extension\    Independent visual grounding extension for Chrome/Edge.

Codex MCP configuration (`%USERPROFILE%\.codex\config.toml`)
-----------------------
```toml
[mcp_servers.shizai-agent]
command = 'C:\path\to\shizai-agent-mcp.exe'
args = []
startup_timeout_sec = 30
tool_timeout_sec = 120
omit_tools_from = ["deferred"]
```

The direct tool path avoids deferred tool-search incompatibilities with some
custom model providers while preserving the complete MCP tool list.

The local Shizai designer must be running and signed in for designer operations.
Run shizai-agent.exe --json agent auth-status to check the session.

Codex may display "Auth: Unsupported" for this MCP. That is expected for a
local stdio server. Shizai authentication comes from the signed-in desktop
designer session; it is independent from Codex OAuth support.

Install options
---------------
1. Run npx -y shizai-agent-mcp@latest install for automated download, checksum
   verification, installation, and Codex registration (Node.js 18+ required).
2. Extract the ZIP and run scripts/register-codex.ps1 for a portable installation.
   The script writes the persistent Codex config directly and does not need to
   start Codex during setup.
3. Installation prepares the browser extension and registers its local host by
   default. Load browser-extension once as an unpacked extension in
   chrome://extensions or edge://extensions. File preparation does not mean the
   browser has loaded it; check shizai_browser_status after loading.
4. release-manifest.json records the actual frozen MCP tool lists and payload
   hashes. Installers verify it before replacing an existing installation.

Install from PowerShell
-----------------------
npx -y shizai-agent-mcp@latest install
npx -y shizai-agent-mcp@latest status

Do not use the old raw GitHub main installer. Source updates live on Gitee;
GitHub Releases contains the verified compiled ZIP and checksum.

After installation, restart Codex and enter `/mcp`. Use natural language; no
`@` or other special invocation prefix is required.

This package contains compiled executables only. It does not contain the project
source tree. See the public GitHub release page for the matching checksum.
