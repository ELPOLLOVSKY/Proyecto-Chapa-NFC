@echo off
title Compilador de Proyecto NFC a Portable (.exe)
echo =========================================================
echo   COMPILANDO INTERFAZ GRAFICA PORTABLE CON 3 SCRIPTS
echo =========================================================
echo.

pyinstaller --noconfirm --onedir --windowed --name "ControlAccesoNFC_Portable" --add-data "generar_analytics_firebase.py;." --add-data "Proyecto Chapa NFC.py;." interfaz_gui_nfc.py

echo.
echo =========================================================
echo   ¡COMPILACION PORTABLE COMPLETADA!
echo   La carpeta "dist\ControlAccesoNFC_Portable" contiene:
echo     - ControlAccesoNFC_Portable.exe (Interfaz Grafica)
echo     - Proyecto Chapa NFC.py (Motor principal)
echo     - generar_analytics_firebase.py (Analitica grafica)
echo =========================================================
pause