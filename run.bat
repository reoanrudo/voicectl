@echo off
rem voicectl を起動する（コンソールなしで常駐。ログは logs\voicectl.log）
cd /d %~dp0
start "" .venv\Scripts\pythonw.exe -m voicectl %*
