# あなたの声診断 — 作業ルール

アプリの内容と残っている作業は `HANDOFF.md` を読むこと。

## 2台のPCで作業している

Windows PC と Mac mini の両方で、この GitHub リポジトリ(chi09chan-create/voice-shindan)を使って作業する。GitHub が唯一の正本。

- **作業を始める前に** `git pull --ff-only` で最新にする。失敗したら、何が起きているかを利用者にやさしく説明してから対処する(勝手に強制上書きしない)
- **作業が終わったら** commit して `git push` する。push しないと、もう一方のPCに変更が届かない
- このリポジトリ以外の場所にあるコピー(古い作業フォルダなど)を編集・コピーで持ち込まない
- リポジトリは公開(Public)。パスワード、個人情報、秘密のURL(スプレッドシートの受け口URLなど)をコミットしない

## 利用者について

- ボイストレーニング講師・音声研究者。プログラミングの知識はない
- 説明は専門用語を避けた、やさしい日本語で。手順は一度に一つずつ
- アカウント作成、パスワード入力、支払い、Render のボタン操作は利用者本人が行う

## 手元で動かす

- Windows:`start-windows.bat` をダブルクリック → http://localhost:5000
- Mac:`start-mac.command` をダブルクリック → http://localhost:5050
  (macOS は 5000 番を AirPlay レシーバーが使うため 5050 番にしている)
- どちらも、起動前に `git pull` で最新にし、必要なパッケージがなければ入れる

## 公開

1. main ブランチへ push
2. Render は自動では反映されない。利用者に Render の「Manual Deploy」→「Deploy latest commit」を押してもらう
3. 反映後、本番(https://voice-shindan.onrender.com)で動作確認する

## コードを書くときの注意

- `index.html` の JavaScript の文字列("..." や '...')の中に、生の改行を入れない。以前これでスクリプト全体が止まった。改行は `\n` と書く
- サーバーが配信するのは `app.py` の `PUBLIC_FILES` に入っているファイルだけ。画面で使うファイルを増やしたら、そこにも追加する
- 結果ページや画像のURLを組み立てるときは、必ずエンコードする(`urlencode` / `URLSearchParams`)。日本語のまま入れると SNS のクローラーが取得できない
