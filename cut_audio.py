#!/usr/bin/env python3
"""
音声から「0.5秒以上の無音」と「連続して繰り返された発言（最後の1回だけ残す）」をカットする。

使い方:
    python cut_audio.py 入力.m4a [-o 出力ディレクトリ] [--model medium] [--silence-db -40]

処理の流れ:
    1. ffmpeg で音声をデコード（ffmpeg が無ければ imageio-ffmpeg 同梱のバイナリを使用）
    2. faster-whisper で単語タイムスタンプ付き文字起こし
    3. 正規化した文字列上で「同じフレーズが連続 2 回以上」を検出し、最後以外を削除区間に
    4. RMS で 0.5 秒以上の無音を検出し削除区間に
    5. 元の時間軸上で両方の削除を適用し、短いフェードで繋いで WAV / M4A を出力
"""
import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata

REQUIRED = {"numpy": "numpy", "faster_whisper": "faster-whisper", "imageio_ffmpeg": "imageio-ffmpeg"}


# ---------------------------------------------------------------- 環境準備
def ensure_packages():
    missing = []
    for mod, pkg in REQUIRED.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)
    if not missing:
        return
    print(f"[setup] 不足パッケージをインストールします: {' '.join(missing)}")
    cmd = [sys.executable, "-m", "pip", "install", "-q", *missing]
    if sys.prefix == sys.base_prefix:  # venv 外ならユーザー領域へ
        cmd.insert(4, "--user")
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as e:
        sys.exit(f"[error] pip install に失敗しました: {e}")
    importlib.invalidate_caches()


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


# ---------------------------------------------------------------- 音声 I/O
def probe(ffmpeg, path):
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", path], capture_output=True, text=True)
    m = re.search(r"Audio:.*?(\d+) Hz, ([^,]+)", r.stderr)
    if not m:
        sys.exit(f"[error] 音声ストリームが見つかりません:\n{r.stderr[-800:]}")
    sr = int(m.group(1))
    layout = m.group(2).strip()
    ch = 1 if layout == "mono" else 2
    return sr, ch


