#define AppName "Azure DevOps Agent Dashboard"
#define AppVersion "1.0.1"
#define AppPublisher "Azure DevOps Agent Dashboard"
#define AppExeName "Azure DevOps Agent Dashboard.exe"

[Setup]
AppId={{41F6FAD6-A3E8-47EA-974A-0569C55E4D30}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=installer-output
OutputBaseFilename=Azure-DevOps-Agent-Dashboard-Setup-{#AppVersion}
SetupIconFile=dashboard.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"

[Tasks]
Name: "desktopicon"; Description: "Crea un'icona sul desktop"; GroupDescription: "Scelte aggiuntive:"; Flags: unchecked

[Files]
Source: "dist-release\Azure DevOps Agent Dashboard\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Avvia {#AppName}"; Flags: nowait postinstall skipifsilent
