; Basilisk's Windows installer (INSTALL.md, section 11). Built by
; tools/windows/build.py --installer, which passes the defines below.
;
; Per-user, no administrator rights: installs to
; %LOCALAPPDATA%\Programs\Basilisk. Data (SQLite, keys, logs) lives apart in
; %LOCALAPPDATA%\Basilisk, so upgrades keep it; uninstall asks about it.

#ifndef AppVersion
  #error Pass /DAppVersion=<version> (build.py does)
#endif
#ifndef BundleDir
  #error Pass /DBundleDir=<build\windows\bundle> (build.py does)
#endif
#ifndef OutputDir
  #define OutputDir "dist"
#endif
#ifndef IconFile
  #define IconFile "basilisk.ico"
#endif

[Setup]
; Never change AppId: Windows matches upgrades and the uninstaller by it.
AppId={{DACCEC39-8172-4C0A-A4D6-3FBB4E6E1CA9}
AppName=Basilisk
AppVersion={#AppVersion}
AppVerName=Basilisk {#AppVersion}
AppPublisher=OldManGan
AppPublisherURL=https://github.com/El-Iguana/basilisk_wapyt
DefaultDirName={localappdata}\Programs\Basilisk
DefaultGroupName=Basilisk
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=Basilisk-{#AppVersion}-setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\app\basilisk.ico
UninstallDisplayName=Basilisk
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
LicenseFile={#BundleDir}\app\LICENSE

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start Basilisk when I sign in (in the tray, without opening the browser)"; GroupDescription: "Startup:"; Flags: unchecked

[InstallDelete]
; An upgrade replaces the runtime and the app wholesale, so a package or
; module dropped by the new version does not linger from the old one.
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\app"

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Basilisk"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\basilisk_launcher.py"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\basilisk.ico"; Comment: "SQL database browser in your browser"
Name: "{group}\Reset Basilisk password"; Filename: "{app}\Reset Basilisk password.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\app\basilisk.ico"
Name: "{group}\Uninstall Basilisk"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Basilisk"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\basilisk_launcher.py"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\basilisk.ico"; Tasks: desktopicon
Name: "{userstartup}\Basilisk"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\basilisk_launcher.py"" --no-browser"; WorkingDir: "{app}\app"; IconFilename: "{app}\app\basilisk.ico"; Tasks: startup

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\basilisk_launcher.py"""; WorkingDir: "{app}\app"; Description: "Start Basilisk now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; A running server holds files open under {app}; stop it first.
Filename: "{app}\python\python.exe"; Parameters: """{app}\app\basilisk_launcher.py"" --stop"; WorkingDir: "{app}\app"; Flags: runhidden waituntilterminated; RunOnceId: "StopBasilisk"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\app\__pycache__"
Type: filesandordirs; Name: "{app}\app\appcode"
Type: filesandordirs; Name: "{app}\python"

[Code]
const
  DataDirName = 'Basilisk';

// Stop a running Basilisk before files are replaced during an upgrade.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Launcher, Python: String;
  ResultCode: Integer;
begin
  Result := '';
  Launcher := ExpandConstant('{app}\app\basilisk_launcher.py');
  Python := ExpandConstant('{app}\python\python.exe');
  if FileExists(Launcher) and FileExists(Python) then
    Exec(Python, '"' + Launcher + '" --stop', ExpandConstant('{app}\app'),
         SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

// Saved connections, accounts and the key that decrypts their passwords live
// outside {app}. Keep them unless the person says otherwise; a silent
// uninstall always keeps them.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  DataDir := ExpandConstant('{localappdata}\' + DataDirName);
  if not DirExists(DataDir) or UninstallSilent then
    exit;
  if MsgBox('Also delete your Basilisk data?' + #13#10 + #13#10 +
            'This removes your accounts, saved connections, the key that ' +
            'decrypts their passwords, and every SQLite and DuckDB file in ' +
            'its databases folder:' + #13#10 + DataDir,
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    DelTree(DataDir, True, True, True);
end;
