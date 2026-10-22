#!/usr/bin/env python3
"""
縦型動画（または音声）を 1 回の Whisper 解析で「無音・言い直しカット」→「テロップ焼き込み」まで仕上げる。

使い方:
    python auto_edit.py 入力.MOV                 # 全自動（カット + テロップ + SRT）
    python auto_edit.py 入力.MOV --prepare       # 解析とテロップ案（_cues.txt）まで。書き出しはしない
    python auto_edit.py 入力.MOV --cues 修正版.txt  # 解析を再利用して、指定のテロップで書き出しだけ行う
    python auto_edit.py 入力.MOV --no-cut        # カットせずテロップだけ（旧 /srt-fast 相当）
    python auto_edit.py 入力.MOV --no-telop      # カットだけ（動画版 cut_audio）
    python auto_edit.py 入力.m4a                 # 音声ならカットした m4a と SRT を出力

処理の流れ:
    1. cut_audio.py と同じ方法で無音と重複（言い直し）を検出する。カット位置は動画のフレーム境界にそろえる
    2. 同じ文字起こしの単語タイムスタンプをカット後の時間軸に写し、テロップ（cues）を自動で組む
       （話の切れ目で区切る・約14文字で改行・短い語は次にまとめる・corrections.tsv で誤認識を直す）
    3. 映像のカット・テロップの焼き込み・音声の差し替えを 1 回のエンコードで行う
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cut_audio as ca  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PUNCT = "、。，．,.？！?!…"
# 区切り・改行の自然さを決める語。直前の語がこれで終わると切りやすく、次の語がこれで始まると切りにくい
STRONG_END = ("ください", "ました", "ます", "です", "ません", "でした", "ね", "よ")
CLAUSE_END = ("が", "けど", "から", "ので", "て", "で", "ば", "たら", "し")
PARTICLE_END = ("は", "を", "に", "の", "と", "も", "へ", "や")
NO_START = ("は", "が", "を", "に", "の", "と", "も", "で", "へ", "から", "まで", "て", "た", "ます", "です",
            "ない", "ん", "っ", "ー")
# 疑問詞は「何から作れば」のように後ろの動詞までひとまとまり。疑問詞の直前は切りやすく、直後では切らない
QUESTION = ("何", "なに", "なん", "どこ", "どう", "どれ", "どの", "どんな", "いつ", "誰", "だれ", "なぜ")
# 「て」の後に続く補助動詞（製作して｜きました、待って｜ください）。ここでは切らない
TE_AUX = ("き", "くる", "い", "いる", "しま", "ちゃ", "くだ", "くれ", "み", "お", "あげ", "もら", "ほし")
QUESTION_TAIL = re.compile("(" + "|".join(QUESTION) + ")(から|を|が|で|に|と|へ|まで)?$")


# ---------------------------------------------------------------- 入力の情報
def probe_video(ffmpeg, path):
    """映像があれば (fps, 開始秒) を返す。音声のみ（カバー画像付きを含む）なら None。"""
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", path], capture_output=True, text=True)
    m = re.search(r"Stream #\S+: Video: (?!.*attached pic).*?([\d.]+) fps", r.stderr)
    if not m:
        return None
    st = re.search(r"start: ([\d.]+)", r.stderr)
    return float(m.group(1)), float(st.group(1)) if st else 0.0


# ---------------------------------------------------------------- 時間軸
def keeps_from_cuts(merged, total, fps=None):
    keeps, t = [], 0.0
    for a, b in merged:
        if a > t:
            keeps.append((t, a))
        t = b
    if t < total:
        keeps.append((t, total))
    if fps:
        # 音声と映像で切り位置がずれないよう、残す区間をフレーム境界にそろえる
        snapped = []
        for a, b in keeps:
            k, m = round(a * fps), round(b * fps)
            if m > k:
                if snapped and k / fps <= snapped[-1][1]:
                    snapped[-1] = (snapped[-1][0], m / fps)
                else:
                    snapped.append((k / fps, m / fps))
        keeps = snapped
    return keeps


def to_cut_time(t, keeps):
    """元の時刻をカット後の時刻に写す。カットされた所は次に残る区間の先頭にする。"""
    acc = 0.0
    for a, b in keeps:
        if t < a:
            return acc
        if t <= b:
            return acc + t - a
        acc += b - a
    return acc


def render_audio(audio, sr, keeps, fade_ms=8):
    import numpy as np
    fade = int(sr * fade_ms / 1000)
    pieces = []
    for a, b in keeps:
        p = audio[int(round(a * sr)):int(round(b * sr))].copy()
        if len(p) > 2 * fade and fade > 0:
            ramp = np.linspace(0, 1, fade, dtype=np.float32)[:, None]
            p[:fade] *= ramp
            p[-fade:] *= ramp[::-1]
        pieces.append(p)
    return np.concatenate(pieces) if pieces else audio[:0]


# ---------------------------------------------------------------- テロップ組み
def load_corrections(path):
    pairs = []
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            if line.strip() and not line.startswith("#") and "\t" in line:
                wrong, right = line.rstrip("\n").split("\t")[:2]
                pairs.append((wrong, right))
    return pairs


def clean(text, corrections):
    for wrong, right in corrections:
        text = text.replace(wrong, right)
    return re.sub(r"[、。，．,.]", "", text)


_tokenizer = None


def morph_levels(text):
    """文字位置ごとに「そこで切ったときの自然さ」を返す（形態素解析 Janome を使う）。

    0: 語の途中（専｜用、パンドラボ｜ックス）  1: 語の切れ目だが文節の途中（ケース｜が、フル｜ボトル）
    2: 文節の切れ目（ケースが｜ヤバすぎた）    3: 文の切れ目らしい（ヤバすぎた｜今回は）
    Whisper の単語は1文字ずつに分かれることが多く、それだけでは語の途中かどうか分からないため。
    Janome が無ければ None（従来の文字列ルールだけで判定する）。
    """
    global _tokenizer
    try:
        from janome.tokenizer import Tokenizer
    except ImportError:
        return None
    _tokenizer = _tokenizer or Tokenizer()
    toks = [(t.surface, t.part_of_speech.split(","), t.infl_form, t.base_form) for t in _tokenizer.tokenize(text)]
    levels, pos = {}, 0
    for k, (surf, ps, form, base) in enumerate(toks):
        if k:
            _, pps, pform, pbase = toks[k - 1]
            independent = (ps[0] in ("名詞", "副詞", "連体詞", "接続詞", "感動詞", "接頭詞")
                           or ps[0] in ("動詞", "形容詞") and ps[1] == "自立") and ps[1] not in ("接尾", "非自立")
            compound = (pps[0] in ("接頭詞", "連体詞")  # 「この｜究極の」は切らない
                        or pps[1] == "副詞化" and k >= 2 and toks[k - 2][1][1] == "形容動詞語幹"  # 「完璧に｜収納」
                        or pps[0] == "名詞" and ps[0] == "名詞" and ps[1] != "副詞可能"
                        or pps[0] == "名詞" and pps[1] == "サ変接続" and ps[0] == "動詞")
            ends = pps[0] in ("助動詞", "動詞", "形容詞") and pform == "基本形"  # 「〜た」「〜できる」の形
            if not independent or compound:
                levels[pos] = 1
            elif pps[1] == "連体化":
                levels[pos] = 1.5  # 「3種類の｜アクリルスタンド」: 札の区切りには向かないが、2行に分ける位置としては使える
            elif text[:pos].endswith(STRONG_END[:6]):
                levels[pos] = 3  # 「販売します｜ビルドが」: ます・です・ください等は必ず言い切り
            elif ends and ps[0] == "名詞" and ps[1] not in ("副詞可能",):
                levels[pos] = 1  # 「持っている｜人」「収納できる｜専用ケース」は名詞を修飾しているので切らない
            elif (ends or pps[0] == "助詞" and pps[1] == "終助詞"
                  or ps[0] in ("接続詞", "感動詞", "連体詞") or ps[0] == "副詞" and pps[0] == "名詞"):
                levels[pos] = 3  # 「ヤバすぎた｜今回は」のように言い切って次の文が始まる、体言止めの後の副詞・接続詞
            else:
                levels[pos] = 2
        pos += len(surf)
    return [levels.get(i, 0) for i in range(len(text) + 1)]


def boundary_score(ws, i, levels=None, offset=None):
    """ws[i] と ws[i+1] の間で切るときの自然さ（大きいほど良い）。

    Whisper の単語は「く」「ださい」のように細切れになることがあるので、前後の文字列で判定する。
    levels / offset があれば形態素解析の結果も使う（offset は ws[i+1] の先頭の文字位置）。
    """
    left = "".join(w["text"] for w in ws[max(0, i - 3):i + 1])
    right = "".join(w["text"] for w in ws[i + 1:i + 4])
    s = min(ws[i + 1]["ostart"] - ws[i]["oend"], 1.0) * 4  # 間（ま）が長いほど切れ目らしい
    if ws[i].get("seg") != ws[i + 1].get("seg"):
        s += 5  # Whisper の文の区切り（「ヤバすぎた｜今回は」）。文の途中で切るより優先する
    if levels is not None:
        s += {0: -10, 1: -2, 1.5: 0, 2: 1, 3: 3}[levels[offset]]
    if left[-1:] in PUNCT:
        s += 2
    left = left.rstrip(PUNCT)
    if left.endswith(STRONG_END):
        s += 3
    elif left.endswith(CLAUSE_END):
        s += 1.5
    elif left.endswith(PARTICLE_END):
        s += 0.7
    if right.startswith(NO_START):
        s -= 3
    if left.endswith(("て", "で")) and right.startswith(TE_AUX):
        s -= 4
    if QUESTION_TAIL.search(left):
        s -= 4  # 「多すぎて何から｜作れば」のような切り方を避ける
    if right.startswith(QUESTION):
        s += 1.5
    return s


def text_len(ws):
    return sum(len(w["text"].rstrip(PUNCT)) for w in ws)


def build_cues(words, corrections, total, max_line=14, max_cue=26, target=18, min_dur=0.8):
    """カット後の単語列からテロップを組む。戻り値は [(開始秒, テキスト), ...] と終了秒。"""
    ws = [dict(w, text=re.sub(r"\s+", "", w["text"])) for w in words]
    ws = [w for w in ws if w["text"]]
    if not ws:
        return [], total
    n = len(ws)
    lens = [len(w["text"].rstrip(PUNCT)) for w in ws]
    starts, pos = [], 0
    for w in ws:
        starts.append(pos)
        pos += len(w["text"])
    levels = morph_levels("".join(w["text"] for w in ws))
    score = [boundary_score(ws, i, levels, starts[i + 1]) for i in range(n - 1)]

    # 1) 区切り方を動的計画法で選ぶ: 自然な切れ目で、1枚 target 文字前後、短すぎる札（0.8秒未満・3文字以下）は避ける
    INF = float("inf")
    best, back = [0.0] + [INF] * n, [0] * (n + 1)
    for j in range(1, n + 1):
        L = 0
        for i in range(j - 1, -1, -1):
            L += lens[i]
            if L > max_cue and i < j - 1:
                break
            end = ws[j]["start"] if j < n else total
            cost = ((L - target) / target) ** 2 * 4
            cost += 3 if L <= 3 else 0
            cost += 2 if j < n and end - ws[i]["start"] < min_dur else 0
            cost -= score[j - 1] if j < n else 0
            if best[i] + cost < best[j]:
                best[j], back[j] = best[i] + cost, i
    bounds, j = [], n
    while j > 0:
        bounds.append((back[j], j))
        j = back[j]
    groups = [(i, j) for i, j in reversed(bounds)]

    # 4) 1行が max_line 文字を超えたら意味の切れ目で 2 行に分ける
    cues = []
    join = lambda a, b: clean("".join(w["text"] for w in ws[a:b]), corrections)  # noqa: E731
    for gi, (i, j) in enumerate(groups):
        text = join(i, j)
        if len(text) > max_line + 1 and j - i >= 2:
            # 2行の長さが釣り合い、意味の切れ目になる位置で改行する
            total_len = sum(lens[i:j])
            k = min(range(i + 1, j), key=lambda k: abs(sum(lens[i:k]) - total_len / 2) / total_len * 6
                    - score[k - 1])
            text = f"{join(i, k)}\\N{join(k, j)}"
        start = 0.0 if gi == 0 else ws[i]["start"]
        if cues and start <= cues[-1][0]:
            start = cues[-1][0] + 0.1
        cues.append((round(start, 2), text))
    return cues, round(max(total - 0.05, cues[-1][0] + 0.5), 2)


def read_cues(path):
    rows = [l.rstrip("\n").split("\t") for l in open(path, encoding="utf-8") if l.strip()]
    end = float(rows[-1][0]) if rows[-1][1] == "END" else None
    cues = [(float(t), x) for t, x in rows if x != "END"]
    return cues, end


def write_cues(path, cues, end):
    with open(path, "w", encoding="utf-8") as f:
        for t, x in cues:
            f.write(f"{t:.2f}\t{x}\n")
        f.write(f"{end:.2f}\tEND\n")


def auto_font_size(cues):
    longest = max((len(line) for _, x in cues for line in x.split("\\N")), default=0)
    if longest <= 15:
        return 120
    if longest <= 18:
        return 104
    return max(72, int(1944 / longest) - 4)  # 画面幅（左右余白 60）に収まる大きさ


def ts(t, ass):
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    if ass:
        return f"{h}:{m:02d}:{s:05.2f}"
    ms = int(round(t * 1000)) % 1000
    return f"{h:02d}:{m:02d}:{int(s):02d},{ms:03d}"


def write_subs(cues, end, srt_path, ass_path, size, font):
    """旧 mk2.py と同じ見た目: 中央・白・黒ぼかしボックス（Layer0）+ くっきり白文字（Layer1）。"""
    srt, ev = [], []
    for i, (a, t) in enumerate(cues):
        b = cues[i + 1][0] if i + 1 < len(cues) else end
        srt.append(f"{i + 1}\n{ts(a, 0)} --> {ts(b, 0)}\n{t.replace(chr(92) + 'N', chr(10))}\n")
        ev.append(f"Dialogue: 0,{ts(a, 1)},{ts(b, 1)},Tel,,0,0,0,,{{\\blur18}}{t}")
        ev.append(f"Dialogue: 1,{ts(a, 1)},{ts(b, 1)},Tel,,0,0,0,,{{\\bord0\\shad0}}{t}")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(srt))
    if not ass_path:
        return
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(f"""[Script Info]
ScriptType: v4.00+
PlayResX: 2160
PlayResY: 3840
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Tel,{font},{size},&H00FFFFFF,&H00FFFFFF,&H40000000,&H00000000,1,0,0,0,100,100,4,0,3,36,0,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + "\n".join(ev) + "\n")


