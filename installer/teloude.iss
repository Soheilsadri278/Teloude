; Inno Setup 6 script. Build with: iscc installer\teloude.iss   (after PyInstaller produced dist\Teloude)
#define AppName "Teloude"
#define AppVersion "1.0.0"
#define AppExe "Teloude.exe"

[Setup]
; Stable AppId: upgrades replace the previous install in place.
AppId={{6B1F3C52-7D0E-4E0B-9C57-3E10D0E00001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Teloude
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=Teloude-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
; Broadcasts SHCNE_ASSOCCHANGED at the end of setup so Explorer refreshes icons itself:
; no manual icon-cache clearing is needed after an upgrade.
ChangesAssociations=yes
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "Start Teloude when I sign in to Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\dist\Teloude\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\{#AppExe}"; IconIndex: 0
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\{#AppExe}"; IconIndex: 0; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExe}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

; User data (%LOCALAPPDATA%\Teloude: database, logs, protected session) is deliberately NOT removed on uninstall,
; so reinstalling keeps backup state. Nothing in [UninstallDelete] touches it.
