#!/usr/bin/env python3
"""
動画と「真似したいキャプション」から、Instagram 用キャプションを複数案つくる。

使い方:
    python insta_caption.py 動画.mov --benchmark 真似したい.txt          # 解析 → 生成まで一括
    python insta_caption.py 動画.mov --benchmark 真似したい.txt -n 5     # 5案つくる
    python insta_caption.py 動画.mov --benchmark 真似したい.txt --no-llm # 解析と生成用プロンプトまで（Claude Code で仕上げる）
    pbpaste | python insta_caption.py 動画.mov --benchmark -             # クリップボードのキャプションを使う

処理の流れ:
    1. 動画の解析: faster-whisper で文字起こし、ffmpeg で等間隔に静止画を抜き出す
    2. お手本の分解: 区切り線・ランキング/箇条書き・→の補足・CTA・メンション・ハッシュタグを判定し、
       行ごとの役割を並べた「型（テンプレート）」にする
    3. 生成: 型・文字起こし・静止画を Gemini に渡し、型に沿ったオリジナルのキャプションを複数案つくる
       （Gemini の API キーが無いときは、生成用プロンプトを書き出して止まる）

出力（動画と同じフォルダの <名前>_caption/）:
    frames/*.jpg      抜き出した静止画
    analysis.json     文字起こし・お手本の型
    prompt.md         生成に使ったプロンプト（Claude Code に渡しても使える）
    captions.md       完成したキャプション案
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cut_audio as ca  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, ".gemini_key")
# 無料枠で使える Gemini。上限や混雑で断られたら次のモデルで試す
MODELS = ["gemini-flash-latest", "gemini-3.8-flash", "gemini-3.7-flash"]  # gemini-2.5-flash は新規ユーザーに提供終了
API = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
SEPARATOR = re.compile(r"^[\s\-ー－―─━=＝_＿~〜・.。…*＊]{6,}$")
LIST_HEAD = re.compile(r"^(✅|☑️|✔️|✓|・|●|■|◆|▶️?|►|[①-⑳]|\d+[.)．、]|第\s*\d+\s*位|No\.?\s*\d+|【\d+】)")
ARROW = re.compile(r"^(→|⇒|➡️?|👉|└)")
CTA_WORDS = ("教えて", "コメント", "保存", "フォロー", "プロフ", "リンク", "DM", "チェック", "いいね", "シェア",
             "お待ちして", "ください", "してね", "開発中", "販売中", "予約")
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


# ---------------------------------------------------------------- 1. 動画の解析
def duration_of(ffmpeg, path):
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def extract_frames(ffmpeg, path, out_dir, count):
    """動画を count 等分した各区間の中央から 1 枚ずつ、長辺 768px の JPEG を抜き出す。"""
    os.makedirs(out_dir, exist_ok=True)
    dur = duration_of(ffmpeg, path)
    frames = []
    for i in range(count):
        t = dur * (i + 0.5) / count
        out = os.path.join(out_dir, f"frame_{i + 1:02d}_{t:05.1f}s.jpg")
        subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
                        "-vf", "scale='if(gt(iw,ih),768,-2)':'if(gt(iw,ih),-2,768)'", "-q:v", "4", out],
                       check=True)
        frames.append({"time": round(t, 1), "path": out})
    return dur, frames


def transcribe(ffmpeg, path, model_name):
    """音声を文字起こしして [(開始秒, 終了秒, テキスト)] を返す。無音区間は VAD で飛ばす。"""
    from faster_whisper import WhisperModel
    audio = ca.decode(ffmpeg, path, 16000, 1)[:, 0]
    print(f"[whisper] モデル '{model_name}' で文字起こし中...")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(audio, language="ja", vad_filter=True, beam_size=5,
                                   condition_on_previous_text=False)
    out = []
    for s in segments:
        text = s.text.strip()
        if text and s.no_speech_prob < 0.8:
            out.append((round(s.start, 1), round(s.end, 1), text))
    print(f"[whisper] {len(out)} 区間")
    return out


# ---------------------------------------------------------------- 2. お手本の分解
def line_role(line, seen_list):
    s = line.strip()
    if not s:
        return "空行"
    if SEPARATOR.match(s):
        return "区切り線"
    if all(t.startswith(("#", "＃")) for t in s.split()):
        return "ハッシュタグ"
    if re.fullmatch(r"(→\s*)?@[\w.]+", s):
        return "メンション"
    if ARROW.match(s):
        return "補足（→）"
    if LIST_HEAD.match(s):
        return "項目見出し"
    if seen_list and any(w in s for w in CTA_WORDS):
        return "CTA"
    return "本文"


def analyze_benchmark(text):
    """お手本を行ごとの役割に分け、構成の骨組み（型）と数値的な特徴を返す。"""
    lines = text.strip("\n").splitlines()
    roles, seen_list = [], False
    for ln in lines:
        r = line_role(ln, seen_list)
        seen_list = seen_list or r == "項目見出し"
        roles.append(r)
    # 最初の本文（区切り線や項目より前）をフックとみなす
    for i, r in enumerate(roles):
        if r == "本文":
            roles[i] = "フック"
            break
        if r in ("区切り線", "項目見出し"):
            break

    skeleton, prev = [], None
    for ln, r in zip(lines, roles):
        if r == "区切り線":
            skeleton.append(ln.strip())
        elif r == "空行":
            if prev != "空行":
                skeleton.append("")
        elif r == "項目見出し":
            head = LIST_HEAD.match(ln.strip()).group(0)
            skeleton.append(f"{head}〈項目名〉")
        elif r == "補足（→）":
            skeleton.append(f"{ARROW.match(ln.strip()).group(0)}〈補足・ベネフィット〉")
        elif r == "ハッシュタグ":
            skeleton.append("#〈ハッシュタグ〉")
        else:
            skeleton.append(f"〈{r}〉")
        prev = r

    hashtags = re.findall(r"[#＃][^\s#＃]+", text)
    items = [ln.strip() for ln, r in zip(lines, roles) if r == "項目見出し"]
    return {
        "lines": [{"text": ln, "role": r} for ln, r in zip(lines, roles)],
        "skeleton": "\n".join(skeleton),
        "stats": {
            "文字数": len(text.strip()),
            "項目数": len(items),
            "項目の書き出し": [LIST_HEAD.match(i).group(0) for i in items],
            "ハッシュタグ": hashtags,
            "ハッシュタグ数": len(hashtags),
            "英語ハッシュタグ数": sum(bool(re.fullmatch(r"[#＃][A-Za-z0-9_]+", h)) for h in hashtags),
            "メンション": re.findall(r"@[\w.]+", text),
            "絵文字": sorted(set(EMOJI.findall(text))),
            "！の数": text.count("！") + text.count("!"),
        },
    }


# ---------------------------------------------------------------- 3. 生成
SYSTEM = """あなたは日本語の Instagram 運用に強いコピーライターです。
お手本キャプションの「型」（構成・リズム・記号の使い方・トーン・CTA・ハッシュタグの組み方）を分析し、
その型に、渡された動画の内容をはめ込んだオリジナルのキャプションを書きます。

