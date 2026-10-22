#!/usr/bin/env python3
"""
監査レポートのデータ（insights/tokusatsu_box.json）をもとに、次のリールの台本と「効果的な理由」を Gemini で作る。

    python script_gen.py                      # おまかせで1本
    python script_gen.py --theme "フルボトルケース再販"   # テーマを指定

サイトでは server.py の /api/script から呼ばれる。理由には必ずデータの数字を根拠として書かせる。
"""
import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from insta_caption import MODELS, GeminiError, call_gemini, gemini_key  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "insights", "tokusatsu_box.json")
BUZZ = os.path.join(HERE, "insights", "buzzwords.json")  # 冒頭3秒に使う心理的バズワード（PDF から取り込み）
HOOK_PRINCIPLES = ["自分ごと化", "知らない不安（損失回避）", "答えを隠す（情報の空白）", "みんなが選んでいる安心", "常識をひっくり返す"]

TYPES = [  # report.html と同じ分け方
    ("参加型（募集・ランキング）", ("募集", "リクエスト", "ランキング", "1位", "トップ", "人気", "教えて")),
    ("告知・セール", ("オフ", "セール", "販売", "損", "出展", "イベント", "記念", "クーポン")),
    ("お役立ち・解説", ("知って", "方法", "比較", "違い", "解説", "おすすめ", "コツ")),
    ("制作の裏側", ("制作", "製作", "開始", "裏側", "作り方")),
    ("新作紹介", ("登場", "完成", "新シリーズ", "ついに")),
]

SYSTEM = """あなたは特撮おもちゃ専用ケースのブランド「特撮ボックス」の Instagram リール企画・構成作家です。
渡されたアカウントの実データ（投稿ごとの成績、型ごとの比較、視聴者層）だけを根拠に、次に投稿するリールの台本を1本書きます。

目的: 視聴維持率を上げる（最初の3秒で離脱させない・最後まで見たくなる展開）、保存・シェア・コメントを増やす、見た人をファン（フォロワー）にする。

守ること:
- 0〜3秒のセリフには必ずバズワードを入れる。「バズワード一覧」か「新しいバズワード」から選び、〇〇は動画の内容で埋める。
  使った言葉と、どの心理（共通点）を狙ったかを hook に書く。お金・セールの「損」は伸びていない（データ参照）ので、知識や飾り方の「知らないと損」に使う
- 0〜3秒の映像は「完成形」か「一番の見どころ」
- 1カット3秒前後のテンポ。全体15〜30秒
- ナレーションとテロップは一言一句同じ。各シーンのセリフ（line）を1つだけ書き、それをそのまま読み上げ・表示する。
  テロップにだけある言葉や、ナレーションにだけある言葉を作らない
- セリフは1シーン28文字以内。1行14文字以内で、意味の切れ目に改行（\n）を入れて最大2行（縦型動画の中央に出す前提）。
  3秒のシーンなら20文字前後が読み切れる長さ
- 最後はコメントで答えたくなる問いかけで締める（例：次に作ってほしいケースは？）
- 価格・日付・個数・商品名など、データやテーマに無い事実は作らない。必要なら【要確認：○○】と書く
- キャプションは、このアカウントの過去のキャプションの構成（【】の見出し、区切り線「ーーー」、✅と→の箇条書き、@tokusatsu_box、1行1つのハッシュタグ）に合わせる
- 理由（reasons）は必ずデータの具体的な数字を引用して説明する。数字の無い一般論だけの理由は書かない
- 数字は渡されたデータのものをそのまま使い、自分で足し算・平均などの計算をしない
- 本数が1本だけの型やケースは「1本だけの実績」と明記し、型全体の傾向のようには書かない
- 店主の肩書きはプロフィールにある「特撮ファン歴20年の店主」を使い、「プロ」など無い肩書きは作らない
- 「避ける切り口」が渡されたら、それとは違うテーマ・構成にする"""

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "投稿の1行目（フックになる見出し）"},
        "concept": {"type": "string", "description": "企画の狙いを1〜2文で"},
        "target": {"type": "string", "description": "誰に向けた投稿か"},
        "post_type": {"type": "string", "description": "投稿の型（新作紹介・制作の裏側・お役立ち・参加型 など）"},
        "duration_sec": {"type": "integer"},
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "time": {"type": "string", "description": "例: 0〜3秒"},
                    "visual": {"type": "string", "description": "映す内容・カメラ"},
                    "line": {"type": "string", "description": "セリフ。テロップとナレーションの両方にそのまま使う（改行は\\n、最大2行）"},
                    "purpose": {"type": "string", "description": "このシーンの役割（離脱防止・保存させる等）"},
                },
                "required": ["time", "visual", "line", "purpose"],
            },
        },
        "hook": {
            "type": "object",
            "properties": {
                "buzzword": {"type": "string", "description": "0〜3秒に使ったバズワード（〇〇を埋めた形）"},
                "base": {"type": "string", "description": "元にした一覧の言葉"},
                "source": {"type": "string", "enum": ["バズワード一覧", "新しいバズワード"]},
                "principle": {"type": "string", "description": "狙った心理（共通点の名前）"},
            },
            "required": ["buzzword", "base", "source", "principle"],
        },
        "caption": {"type": "string", "description": "そのまま投稿できるキャプション全文"},
        "reasons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "point": {"type": "string", "description": "台本のどの工夫か"},
                    "why": {"type": "string", "description": "なぜ効くか"},
                    "evidence": {"type": "string", "description": "根拠となるデータの数字"},
                },
                "required": ["point", "why", "evidence"],
            },
        },
    },
    "required": ["title", "concept", "target", "post_type", "duration_sec", "hook", "scenes", "caption", "reasons"],
}


