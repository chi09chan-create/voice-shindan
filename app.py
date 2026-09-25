# -*- coding: utf-8 -*-
"""
声診断 — Praat(Parselmouth)解析サーバー
ブラウザから送られた音声(16kHz/Int16 または Float32 PCM)を Praat で解析し、JSON で返す。

ローカル起動: python app.py  → http://localhost:5000
本番(Render): gunicorn app:app
"""
import os
import sys

import numpy as np
import parselmouth
from parselmouth.praat import call
from flask import Flask, request, jsonify, send_from_directory, abort
from flask_cors import CORS

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
