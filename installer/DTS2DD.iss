; Inno Setup script for DTS to AC-3 Converter (Windows installer)
;
; Packages the PyInstaller one-folder build (dist\DTS2DD) plus the bundled
; tools. Expected layout before compiling (build_windows.bat / CI create it):
;
;   dist\DTS2DD\DTS to AC-3 Converter.exe, _internal\...   (PyInstaller)
;   dist\DTS2DD\ffmpeg\ffmpeg.exe, ffprobe.exe              (standard FFmpeg)
;   dist\DTS2DD\librempeg\ffmpeg.exe, ffprobe.exe           (LibreMPEG, TrueHD)
;   dist\DTS2DD\tsmuxer\tsMuxeR.exe                         (tsMuxeR, M2TS remux)
;   dist\DTS2DD\licenses\...                                (third-party licences)
;
; Compile:  iscc /DAppVersion=1.1.0 installer\DTS2DD.iss
; Output:   dist\installer\DTS2DD-Setup-<version>.exe
;
; Installs per user (no administrator prompt) to
; %LOCALAPPDATA%\Programs\DTS2DD, adds Start menu and desktop shortcuts and an
; uninstaller, and offers to launch the app at the end.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "DTS to AC-3 Converter"
#define AppExe "DTS to AC-3 Converter.exe"
#define SourceDir "..\dist\DTS2DD"

[Setup]
AppId={{6B3E6F0A-2C7D-4E59-9C1B-8F2D7A41D2C5}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Simon Woollams
AppPublisherURL=https://github.com/tjsDangermouse/DTS2DD
AppSupportURL=https://github.com/tjsDangermouse/DTS2DD/issues
DefaultDirName={autopf}\DTS2DD
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Keep the wizard to "Install" then "Finish"
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=no
; Close a running copy before upgrading
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
SetupIconFile=..\icons\converter_icon.ico
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
OutputDir=..\dist\installer
OutputBaseFilename=DTS2DD-Setup-{#AppVersion}

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Remove files from an older version that are no longer shipped
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autoprograms}\{#AppName} licences"; Filename: "{app}\licenses"

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; FFmpeg downloaded at runtime (only if the bundled copy was missing).
; Settings in %APPDATA%\DTS2DD are kept so a reinstall remembers them.
Type: filesandordirs; Name: "{localappdata}\DTS2DD\ffmpeg"
