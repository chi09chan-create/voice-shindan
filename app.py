# -*- coding: utf-8 -*-
"""
声診断 — Praat(Parselmouth)解析サーバー
ブラウザから送られた音声(16kHz/Int16 または Float32 PCM)を Praat で解析し、JSON で返す。

ローカル起動: python app.py  → http://localhost:5000
本番(Render): gunicorn app:app
"""
import io
import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

import numpy as np
import parselmouth
from parselmouth.praat import call
from flask import Flask, request, jsonify, send_from_directory, abort, Response, render_template_string
from flask_cors import CORS
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__, static_folder=None)
CORS(app)

# 巨大データの送りつけを防ぐ(通常の録音は 1MB 以下)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

# 単体アプリ(.exe)化したときは同梱ファイルの場所が変わる
if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 公開してよいファイルだけを配信する(app.py 等のソースを配らないため)
PUBLIC_FILES = {"index.html", "profile.jpg"}

AXIS_NAMES = ["高さ・音域", "声の安定性", "響き・明るさ", "口の開き", "声量", "クリアさ"]
FONT_DIR = os.path.join(BASE_DIR, "fonts")
JST = timezone(timedelta(hours=9))

# 結果をスプレッドシートへ送る先(Renderの環境変数で設定。未設定なら記録しない)
SHEET_WEBHOOK_URL = os.environ.get("SHEET_WEBHOOK_URL", "").strip()


def _font(bold, size):
    name = "NotoSansJP-Bold.otf" if bold else "NotoSansJP-Regular.otf"
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size)


def safe(fn, default=None):
    """Praat 呼び出しが nan / 例外でも落ちないようにする"""
    try:
        v = fn()
        if v is None:
            return default
        if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
            return default
        return v
    except Exception:
        return default


def analyze_sound(values, sr):
    """1つの録音から各音響指標を算出して dict で返す"""
    snd = parselmouth.Sound(values, sampling_frequency=sr)

    result = {"valid": False}

    # ---- ピッチ(F0:声の高さ・音域) ----
    pitch = snd.to_pitch(pitch_floor=75.0, pitch_ceiling=600.0)
    f0 = pitch.selected_array["frequency"]
    voiced = f0[f0 > 0]
    if voiced.size < 5:
        return result  # 声がほとんど検出できない

    result["valid"] = True
    result["f0_med"] = float(np.percentile(voiced, 50))
    result["f0_low"] = float(np.percentile(voiced, 10))
    result["f0_high"] = float(np.percentile(voiced, 90))
    result["range_st"] = float(12.0 * np.log2(result["f0_high"] / result["f0_low"]))

    # ---- フォルマント(F1=口の開き / F2・F3=響き・明るさ) ----
    # 16kHz 録音でも F3 まで測れるよう上限を調整する
    max_formant = min(5500.0, sr / 2.0 - 50.0)
    formant = snd.to_formant_burg(max_number_of_formants=5, maximum_formant=max_formant)
    f1s, f2s, f3s = [], [], []
    for t in pitch.xs():
        if pitch.get_value_at_time(t) and pitch.get_value_at_time(t) > 0:
            v1 = formant.get_value_at_time(1, t)
            v2 = formant.get_value_at_time(2, t)
            v3 = formant.get_value_at_time(3, t)
            if v1 and not np.isnan(v1):
                f1s.append(v1)
            if v2 and not np.isnan(v2):
                f2s.append(v2)
            if v3 and not np.isnan(v3):
                f3s.append(v3)
    result["f1"] = float(np.mean(f1s)) if f1s else None
    result["f2"] = float(np.mean(f2s)) if f2s else None
    result["f3"] = float(np.mean(f3s)) if f3s else None

    # ---- ジッター/シマー(声の安定性) ----
    pp = safe(lambda: call(snd, "To PointProcess (periodic, cc)", 75.0, 600.0))
    if pp is not None:
        result["jitter"] = safe(
            lambda: call(pp, "Get jitter (local)", 0, 0, 0.0001, 0.02, 1.3) * 100.0
        )
        result["shimmer"] = safe(
            lambda: call([snd, pp], "Get shimmer (local)", 0, 0, 0.0001, 0.02, 1.3, 1.6) * 100.0
        )
    else:
        result["jitter"] = None
        result["shimmer"] = None

    # ---- HNR(クリアさ:雑音混じり具合) ----
    harm = safe(lambda: call(snd, "To Harmonicity (cc)", 0.01, 75.0, 0.1, 1.0))
    result["hnr"] = safe(lambda: call(harm, "Get mean", 0, 0)) if harm is not None else None

    # ---- 強度(声量) ----
    intensity = safe(lambda: snd.to_intensity())
    result["intensity_db"] = (
        safe(lambda: call(intensity, "Get mean", 0, 0, "energy")) if intensity is not None else None
    )

    return result


