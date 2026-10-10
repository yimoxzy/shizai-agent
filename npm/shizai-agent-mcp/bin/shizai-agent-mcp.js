#!/usr/bin/env node

/* Lightweight distribution layer. The MCP binary remains in GitHub Releases. */
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { execFileSync, spawn } = require('node:child_process');

const SERVER = 'shizai-agent';
const REPO = process.env.SHIZAI_AGENT_REPO || 'yimoxzy/shizai-agent';
const DEFAULT_VERSION = process.env.SHIZAI_AGENT_VERSION || require('../package.json').version;
const installRoot = process.env.SHIZAI_AGENT_HOME || path.join(
  process.env.LOCALAPPDATA || path.join(os.homedir(), '.local'), 'Programs', 'ShizaiAgent'
);

function fail(message) { console.error(`shizai-agent-mcp: ${message}`); process.exitCode = 1; }
function json(value) { console.log(JSON.stringify(value, null, 2)); }
function help() {
  console.log(`Shizai Agent MCP installer\n\nUsage:\n  shizai-agent-mcp install [--version VERSION] [--without-browser-extension]\n  shizai-agent-mcp register <codex|cursor|claude|browser>\n  shizai-agent-mcp status\n  shizai-agent-mcp path\n  shizai-agent-mcp uninstall\n\nBrowser extension files and native host registration are prepared by default.\nLoad the extension in Chrome/Edge once; --without-browser-extension skips host setup.\n\nEnvironment:\n  SHIZAI_AGENT_VERSION  Release version (default: ${DEFAULT_VERSION})\n  SHIZAI_AGENT_HOME     Installation directory\n  SHIZAI_AGENT_REPO     GitHub repository owner/name\n  SHIZAI_AGENT_RELEASE_BASE_URL  Override release asset base URL\n`);
}
function binaryPath() { return path.join(installRoot, 'shizai-agent-mcp.exe'); }
function validateInstallRoot() {
  const resolved = path.resolve(installRoot);
  const parsed = path.parse(resolved);
  if (resolved === parsed.root || resolved === path.resolve(os.homedir())) {
    throw new Error(`拒绝使用过宽的安装目录：${resolved}`);
  }
  if (fs.existsSync(resolved) && fs.readdirSync(resolved).length && !fs.existsSync(path.join(resolved, 'shizai-agent-mcp.exe'))) {
    throw new Error('拒绝替换非 Shizai Agent 安装目录');
  }
  return resolved;
}
function psQuote(value) { return `'${String(value).replaceAll("'", "''")}'`; }
function powerShellOptions() {
  // A host running PowerShell 7 can pass its module search path through Node.
  // Windows PowerShell must resolve its own built-in modules first.
  const builtin = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'Modules');
  return { encoding: 'utf8', windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, PSModulePath: [builtin, process.env.PSModulePath].filter(Boolean).join(path.delimiter) } };
}
function runPowerShell(script, args = []) {
  return execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script, ...args], powerShellOptions());
}
function sleepSync(milliseconds) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, milliseconds);
}
function removeDirectoryWithRetries(directory, attempts = 10) {
  if (!fs.existsSync(directory)) return;
  let lastError;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      fs.rmSync(directory, { recursive: true, force: true });
      return;
    } catch (error) {
      lastError = error;
      sleepSync(200 * (attempt + 1));
    }
  }
  throw lastError;
}
function stopInstalledProcesses(destination) {
  if (process.platform !== 'win32') return [];
  const executables = [
    path.join(destination, 'shizai-agent-mcp.exe'),
    path.join(destination, 'shizai-agent.exe'),
    path.join(destination, 'shizai-agent-browser-host.exe')
  ];
  const targetList = executables.map(psQuote).join(', ');
  const script = String.raw`
$ErrorActionPreference = 'Stop'
$targets = @(${targetList} | ForEach-Object { [IO.Path]::GetFullPath([string]$_).ToLowerInvariant() })
$matches = @(Get-CimInstance Win32_Process | Where-Object {
  if (-not $_.ExecutablePath) { return $false }
  try { $targets -contains [IO.Path]::GetFullPath([string]$_.ExecutablePath).ToLowerInvariant() } catch { $false }
})
$matchedIds = @($matches | ForEach-Object { [int]$_.ProcessId })
foreach ($process in $matches) {
  Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
}
$deadline = [DateTime]::UtcNow.AddSeconds(5)
do {
  $remainingIds = @()
  foreach ($targetId in $matchedIds) {
    if ($null -ne (Get-Process -Id $targetId -ErrorAction SilentlyContinue)) {
      $remainingIds += $targetId
    }
  }
  if ($remainingIds.Count -eq 0) { break }
  Start-Sleep -Milliseconds 100
} while ([DateTime]::UtcNow -lt $deadline)
if ($remainingIds.Count -gt 0) {
  throw ('安装前无法停止实在 Agent 进程：' + (($remainingIds | Sort-Object) -join ', '))
}
@($matchedIds) | ConvertTo-Json -Compress
`;
  const output = runPowerShell(script).trim();
  if (!output) return [];
  const parsed = JSON.parse(output);
  return Array.isArray(parsed) ? parsed : [parsed];
}
function cleanupInstallArtifacts(parent, excluded = []) {
  const excludedPaths = new Set(excluded.map(item => path.resolve(item).toLowerCase()));
  for (const entry of fs.readdirSync(parent, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    if (!entry.name.startsWith('.shizai-agent-stage-') && !entry.name.startsWith('.shizai-agent-backup-')) continue;
    const candidate = path.join(parent, entry.name);
    if (excludedPaths.has(path.resolve(candidate).toLowerCase())) continue;
    removeDirectoryWithRetries(candidate);
  }
}
function assetName(version) {
  if (process.platform !== 'win32' || process.arch !== 'x64') {
    throw new Error(`当前 npm 安装器暂时只提供 Windows x64，检测到 ${process.platform}/${process.arch}`);
  }
  return process.env.SHIZAI_AGENT_ASSET_NAME || `shizai-agent-v${version}-windows-x64.zip`;
}
function releaseUrl(version, asset) {
  const base = process.env.SHIZAI_AGENT_RELEASE_BASE_URL || `https://github.com/${REPO}/releases/download/v${version}`;
  return `${base.replace(/\/$/, '')}/${asset}`;
}
function verifySha256(file, expected) {
  const actual = crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex').toLowerCase();
  if (actual !== expected.toLowerCase()) throw new Error(`SHA256 校验失败：期望 ${expected}，实际 ${actual}`);
}
function download(url, destination, options = {}) {
  const progress = options.progress !== false;
  const curl = process.env.SHIZAI_AGENT_CURL || 'curl.exe';
  const args = ['--fail', '--location', '--silent', '--show-error'];
  if (progress) args.push('--progress-bar');
  args.push('--output', destination, url);
  try {
    execFileSync(curl, args, { stdio: ['ignore', 'ignore', 'inherit'] });
    if (progress) process.stderr.write('\n');
    return;
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
  }
  const script = `$ProgressPreference = 'SilentlyContinue'; Invoke-WebRequest -UseBasicParsing -Uri ${psQuote(url)} -OutFile ${psQuote(destination)}`;
  runPowerShell(script);
}
function install(version, options = {}) {
  const destination = validateInstallRoot();
  const asset = assetName(version);
  const url = releaseUrl(version, asset);
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'shizai-agent-'));
  const archive = path.join(temp, asset);
  const checksum = path.join(temp, `${asset}.sha256`);
  const parent = path.dirname(destination);
  const nonce = `${process.pid}-${Date.now()}`;
  const stage = path.join(parent, `.shizai-agent-stage-${nonce}`);
  const backup = path.join(parent, `.shizai-agent-backup-${nonce}`);
  let verified = false;
  let committed = false;
  const stoppedProcesses = new Set();
  try {
    download(url, archive, { progress: true });
    try {
      download(`${url}.sha256`, checksum, { progress: false });
      const text = fs.readFileSync(checksum, 'utf8');
      const expected = (text.match(/[a-f0-9]{64}/i) || [])[0];
      if (!expected) throw new Error('校验文件中没有 SHA-256');
      verifySha256(archive, expected);
      verified = true;
    } catch (error) {
      if (process.env.SHIZAI_AGENT_ALLOW_UNVERIFIED !== '1') throw error;
      console.warn('SHIZAI_AGENT_ALLOW_UNVERIFIED=1：本次跳过发布包校验。');
    }
    fs.mkdirSync(parent, { recursive: true });
    fs.mkdirSync(stage);
    runPowerShell(`Expand-Archive -LiteralPath ${psQuote(archive)} -DestinationPath ${psQuote(stage)} -Force`);
    let source = stage;
    if (!fs.existsSync(path.join(source, 'shizai-agent-mcp.exe'))) {
      const nested = fs.readdirSync(stage, { withFileTypes: true })
        .filter(entry => entry.isDirectory())
        .map(entry => path.join(stage, entry.name))
        .find(candidate => fs.existsSync(path.join(candidate, 'shizai-agent-mcp.exe')));
      if (!nested) throw new Error('发布包中未找到 shizai-agent-mcp.exe');
      source = nested;
    }
    const verification = verifyPackage(source, version);
    fs.writeFileSync(path.join(source, 'version.json'), JSON.stringify({
      version, asset, installedAt: new Date().toISOString(),
      manifest_sha256: verification.manifest_sha256, tools: verification.tools
    }, null, 2));
    const oldBridge = path.join(destination, 'browser-bridge.json');
    if (fs.existsSync(oldBridge)) fs.copyFileSync(oldBridge, path.join(source, 'browser-bridge.json'));
    for (const processId of stopInstalledProcesses(destination)) stoppedProcesses.add(processId);
    let previousMoved = false;
    const result = { installed: true, version, directory: destination, command: binaryPath(), verified,
      tools: verification.tools, browser_extension_version: verification.extension_version };
    try {
      if (fs.existsSync(destination)) {
        fs.renameSync(destination, backup);
        previousMoved = true;
        for (const processId of stopInstalledProcesses(destination)) stoppedProcesses.add(processId);
      }
      fs.renameSync(source, destination);
      if (!options.skipRegister) result.registration = registerCodex(true);
      if (options.withBrowserExtension !== false) result.browser_extension = registerBrowser(true);
      committed = true;
    } catch (error) {
      if (previousMoved && fs.existsSync(backup)) {
        for (const processId of stopInstalledProcesses(destination)) stoppedProcesses.add(processId);
        // Keep the old installation recoverable until registration also succeeds.
        const failedInstall = path.join(parent, `.shizai-agent-failed-${nonce}`);
        if (fs.existsSync(destination)) fs.renameSync(destination, failedInstall);
        fs.renameSync(backup, destination);
        removeDirectoryWithRetries(failedInstall);
      }
      throw error;
    }
    result.stopped_processes = [...stoppedProcesses].sort((left, right) => left - right);
    console.log(`安装成功：Shizai Agent MCP v${version}`);
    if (result.registration?.registered) console.log('Codex MCP 已注册');
    if (result.browser_extension?.registered) {
      console.log(`浏览器扩展文件已准备：${result.browser_extension.extension_path}`);
      console.log('首次使用：在 chrome://extensions 或 edge://extensions 加载该目录；连接状态以 shizai_browser_status 为准。');
    }
    return result;
  } finally {
    removeDirectoryWithRetries(temp);
    removeDirectoryWithRetries(stage);
    if (committed && fs.existsSync(backup)) removeDirectoryWithRetries(backup);
  }
}
function verifyPackage(root, version) {
  const script = path.join(root, 'scripts', 'verify-package.ps1');
  if (!fs.existsSync(script)) throw new Error('发布包缺少完整性校验器，请使用新版发布包');
  const output = execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
    '-File', script, '-PackageRoot', root, '-Version', version],
    powerShellOptions()).replace(/^\uFEFF/, '').trim();
  return JSON.parse(output);
}
function installationStatus() {
  const metadataFile = path.join(installRoot, 'version.json');
  const metadata = fs.existsSync(metadataFile) ? JSON.parse(fs.readFileSync(metadataFile, 'utf8').replace(/^\uFEFF/, '')) : {};
  let verification;
  try { verification = verifyPackage(installRoot, metadata.version || DEFAULT_VERSION); }
  catch (_) { verification = { verified: false, reason: '安装文件缺失、损坏或来自未校验的旧发布包，请重新安装最新版' }; }
  return { installed: fs.existsSync(binaryPath()), version: metadata.version || null, installer_version: require('../package.json').version,
    directory: installRoot, command: binaryPath(), verification,
    browser_extension_files_present: fs.existsSync(path.join(installRoot, 'browser-extension', 'manifest.json')),
    native_host_present: fs.existsSync(path.join(installRoot, 'shizai-agent-browser-host.exe')),
    browser_config_present: fs.existsSync(path.join(installRoot, 'browser-bridge.json')),
    browser_extension_loaded: null, browser_extension_path: path.join(installRoot, 'browser-extension') };
}
function replaceTomlSection(content, section, replacement) {
  const lines = String(content || '').split(/\r?\n/);
  const kept = [];
  let skipping = false;
  for (const line of lines) {
    const match = line.trim().match(/^\[([^\]]+)\]$/);
    if (match) {
      const table = match[1];
      if (table === section || table.startsWith(`${section}.`)) {
        skipping = true;
        continue;
      }
      skipping = false;
    }
    if (!skipping) kept.push(line);
  }
  const next = kept.join('\n').trimEnd();
  return `${next}${next ? '\r\n\r\n' : ''}${replacement.trim()}\r\n`;
}
function tomlString(value) { return JSON.stringify(String(value)); }
function registerCodex(silent = false) {
  const config = process.env.SHIZAI_CODEX_CONFIG || path.join(os.homedir(), '.codex', 'config.toml');
  fs.mkdirSync(path.dirname(config), { recursive: true });
  const old = fs.existsSync(config) ? fs.readFileSync(config, 'utf8') : '';
  if (old) fs.copyFileSync(config, `${config}.shizai-agent.bak`);
  // Keep this local stdio server on Codex's direct tool path. Some custom
  // Responses providers do not support Codex's deferred tool-search payload.
  const section = `[mcp_servers.${SERVER}]\ncommand = ${tomlString(binaryPath())}\nargs = []\nenabled = true\nstartup_timeout_sec = 30\ntool_timeout_sec = 120\nomit_tools_from = [\"deferred\"]\n\n[mcp_servers.${SERVER}.env]\nSHIZAI_MCP_TOOL_PROFILE = \"rpa-authoring\"`;
  const next = replaceTomlSection(old, `mcp_servers.${SERVER}`, section);
  fs.writeFileSync(config, next, 'utf8');
  const persisted = fs.readFileSync(config, 'utf8');
  const verified = persisted.includes(`[mcp_servers.${SERVER}]`) &&
    persisted.includes(`[mcp_servers.${SERVER}.env]`) &&
    persisted.includes(`command = ${tomlString(binaryPath())}`) &&
    persisted.includes('enabled = true') &&
    persisted.includes('SHIZAI_MCP_TOOL_PROFILE = "rpa-authoring"');
  if (!verified) throw new Error('Codex MCP 配置写入后校验失败');
  const result = {
    registered: true,
    verified,
    client: 'codex',
    config,
    command: binaryPath(),
    tool_profile: 'rpa-authoring',
    restart_required: true
  };
  if (!silent) json(result);
  return result;
}
function registerJsonClient(client) {
  const locations = {
    cursor: process.env.SHIZAI_CURSOR_CONFIG || path.join(process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming'), 'Cursor', 'User', 'globalStorage', 'mcp.json'),
    claude: process.env.SHIZAI_CLAUDE_CONFIG || path.join(process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming'), 'Claude', 'claude_desktop_config.json')
  };
  const file = locations[client];
  if (!file) throw new Error(`未知客户端：${client}`);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const data = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : {};
  data.mcpServers = data.mcpServers || {};
  data.mcpServers[SERVER] = { command: binaryPath(), args: [] };
  fs.writeFileSync(file, `${JSON.stringify(data, null, 2)}\n`, 'utf8');
  json({ registered: true, client, config: file, command: binaryPath() });
}
function registerBrowser(silent = false) {
  const script = path.join(installRoot, 'scripts', 'register-browser.ps1');
  if (!fs.existsSync(script)) throw new Error('发布包中缺少 register-browser.ps1');
  const output = execFileSync('powershell.exe', [
    '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
    '-File', script, '-InstallDir', installRoot
  ], powerShellOptions()).replace(/^\uFEFF/, '').trim();
  const result = JSON.parse(output);
  if (!silent) json(result);
  return result;
}
function unregisterBrowser() {
  const script = path.join(installRoot, 'scripts', 'unregister-browser.ps1');
  if (!fs.existsSync(script)) return null;
  const output = execFileSync('powershell.exe', [
    '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
    '-File', script, '-InstallDir', installRoot
  ], powerShellOptions()).replace(/^\uFEFF/, '').trim();
  return output ? JSON.parse(output) : null;
}
function launch() {
  // stdout is the MCP protocol, so setup/progress must never be printed here.
  // Upgrades also must not stop other running MCP sessions during a new launch.
  const metadataFile = path.join(installRoot, 'version.json');
  const metadata = fs.existsSync(metadataFile) ? JSON.parse(fs.readFileSync(metadataFile, 'utf8').replace(/^\uFEFF/, '')) : {};
  if (!fs.existsSync(binaryPath()) || metadata.version !== DEFAULT_VERSION) {
    throw new Error(`请先运行 npx -y shizai-agent-mcp@${DEFAULT_VERSION} install，再启动 MCP`);
  }
  verifyPackage(installRoot, DEFAULT_VERSION);
  const child = spawn(binaryPath(), process.argv.slice(2), { stdio: 'inherit', windowsHide: true });
  child.on('error', error => fail(error.message));
  child.on('exit', code => { process.exitCode = code ?? 0; });
}
function main() {
  const [command, target, ...rest] = process.argv.slice(2);
  if (!command) return launch();
  if (command === '--help' || command === '-h') return help();
  if (command === 'install') {
    const args = [target, ...rest].filter(value => value !== undefined);
    const index = args.indexOf('--version');
    const version = index >= 0 ? args[index + 1] : args.find(value => !value.startsWith('-'));
    if (index >= 0 && !version) throw new Error('--version 后需要版本号');
    return install(version || DEFAULT_VERSION, {
      skipRegister: args.includes('--skip-register'),
      withBrowserExtension: !args.includes('--without-browser-extension')
    });
  }
  if (command === 'register') {
    if (!fs.existsSync(binaryPath())) throw new Error('尚未安装，请先运行 install');
    if (target === 'codex') return registerCodex();
    if (target === 'cursor' || target === 'claude') return registerJsonClient(target);
    if (target === 'browser') return registerBrowser();
    throw new Error('请指定 codex、cursor、claude 或 browser');
  }
  if (command === 'status') return json(installationStatus());
  if (command === 'path') return console.log(binaryPath());
  if (command === 'uninstall') {
    const destination = validateInstallRoot();
    const stoppedProcesses = stopInstalledProcesses(destination);
    const browser = unregisterBrowser();
    removeDirectoryWithRetries(destination);
    if (fs.existsSync(path.dirname(destination))) cleanupInstallArtifacts(path.dirname(destination));
    return json({ uninstalled: true, directory: destination, stopped_processes: stoppedProcesses, browser });
  }
  throw new Error(`未知命令：${command}`);
}
try { main(); } catch (error) { fail(error.message); }
