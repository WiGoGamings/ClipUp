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
Name: "iatask"; Description: "Instalar la IA local (Ollama + modelo, unos 5 GB, necesita internet)"

[Files]
Source: "dist\ClipAgentes\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "instalar-ia.bat"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Clip Agentes"; Filename: "{app}\ClipAgentes.exe"
Name: "{autoprograms}\Instalar la IA de Clip Agentes"; Filename: "{app}\instalar-ia.bat"
Name: "{autodesktop}\Clip Agentes"; Filename: "{app}\ClipAgentes.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\instalar-ia.bat"; Description: "Instalando la IA local"; Flags: waituntilterminated; Tasks: iatask
Filename: "{app}\ClipAgentes.exe"; Description: "Abrir Clip Agentes"; Flags: nowait postinstall skipifsilent
