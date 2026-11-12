; StellAI Server for Windows: Inno Setup script (built by .github/workflows/windows.yml)
; The app itself is small; models and engines download on first run into %LOCALAPPDATA%\StellAI.

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{6B0E4C5A-8F2D-4C1B-9E7A-57E11A1A0001}
AppName=StellAI Server
AppVersion={#AppVersion}
AppVerName=StellAI Server {#AppVersion} (preview)
AppPublisher=StellAI
; Per-user install (in %LOCALAPPDATA%\Programs): updates install silently, with no admin prompt.
DefaultDirName={autopf}\StellAI
DefaultGroupName=StellAI
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=StellAI-Setup-{#AppVersion}
SetupIconFile=stellai.ico
UninstallDisplayIcon={app}\StellAI.exe
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
PrivilegesRequired=lowest
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "Start StellAI with Windows (runs in the tray)"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\dist\StellAI\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\StellAI Server"; Filename: "{app}\StellAI.exe"
Name: "{group}\Uninstall StellAI"; Filename: "{uninstallexe}"
Name: "{autodesktop}\StellAI Server"; Filename: "{app}\StellAI.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "StellAI"; \
  ValueData: """{app}\StellAI.exe"" --minimized"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\StellAI.exe"; Description: "Start StellAI Server now"; Flags: nowait postinstall skipifsilent
; Automatic updates run the installer silently; start the app again afterwards
Filename: "{app}\StellAI.exe"; Parameters: "--after-update"; Flags: nowait; Check: WizardSilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im StellAI.exe"; Flags: runhidden; RunOnceId: "StellAIKill"
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im llama-server.exe"; Flags: runhidden; RunOnceId: "StellAIKillLlama"
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im whisper-server.exe"; Flags: runhidden; RunOnceId: "StellAIKillWhisper"

[Code]
const
  RuleArgs = 'advfirewall firewall add rule name="StellAI" dir=in action=allow protocol=TCP localport=8080,8092 profile=private,domain remoteip=LocalSubnet,100.64.0.0/10';

function FirewallRuleExists(): Boolean;
var
  Code: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\netsh.exe'), 'advfirewall firewall show rule name="StellAI"', '',
                 SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

{ Lets phones on your home network and on Tailscale (100.64.0.0/10) reach Stella, and nobody else:
  8080 = AI, 8092 = voice/sync. An older, wider rule is tightened from the app (Options -> Limit firewall).
  Needs admin once, so Windows asks on the first install only; updates skip this. }
procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
begin
  if (CurStep = ssPostInstall) and (not FirewallRuleExists()) then
    if not ShellExec('runas', ExpandConstant('{sys}\netsh.exe'), RuleArgs, '', SW_HIDE, ewWaitUntilTerminated, Code) then
      if not WizardSilent() then
        MsgBox('The firewall rule for your phone wasn''t added, so phones may not be able to connect.' + #13#10 +
               'Reinstall and allow the administrator prompt, or allow StellAI when Windows asks.', mbInformation, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Dir: String;
  Code: Integer;
begin
  if CurUninstallStep = usUninstall then
    ShellExec('runas', ExpandConstant('{sys}\netsh.exe'), 'advfirewall firewall delete rule name="StellAI"', '',
              SW_HIDE, ewWaitUntilTerminated, Code);
  if CurUninstallStep = usPostUninstall then
  begin
    Dir := ExpandConstant('{localappdata}\StellAI');
    if DirExists(Dir) then
      if MsgBox('Also delete downloaded models, chats and memories in' + #13#10 + Dir + '?' + #13#10#13#10 +
                'Choose No to keep them for a later reinstall.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Dir, True, True, True);
  end;
end;
