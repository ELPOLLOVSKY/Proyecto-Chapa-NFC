@echo off
title Sistema Control de Acceso NFC
rem El motor inicia y vigila el servidor web (app.py) por su cuenta
cd /d "%~dp0Sistema_NFC\codigo"
echo Iniciando Motor Principal (Proyecto Chapa NFC.py)...
start cmd /k "python "Proyecto Chapa NFC.py""
exit
