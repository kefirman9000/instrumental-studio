; Instrumental Studio - Windows installer (Inno Setup 6), after Studio Lab's.
; Built by .github/workflows/installers.yml:  ISCC /DAppVer=1.2.3 /DSrc=<staged app folder> instrumental-studio.iss
; The staged folder holds the program, runtime\python (a private Python with numpy, scipy and pywebview already
; installed) and bin\ffmpeg.exe. The separation model (PyTorch + Demucs) installs itself on first start, matched to
; the graphics card, into that private Python - so this download stays small.

#ifndef AppVer
  #define AppVer "0.0.0"
#endif
#ifndef Src
  #define Src "..\..\build\win\app"
#endif

[Setup]
AppId={{3E7A2C91-5B4D-4F8E-A1C6-9D2B7E40F5A3}
AppName=Instrumental Studio
AppVersion={#AppVer}
AppVerName=Instrumental Studio {#AppVer}
AppPublisher=Instrumental Studio
AppPublisherURL=https://github.com/kefirman9000/instrumental-studio
; per-user install: no admin password, and the app can update itself (and install its model) in place
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Instrumental Studio
DisableProgramGroupPage=yes
DisableDirPage=auto
UsePreviousAppDir=yes
OutputDir=..\..\dist
OutputBaseFilename=InstrumentalStudio-Setup-{#AppVer}
SetupIconFile=instrumental-studio.ico
UninstallDisplayIcon={app}\instrumental-studio.ico
UninstallDisplayName=Instrumental Studio
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Put an Instrumental Studio icon on the desktop"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#Src}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "instrumental-studio.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Instrumental Studio"; Filename: "{app}\InstrumentalStudio.exe"; WorkingDir: "{app}"; IconFilename: "{app}\instrumental-studio.ico"; Comment: "Make clean instrumentals"
Name: "{autodesktop}\Instrumental Studio"; Filename: "{app}\InstrumentalStudio.exe"; WorkingDir: "{app}"; IconFilename: "{app}\instrumental-studio.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\InstrumentalStudio.exe"; WorkingDir: "{app}"; Description: "Open Instrumental Studio now"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; program files, the model the app installed into its Python, and update leftovers.
; Your songs and exports (in your InstrumentalStudio folder) are left alone.
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\studio"
Type: filesandordirs; Name: "{app}\ui"
Type: filesandordirs; Name: "{app}\bin"
Type: filesandordirs; Name: "{app}\tests"
Type: filesandordirs; Name: "{app}\docs"
Type: filesandordirs; Name: "{app}\installer"
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\.backup"