def decode(ffmpeg, path, sr, ch):
    import numpy as np
    cmd = [ffmpeg, "-v", "error", "-i", path, "-vn", "-f", "f32le", "-acodec", "pcm_f32le",
           "-ac", str(ch), "-ar", str(sr), "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        sys.exit(f"[error] デコード失敗: {r.stderr.decode(errors='ignore')}")
    a = np.frombuffer(r.stdout, dtype=np.float32)
    return a.reshape(-1, ch).copy()


def encode(ffmpeg, audio, sr, out_wav, out_m4a):
    import numpy as np
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    ch = audio.shape[1]
    base = [ffmpeg, "-v", "error", "-y", "-f", "s16le", "-ar", str(sr), "-ac", str(ch), "-i", "-"]
    for out, extra in ((out_wav, []), (out_m4a, ["-c:a", "aac", "-b:a", "192k"])):
        r = subprocess.run(base + extra + [out], input=pcm.tobytes(), capture_output=True)
        if r.returncode != 0:
            sys.exit(f"[error] 書き出し失敗 ({out}): {r.stderr.decode(errors='ignore')}")


# ---------------------------------------------------------------- 文字起こし
def transcribe(audio16k, chunks, model_name, language, pad=0.2, gap=1.0, window=28.0):
    """無音で区切った発話チャンクを、1秒の無音を挟んで 28 秒以内の窓に詰めて文字起こしする。

    全体を一度に渡すと Whisper が「最大1980円オフで 最大1980円オフで」のような
    言い直しを1回にまとめてしまうため、チャンクの間に無音を挟んで別々の発話として認識させる。
    Whisper は入力を常に 30 秒に詰めて処理するので、チャンクを1つずつ渡すより窓に詰めた方が
    同じ精度のまま数倍速い。
    """
    import numpy as np
    from faster_whisper import WhisperModel
    print(f"[whisper] モデル '{model_name}' を読み込み中（初回はダウンロードあり）...")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    sr = 16000
    silence = np.zeros(int(gap * sr), dtype=np.float32)

    # 窓ごとに (窓内の開始秒, 元音声の開始秒, 長さ秒) の対応表を作る
    windows, cur, cur_len = [], [], 0.0
    for a, b in chunks:
        a0 = max(0.0, a - pad)
        clip = audio16k[int(a0 * sr):int((b + pad) * sr)]
        if len(clip) < sr * 0.1:
            continue
        L = len(clip) / sr
        if cur and cur_len + L > window:
            windows.append(cur)
            cur, cur_len = [], 0.0
        cur.append((cur_len, a0, L, clip))
        cur_len += L + gap
    if cur:
        windows.append(cur)

    def to_orig(start, end, pieces):
        # 開始時刻で所属チャンクを決め、終了時刻は同じチャンク内に収める
        # （語尾の終了時刻が無音の継ぎ目を越えて次のチャンクに入ると切り位置がずれるため）
        for off, a0, L, _ in reversed(pieces):
            if start >= off:
                return float(a0 + min(start - off, L)), float(a0 + min(max(end - off, 0.0), L))
        a0 = pieces[0][1]
        return float(a0), float(a0)

    words, texts = [], []
    for wi, pieces in enumerate(windows, 1):
        print(f"[whisper] 窓 {wi}/{len(windows)}（チャンク {len(pieces)} 個）")
        audio = np.concatenate([x for p in pieces for x in (p[3], silence)])
        segments, _ = model.transcribe(
            audio,
            language=language,
            word_timestamps=True,
            # 前文脈を引き継ぐと幻覚の繰り返しが出やすく、実在しない「重複」を誤検出するので無効化
            condition_on_previous_text=False,
            vad_filter=False,
            beam_size=5,
        )
        for seg in segments:
            # 無音部分に出る幻覚（「ご視聴ありがとうございました」等）は発話速度が異常に速いので除外
            if len(seg.text.strip()) / max(seg.end - seg.start, 0.01) > 20 or seg.no_speech_prob > 0.8:
                continue
            texts.append((*to_orig(seg.start, seg.end, pieces), seg.text.strip()))
            for w in seg.words or []:
                ws, we = to_orig(w.start, w.end, pieces)
                words.append({"start": ws, "end": we, "text": w.word, "seg": len(texts) - 1})
    print(f"[whisper] チャンク数={len(chunks)} 窓数={len(windows)} 単語数={len(words)}")
    return words, texts


def normalize_char(c):
    c = unicodedata.normalize("NFKC", c).lower()
    return c if c and unicodedata.category(c[0])[0] in "LN" else ""


# ---------------------------------------------------------------- 重複検出
def find_repeats(words, min_chars, max_chars, min_copy_sec, max_gap):
    """連続する同一フレーズを検出し、最後の1回以外の削除区間を返す。

    Whisper の単語分割は同じ語でも揺れるので、正規化した文字列上で比較する。
    ただし切り位置は単語の先頭に限定する（単語途中で切らないため）。
    """
    chars, times, is_start, cut_at = [], [], [], []
    prev_end = None
    for w in words:
        norm = [normalize_char(c) for c in w["text"]]
        norm = [c for c in "".join(norm)]
        n = len(norm)
        for k, c in enumerate(norm):
            chars.append(c)
            times.append(w["start"] + (w["end"] - w["start"]) * k / max(n, 1))
            is_start.append(k == 0)
            # Whisper の単語開始時刻は遅れがちで語頭が欠けるため、
            # 実際の切り位置は「直前の単語の終わり」にする（間の無音は無音カット側で処理）
            cut_at.append(min(prev_end, w["start"]) if prev_end is not None else w["start"])
        if n:
            prev_end = w["end"]
    N = len(chars)
    s = "".join(chars)
    pairs = []  # (phrase, 前のコピー先頭 index, 次のコピー先頭 index)
    i = 0
    while i < N:
        nxt = None
        if is_start[i]:
            for L in range(min(max_chars, (N - i) // 2), min_chars - 1, -1):
                # gap=0 は完全な連続。gap>0 は「それぞれ3、それぞれ先着3個」のような
                # 言い直し（言いかけの断片を挟んだ繰り返し）。短いフレーズでは誤検出しやすいので L>=3 のみ
                for gap in range(0, (max_gap if L >= 3 else 0) + 1):
                    j = i + L + gap
                    if j + L > N or not is_start[j] or s[i:i + L] != s[j:j + L]:
                        continue
                    # 間隔が極端に短い場合は Whisper の幻覚とみなして無視
                    if times[j] - times[i] < min_copy_sec:
                        continue
                    nxt = j
                    pairs.append((s[i:i + L], i, j))
                    break
                if nxt is not None:
                    break
        i = nxt if nxt is not None else i + 1

    # A→A→A のような連鎖を1件にまとめる（最後のコピーだけ残す）
    groups = []
    for phrase, a, b in pairs:
        if groups and groups[-1]["phrase"] == phrase and groups[-1]["idx"][-1] == a:
            groups[-1]["idx"].append(b)
        else:
            groups.append({"phrase": phrase, "idx": [a, b]})
    cuts, found = [], []
    for g in groups:
        starts = [times[x] for x in g["idx"]]
        a, b = cut_at[g["idx"][0]], cut_at[g["idx"][-1]]
        cuts.append((a, b))
        found.append({"phrase": g["phrase"], "count": len(starts),
                      "copy_starts": [round(t, 2) for t in starts],
                      "removed": [round(a, 2), round(b, 2)]})
    return cuts, found


# ---------------------------------------------------------------- 無音検出
def find_silences(audio, sr, thresh_db, min_sec, pad_sec, frame_ms=10):
    import numpy as np
    mono = audio.mean(axis=1)
    hop = int(sr * frame_ms / 1000)
    nf = len(mono) // hop
    if nf == 0:
        return []
    frames = mono[:nf * hop].reshape(nf, hop)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    silent = db < thresh_db
    cuts = []
    k = 0
    while k < nf:
        if silent[k]:
            e = k
            while e < nf and silent[e]:
                e += 1
            a, b = k * frame_ms / 1000, e * frame_ms / 1000
            if e == nf:
                b = len(mono) / sr
            if b - a >= min_sec:
                # 先頭/末尾の無音は全部、途中の無音は前後に少し余白を残す
                ca = a if k == 0 else a + pad_sec
                cb = b if e == nf else b - pad_sec
                if cb > ca:
                    cuts.append((ca, cb))
            k = e
        else:
            k += 1
    return cuts


# ---------------------------------------------------------------- 結合
def apply_cuts(audio, sr, cuts, fade_ms=8):
    import numpy as np
    total = len(audio) / sr
    cuts = sorted((max(0, a), min(total, b)) for a, b in cuts if b > a)
    merged = []
    for a, b in cuts:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    keeps, t = [], 0.0
    for a, b in merged:
        if a > t:
            keeps.append((t, a))
        t = b
    if t < total:
        keeps.append((t, total))

    fade = int(sr * fade_ms / 1000)
    pieces = []
    for a, b in keeps:
        p = audio[int(a * sr):int(b * sr)].copy()
        if len(p) > 2 * fade and fade > 0:
            ramp = np.linspace(0, 1, fade, dtype=np.float32)[:, None]
            p[:fade] *= ramp
            p[-fade:] *= ramp[::-1]
        pieces.append(p)
    out = np.concatenate(pieces) if pieces else audio[:0]
    return out, merged


# ---------------------------------------------------------------- main
def main():
    t_start = time.time()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("-o", "--outdir", help="出力先（既定: 入力と同じフォルダ）")
    ap.add_argument("--model", default="medium", help="Whisper モデル (tiny/base/small/medium/large-v3)")
    ap.add_argument("--language", default="ja")
    ap.add_argument("--silence-db", type=float, default=None,
                    help="無音とみなす閾値 dBFS（既定: 音量分布から自動決定）")
    ap.add_argument("--min-silence", type=float, default=0.5, help="カットする無音の最小長（秒）")
    ap.add_argument("--pad", type=float, default=0.1, help="無音カット時に前後に残す余白（秒）")
    ap.add_argument("--min-chars", type=int, default=2, help="重複とみなすフレーズの最小文字数")
    ap.add_argument("--max-chars", type=int, default=80, help="重複とみなすフレーズの最大文字数")
    ap.add_argument("--min-copy-sec", type=float, default=0.15, help="繰り返し1回分の最小長（秒）")
    ap.add_argument("--max-gap", type=int, default=3, help="言い直しとして許容する、繰り返しの間に挟まる断片の最大文字数")
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        sys.exit(f"[error] 入力ファイルが見つかりません: {args.input}")

    ensure_packages()
    import numpy as np

    ffmpeg = find_ffmpeg()
    print(f"[setup] ffmpeg: {ffmpeg}")

    sr, ch = probe(ffmpeg, args.input)
    audio = decode(ffmpeg, args.input, sr, ch)
    audio16k = decode(ffmpeg, args.input, 16000, 1)[:, 0]
    dur = len(audio) / sr
    print(f"[input] {args.input}  {dur:.2f}s  {sr}Hz  {ch}ch")

    # 1) 無音検出（閾値未指定なら静かな側 10% の音量 + 10dB、-50〜-30dB の範囲に収める）
    if args.silence_db is None:
        mono = audio.mean(axis=1)
        hop = int(sr * 0.01)
        fr = mono[:len(mono) // hop * hop].reshape(-1, hop)
        db = 20 * np.log10(np.sqrt((fr ** 2).mean(axis=1) + 1e-12))
        thresh = float(np.clip(np.percentile(db, 10) + 10, -50, -30))
    else:
        thresh = args.silence_db
    sil_cuts = find_silences(audio, sr, thresh, args.min_silence, args.pad)
    print(f"[silence] 閾値 {thresh:.1f} dBFS / {args.min_silence}s 以上の無音 {len(sil_cuts)} 箇所")

    # 2) 0.3 秒以上の無音で区切った発話チャンクごとに文字起こし + 重複検出
    chunks, t = [], 0.0
    for a, b in find_silences(audio, sr, thresh, 0.3, 0.0):
        if a > t:
            chunks.append((t, a))
        t = b
    if t < dur:
        chunks.append((t, dur))
    try:
        words, segs = transcribe(audio16k, chunks, args.model, args.language)
    except Exception as e:
        sys.exit(f"[error] 文字起こしに失敗しました: {e}")
    rep_cuts, repeats = find_repeats(words, args.min_chars, args.max_chars, args.min_copy_sec, args.max_gap)
    print(f"[repeat] 検出 {len(repeats)} 箇所")
    for r in repeats:
        print(f"   「{r['phrase']}」×{r['count']}  開始={r['copy_starts']}  → {r['removed'][0]}s〜{r['removed'][1]}s を削除")

    # 3) 適用・書き出し
    out, merged = apply_cuts(audio, sr, rep_cuts + sil_cuts)
    if len(out) == 0:
        sys.exit("[error] 全区間がカット対象になりました。--silence-db を下げてください。")

    outdir = args.outdir or os.path.dirname(os.path.abspath(args.input))
    os.makedirs(outdir, exist_ok=True)
    stem = re.sub(r"[^\w\-]+", "_", os.path.splitext(os.path.basename(args.input))[0])
    out_wav = os.path.join(outdir, f"{stem}_output.wav")
    out_m4a = os.path.join(outdir, f"{stem}_output.m4a")
    out_json = os.path.join(outdir, f"{stem}_report.json")
    encode(ffmpeg, out, sr, out_wav, out_m4a)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({
            "input": args.input, "input_sec": round(dur, 2), "output_sec": round(len(out) / sr, 2),
            "silence_threshold_db": round(thresh, 1),
            "transcript": [{"start": round(a, 2), "end": round(b, 2), "text": t} for a, b, t in segs],
            "repeats": repeats,
            "silence_cuts": [[round(a, 2), round(b, 2)] for a, b in sil_cuts],
            "all_cuts": [[round(a, 2), round(b, 2)] for a, b in merged],
        }, f, ensure_ascii=False, indent=2)

    print(f"[done] {dur:.2f}s → {len(out) / sr:.2f}s（処理時間 {time.time() - t_start:.0f} 秒）")
    print(f"   {out_wav}\n   {out_m4a}\n   {out_json}")


if __name__ == "__main__":
    main()