@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        sr = int(request.args.get("sr", 48000))
    except ValueError:
        return jsonify({"valid": False, "error": "bad sample rate"}), 400
    if not (4000 <= sr <= 192000):
        return jsonify({"valid": False, "error": "bad sample rate"}), 400

    fmt = request.args.get("fmt", "f32")
    raw = request.get_data()
    if not raw:
        return jsonify({"valid": False, "error": "no audio"})

    if fmt == "i16":
        # ブラウザ側で 16kHz/16bit に圧縮して送られてくる(通常はこちら)
        values = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    else:
        values = np.frombuffer(raw, dtype=np.float32).astype(np.float64)

    if values.size < sr // 10:  # 0.1秒未満は解析しない
        return jsonify({"valid": False, "error": "too short"})

    try:
        res = analyze_sound(values, sr)
    except Exception:
        return jsonify({"valid": False, "error": "analysis failed"})
    return jsonify(res)


# =========================================================================
#  シェア用の結果画像 & 結果ページ
#  SNSに貼ったリンクから、レーダーチャート入りの画像が表示されるようにする
# =========================================================================

def _parse_result_params(args):
    """URLのパラメータから、名前・総合点・6項目の点数を安全に取り出す"""
    name = (args.get("n", "") or "")[:12]
    try:
        total = max(0, min(100, int(args.get("t", 0))))
    except ValueError:
        total = 0
    scores = []
    for part in (args.get("a", "") or "").split(",")[:6]:
        try:
            scores.append(max(0, min(100, int(part))))
        except ValueError:
            scores.append(0)
    while len(scores) < 6:
        scores.append(0)
    return name, total, scores


def _draw_radar(d, scores, cx, cy, R, font):
    n = len(scores)
    ang = lambda i: -math.pi / 2 + i * 2 * math.pi / n

    for g in range(1, 5):
        r = R * g / 4
        d.polygon([(cx + r * math.cos(ang(i)), cy + r * math.sin(ang(i))) for i in range(n)],
                  outline=(190, 182, 196))

    pts = [(cx + R * scores[i] / 100 * math.cos(ang(i)),
            cy + R * scores[i] / 100 * math.sin(ang(i))) for i in range(n)]
    d.polygon(pts, fill=(255, 176, 190), outline=(255, 141, 161))
    d.line(pts + [pts[0]], fill=(255, 141, 161), width=4)
    for px, py in pts:
        d.ellipse([px - 6, py - 6, px + 6, py + 6], fill=(87, 205, 195), outline=(255, 255, 255), width=3)

    for i, label in enumerate(AXIS_NAMES[:n]):
        a = ang(i)
        lx, ly = cx + (R + 56) * math.cos(a), cy + (R + 36) * math.sin(a)
        w = d.textlength(label, font=font)
        d.text((lx - w / 2, ly - 16), label, font=font, fill=(122, 119, 144))


