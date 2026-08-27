#define AppName "Azure DevOps Agent Dashboard"
#ifndef AppVersion
  #define AppVersion "1.0.2"
#endif
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
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"; GroupDescription: "Additional tasks:"; Flags: unchecked

[Files]
Source: "dist-release\Azure DevOps Agent Dashboard\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
