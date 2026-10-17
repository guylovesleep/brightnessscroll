@echo off
rem ===================================================================
rem  Brightness Scroll - launcher
rem  Double-click this file to start the app with no console window.
rem ===================================================================
setlocal
set "SCRIPT=%~dp0brightness_scroll.pyw"

if not exist "%SCRIPT%" (
    echo ERROR: brightness_scroll.pyw was not found next to this launcher.
    echo Expected at: %SCRIPT%
    pause
    exit /b 1
)

rem Preferred: the Windows Python launcher (always resolves to the
rem newest installed Python, and runs windowed with no console).
where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw "%SCRIPT%" %*
    exit /b 0
)

rem Fallback: any pythonw.exe on PATH.
for /f "delims=" %%P in ('where pythonw 2^>nul') do (
    start "" "%%P" "%SCRIPT%" %*
    exit /b 0
)

echo ERROR: Python 3 was not found.
echo Install it from https://www.python.org/downloads/ and tick
echo "Add python.exe to PATH", then run this launcher again.
pause
exit /b 1
