@echo off
title Sistema Control de Acceso NFC
echo Iniciando Servidor Web (app.py)...
start cmd /k "python app.py"
timeout /t 2 >nul
echo Iniciando Motor Principal (Proyecto Chapa NFC.py)...
start cmd /k "python "Proyecto Chapa NFC.py"
exit