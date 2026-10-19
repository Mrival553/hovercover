@echo off
rem ============================================================
rem  Optional dependency: Pillow
rem  After installing, JPG / JPEG / WebP / BMP / TIFF previews work.
rem  Without it the app still runs (PNG / GIF always work).
rem  ASCII + CRLF on purpose: cmd.exe mis-parses UTF-8 batch files.
rem ============================================================
setlocal
cd /d "%~dp0"
echo Installing Pillow (optional dependency) ...
where python.exe >nul 2>nul && goto :runpy
where py.exe     >nul 2>nul && goto :runlauncher
echo Python was not found. Please install Python 3.8+ first.
pause
exit /b 1

:runpy
python.exe -m pip install --user --upgrade pillow
goto :done

:runlauncher
py.exe -m pip install --user --upgrade pillow

:done
echo.
echo Done. Reopen the file manager to preview JPG / WebP images.
pause