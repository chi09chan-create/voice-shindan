# -*- coding: utf-8 -*-
"""
声診断 — Praat(Parselmouth)解析サーバー
ブラウザから送られた音声(16kHz/Int16 または Float32 PCM)を Praat で解析し、JSON で返す。

ローカル起動: python app.py  → http://localhost:5000
本番(Render): gunicorn app:app
"""
import hashlib
import hmac
import io
import json
import math
import os
import secrets
import sys
import urllib.request
from functools import lru_cache
from urllib.parse import urlencode
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

# 診断結果に付ける署名の鍵(Renderの環境変数 SHARE_SECRET で設定)
# 未設定だと起動のたびに鍵が変わり、以前シェアしたリンクの画像が出なくなる
SHARE_SECRET = os.environ.get("SHARE_SECRET", "").strip()
if not SHARE_SECRET:
    print("注意: SHARE_SECRET が未設定です。一時的な鍵で起動します。", file=sys.stderr)
    SHARE_SECRET = secrets.token_hex(32)
_KEY = SHARE_SECRET.encode("utf-8")


def _sign(purpose, payload):
    """payload(dict)に署名する。purpose を混ぜて、用途の違う署名を使い回せないようにする"""
    # 数値はすべて小数として扱う(ブラウザを経由すると 5.0 が 5 に変わるため)
    norm = {k: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
            for k, v in payload.items()}
    msg = purpose + ":" + json.dumps(norm, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hmac.new(_KEY, msg.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _verify(purpose, payload, sig):
    return isinstance(sig, str) and hmac.compare_digest(_sign(purpose, payload), sig)


@lru_cache(maxsize=32)
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
    # サーバーで解析した本物の値であることの印
    res["sig"] = _sign("analysis", res)
    return jsonify(res)


# =========================================================================
#  点数をつける(基準値は指導方針に合わせて調整可)
#  点数はサーバーで計算する。画面側で計算すると、点数を書き換えた
#  偽の結果画像を作れてしまうため
# =========================================================================

def _jsround(x):
    """JavaScript の Math.round と同じ四捨五入(Python の round は .5 を偶数に丸める)"""
    return int(math.floor(x + 0.5))


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _num(v, default):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v):
        return default
    return v


def score_from(rA, rB):
    stab = rA if rA.get("valid") else rB
    read = rB if rB.get("valid") else rA

    range_st = _num(read.get("range_st"), 4)
    s_height = _clamp(_jsround(range_st / 12 * 100), 5, 100)

    jit = _num(stab.get("jitter"), 1.5)
    shim = _num(stab.get("shimmer"), 6)
    s_jit = _clamp(100 - (jit - 0.4) * 45, 5, 100)
    s_shim = _clamp(100 - (shim - 2.0) * 13, 5, 100)
    s_stable = _jsround((s_jit + s_shim) / 2)

    f2 = _num(read.get("f2"), 1500)
    s_bright = _clamp(_jsround((f2 - 1200) / 1100 * 100), 5, 100)

    f1 = _num(read.get("f1"), 450)
    s_open = _clamp(_jsround((f1 - 350) / 350 * 100), 5, 100)

    db = _num(read.get("intensity_db"), 62)
    s_volume = _clamp(_jsround((db - 52) / 28 * 100), 5, 100)

    hnr = _num(stab.get("hnr"), 12)
    s_clear = _clamp(_jsround((hnr - 5) / 18 * 100), 5, 100)

    return [s_height, s_stable, s_bright, s_open, s_volume, s_clear]


def _checked_analysis(r):
    """/analyze が返した結果か(署名が正しいか)を確かめる。正しくなければ空の結果"""
    if not isinstance(r, dict):
        return {"valid": False}
    body = {k: v for k, v in r.items() if k != "sig"}
    if not _verify("analysis", body, r.get("sig")):
        return {"valid": False}
    return body


def _share_params(name, total, scores):
    """結果ページ・結果画像のURLに付けるパラメータ(署名入り)"""
    a = ",".join(map(str, scores))
    return {"n": name, "t": total, "a": a, "s": _sign("share", {"n": name, "t": total, "a": a})}


@app.route("/score", methods=["POST"])
def score():
    body = request.get_json(force=True, silent=True) or {}
    rA = _checked_analysis(body.get("rA"))
    rB = _checked_analysis(body.get("rB"))
    if not (rA.get("valid") or rB.get("valid")):
        return jsonify({"ok": False}), 400
    scores = score_from(rA, rB)
    total = _jsround(sum(scores) / len(scores))
    name = str(body.get("name", "") or "")[:12]
    return jsonify({"ok": True, "scores": scores, "total": total,
                    "share": _share_params(name, total, scores)})


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
    # 署名が合わない(URLを書き換えた)ときは、点数を出さない
    if not _verify("share", {"n": name, "t": total, "a": ",".join(map(str, scores))}, args.get("s")):
        return None
    return name, total, scores


def _draw_radar(d, scores, cx, cy, R, font):
    n = len(scores)
    ang = lambda i: -math.pi / 2 + i * 2 * math.pi / n

    for g in range(1, 5):
        r = R * g / 4
        d.polygon([(cx + r * math.cos(ang(i)), cy + r * math.sin(ang(i))) for i in range(n)],
                  outline=(190, 182, 196))

    if any(scores):  # 点数のない案内用の画像では、目盛りと項目名だけ描く
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


@lru_cache(maxsize=1)
def _profile_photo(size):
    try:
        ph = Image.open(os.path.join(BASE_DIR, "profile.jpg")).convert("RGB").resize((size, size))
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
        return ph, mask
    except Exception:
        return None


def _render_card(name, total, scores):
    """SNSカード用の横長画像(1200x630)を作る"""
    W, H = 1200, 630
    # やわらかいグラデーション背景(小さい画像を引き伸ばして高速に)
    g = Image.new("RGB", (1, 2))
    g.putpixel((0, 0), (255, 246, 240))
    g.putpixel((0, 1), (255, 236, 244))
    img = g.resize((W, H), Image.BILINEAR)
    d = ImageDraw.Draw(img)

    LX = 330  # 左側の中心

    def center(text, x, y, font, fill):
        d.text((x - d.textlength(text, font=font) / 2, y), text, font=font, fill=fill)

    ph = _profile_photo(112)
    if ph:
        img.paste(ph[0], (LX - 56, 42), ph[1])
        d.ellipse([LX - 58, 40, LX + 58, 156], outline=(255, 141, 161), width=4)

    center("アナウンサー・音声研究者 かしまだちほ(Ph.D) 監修", LX, 172, _font(False, 20), (58, 56, 70))
    center("あなたの声診断", LX, 204, _font(True, 54), (255, 126, 149))

    y = 284
    if total is None:
        # 本物の結果でないときは、点数のない案内用の画像にする
        center("あなたの声は何点?", LX, y + 40, _font(True, 44), (59, 191, 159))
        center("6つの観点で声を分析します", LX, y + 120, _font(False, 24), (95, 91, 112))
        center("無料・約1分・登録不要", LX, y + 162, _font(False, 24), (95, 91, 112))
    else:
        if name:
            center(f"{name}さんの結果", LX, y, _font(True, 28), (58, 56, 70))
            y += 42
        center("総合スコア", LX, y, _font(False, 20), (95, 91, 112))
        center(str(total), LX, y + 18, _font(True, 116), (59, 191, 159))
        center("点 / 100点", LX, y + 158, _font(False, 22), (95, 91, 112))

    center("voice-shindan.onrender.com", LX, 584, _font(False, 18), (154, 150, 170))

    # 右側:白いカードの上にレーダーチャート
    d.rounded_rectangle([650, 36, 1166, 594], radius=30, fill=(255, 255, 255))
    _draw_radar(d, scores, 908, 322, 150, _font(False, 22))

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


@app.route("/ogp.png")
def ogp_png():
    name, total, scores = _parse_result_params(request.args) or ("", None, [0] * 6)
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
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="あなたの声診断の結果(総合スコアとレーダーチャート)">
<meta property="og:site_name" content="あなたの声診断">
<meta property="og:locale" content="ja_JP">
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
 <p style="font-size:11px;margin:18px 0 0;">※ 声の傾向を楽しく知るための診断です。医療的な診断ではありません。</p>
</div></body></html>"""


@app.route("/r")
def result_page():
    base = request.url_root.rstrip("/")
    parsed = _parse_result_params(request.args)
    if parsed is None:
        # 署名が合わないURLは、点数のない案内ページにする
        qs = ""
        title = "あなたの声は何点?｜あなたの声診断"
    else:
        name, total, scores = parsed
        qs = urlencode(_share_params(name, total, scores))
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
