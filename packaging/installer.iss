; Pacer installer: per user (no admin), installs the PyInstaller folder, Start menu entry, optional start with Windows.
; User data (settings, logs) lives outside the install folder and survives uninstall.
#define AppName "Pacer"
#define AppVersion "1.0.0"

[Setup]
AppId={{6F0A8D55-5B7E-4C2B-9E0B-3A6D1E5C9A11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Pacer
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=Pacer-Setup
SetupIconFile=pacer.ico
UninstallDisplayIcon={app}\Pacer.exe
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "autostart"; Description: "Start Pacer with Windows"; Flags: checkedonce

[Files]
Source: "dist\Pacer\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\Pacer.exe"; Parameters: "launch"; IconFilename: "{app}\Pacer.exe"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Pacer"; \
    ValueData: """{app}\Pacer.exe"" tray"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\Pacer.exe"; Parameters: "launch"; Description: "Start Pacer"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/im Pacer.exe /f"; Flags: runhidden; RunOnceId: "KillPacer"
