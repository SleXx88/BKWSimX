; --- BKWSimX.iss ---
#define MyAppName    "BKWSimX"
#define MyAppVersion "0.1.2"
#define SourceDir    "build\\exe.win-amd64-3.12"  ; Pfad zum cx_Freeze-Output

[Setup]
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={pf}\{#MyAppName}
; Deutsch als einzige Sprache, kein Auswahl-Dialog
ShowLanguageDialog=no
LanguageDetectionMethod=none
DisableWelcomePage=yes
AllowNoIcons=yes
LicenseFile=src\LICENSE.rtf

OutputBaseFilename=BKWSimX_Installer_{#MyAppVersion}

SetupIconFile=src\icons\icon.ico
UninstallDisplayIcon={app}\icon.ico

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppName}.exe"
Name: "{userdesktop}\{#MyAppName}";   Filename: "{app}\{#MyAppName}.exe"

[Run]
Filename: "{app}\{#MyAppName}.exe"; Description: "Starte {#MyAppName}"; Flags: nowait postinstall skipifsilent
