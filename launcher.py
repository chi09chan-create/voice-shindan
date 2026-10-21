# -*- coding: utf-8 -*-
"""
声診断アプリ 単体版のランチャー
ダブルクリックすると解析サーバーを起動し、ブラウザを自動で開く。
"""
import logging
import socket
import sys
import threading
import time
import webbrowser

from app import app


def find_free_port(preferred=5000):
    """使えるポートを探す(他のアプリと衝突しないように)"""
    for port in [preferred] + list(range(5001, 5051)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return 0  # OSに任せる


def main():
    # Flask の細かいログは表示しない(画面を見やすくするため)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    port = find_free_port()
    url = f"http://localhost:{port}/"

    print("=" * 52)
    print("  あなたの声診断  -  かしまだちほ(Ph.D)監修")
    print("=" * 52)
    print()
    print("  起動中です... ブラウザが自動で開きます。")
    print(f"  開かないときは、次のアドレスを入力してください:")
    print(f"    {url}")
    print()
    print("  ※ この黒い画面は閉じないでください。")
    print("     閉じるとアプリが止まります。")
    print("  ※ 終わるときは、この画面を閉じてください。")
    print()
    print("=" * 52)

    def open_browser():
        time.sleep(1.5)
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()

    try:
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    except Exception as e:
        print()
        print(f"エラーが発生しました: {e}")
        input("Enter キーを押すと終了します...")
        sys.exit(1)


if __name__ == "__main__":
    main()
