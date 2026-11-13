@echo off
title Generador del Instalador Unico .EXE - Control NFC
color 0A

:: Esto asegura que la consola no se pierda en System32, ¡obligatorio!
cd /d "%~dp0"

echo ====================================================================
echo    PASO 1: VERIFICANDO E INSTALANDO DEPENDENCIAS EN PYTHON
echo ====================================================================
echo Directorio actual: %CD%
echo.

echo 📦 Instalando PyInstaller, PySerial, Requests, Pandas, Matplotlib, Flask...
python -m pip install --quiet pyinstaller pyserial requests pandas matplotlib flask pyttsx3

if %errorlevel% neq 0 (
    echo ⚠️ Forzando instalacion directa...
    "C:\Python314\python.exe" -m pip install pyinstaller pyserial requests pandas matplotlib flask pyttsx3
)

echo.
echo ====================================================================
echo    PASO 2: COMPILANDO PROYECTO PRINCIPAL CON PYINSTALLER
echo ====================================================================
echo.

:: Nota: Compilamos "--console" para que muestre la terminal negra del motor principal
python -m PyInstaller --noconfirm --onedir --console --name "Motor_Chapa_NFC" --add-data "generar_analytics_firebase.py;." --add-data "app.py;." --add-data "templates;templates" "Proyecto Chapa NFC.py"

if %errorlevel% neq 0 (
    echo ⚠️ Reintentando compilacion forzada...
    "C:\Python314\python.exe" -m PyInstaller --noconfirm --onedir --console --name "Motor_Chapa_NFC" --add-data "generar_analytics_firebase.py;." --add-data "app.py;." --add-data "templates;templates" "Proyecto Chapa NFC.py"
)

if %errorlevel% neq 0 (
    echo.
    echo ❌ Fallo la compilacion. Revisa los errores arriba.
    pause
    exit /b
)

echo.
echo ====================================================================
echo    PASO 3: EMPAQUETANDO TODO EN UN SOLO INSTALADOR (INNO SETUP)
echo ====================================================================
echo.

set INNO_PATH="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist %INNO_PATH% (
    set INNO_PATH="C:\Program Files\Inno Setup 6\ISCC.exe"
)

if exist %INNO_PATH% (
    %INNO_PATH% "Crear_Instalador.iss"
    echo.
    echo ====================================================================
    echo 🚀 ¡CORONASTE! INSTALADOR CREADO CON EXITO.
    echo    Revisa la carpeta: .\Instalador_Final\
    echo ====================================================================
) else (
    echo ⚠️ Loco, Inno Setup no esta instalado.
    echo    Descargalo de: https://jrsoftware.org/isdl.php
    echo    Pero tranquilo, tus archivos portables estan listos en: dist\Motor_Chapa_NFC
)

pause