# ---------------------------------------------------------------- 映像の書き出し
def render_video(ffmpeg, src, out_mp4, work, keeps, fps, start, audio_wav, ass):
    """映像のカット・テロップ焼き込み・音声差し替えを 1 回のエンコードで行う。

    ass フィルタに日本語や空白を含むパスを渡すとエスケープでつまずくため、作業フォルダに cd して
    ファイル名だけを渡す。select の式は長くなるのでフィルタをファイルに書いて読ませる。
    """
    filters = []
    if keeps is not None:
        # 半フレーム手前から選べば丸め誤差で取りこぼさない。iPhone の動画は可変フレームレートなので、
        # 番号で振り直さず各フレームの時刻からカットした長さだけを引いて、音声とずれないようにする
        rng = [(start + a - 0.5 / fps, start + b - 0.5 / fps, start + a - acc) for a, b, acc in
               zip([k[0] for k in keeps], [k[1] for k in keeps],
                   [sum(b - a for a, b in keeps[:i]) for i in range(len(keeps))])]
        sel = "+".join(f"between(t,{a:.6f},{b:.6f})" for a, b, _ in rng)
        shift = "+".join(f"between(T,{a:.6f},{b:.6f})*{d:.6f}" for a, b, d in rng)
        filters += [f"select='{sel}'", f"setpts='(T-({shift}))/TB'"]
    if ass:
        filters.append("ass=telop.ass")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", src]
    if audio_wav:
        cmd += ["-i", audio_wav, "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0", "-c:a", "copy"]
    if filters:
        with open(os.path.join(work, "vf.txt"), "w") as f:
            f.write(",".join(filters))
        cmd += ["-filter_script:v", "vf.txt", "-fps_mode", "vfr"]
    cmd += ["-c:v", "hevc_videotoolbox", "-b:v", "30M", "-tag:v", "hvc1",
            "-movflags", "+faststart", out_mp4]
    r = subprocess.run(cmd, cwd=work, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"[error] 動画の書き出しに失敗しました:\n{r.stderr[-1500:]}")


