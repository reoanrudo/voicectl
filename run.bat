@echo off
rem voicectl を起動する（コンソールなしで常駐。ログは logs\voicectl.log）
rem 初回は仮想環境 .venv を作って依存パッケージを入れてから起動する（数分かかります）
cd /d %~dp0
if exist .venv\Scripts\pythonw.exe goto :run
echo 初回セットアップ: 仮想環境を作成しています（数分かかります。そのままお待ちください）...
python -m venv .venv
if errorlevel 1 goto :err
.venv\Scripts\python.exe -m pip install --upgrade pip -q
.venv\Scripts\python.exe -m pip install -r requirements.txt -q
if errorlevel 1 goto :err
echo セットアップ完了。起動します...
:run
start "" .venv\Scripts\pythonw.exe -m voicectl %*
exit /b 0
:err
echo セットアップに失敗しました。Python 3.12 がインストールされているか確認してください。
pause
exit /b 1
