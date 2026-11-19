@echo off
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found. Install Python 3.10+ from https://www.python.org/downloads/ ^(check "Add python.exe to PATH"^)
  pause
  exit /b 1
)

echo === Installing dependencies (playwright + chromium) ===
python -m pip install playwright
if errorlevel 1 goto :err
python -m playwright install chromium
if errorlevel 1 goto :err

echo === Full scrape of zakup.sk.kz. Results: zakup_all. Ctrl+C to stop, run again to continue ===
python zakup_sk_parser.py --workers 8 --delay 0.3 --out zakup_all %*
pause
exit /b 0

:err
echo Dependency install failed - see the error above.
pause
exit /b 1
