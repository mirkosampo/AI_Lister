@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creo l'ambiente Python...
    where uv >nul 2>nul
    if %errorlevel%==0 (
        uv venv .venv --python 3.11 || goto :error
        uv pip install --python .venv\Scripts\python.exe -r requirements.txt || goto :error
    ) else (
        python -m venv .venv || goto :error
        .venv\Scripts\python.exe -m pip install -r requirements.txt || goto :error
    )
)

set PORT=8765
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
