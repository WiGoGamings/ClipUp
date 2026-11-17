#ifndef Version
  #define Version "1.0.0"
#endif

[Setup]
AppId={{6F0B7E1A-4C5D-4E8B-9A57-C110A9E27A50}
AppName=Clip Agentes
AppVersion={#Version}
AppPublisher=WiGoGamings
DefaultDirName={autopf}\Clip Agentes
DefaultGroupName=Clip Agentes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=salida
OutputBaseFilename=ClipAgentes-Setup
SetupIconFile=icono.ico
UninstallDisplayIcon={app}\ClipAgentes.exe
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el escritorio"; Flags: unchecked

[Files]
Source: "dist\ClipAgentes\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Clip Agentes"; Filename: "{app}\ClipAgentes.exe"
Name: "{autodesktop}\Clip Agentes"; Filename: "{app}\ClipAgentes.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ClipAgentes.exe"; Description: "Abrir Clip Agentes"; Flags: nowait postinstall skipifsilent
