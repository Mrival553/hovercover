@echo off
rem ============================================================
rem  Simple Read-Only File Manager - launcher (double click me)
rem  It never deletes, moves, renames or modifies your files.
rem  NOTE: keep this file ASCII + CRLF, cmd.exe mis-parses
rem        UTF-8 / LF-only batch files (Chinese text broke it).
rem ============================================================
setlocal
cd /d "%~dp0"
set "APP=%~dp0file_manager.py"

where pythonw.exe >nul 2>nul && goto :pythonw
where pyw.exe     >nul 2>nul && goto :pyw
where python.exe  >nul 2>nul && goto :python
where py.exe      >nul 2>nul && goto :py
goto :nopython

:pythonw
start "" pythonw.exe "%APP%" %*
exit /b 0

:pyw
start "" pyw.exe "%APP%" %*
exit /b 0

:python
python.exe "%APP%" %*
exit /b 0

:py
py.exe "%APP%" %*
exit /b 0

:nopython
echo.
echo Python 3.8+ was not found on this computer.
echo Install it from https://www.python.org/downloads/windows/
echo Remember to tick "Add python.exe to PATH", then run this file again.
echo.
pause
exit /b 1