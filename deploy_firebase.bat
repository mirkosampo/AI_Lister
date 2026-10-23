@echo off
rem Pubblica il sito su Firebase Hosting e aggiorna le regole di Firestore (vedi docs/FIREBASE.md)
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Avvia prima start.bat per creare l'ambiente Python.
    pause
    exit /b 1
)
.venv\Scripts\python.exe scripts\deploy_firebase.py %*
pause