守ること:
- お手本の言い回しや固有名詞をそのまま流用しない。項目・数字・ベネフィットはすべて動画の内容から作る
- 動画（文字起こし・静止画）から読み取れない事実は作らない。数字や名称が不明なら【要確認: ○○】と書く
- お手本の区切り線・✅や→などの記号・改行のリズム・ハッシュタグの並べ方（1行1つ等）・CTA の位置は踏襲する
- 項目数は動画の内容に合わせてよい（無理にお手本と同じ数にしない）
- アカウント名（@〜）はお手本と同じものを使う（同じアカウントの投稿として書く）
- 各案はフックの切り口を変える（例: 結論先出し / 問いかけ / 悩みへの共感 / 意外性 / 数字）"""

SCHEMA = {
    "type": "object",
    "properties": {
        "benchmark_analysis": {
            "type": "object",
            "properties": {
                "hook": {"type": "string", "description": "冒頭フックの型と狙い"},
                "body": {"type": "string", "description": "本文の展開の型"},
                "cta": {"type": "string", "description": "CTA の型"},
                "hashtags": {"type": "string", "description": "ハッシュタグの構成（ビッグ/ブランド/ジャンル等）"},
                "tone": {"type": "string", "description": "トーン・記号・絵文字の使い方"},
            },
            "required": ["hook", "body", "cta", "hashtags", "tone"],
            "additionalProperties": False,
        },
        "video_summary": {"type": "string", "description": "動画の主題・見どころ・伝えたいこと"},
        "captions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "angle": {"type": "string", "description": "この案のフックの切り口"},
                    "caption": {"type": "string", "description": "そのまま投稿できるキャプション全文"},
                },
                "required": ["angle", "caption"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["benchmark_analysis", "video_summary", "captions"],
    "additionalProperties": False,
}


def build_prompt(benchmark, bench, transcript, frames, n, note):
    tr = "\n".join(f"[{a:5.1f}-{b:5.1f}s] {t}" for a, b, t in transcript) or "（音声なし・聞き取れず）"
    stats = json.dumps(bench["stats"], ensure_ascii=False, indent=2)
    fr = "\n".join(f"- {os.path.basename(f['path'])}（{f['time']}秒）" for f in frames)
    note_block = f"\n# 補足（投稿者より）\n{note}\n" if note else ""
    return f"""# お手本キャプション
