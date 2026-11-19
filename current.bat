@echo off
cd /d "%~dp0"
echo === Current adverts (all statuses except CLOSED/CANCELLED) with cards and lots + registries + complaints ===
echo Results: zakup_current folder. Ctrl+C to stop, run again to continue.
python zakup_sk_parser.py --sections open,adverts --advert-status CURRENT --workers 8 --delay 0.3 --out zakup_current %*
pause
