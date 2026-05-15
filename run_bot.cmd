@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
"C:\Users\itsup3\AppData\Local\Programs\Python\Launcher\py.exe" bot.py >> logs\bot.out.log 2>> logs\bot.err.log
