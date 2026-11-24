@echo off
cd /d "%~dp0..\codigo"
title Compilador de Proyecto NFC a Portable (.exe)
echo =========================================================
echo   COMPILANDO INTERFAZ GRAFICA PORTABLE CON 3 SCRIPTS
echo =========================================================
echo.

pyinstaller --noconfirm --onedir --windowed --distpath "%~dp0dist" --workpath "%~dp0build" --specpath "%~dp0" --name "ControlAccesoNFC_Portable" --add-data "generar_analytics_firebase.py;." --add-data "Proyecto Chapa NFC.py;." interfaz_gui_nfc.py

echo.
echo =========================================================
echo   ¡COMPILACION PORTABLE COMPLETADA!
echo   La carpeta "dist\ControlAccesoNFC_Portable" contiene:
echo     - ControlAccesoNFC_Portable.exe (Interfaz Grafica)
echo     - Proyecto Chapa NFC.py (Motor principal)
echo     - generar_analytics_firebase.py (Analitica grafica)
echo =========================================================
pause