; Inno Setup — установщик «Полировщик клипов».
; Пакует dist\ClipPolisher (PyInstaller onedir) в один Setup.exe.
; Модель Whisper и CUDA-библиотеки НЕ входят — докачиваются при первом запуске.

#define MyAppName "Полировщик клипов"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "GoLsik"
#define MyAppExeName "ClipPolisher.exe"

[Setup]
AppId={{9F1B7C2E-4A3D-4E2B-9C7A-CLIPPOLISHER01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Установка без прав администратора — в локальную папку пользователя (нет UAC).
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\ClipPolisher
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=ClipPolisher-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#MyAppName}
; На первом запуске приложение докачает ~5 ГБ (модель + GPU-библиотеки).

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
Source: "..\dist\ClipPolisher\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Удалить {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