def snapshot(ffmpeg, video, t, png):
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", video,
                    "-vf", "scale=540:-1", "-frames:v", "1", png], check=False)


# ---------------------------------------------------------------- 解析
def analyze(args, ffmpeg, video):
    import numpy as np
    sr, ch = ca.probe(ffmpeg, args.input)
    audio = ca.decode(ffmpeg, args.input, sr, ch)
    audio16k = ca.decode(ffmpeg, args.input, 16000, 1)[:, 0]
    dur = len(audio) / sr
    print(f"[input] {args.input}  {dur:.2f}s  {sr}Hz  {ch}ch" + (f"  {video[0]}fps" if video else "  (音声のみ)"))

    mono = audio.mean(axis=1)
    hop = int(sr * 0.01)
    fr = mono[:len(mono) // hop * hop].reshape(-1, hop)
    db = 20 * np.log10(np.sqrt((fr ** 2).mean(axis=1) + 1e-12))
    thresh = args.silence_db if args.silence_db is not None else float(np.clip(np.percentile(db, 10) + 10, -50, -30))
    sil_cuts = [] if args.no_cut or args.no_silence_cut else ca.find_silences(audio, sr, thresh, args.min_silence, args.pad)
    print(f"[silence] 閾値 {thresh:.1f} dBFS / {args.min_silence}s 以上の無音 {len(sil_cuts)} 箇所")

    chunks, t = [], 0.0
    for a, b in ca.find_silences(audio, sr, thresh, 0.3, 0.0):
        if a > t:
            chunks.append((t, a))
        t = b
    if t < dur:
        chunks.append((t, dur))
    try:
        words, segs = ca.transcribe(audio16k, chunks, args.model, args.language)
    except Exception as e:
        sys.exit(f"[error] 文字起こしに失敗しました: {e}")

    rep_cuts, repeats = [], []
    if not args.no_cut and not args.no_repeat_cut:
        rep_cuts, repeats = ca.find_repeats(words, 2, 80, 0.15, args.max_gap)
        print(f"[repeat] 検出 {len(repeats)} 箇所")
        for r in repeats:
            print(f"   「{r['phrase']}」×{r['count']}  開始={r['copy_starts']}  → {r['removed'][0]}s〜{r['removed'][1]}s を削除")
    merged = []
    for a, b in sorted((max(0.0, a), min(dur, b)) for a, b in rep_cuts + sil_cuts if b > a):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    keeps = keeps_from_cuts(merged, dur, video[0] if video else None)
    if not keeps:
        sys.exit("[error] 全区間がカット対象になりました。--silence-db を下げてください。")

    # カット後の時間軸に写す（中央がカット区間に入った単語は捨てる）
    kept_words = []
    for w in words:
        mid = (w["start"] + w["end"]) / 2
        if any(a <= mid <= b for a, b in keeps):
            kept_words.append({"start": round(to_cut_time(w["start"], keeps), 3),
                               "end": round(to_cut_time(w["end"], keeps), 3),
                               "ostart": w["start"], "oend": w["end"], "text": w["text"], "seg": w.get("seg")})
    report = {
        "input": args.input, "input_sec": round(dur, 2), "output_sec": round(sum(b - a for a, b in keeps), 3),
        "fps": video[0] if video else None, "video_start": video[1] if video else None,
        "silence_threshold_db": round(thresh, 1),
        "transcript": [{"start": round(a, 2), "end": round(b, 2), "text": t} for a, b, t in segs],
        "repeats": repeats,
        "silence_cuts": [[round(a, 2), round(b, 2)] for a, b in sil_cuts],
        "all_cuts": [[round(a, 2), round(b, 2)] for a, b in merged],
        "keeps": [[a, b] for a, b in keeps],
        "words": kept_words,
    }
    return report, audio, sr


# ---------------------------------------------------------------- main
def main():
    t0 = time.time()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("-o", "--outdir", help="出力先（既定: 入力と同じフォルダ）")
    ap.add_argument("--prepare", action="store_true", help="解析とテロップ案の作成まで（書き出ししない）")
    ap.add_argument("--cues", help="このテロップファイルで書き出す（解析は前回の _report.json を再利用）")
    ap.add_argument("--no-cut", action="store_true", help="カットしない（テロップだけ付ける）")
    ap.add_argument("--no-telop", action="store_true", help="テロップを付けない（カットだけ）")
    ap.add_argument("--no-silence-cut", action="store_true", help="無音はカットしない（言い直しだけカット）")
    ap.add_argument("--no-repeat-cut", action="store_true", help="言い直しはカットしない（無音だけカット）")
    ap.add_argument("--font-size", type=int, help="文字サイズ（既定: 最長行から自動。15字以下 120 / 18字以下 104）")
    ap.add_argument("--font", default="Hiragino Mincho ProN")
    ap.add_argument("--corrections", default=os.path.join(HERE, "corrections.tsv"), help="誤認識の置換表")
    ap.add_argument("--model", default="medium")
    ap.add_argument("--language", default="ja")
    ap.add_argument("--silence-db", type=float, default=None)
    ap.add_argument("--min-silence", type=float, default=0.5)
    ap.add_argument("--pad", type=float, default=0.1)
    ap.add_argument("--max-gap", type=int, default=3)
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        sys.exit(f"[error] 入力ファイルが見つかりません: {args.input}")
    ca.ensure_packages()
    ffmpeg = ca.find_ffmpeg()
    video = probe_video(ffmpeg, args.input)

    outdir = args.outdir or os.path.dirname(os.path.abspath(args.input))
    os.makedirs(outdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(args.input))[0]
    p = lambda suffix: os.path.join(outdir, base + suffix)  # noqa: E731
    report_path, cues_path = p("_report.json"), p("_cues.txt")

    # 1) 解析（--cues のときは前回の結果を再利用して Whisper を回さない）
    if args.cues:
        if not os.path.isfile(report_path):
            sys.exit(f"[error] {report_path} がありません。先に --prepare で解析してください。")
        report = json.load(open(report_path, encoding="utf-8"))
        sr, ch = ca.probe(ffmpeg, args.input)
        audio = ca.decode(ffmpeg, args.input, sr, ch)
        cues, end = read_cues(args.cues)
    else:
        report, audio, sr = analyze(args, ffmpeg, video)
        json.dump(report, open(report_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        cues, end = build_cues(report["words"], load_corrections(args.corrections), report["output_sec"])
        write_cues(cues_path, cues, end)
    keeps = [tuple(k) for k in report["keeps"]]
    cut = not args.no_cut and report["all_cuts"]
    print(f"[cut] {report['input_sec']}s → {report['output_sec']:.2f}s（残す区間 {len(keeps)} 個）")
    print("[telop]")
    for t, x in cues:
        print(f"   {t:6.2f}  {x}")

    if args.prepare:
        print(f"[prepare] テロップ案: {cues_path}\n  修正後: --cues \"{cues_path}\" で書き出し（{time.time() - t0:.0f} 秒）")
        return

    # 2) 書き出し
    size = args.font_size or auto_font_size(cues)
    work = tempfile.mkdtemp(prefix="autoedit_")
    try:
        srt_path = p(".srt")
        if cues:
            write_subs(cues, end, srt_path, os.path.join(work, "telop.ass") if video else None, size, args.font)
        outputs = [srt_path] if cues else []
        if video:
            wav = None
            if cut:
                wav = os.path.join(work, "audio.wav")
                ca.encode(ffmpeg, render_audio(audio, sr, keeps), sr, wav, os.path.join(work, "audio.m4a"))
            out_mp4 = p("_telop.mp4" if not args.no_telop else "_cut.mp4")
            print(f"[render] エンコード中（文字サイズ {size}）...")
            render_video(ffmpeg, os.path.abspath(args.input), out_mp4, work,
                         keeps if cut else None, report["fps"], report["video_start"] or 0.0,
                         wav, cues and not args.no_telop)
            if cues:
                i = max(range(len(cues)), key=lambda k: len(cues[k][1]))
                nxt = cues[i + 1][0] if i + 1 < len(cues) else end
                snapshot(ffmpeg, out_mp4, (cues[i][0] + nxt) / 2, p("_preview.png"))
                outputs.append(p("_preview.png"))
            outputs.insert(0, out_mp4)
        else:
            stem = re.sub(r"[^\w\-]+", "_", base)
            out_m4a = os.path.join(outdir, f"{stem}_output.m4a")
            ca.encode(ffmpeg, render_audio(audio, sr, keeps), sr, os.path.join(outdir, f"{stem}_output.wav"), out_m4a)
            outputs.insert(0, out_m4a)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"[done] 処理時間 {time.time() - t0:.0f} 秒")
    for o in outputs + [cues_path, report_path]:
        print(f"   {o}")


if __name__ == "__main__":
    main()
