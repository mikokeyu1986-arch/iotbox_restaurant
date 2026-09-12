#define AppName "IOTBOX"
#define AppVersion "2026.08.21"
#define AppPublisher "IOTBOX"
#define AppExeName "gui_app.exe"

[Setup]
AppId={{B7C9C4E7-7E1D-4F47-9A0F-IOTBOX2026}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\IOTBOX
DefaultGroupName=IOTBOX
OutputDir=..\release
OutputBaseFilename=IOTBOX-SETUP-2026.08.21
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
CloseApplications=no
RestartApplications=no
UninstallDisplayIcon={app}\gui_app.exe
SetupIconFile=..\assets\iotbox-icon.ico

[Files]
Source: "..\dist\gui_app\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "..\dist\run_https\*"; DestDir: "{app}\runtime"; Flags: recursesubdirs ignoreversion
Source: "..\dist\redsys_service\*"; DestDir: "{app}\runtime\redsys_service"; Flags: recursesubdirs ignoreversion
Source: "..\redsys\config.yaml"; DestDir: "{app}\redsys"; Flags: onlyifdoesntexist ignoreversion
; REDSYS resolves these paths relative to config.yaml.  Keep the vendor
; runtime separate from the PyInstaller service runtime above.
Source: "..\redsys\server\redsys_server\bridge\*"; DestDir: "{app}\redsys\server\redsys_server\bridge"; Flags: recursesubdirs ignoreversion
Source: "..\redsys\lib\*"; DestDir: "{app}\redsys\lib"; Flags: recursesubdirs ignoreversion
Source: "..\redsys\img\*"; DestDir: "{app}\redsys\img"; Flags: recursesubdirs ignoreversion
Source: "..\redsys\_internal\Py310Host.exe"; DestDir: "{app}\redsys"; Flags: ignoreversion
Source: "..\redsys\_internal\runtime\*"; DestDir: "{app}\redsys\runtime"; Flags: recursesubdirs ignoreversion
Source: "..\redsys\_internal\vendor\*"; DestDir: "{app}\redsys\vendor"; Flags: recursesubdirs ignoreversion
Source: "..\assets\iotbox-icon.ico"; DestDir: "{app}"; Flags: ignoreversion
; Never ship a paired development configuration. Each machine starts from the
; blank template and generates its own stable IOTBOX identifier on first run.
Source: "..\runtime_config.example.json"; DestDir: "{app}"; DestName: "runtime_config.json"; Flags: onlyifdoesntexist ignoreversion

[Icons]
Name: "{group}\IOTBOX"; Filename: "{app}\gui_app.exe"; IconFilename: "{app}\iotbox-icon.ico"
Name: "{userdesktop}\IOTBOX"; Filename: "{app}\gui_app.exe"; IconFilename: "{app}\iotbox-icon.ico"

[Run]
Filename: "{app}\gui_app.exe"; Description: "启动 IOTBOX"; Flags: nowait postinstall skipifsilent

[Code]
procedure MigratePersistentData();
var
  OldInternal, PersistentConfig, OldCerts, PersistentCerts: String;
begin
  { Older releases stored mutable state inside PyInstaller's _internal
    directory, which is replaced by each upgrade. Copy it once before files
    are updated; existing root-level settings always take precedence. }
  OldInternal := ExpandConstant('{app}\_internal');
  PersistentConfig := ExpandConstant('{app}\runtime_config.json');
  if (not FileExists(PersistentConfig)) and FileExists(AddBackslash(OldInternal) + 'runtime_config.json') then
    FileCopy(AddBackslash(OldInternal) + 'runtime_config.json', PersistentConfig, False);

  OldCerts := AddBackslash(OldInternal) + 'certs';
  PersistentCerts := ExpandConstant('{app}\certs');
  if DirExists(OldCerts) and (not DirExists(PersistentCerts)) then begin
    ForceDirectories(PersistentCerts);
    if FileExists(AddBackslash(OldCerts) + 'iotbox.key') then
      FileCopy(AddBackslash(OldCerts) + 'iotbox.key', AddBackslash(PersistentCerts) + 'iotbox.key', False);
    if FileExists(AddBackslash(OldCerts) + 'iotbox.crt') then
      FileCopy(AddBackslash(OldCerts) + 'iotbox.crt', AddBackslash(PersistentCerts) + 'iotbox.crt', False);
    if FileExists(AddBackslash(OldCerts) + 'iotbox.p12') then
      FileCopy(AddBackslash(OldCerts) + 'iotbox.p12', AddBackslash(PersistentCerts) + 'iotbox.p12', False);
    if FileExists(AddBackslash(OldCerts) + '.p12_password') then
      FileCopy(AddBackslash(OldCerts) + '.p12_password', AddBackslash(PersistentCerts) + '.p12_password', False);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  MigratePersistentData();
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM gui_app.exe', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM run_https.exe', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM customer_display_app.exe', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Sleep(800);
  Result := '';
end;
