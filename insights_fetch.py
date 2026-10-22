#!/usr/bin/env python3
"""
Windsor.ai から Instagram のインサイトを取り直し、データページ（report.html）が読む JSON を書き換える。

    python insights_fetch.py          # insights/tokusatsu_box.json を最新にする

server.py が起動時と数時間おきに自動で実行する。API キーは .windsor_key（このフォルダ、本人だけ読める）か
WINDSOR_API_KEY から読む。途中で失敗したときは前のファイルをそのまま残す。
"""
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, ".windsor_key")
ACCOUNT = "17841428375624511"  # tokusatsu_box
OUT = os.path.join(HERE, "insights", "tokusatsu_box.json")
# 投稿ごとの数字を更新のたびに記録する。新作を「公開から同じ時間がたった時点」の過去の投稿と比べるため
HISTORY = os.path.join(HERE, "insights", "post_history.json")
API = "https://connectors.windsor.ai/instagram"
DAYS = 60  # 前の期間と比べるため、1ヶ月の2倍を取る
AGES = ["13-17", "18-24", "25-34", "35-44", "45-54", "55-64", "65+"]


def api_key():
    key = os.environ.get("WINDSOR_API_KEY", "").strip()
    if not key and os.path.isfile(KEY_FILE):
        key = open(KEY_FILE, encoding="utf-8").read().strip()
    if not key:
        sys.exit("[error] Windsor.ai の API キーがありません（.windsor_key か WINDSOR_API_KEY）")
    return key


def get(key, fields, preset):
    q = urllib.parse.urlencode({"api_key": key, "date_preset": preset, "fields": ",".join(fields),
                                "select_accounts": ACCOUNT})
    try:
        with urllib.request.urlopen(f"{API}?{q}", timeout=600) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error", "")
        except Exception:
            msg = e.reason
        sys.exit(f"[error] Windsor.ai から取得できませんでした（HTTP {e.code}）: {msg}")
    except urllib.error.URLError as e:
        sys.exit(f"[error] Windsor.ai につながりません: {e.reason}")
    return res.get("data", []) if isinstance(res, dict) else res


def write_json(path, obj):
    # 書き込み途中でページが読んでも壊れないよう、別名で書いてから置き換える
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def record_history(posts):
    """投稿ごとに [公開からの時間, リーチ, 閲覧, いいね, コメント, 保存, シェア] を追記する（60日より古い投稿は消す）。"""
    hist = json.load(open(HISTORY, encoding="utf-8")) if os.path.isfile(HISTORY) else {}
    now = datetime.datetime.now(datetime.timezone.utc)
    keep = {}
    for p in posts:
        if not p["id"] or not p["timestamp"]:
            continue
        ts = datetime.datetime.strptime(p["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
        age = round((now - ts).total_seconds() / 3600, 1)
        row = [age, p["reach"], p["views"], p["likes"], p["comments"], p["saved"], p["shares"]]
        rows = hist.get(p["id"], [])
        if not rows or rows[-1][1:] != row[1:]:  # 数字が変わったときだけ記録する
            rows.append(row)
        keep[p["id"]] = rows
    write_json(HISTORY, keep)


def num(v):
    return int(round(v)) if isinstance(v, (int, float)) else 0


def main():
    key = api_key()
    print("[insights] 取得中...")
    profile = get(key, ["username", "name", "biography", "website", "followers_count", "follows_count",
                        "media_count"], "last_7d")
    daily = get(key, ["date", "reach", "views", "accounts_engaged", "total_interactions", "likes", "comments",
                      "saves", "shares"], f"last_{DAYS}d")
    split = get(key, ["date", "reach_followers", "reach_non_followers"], f"last_{DAYS}d")
    # 新規フォロワーは Instagram の制限で直近30日しか取れない
    follows = get(key, ["date", "follower_count_1d"], "last_30d")
    media = get(key, ["date", "timestamp", "media_id", "media_type", "media_caption", "media_permalink", "media_reach", "media_views",
                      "media_like_count", "media_comments_count", "media_saved", "media_shares",
                      "media_reel_avg_watch_time", "media_reel_skip_rate"], f"last_{DAYS}d")
    audience = get(key, ["audience_gender_age_name", "audience_gender_age_size"], "last_7d")
    if not profile or not daily:
        sys.exit("[error] データが空でした。Windsor.ai の接続を確認してください")

    # 60日分の日付を最新日から逆算してそろえる（データの無い日は 0）
    last = max(datetime.date.fromisoformat(r["date"]) for r in daily)
    dates = [(last - datetime.timedelta(days=DAYS - 1 - i)).isoformat() for i in range(DAYS)]
    by_date = {}
    for rows in (daily, split):
        for r in rows:
            by_date.setdefault(r["date"], {}).update(r)
    keymap = {"reach": "reach", "views": "views", "engaged": "accounts_engaged", "interactions": "total_interactions",
              "likes": "likes", "comments": "comments", "saves": "saves", "shares": "shares",
              "reach_followers": "reach_followers", "reach_non_followers": "reach_non_followers"}
    out_daily = {k: [num(by_date.get(d, {}).get(src)) for d in dates] for k, src in keymap.items()}
    nf = {r["date"]: num(r.get("follower_count_1d")) for r in follows}
    out_daily["new_followers"] = [nf.get(d) for d in dates]  # 30日より前は None（データなし）

    posts = []
    for m in media:
        cap = (m.get("media_caption") or "").strip()
        watch = m.get("media_reel_avg_watch_time")
        posts.append({
            "id": m.get("media_id") or "", "timestamp": m.get("timestamp") or "",
            "date": m["date"],
            "type": "CAROUSEL" if m.get("media_type") == "CAROUSEL_ALBUM" else (m.get("media_type") or ""),
            "title": cap.splitlines()[0] if cap else "（キャプションなし）",
            "caption": cap[:1500],  # 台本づくりで文体・構成を参考にする
            "url": m.get("media_permalink") or "",
            "reach": num(m.get("media_reach")), "views": num(m.get("media_views")),
            "likes": num(m.get("media_like_count")), "comments": num(m.get("media_comments_count")),
            "saved": num(m.get("media_saved")), "shares": num(m.get("media_shares")),
            "avg_watch_sec": None if watch is None else round(watch / 1000, 1),
            "skip_rate": m.get("media_reel_skip_rate"),
        })
    posts = [p for p in posts if p["reach"] > 0]  # 公開直後などでまだ数字の無い投稿は除く
    posts.sort(key=lambda p: p["date"])

    aud = {a: {"age": a, "M": 0, "F": 0, "U": 0} for a in AGES}
    for r in audience:
        g, _, age = (r.get("audience_gender_age_name") or "").partition(".")
        if age in aud and g in ("M", "F", "U"):
            aud[age][g] = num(r.get("audience_gender_age_size"))

    p = profile[0]
    data = {
        "source": "Windsor.ai（Instagram インサイト）",
        "fetched_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "last_date": dates[-1],
        "profile": {"username": p.get("username"), "name": p.get("name"), "biography": p.get("biography") or "",
                    "website": p.get("website") or "", "followers": num(p.get("followers_count")),
                    "following": num(p.get("follows_count")), "media_count": num(p.get("media_count"))},
        "dates": dates, "daily": out_daily, "posts": posts,
        "audience_age_gender": [aud[a] for a in AGES],
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    record_history(posts)
    write_json(OUT, data)
    print(f"[insights] 更新しました: {dates[0]}〜{dates[-1]}、投稿 {len(posts)} 件、フォロワー {data['profile']['followers']}")


if __name__ == "__main__":
    main()
