---
name: cut-telop
description: 縦型動画（iPhone .MOV）や音声（.m4a）から、0.5秒以上の無音と言い直し・繰り返しをカットし、中央・明朝Bold・黒ぼかしのテロップを焼き込む。「/srt-fast」「テロップ」「字幕」「間を消して」「繰り返しをカット」などの依頼で使う。
---

# 無音・言い直しカット + テロップ焼き込み（一括）

`auto_edit.py` が、1回の Whisper 解析で「カット → テロップ作成 → 焼き込み」まで行う。
旧 `/srt-fast`（テロップのみ）と `cut_audio.py`（音声カットのみ）を統合したもの。

## 環境
- フォルダ: `/Users/Riku/Documents/audio-cut`（`auto_edit.py`、`cut_audio.py`、`corrections.tsv`、`.venv/`）
- Python: `/Users/Riku/Documents/audio-cut/.venv/bin/python`（faster-whisper / numpy / imageio-ffmpeg）
- ffmpeg はスクリプトが imageio-ffmpeg 同梱のものを自動で使う（システムには無い）
- Mac は遅い（Intel i5 1.1GHz）。**1件10分以内**。試行錯誤の再実行はしない。コマンドは `run_in_background` で実行する
- 実績: 21秒の動画で解析＋書き出し 約4分、テロップ修正後の書き出しのみ 約2〜3分

## 手順（動画）

### 1. 解析とテロップ案（Whisper はここで1回だけ）
```bash
/Users/Riku/Documents/audio-cut/.venv/bin/python /Users/Riku/Documents/audio-cut/auto_edit.py "<動画パス>" --prepare
```
- 依頼が「カットなし」なら `--no-cut` を付ける（旧 /srt-fast と同じ。カットしない分、映像は元のまま）
- 出力（元動画と同じフォルダ）: `<名前>_cues.txt`（テロップ案）、`<名前>_report.json`（カット区間・単語）

### 2. テロップ案を確認して直す（Claude が行う）
`<名前>_cues.txt` を読み、Edit で直す。書式は「開始秒<TAB>テキスト」、改行は `\N`、最後の行は「終了秒<TAB>END」。
- 誤認識を直す（例: フィットガン→フィット感、製作／制作は「製作」）。固有名詞（オーメダル、オーズ、9枚3コンボ、プロフのリンク）はそのまま
- **テロップの区切りは文節・意味のまとまりで切る**。各テロップが単独で読んで自然か必ず確認する
  - 疑問詞（何・どこ・どう・いつ・誰）は後ろの動詞までまとめる: ×「多すぎて何から｜作ればいいか」→ ○「作りたいものが多すぎて｜何から作ればいいか悩んでいます」
  - 「て」+補助動詞は切らない: ×「製作して｜きましたが」
  - 文の終わりと次の文の頭を同じテロップに入れない: ×「発表フォローして」→ ○「〜結果を発表｜フォローしてお待ちください」
  - 区切りを動かしたら、新しいテロップの開始秒は `_report.json` の words（ostart）でその語の時刻を調べて付ける
- 1行が約14文字を超えたら意味の切れ目で `\N`（例: `欲しい方は\Nプロフのリンクをクリックしてください`）
- 0.5秒程度の短い語（「そこで」など）は次のテロップにまとめる（行を消し、文字を次の行の先頭へ）
- 時刻はカット後の動画の時刻。行をまとめる・分ける以外で時刻はいじらない
- 何度も出る誤認識は `corrections.tsv`（誤<TAB>正）に追記すると、次回から自動で直る
- 直すところが無ければそのまま次へ

### 3. 書き出し（Whisper は回さない）
```bash
/Users/Riku/Documents/audio-cut/.venv/bin/python /Users/Riku/Documents/audio-cut/auto_edit.py "<動画パス>" --cues "<名前>_cues.txt のフルパス"
```
（手順1で `--no-cut` を付けたら、ここにも付ける）
- 出力: `<名前>_telop.mp4`（HEVC 30Mbps・hvc1・faststart、音声は AAC 192k）、`<名前>.srt`、`<名前>_preview.png`
- `<名前>_preview.png`（一番長いテロップのコマ）を Read で見て、はみ出し・位置を確認する

全自動でよい場合は、オプション無しで実行すれば 1〜3 を一度に行う（テロップは自動組みのまま）。

## 音声（.m4a）の場合
```bash
/Users/Riku/Documents/audio-cut/.venv/bin/python /Users/Riku/Documents/audio-cut/auto_edit.py "<音声パス>" --no-telop
```
- 出力は従来どおり `<名前>_output.m4a`（本命）、`_output.wav`、`_report.json`（記号・空白は `_` に置換）
- 従来の `cut_audio.py` もそのまま使える

## テロップの仕様（固定）
- 位置: 画面の縦横中央（ASS Alignment 5）／フォント: Hiragino Mincho ProN Bold・白・字間4
- 黒ぼかし背景: Layer0 にボックス（Outline 36、&H40000000、`\blur18`）、Layer1 に白文字だけ
- 文字サイズ: 最長行 15字以下 → 120、18字以下 → 104（`--font-size` で指定も可）
- 各テロップは次のテロップの開始まで表示。最後は動画の終わりの少し前まで
- 依頼の [テロップ位置][フォント][フォント背景] がこれと違う場合は、スクリプトを変えずにユーザーに確認する

## カットの仕様（cut_audio.py と同じ）
- 0.5秒以上の無音をカット（途中は前後0.1秒残す、先頭・末尾は全部）
- 連続する同じ語・フレーズ・言い直しは最後の1回だけ残す
- 動画ではカット位置をフレーム境界にそろえ、映像・音声を同じ区間で切る

## 完了報告に入れる内容
出力ファイルのパス、元の長さ→カット後の長さ、カットした繰り返し（report.json の repeats）、テロップ一覧、直した誤認識・表記。最後に「文言・改行の修正があれば伝えてください」と添える。
修正依頼が来たら `_cues.txt` を直して手順3だけ再実行する。
