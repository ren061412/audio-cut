---
name: insta-report
description: サイトの「データ」ページ（Instagram プロフィール監査レポート）の数字を最新にする。「インサイトを更新」「レポートを最新に」「監査レポート」などの依頼で使う。
---

# Instagram 監査レポートのデータ更新

サイトの「データ」ページ（`report.html`）は `insights/tokusatsu_box.json` を読んでグラフ・監査を描く。
**この JSON は server.py が自動で更新する**（起動時にデータが1時間より古ければすぐ、その後3時間おき）。
中身は `insights_fetch.py` が Windsor.ai の REST API から取り直す。API キーは `.windsor_key`（本人だけ読める）。

## すぐ最新にしたいとき
```bash
/Users/Riku/Documents/audio-cut/.venv/bin/python /Users/Riku/Documents/audio-cut/insights_fetch.py
```
約40秒。失敗しても前のファイルは残る。直近の自動更新の結果は `http://127.0.0.1:8765/api/insights/status`。
スクリプトが使えないときは、下の手順で Windsor.ai の MCP から取り直して JSON を手で作る。

## ページの中身（report.html の JavaScript がデータから計算）
- 新作投稿の評価: 最新の投稿を、直前の投稿と直近10本の中央値で比べて5段階（良好〜悪い）。公開72時間未満のリーチは
  `insights/post_history.json`（更新のたびに投稿ごとの数字を記録）で「同じ経過時間」の値と比べる
- 改善案: 投稿の型・冒頭離脱率・保存率・週の投稿数・コメント・伸びた投稿後のフォロー獲得を比べ、差が大きい順に根拠つきで表示。
  比べる片方が2本（2週）未満のものは出さない
- 次の投稿の台本（AI 提案）: `script_gen.py` が JSON の要約（投稿ごとの成績・型ごとの比較・離脱率の傾向・視聴者層・
  伸びた/伸びなかった投稿のキャプション全文）を Gemini に渡し、台本・キャプション・根拠つきの理由を返す。
  サーバーの `POST /api/script {theme, avoid}`。「別の台本を作る」は直近5本のタイトルを avoid に入れて別の切り口にする
- バズワード: `insights/buzzwords.json`（PDF「投稿に使える心理的なバズワード172選」から重複を除いた160語・10カテゴリ、
  共通点 core、8つの心理 principles（タイトル判定用の正規表現 pattern 付き）、新しいバズワード new_words 20個）。
  台本の0〜3秒には必ずこの一覧か新しいバズワードを使い、hook に記録する。各シーンは line 1つ（テロップ＝ナレーション、一言一句同じ）

## 取得するもの（get_data、accounts は上の ID）
1. プロフィール: `username, name, biography, website, followers_count, follows_count, media_count`（date_preset `last_7d`）
2. 日別: `date, reach, views, accounts_engaged, total_interactions, likes, comments, saves, shares`（`last_60d`）
3. フォロワー別リーチ: `date, reach_followers, reach_non_followers`（`last_60d`）
4. 新規フォロワー: `date, follower_count_1d`（**`last_30d` まで**。60日を頼むとエラー）
5. 投稿: `date, media_type, media_caption, media_permalink, media_reach, media_views, media_like_count, media_comments_count, media_saved, media_shares, media_reel_avg_watch_time, media_reel_skip_rate`（`last_60d`）
6. 年齢・性別: `audience_gender_age_name, audience_gender_age_size`

## JSON の形（今のファイルと同じにする）
- `dates`: 60日分の日付。`daily.*` は各60個の配列（キー: reach, views, engaged, interactions, likes, comments, saves, shares, reach_followers, reach_non_followers, new_followers）
- `daily.new_followers` は先頭30個を `null`、後ろ30個に値
- `posts`: date / type（REELS か CAROUSEL）/ title（キャプションの1行目）/ url / reach / views / likes / comments / saved / shares / avg_watch_sec（ミリ秒÷1000）/ skip_rate
- `audience_age_gender`: `{age, M, F, U}` を 13-17〜65+ の順で
- `fetched_at`（取得日）、`last_date`（最後の日付）を更新する

## 注意
- 最新日の `follower_count_1d` は 0 で返ることがある（集計前）。画面では「集計前の可能性」と出るので、そのまま入れてよい
- 書き換えたら `http://127.0.0.1:8765/report.html` を開いて 1日／1週間／1ヶ月を切り替え、数字が出ることを確認する