def _render_card(name, total, scores):
    """1080x1080 の結果画像を作る"""
    W = H = 1080
    img = Image.new("RGB", (W, H), (255, 246, 240))
    d = ImageDraw.Draw(img)

    # 背景のやわらかいグラデーション
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)],
               fill=(int(255 - 0 * t), int(246 + 8 * t), int(240 + 5 * t)))

    def center(text, y, font, fill):
        d.text(((W - d.textlength(text, font=font)) / 2, y), text, font=font, fill=fill)

    # 監修者の写真(丸く切り抜き)
    try:
        ph = Image.open(os.path.join(BASE_DIR, "profile.jpg")).convert("RGB").resize((128, 128))
        mask = Image.new("L", (128, 128), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, 127, 127], fill=255)
        img.paste(ph, (W // 2 - 64, 60), mask)
        d.ellipse([W // 2 - 66, 58, W // 2 + 66, 190], outline=(255, 141, 161), width=5)
    except Exception:
        pass

    center("アナウンサー・音声研究者 かしまだちほ(Ph.D) 監修", 212, _font(False, 23), (58, 56, 70))
    center("あなたの声診断", 252, _font(True, 58), (255, 126, 149))

    y = 332
    if name:
        center(f"{name}さんの結果", y, _font(True, 30), (58, 56, 70))
        y += 44
    center("総合スコア", y, _font(False, 22), (95, 91, 112))
    center(str(total), y + 28, _font(True, 110), (59, 191, 159))
    center("点 / 100点", y + 150, _font(False, 24), (95, 91, 112))

    _draw_radar(d, scores, W // 2, 802, 150, _font(False, 23))

    center("voice-shindan.onrender.com", 1030, _font(False, 23), (154, 150, 170))

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


@app.route("/ogp.png")
def ogp_png():
    name, total, scores = _parse_result_params(request.args)
    png = _render_card(name, total, scores)
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


RESULT_PAGE = """<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }}</title>
<meta property="og:type" content="website">
<meta property="og:title" content="{{ title }}">
<meta property="og:description" content="音声学の研究ツール Praat があなたの声を6つの観点で分析します。無料・約1分・登録不要。">
<meta property="og:image" content="{{ img }}">
<meta property="og:image:width" content="1080">
<meta property="og:image:height" content="1080">
<meta property="og:url" content="{{ page }}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{{ title }}">
<meta name="twitter:description" content="音声学の研究ツール Praat があなたの声を6つの観点で分析します。無料・約1分・登録不要。">
<meta name="twitter:image" content="{{ img }}">
<style>
 body{margin:0;background:linear-gradient(165deg,#fff6f0,#ffeef5);
  font-family:"Hiragino Maru Gothic ProN","Yu Gothic",Meiryo,sans-serif;color:#2c2a38;}
 .wrap{max-width:560px;margin:0 auto;padding:28px 18px 60px;text-align:center;}
 img.card{width:100%;border-radius:22px;box-shadow:0 10px 30px rgba(255,150,170,.25);}
 h1{font-size:22px;margin:18px 0 6px;}
 p{color:#5f5b70;font-size:14px;margin:0 0 22px;}
 a.btn{display:block;text-decoration:none;background:linear-gradient(135deg,#ff8da1,#57cdc3);
  color:#fff;font-weight:800;font-size:18px;padding:17px;border-radius:16px;
  box-shadow:0 8px 20px rgba(87,205,195,.35);}
</style></head>
<body><div class="wrap">
 <img class="card" src="{{ img }}" alt="声診断の結果">
 <h1>あなたの声は何点でしょう?</h1>
 <p>音声学の研究ツール Praat が、あなたの声を6つの観点で分析します。<br>無料・約1分・登録不要</p>
 <a class="btn" href="/">🎤 自分も診断してみる</a>
</div></body></html>"""


@app.route("/r")
def result_page():
    name, total, scores = _parse_result_params(request.args)
    base = request.url_root.rstrip("/")
    qs = f"n={request.args.get('n','')}&t={total}&a={','.join(map(str, scores))}"
    title = f"{name}さんの声は {total}点 でした｜あなたの声診断" if name else f"声診断の結果は {total}点 でした｜あなたの声診断"
    return render_template_string(RESULT_PAGE, title=title,
                                  img=f"{base}/ogp.png?{qs}", page=f"{base}/r?{qs}")


# =========================================================================
#  診断結果をスプレッドシートへ記録(任意)
# =========================================================================

@app.route("/log", methods=["POST"])
def log_result():
    if not SHEET_WEBHOOK_URL:
        return jsonify({"ok": False, "reason": "not configured"})
    try:
        body = request.get_json(force=True, silent=True) or {}
        row = {
            "time": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            "name": str(body.get("name", ""))[:12],
            "sex": str(body.get("sex", ""))[:10],
            "age": str(body.get("age", ""))[:10],
            "total": body.get("total"),
        }
        for key, label in zip(
            ["height", "stable", "bright", "open", "volume", "clear"], AXIS_NAMES
        ):
            row[key] = (body.get("scores") or {}).get(label)
        for k in ["f0_med", "f0_low", "f0_high", "range_st", "f1", "f2", "f3",
                  "jitter", "shimmer", "hnr", "intensity_db"]:
            v = (body.get("raw") or {}).get(k)
            row[k] = round(v, 2) if isinstance(v, (int, float)) else None

        req = urllib.request.Request(
            SHEET_WEBHOOK_URL,
            data=json.dumps(row, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=8).read()
        return jsonify({"ok": True})
    except Exception:
        # 記録に失敗しても診断そのものは止めない
        return jsonify({"ok": False})


# 同じサーバーから index.html も配信(別サーバー不要に)
@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    if path not in PUBLIC_FILES:
        abort(404)
    return send_from_directory(BASE_DIR, path)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"声診断 Praat サーバー起動: http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