<benchmark>
{benchmark.strip()}
</benchmark>

# お手本の骨組み（スクリプトによる自動分解。〈〉が差し替える部分）
<skeleton>
{bench["skeleton"]}
</skeleton>

<benchmark_stats>
{stats}
</benchmark_stats>

# 動画の文字起こし（Whisper）
<transcript>
{tr}
</transcript>

# 動画の静止画（時刻順に添付）
{fr}
{note_block}
# 依頼
1. お手本の型を分析する（フック / 本文の展開 / CTA / ハッシュタグ構成 / トーン）
2. 文字起こしと静止画から、動画の主題と見どころをまとめる
3. お手本の型に動画の内容をはめ込み、キャプションを {n} 案つくる。各案はフックの切り口を変える
"""


class GeminiError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def gemini_key():
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"].strip()
    if os.path.isfile(KEY_FILE):
        return open(KEY_FILE, encoding="utf-8").read().strip()
    return ""


def call_gemini(model, key, body):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(API.format(model), data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        raise GeminiError(e.code, msg or e.reason)
    except (TimeoutError, urllib.error.URLError) as e:  # 返事が無い・つながらないときは混雑扱いで次のモデルへ
        raise GeminiError(503, f"Gemini から返事がありません（{e}）")


def generate(prompt, frames, n):
    key = gemini_key()
    if not key:
        raise RuntimeError("Gemini の API キーがありません（.gemini_key か GEMINI_API_KEY）")
    parts = []
    for f in frames:
        with open(f["path"], "rb") as fp:
            data = base64.standard_b64encode(fp.read()).decode("utf-8")
        parts.append({"text": f"{f['time']}秒の静止画:"})
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": data}})
    parts.append({"text": prompt})
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": SCHEMA,
                             "temperature": 1.0, "maxOutputTokens": 16000},
    }
    last = None
    for model in MODELS:
        print(f"[gemini] {model} で {n} 案を生成中...")
        try:
            res = call_gemini(model, key, body)
        except GeminiError as e:
            last = e
            if e.status in (404, 429, 500, 503):  # 無料枠の上限・混雑・モデル廃止は次のモデルへ
                print(f"[gemini] {model} は使えませんでした（{e}）")
                continue
            raise
        cand = (res.get("candidates") or [{}])[0]
        if cand.get("finishReason") not in (None, "STOP"):
            raise RuntimeError(f"生成が途中で止まりました（{cand.get('finishReason')}）")
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))
        if not text:
            raise RuntimeError("Gemini から返答がありませんでした" + (f"（{res.get('promptFeedback')}）" if res.get("promptFeedback") else ""))
        return json.loads(text)
    raise last


def write_markdown(path, result):
    a = result["benchmark_analysis"]
    out = ["# Instagram キャプション案", "",
           "## お手本の型", "",
           f"- **フック**: {a['hook']}", f"- **本文**: {a['body']}", f"- **CTA**: {a['cta']}",
           f"- **ハッシュタグ**: {a['hashtags']}", f"- **トーン**: {a['tone']}", "",
           "## 動画の要約", "", result["video_summary"], ""]
    for i, c in enumerate(result["captions"], 1):
        out += [f"## 案{i}：{c['angle']}", "", "```text", c["caption"].strip(), "```", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


# ---------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(description="動画とお手本キャプションから Instagram キャプションを生成する")
    p.add_argument("video", help="動画ファイル")
    p.add_argument("--benchmark", required=True, help="お手本キャプションのテキストファイル（- で標準入力）")
    p.add_argument("-n", type=int, default=3, help="生成する案の数（既定 3）")
    p.add_argument("--frames", type=int, default=8, help="抜き出す静止画の枚数（既定 8）")
    p.add_argument("--model", default="small", help="Whisper モデル（既定 small。精度重視なら medium）")
    p.add_argument("--note", default="", help="補足（商品名・価格・告知など動画に無い情報）")
    p.add_argument("--no-llm", action="store_true", help="解析とプロンプト作成まで（生成は Claude Code で行う）")
    p.add_argument("-o", "--outdir", help="出力フォルダ（既定: 動画と同じ場所の <名前>_caption/）")
    args = p.parse_args()

    benchmark = sys.stdin.read() if args.benchmark == "-" else open(args.benchmark, encoding="utf-8").read()
    stem = re.sub(r"[^\w\-]+", "_", os.path.splitext(os.path.basename(args.video))[0]).strip("_")
    outdir = args.outdir or os.path.join(os.path.dirname(os.path.abspath(args.video)), f"{stem}_caption")
    os.makedirs(outdir, exist_ok=True)

    ffmpeg = ca.find_ffmpeg()
    dur, frames = extract_frames(ffmpeg, args.video, os.path.join(outdir, "frames"), args.frames)
    print(f"[video] {dur:.1f}秒、静止画 {len(frames)} 枚")
    transcript = transcribe(ffmpeg, args.video, args.model)
    bench = analyze_benchmark(benchmark)

    with open(os.path.join(outdir, "analysis.json"), "w", encoding="utf-8") as f:
        json.dump({"video": os.path.abspath(args.video), "duration": dur, "frames": frames,
                   "transcript": transcript, "benchmark": bench}, f, ensure_ascii=False, indent=2)
    prompt = build_prompt(benchmark, bench, transcript, frames, args.n, args.note)
    with open(os.path.join(outdir, "prompt.md"), "w", encoding="utf-8") as f:
        f.write(SYSTEM + "\n\n" + prompt)

    if args.no_llm:
        print(f"[done] 解析のみ: {outdir}/prompt.md を Claude に渡して生成してください")
        return
    try:
        result = generate(prompt, frames, args.n)
    except Exception as e:  # 認証エラーなど。解析結果は残っているので prompt.md で続行できる
        print(f"[gemini] 生成に失敗しました: {e}\n{outdir}/prompt.md を Claude Code に渡して生成してください")
        return
    md = os.path.join(outdir, "captions.md")
    write_markdown(md, result)
    with open(os.path.join(outdir, "captions.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[done] {md}")


if __name__ == "__main__":
    main()
