; installer.iss
; --------------
; Inno Setup script that wraps the PyInstaller-built ContainerExplorer.exe
; into a proper Windows installer: a Next/Next/Install wizard, Start Menu
; + Desktop shortcuts, and an uninstaller - so the end user's entire
; "install" experience is: download one file, double-click, click through
; a few screens, done. No Python, no pip, no terminal.
;
; HOW TO USE:
;   1. Build the .exe first (see BUILD.md / the GitHub Actions workflow):
;        pyinstaller --onefile --windowed --name ContainerExplorer gui_explorer.py
;      This produces dist\ContainerExplorer.exe
;   2. Install Inno Setup (https://jrsoftware.org/isinfo.php) - free.
;   3. Compile this script: iscc installer.iss
;      Output: Output\ContainerExplorer-Setup.exe  <-- this is what you
;      upload to GitHub Releases / hand to the end user.
;
; The AppVersion below should be kept in sync with version.py's
; __version__ - the GitHub Actions workflow does this automatically by
; passing /DMyAppVersion=... on the command line, overriding the
; fallback default here.

#ifndef MyAppVersion
  #define MyAppVersion "1.0.2"
#endif

#define MyAppName "ContainerExplorer"
#define MyAppPublisher "RajWorks"
#define MyAppExeName "ContainerExplorer.exe"

[Setup]
AppId={{B6C1F0C4-9B7A-4B7E-9F0D-1234567890AB}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=ContainerExplorer-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Per-user install (no admin prompt) is friendlier for non-technical end
; users who may not have admin rights on a work laptop:
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

