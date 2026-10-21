#!/bin/bash
cd "$(dirname "$0")"
echo "============================================"
echo "  あなたの声診断 を起動しています"
echo "============================================"
echo
echo "[1/3] 最新の状態に更新しています..."
if ! git pull --ff-only; then
  echo
  echo " ※ 最新への更新ができませんでした。"
  echo "   このまま起動しますが、Claude に「git pull がうまくいかない」と伝えてください。"
  echo
fi
echo "[2/3] 必要な部品を確認しています..."
python3 -c "import parselmouth, flask, flask_cors, PIL" 2>/dev/null || python3 -m pip install --user -r requirements.txt
echo "[3/3] アプリを起動します。ブラウザが自動で開きます。"
echo
echo " ※ このウィンドウは閉じないでください。閉じるとアプリが止まります。"
echo " ※ 終わるときは、このウィンドウを閉じてください。"
echo
# macOS は 5000 番を AirPlay レシーバーが使うので 5050 番で起動する
(sleep 3; open "http://localhost:5050/") &
PORT=5050 python3 app.py
