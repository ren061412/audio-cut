---
name: insta-caption
description: 動画と「真似したいキャプション」から Instagram 用キャプションを複数案つくる。「キャプション」「インスタの投稿文」「この文章を真似して」などの依頼で使う。
---

# Instagram キャプション生成

`insta_caption.py` が「動画の解析（文字起こし・静止画）→ お手本の型の分解 → 生成」を行う。
生成は Google Gemini（無料枠）。API キーは `.gemini_key`（このフォルダ、本人だけ読める）か `GEMINI_API_KEY` から読む。
キーが無い・無料枠の上限に達したときは `--no-llm` で解析まで行い、生成は Claude（このセッション）が行う。

## 環境
- フォルダ: `/Users/Riku/Documents/audio-cut`（`insta_caption.py`、`benchmarks/`、`.venv/`）
- Python: `/Users/Riku/Documents/audio-cut/.venv/bin/python`（faster-whisper / imageio-ffmpeg。Gemini は標準ライブラリの HTTP で呼ぶので追加導入なし）
- Mac は遅い。コマンドは `run_in_background` で実行し、1回だけ回す

## 手順
1. お手本キャプションを `benchmarks/<名前>.txt` に保存する（同じお手本は使い回す）
2. 解析する
   ```bash
   /Users/Riku/Documents/audio-cut/.venv/bin/python /Users/Riku/Documents/audio-cut/insta_caption.py "<動画パス>" --benchmark benchmarks/<名前>.txt --no-llm
   ```
   出力: 動画と同じフォルダの `<名前>_caption/`（`prompt.md`、`analysis.json`、`frames/*.jpg`）
3. `prompt.md` を読み、`frames/` の静止画を Read で見て、プロンプトの「依頼」どおりに
   型の分析 → 動画の要約 → キャプション案（既定 3 案）を `captions.md` に書く
   - お手本の言い回し・固有名詞は流用しない。動画から読めない数字・名称は【要確認: ○○】
   - 区切り線・✅/→ などの記号・ハッシュタグの並べ方（1行1つ等）・CTA とメンションの位置は踏襲
   - 文字起こしの誤認識は静止画と文脈で直す
4. 案を会話にも貼り、`captions.md` のパスを伝える

## Gemini で一括実行する場合（通常はこちら）
`--no-llm` を外すと `captions.md` / `captions.json` まで自動で作られる（21秒の動画で約1.5分）。
モデルは `gemini-flash-latest`、使えなければ `gemini-2.5-flash` に切り替わる。
サイト（server.py）の「Instagram のキャプションを作る」もこの方法で動く。
