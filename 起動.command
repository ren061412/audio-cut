#!/bin/bash
# ダブルクリックで「字幕生成・音声カット」のページを開く
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "初回セットアップ中です（5分ほどかかります）..."
  python3 -m venv .venv && .venv/bin/pip install -q faster-whisper imageio-ffmpeg numpy janome || {
    echo "セットアップに失敗しました。この画面を担当者に見せてください。"; read -r; exit 1; }
fi
# 字幕の区切りに使う形態素解析（後から追加したので、セットアップ済みのパソコンにも入れる）
.venv/bin/python -c "import janome" 2>/dev/null || .venv/bin/pip install -q janome
.venv/bin/python server.py