def kind(title):
    for name, words in TYPES:
        if any(w in title for w in words):
            return name
    return "その他"


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def buzz_context(posts):
    """バズワード一覧・共通点・新しいバズワードと、このアカウントでの実績（心理の型を使ったタイトルの成績）。"""
    if not os.path.isfile(BUZZ):
        return {}
    b = json.load(open(BUZZ, encoding="utf-8"))
    import re
    pats = {p["name"]: re.compile(p["pattern"]) for p in b["principles"]}
    used, plain = [], []
    for p in posts:
        hits = [k for k in HOOK_PRINCIPLES if pats[k].search(p["title"])]
        (used if hits else plain).append((p, hits))
    examples = [f"「{p['title']}」リーチ{p['reach']:,}（{'・'.join(h)}）" for p, h in sorted(used, key=lambda x: -x[0]["reach"])]
    return {
        "バズワード一覧（カテゴリ別）": {c["name"] + "：" + c["effect"]: c["words"] for c in b["categories"]},
        "バズワードの共通点": b["core"],
        "心理の型": [f"{p['name']}：{p['desc']}" for p in b["principles"]],
        "新しいバズワード（共通点から開発）": {w["word"]: "×".join(w["principles"]) for w in b["new_words"]},
        "このアカウントでの実績": [
            f"心理の型を使ったタイトルの投稿：リーチ中央値{med([p['reach'] for p, _ in used]):,.0f}（{len(used)}本）、"
            f"使っていない投稿：{med([p['reach'] for p, _ in plain]):,.0f}（{len(plain)}本）",
            "使った投稿：" + " / ".join(examples),
            "お金・セールの「損」（1日1000円損・1,980円も損）はリーチ8,375・5,514と伸びず、知識の「知っておきたい」は85,196",
        ],
    }


