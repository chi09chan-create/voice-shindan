@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   あなたの声診断 を起動しています
echo ============================================
echo.
echo [1/3] 最新の状態に更新しています...
git pull --ff-only
if errorlevel 1 (
  echo.
  echo  ※ 最新への更新ができませんでした。
  echo    このまま起動しますが、Claude に「git pull がうまくいかない」と伝えてください。
  echo.
)
echo [2/3] 必要な部品を確認しています...
python -c "import parselmouth, flask, flask_cors, PIL" 2>nul || python -m pip install -r requirements.txt
echo [3/3] アプリを起動します。ブラウザが自動で開きます。
echo.
echo  ※ この黒い画面は閉じないでください。閉じるとアプリが止まります。
echo  ※ 終わるときは、この画面を閉じてください。
echo.
start "" http://localhost:5000/
python app.py
pause
