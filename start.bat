@echo off
setlocal
cd /d "%~dp0"

set PORT=8765

rem Se AI Lister e' gia' acceso (anche una versione precedente) lo chiude, cosi' parte il codice aggiornato.
rem Va fatto prima di aggiornare le dipendenze: su Windows non si sostituiscono librerie in uso.
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\stop_previous.ps1" -Port %PORT%
if %errorlevel%==2 (
    echo Lascio acceso AI Lister com'e'.
    start "" http://localhost:%PORT%
    goto :eof
)
if %errorlevel%==1 (
    pause
    goto :eof
)

where uv >nul 2>nul
if %errorlevel%==0 (set USE_UV=1) else (set USE_UV=0)

if not exist ".venv\Scripts\python.exe" (
    echo Creo l'ambiente Python...
    if %USE_UV%==1 (
        uv venv .venv --python 3.11 || goto :error
    ) else (
        python -m venv .venv || goto :error
    )
)

rem Installa o aggiorna le dipendenze (veloce se sono gia' presenti)
echo Controllo le dipendenze...
if %USE_UV%==1 (
    uv pip install --quiet --python .venv\Scripts\python.exe -r requirements.txt || goto :error
) else (
    .venv\Scripts\python.exe -m pip install --quiet --disable-pip-version-check -r requirements.txt || goto :error
)

echo.
echo  AI Lister in esecuzione su http://localhost:%PORT%
echo  (chiudi questa finestra o premi CTRL+C per fermarlo)
echo.
start "" http://localhost:%PORT%
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
goto :eof

:error
echo Installazione non riuscita.
pause