def build_context(d):
    """台本づくりに渡すデータの要約（数字は report.html と同じ計算）。"""
    posts = sorted(d["posts"], key=lambda p: p.get("timestamp") or p["date"])
    rows = []
    for p in posts:
        r = p["reach"] or 1
        rows.append({
            "date": p["date"], "title": p["title"], "型": kind(p["title"]),
            "種類": "リール" if p.get("avg_watch_sec") is not None else "カルーセル",
            "リーチ": p["reach"], "閲覧数": p["views"], "いいね": p["likes"], "保存": p["saved"], "シェア": p["shares"],
            "コメント": p["comments"], "保存率": f"{p['saved'] / r:.1%}", "シェア率": f"{p['shares'] / r:.1%}",
            "平均視聴秒": p.get("avg_watch_sec"),
            "冒頭3秒離脱率": None if p.get("skip_rate") is None else f"{p['skip_rate']:.0%}",
        })
    by_kind = {}
    for p in posts:
        by_kind.setdefault(kind(p["title"]), []).append(p)
    kinds = {k: {"本数": len(v), "リーチ中央値" if len(v) > 1 else "リーチ（1本だけ）": med([p["reach"] for p in v]),
                 "保存率中央値": f"{med([p['saved'] / p['reach'] for p in v]):.2%}",
                 "コメント中央値": med([p["comments"] for p in v])} for k, v in by_kind.items()}
    reels = [p for p in posts if p.get("skip_rate") is not None]
    skip_note = None
    if len(reels) >= 6:
        ms = med([p["skip_rate"] for p in reels])
        lo = [p["reach"] for p in reels if p["skip_rate"] <= ms]
        hi = [p["reach"] for p in reels if p["skip_rate"] > ms]
        skip_note = f"冒頭3秒の離脱率が{ms:.0%}以下のリールはリーチ中央値{med(lo):,.0f}（{len(lo)}本）、超えるリールは{med(hi):,.0f}（{len(hi)}本）"
    top = max(posts, key=lambda p: p["reach"])
    dates, nf = d["dates"], d["daily"]["new_followers"]
    follow_note = None
    if top["date"] in dates:
        i = dates.index(top["date"])
        if i >= 7 and i + 7 <= len(dates) and nf[i - 7] is not None:
            follow_note = f"「{top['title']}」公開前7日の新規フォロワー{sum(nf[i - 7:i])}人 → 公開後7日{sum(nf[i:i + 7])}人"
    # 視聴者層は合計まで計算して渡す（モデルに足し算させない）
    ages = d["audience_age_gender"]
    total = sum(a["M"] + a["F"] + a["U"] for a in ages) or 1
    by_age = sorted(((a["age"], a["M"] + a["F"] + a["U"]) for a in ages), key=lambda x: -x[1])
    by_gender = {g: sum(a[k] for a in ages) for g, k in (("男性", "M"), ("女性", "F"), ("性別不明", "U"))}
    # 文体の参考に、伸びた投稿と伸びなかった投稿のキャプションを全文で渡す
    ranked = sorted(posts, key=lambda p: -p["reach"])
    examples = [{"評価": "伸びた", "title": p["title"], "リーチ": p["reach"], "caption": p.get("caption", "")} for p in ranked[:3]] + \
               [{"評価": "伸びなかった", "title": p["title"], "リーチ": p["reach"], "caption": p.get("caption", "")} for p in ranked[-2:]]
    return {
        "アカウント": {"名前": d["profile"]["name"], "ユーザー名": d["profile"]["username"], "フォロワー": d["profile"]["followers"],
                   "プロフィール": d["profile"]["biography"]},
        "フォロワーの年齢（多い順）": [f"{age}歳: {n}人（{n / total:.0%}）" for age, n in by_age[:4]],
        "フォロワーの性別": {g: f"{n}人（{n / total:.0%}）" for g, n in by_gender.items()},
        "投稿ごとの成績（古い順）": rows,
        "型ごとの比較": kinds,
        "分かっている傾向": [x for x in [skip_note, follow_note] if x],
        "過去のキャプション例": examples,
        **buzz_context(posts),
    }


def generate(theme="", avoid=None):
    key = gemini_key()
    if not key:
        raise RuntimeError("Gemini の API キーが設定されていません。")
    data = json.load(open(DATA, encoding="utf-8"))
    ctx = build_context(data)
    ask = "次に投稿するリールの台本を1本作ってください。"
    if theme:
        ask += f"\nテーマ（必ずこれを扱う）: {theme}"
    else:
        ask += "\nテーマはおまかせ。データで最も伸びている型・切り口を優先してください。"
    if avoid:
        ask += "\n避ける切り口（前に提案済み。これとは違う企画にする）: " + " / ".join(avoid[-5:])
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": "# アカウントのデータ\n" + json.dumps(ctx, ensure_ascii=False, indent=1) + "\n\n# 依頼\n" + ask}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": SCHEMA,
                             "temperature": 1.0, "maxOutputTokens": 16000},
    }
    last = None
    for model in MODELS:
        try:
            res = call_gemini(model, key, body)
        except GeminiError as e:
            last = e
            if e.status in (404, 429, 500, 503):  # 無料枠の上限・混雑・モデル廃止は次のモデルへ
                continue
            raise
        cand = (res.get("candidates") or [{}])[0]
        if cand.get("finishReason") not in (None, "STOP"):
            raise RuntimeError(f"生成が途中で止まりました（{cand.get('finishReason')}）")
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))
        if not text:
            raise RuntimeError("Gemini から返答がありませんでした")
        out = json.loads(text)
        out["model"] = model
        for sc in out.get("scenes", []):
            # ナレーションとテロップは同じ文言。テロップは改行付き、ナレーションは改行を除いたもの
            line = sc.get("line", "").replace("\\n", "\n").strip()
            sc["telop"] = line
            sc["narration"] = line.replace("\n", "")
        return out
    raise last


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--theme", default="")
    args = ap.parse_args()
    print(json.dumps(generate(args.theme), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
