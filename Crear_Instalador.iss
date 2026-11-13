[Setup]
AppName=Control de Acceso NFC IoT
AppVersion=2.0
AppPublisher=DirtyCutz Beatz / INACAP
DefaultDirName={autopf}\ControlAccesoNFC
DefaultGroupName=Control de Acceso NFC
OutputBaseFilename=Instalador_ControlAcceso_NFC
OutputDir=.\Instalador_Final
Compression=lzma2/ultra64
SolidCompression=yes
UninstallDisplayIcon={app}\Motor_Chapa_NFC.exe
PrivilegesRequired=admin

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el Escritorio"; GroupDescription: "Accesos directos:"; Flags: checked

[Files]
Source: "dist\Motor_Chapa_NFC\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Motor Principal Chapa NFC"; Filename: "{app}\Motor_Chapa_NFC.exe"
Name: "{group}\Levantar Servidor Web (App)"; Filename: "{app}\app.py"
Name: "{group}\Generar Graficos Analytics"; Filename: "{app}\generar_analytics_firebase.py"
Name: "{autodesktop}\Motor Principal Chapa NFC"; Filename: "{app}\Motor_Chapa_NFC.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Motor_Chapa_NFC.exe"; Description: "Iniciar Motor de Terminal NFC"; Flags: nowait postinstall skipifsilent