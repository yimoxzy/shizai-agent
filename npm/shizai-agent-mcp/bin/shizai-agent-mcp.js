#!/usr/bin/env node

/* Lightweight distribution layer. The MCP binary remains in GitHub Releases. */
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { execFileSync, spawn } = require('node:child_process');

const SERVER = 'shizai-agent';
const REPO = process.env.SHIZAI_AGENT_REPO || 'yimoxzy/shizai-agent';
const DEFAULT_VERSION = process.env.SHIZAI_AGENT_VERSION || '0.7.27';
const installRoot = process.env.SHIZAI_AGENT_HOME || path.join(
  process.env.LOCALAPPDATA || path.join(os.homedir(), '.local'), 'Programs', 'ShizaiAgent'
);

function fail(message) { console.error(`shizai-agent-mcp: ${message}`); process.exitCode = 1; }
function json(value) { console.log(JSON.stringify(value, null, 2)); }
function help() {
  console.log(`Shizai Agent MCP installer\n\nUsage:\n  shizai-agent-mcp install [--version VERSION]\n  shizai-agent-mcp register <codex|cursor|claude>\n  shizai-agent-mcp status\n  shizai-agent-mcp path\n  shizai-agent-mcp uninstall\n\nEnvironment:\n  SHIZAI_AGENT_VERSION  Release version (default: ${DEFAULT_VERSION})\n  SHIZAI_AGENT_HOME     Installation directory\n  SHIZAI_AGENT_REPO     GitHub repository owner/name\n  SHIZAI_AGENT_RELEASE_BASE_URL  Override release asset base URL\n`);
}
function binaryPath() { return path.join(installRoot, 'shizai-agent-mcp.exe'); }
function validateInstallRoot() {
  const resolved = path.resolve(installRoot);
  const parsed = path.parse(resolved);
  if (resolved === parsed.root || resolved === path.resolve(os.homedir())) {
    throw new Error(`拒绝使用过宽的安装目录：${resolved}`);
  }
  return resolved;
}
function psQuote(value) { return `'${String(value).replaceAll("'", "''")}'`; }
function runPowerShell(script, args = []) {
  return execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script, ...args], { encoding: 'utf8' });
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
    path.join(destination, 'shizai-agent.exe')
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
  const script = `Invoke-WebRequest -UseBasicParsing -Uri ${psQuote(url)} -OutFile ${psQuote(destination)} -ProgressAction SilentlyContinue`;
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
    fs.writeFileSync(path.join(source, 'version.json'), JSON.stringify({ version, asset, installedAt: new Date().toISOString() }, null, 2));
    cleanupInstallArtifacts(parent, [stage, backup]);
    for (const processId of stopInstalledProcesses(destination)) stoppedProcesses.add(processId);
    let previousMoved = false;
    try {
      if (fs.existsSync(destination)) {
        fs.renameSync(destination, backup);
        previousMoved = true;
        for (const processId of stopInstalledProcesses(destination)) stoppedProcesses.add(processId);
      }
      fs.renameSync(source, destination);
    } catch (error) {
      if (!fs.existsSync(destination) && previousMoved && fs.existsSync(backup)) {
        fs.renameSync(backup, destination);
      }
      throw error;
    }
    if (previousMoved) {
      try {
        removeDirectoryWithRetries(backup);
      } catch (error) {
        for (const processId of stopInstalledProcesses(destination)) stoppedProcesses.add(processId);
        try {
          removeDirectoryWithRetries(backup);
        } catch (retryError) {
          const failedInstall = path.join(temp, 'failed-install');
          fs.renameSync(destination, failedInstall);
          fs.renameSync(backup, destination);
          removeDirectoryWithRetries(failedInstall);
          throw new Error(`旧版本清理失败，已恢复原安装：${retryError.message}`);
        }
      }
    }
    cleanupInstallArtifacts(parent, [stage, backup]);
    const result = { installed: true, version, directory: destination, command: binaryPath(), verified, stopped_processes: [...stoppedProcesses].sort((left, right) => left - right) };
    if (!options.skipRegister) result.registration = registerCodex(true);
    console.log(`安装成功：Shizai Agent MCP v${version}`);
    if (result.registration?.registered) console.log('Codex MCP 已注册');
    return result;
  } finally {
    removeDirectoryWithRetries(temp);
    removeDirectoryWithRetries(stage);
    if (fs.existsSync(backup) && fs.existsSync(destination)) removeDirectoryWithRetries(backup);
  }
}
function replaceTomlSection(content, section, replacement) {
  const header = `[${section}]`;
  const lines = String(content || '').split(/\r?\n/);
  const kept = [];
  let skipping = false;
  for (const line of lines) {
    if (line.trim() === header) {
      skipping = true;
      continue;
    }
    if (skipping && /^\s*\[.*\]\s*$/.test(line)) skipping = false;
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
  const section = `[mcp_servers.${SERVER}]\ncommand = ${tomlString(binaryPath())}\nargs = []\nstartup_timeout_sec = 30\ntool_timeout_sec = 120\nomit_tools_from = [\"deferred\"]`;
  const next = replaceTomlSection(old, `mcp_servers.${SERVER}`, section);
  fs.writeFileSync(config, next, 'utf8');
  const result = { registered: true, client: 'codex', config, command: binaryPath() };
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
function launch() {
  if (!fs.existsSync(binaryPath())) install(DEFAULT_VERSION);
  const child = spawn(binaryPath(), process.argv.slice(2), { stdio: 'inherit', windowsHide: true });
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
    return install(version || DEFAULT_VERSION, { skipRegister: args.includes('--skip-register') });
  }
  if (command === 'register') {
    if (!fs.existsSync(binaryPath())) throw new Error('尚未安装，请先运行 install');
    if (target === 'codex') return registerCodex();
    if (target === 'cursor' || target === 'claude') return registerJsonClient(target);
    throw new Error('请指定 codex、cursor 或 claude');
  }
  if (command === 'status') return json({ installed: fs.existsSync(binaryPath()), directory: installRoot, command: binaryPath(), versionFile: path.join(installRoot, 'version.json') });
  if (command === 'path') return console.log(binaryPath());
  if (command === 'uninstall') {
    const destination = validateInstallRoot();
    const stoppedProcesses = stopInstalledProcesses(destination);
    removeDirectoryWithRetries(destination);
    if (fs.existsSync(path.dirname(destination))) cleanupInstallArtifacts(path.dirname(destination));
    return json({ uninstalled: true, directory: destination, stopped_processes: stoppedProcesses });
  }
  throw new Error(`未知命令：${command}`);
}
try { main(); } catch (error) { fail(error.message); }
