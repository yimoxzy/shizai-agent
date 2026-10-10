#define MyAppName "Shizai Agent"
#define MyAppVersion "0.7.28"
#define MyAppPublisher "Shizai Agent"
#define MyAppExeName "shizai-agent.exe"

[Setup]
AppId={{A203C67C-11D0-4CD4-86A6-A741E258F92E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\ShizaiAgent
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
OutputDir=output
OutputBaseFilename=ShizaiAgentSetup-v{#MyAppVersion}
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
CloseApplicationsFilter=Codex.exe,shizai-agent-mcp.exe,shizai-agent-browser-host.exe
RestartApplications=no

[Files]
Source: "dist\shizai-agent.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\shizai-agent-mcp.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\shizai-agent-browser-host.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\browser_extension\*"; DestDir: "{app}\browser-extension"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "scripts\register-codex.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "scripts\unregister-codex.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "scripts\register-browser.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "scripts\unregister-browser.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion

[Tasks]
Name: "browserextension"; Description: "Prepare the optional MCP browser extension and Native Messaging host"; Flags: unchecked

[Icons]
Name: "{group}\Check Shizai Agent status"; Filename: "{app}\shizai-agent.exe"; Parameters: "--json agent auth-status"
Name: "{group}\Uninstall Shizai Agent"; Filename: "{uninstallexe}"

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\register-codex.ps1"" -McpPath ""{app}\shizai-agent-mcp.exe"""; StatusMsg: "Registering Shizai Agent with Codex..."; Flags: runhidden waituntilterminated
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\register-browser.ps1"" -InstallDir ""{app}"""; StatusMsg: "Preparing MCP browser extension..."; Flags: runhidden waituntilterminated; Tasks: browserextension
Filename: "{app}\shizai-agent.exe"; Parameters: "--json agent auth-status"; Description: "Check Shizai designer login status"; Flags: postinstall nowait skipifsilent unchecked

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\unregister-codex.ps1"""; Flags: runhidden waituntilterminated; RunOnceId: "ShizaiAgentUnregister"
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\unregister-browser.ps1"" -InstallDir ""{app}"""; Flags: runhidden waituntilterminated; RunOnceId: "ShizaiAgentBrowserUnregister"

[Code]
procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
    WizardForm.FinishedLabel.Caption :=
      'Shizai Agent has been installed and registered with Codex.' + #13#10 + #13#10 +
      'Restart Codex, enter /mcp, and use shizai-agent with natural language.' + #13#10 +
      'Auth: Unsupported is normal: authentication comes from the signed-in Shizai designer.';
end;
