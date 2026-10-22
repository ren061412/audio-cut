#!/usr/bin/env python3
"""
ブラウザから auto_edit.py を使うためのローカルサーバー。

    .venv/bin/python server.py      # http://127.0.0.1:8765 が開く

動画・音声をページに貼ると jobs/<ID>/ に保存し、1件ずつ順番に auto_edit.py で処理する。
同じネットワーク（社内 Wi-Fi など）の社員は http://<このMacのIP>:8765 で使える。処理はこの Mac の中だけで行う。
"""
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(HERE, "jobs")
KEY_FILE = os.path.join(HERE, ".gemini_key")  # キャプション生成に使う Gemini の API キー
PORT = 8765
HOST = "0.0.0.0"  # 社内の他のパソコンからも使えるようにする
MAX_UPLOAD = 4 << 30  # 4GB
VIDEO_EXT = {".mov", ".mp4", ".m4v"}
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac"}
# 画面に出す成果物（作業用の _report.json や _cues.txt、wav は出さない）
SHOWN = [("_telop.mp4", "video"), ("_cut.mp4", "video"), ("_output.m4a", "audio"),
         (".srt", "srt"), ("_preview.png", "image")]

jobs = {}
procs = {}  # 実行中のジョブID → auto_edit.py のプロセス（キャンセル用）
work_queue = queue.Queue()
lock = threading.Lock()


# ---------------------------------------------------------------- ジョブ
def save(job):
    """サーバーを閉じても結果が残るよう、ジョブの状態を jobs/<ID>/job.json に書く。"""
    with open(os.path.join(job["outdir"], "job.json"), "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False)


def api_key():
    if os.path.isfile(KEY_FILE):
        return open(KEY_FILE, encoding="utf-8").read().strip()
    return os.environ.get("GEMINI_API_KEY", "")


def job_args(job):
    if job["mode"] == "caption":  # insta_caption.py に渡す引数（入力ファイルの後ろ）
        return ["--benchmark", os.path.join(job["outdir"], "benchmark.txt"), "-n", "1",
                "-o", os.path.join(job["outdir"], "caption")]
    # 字幕は動画をそのまま使う（カットは音声カットの機能）
    if job["mode"] == "telop":
        return ["--no-cut"]
    extra = ["--no-telop"]
    if not job.get("silence", True):
        extra.append("--no-silence-cut")
    if not job.get("repeat", True):
        extra.append("--no-repeat-cut")
    return extra


def load_jobs():
    """前回の結果を読み込む。途中だったジョブ（サーバーを閉じた等）は最初からやり直す。"""
    pending = []
    for jid in os.listdir(JOBS_DIR):
        path = os.path.join(JOBS_DIR, jid, "job.json")
        if os.path.isfile(path):
            job = json.load(open(path, encoding="utf-8"))
            jobs[jid] = job
            if job["status"] in ("queued", "running") and os.path.isfile(job["input"]):
                job.update(status="queued", log=[])
                pending.append(job)
    for job in sorted(pending, key=lambda j: j["created"]):
        work_queue.put((job, job_args(job)))


def run_job(job, extra):
    if job["mode"] == "caption":
        cmd = [sys.executable, "-u", os.path.join(HERE, "insta_caption.py"), job["input"]] + extra
    else:
        cmd = [sys.executable, "-u", os.path.join(HERE, "auto_edit.py"), job["input"], "-o", job["outdir"]] + extra
    env = dict(os.environ, GEMINI_API_KEY=api_key()) if api_key() else None
    with lock:
        if job["status"] == "cancelled":  # 順番待ちの間に × で取り消された
            return
        job.update(status="running", started=time.time(), finished=None, error=None)
        job["log"] = []
        p = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        procs[job["id"]] = p
    for line in p.stdout:
        with lock:
            job["log"].append(line.rstrip("\n"))
    p.wait()
    with lock:
        procs.pop(job["id"], None)
        job["finished"] = time.time()
        if job["status"] == "cancelled":
            pass
        elif job["mode"] == "caption" and p.returncode == 0 and not caption_texts(job):
            # insta_caption.py は生成に失敗しても解析結果を残して正常終了するので、結果の有無で判定する
            job["status"] = "error"
            msg = next((l for l in reversed(job["log"]) if "生成に失敗" in l or "未インストール" in l), "")
            job["error"] = ("API キーが正しくありません。サイトの管理者に確認してください。"
                            if any(w in msg for w in ("API key not valid", "API_KEY_INVALID", "HTTP 401", "HTTP 403"))
                            else "Gemini の無料枠の上限に達しました。時間をおいて（翌日以降）もう一度お試しください。" if "HTTP 429" in msg
                            else "Gemini が混み合っています。少し待ってからもう一度お試しください。" if "HTTP 503" in msg or "HTTP 500" in msg
                            else "キャプションを作れませんでした。" + (msg.replace("[gemini]", "").strip()[:200] if msg else ""))
        elif p.returncode == 0:
            job["status"] = "done"
        else:
            job["status"] = "error"
            errors = [l for l in job["log"] if "[error]" in l]
            job["error"] = errors[-1].replace("[error]", "").strip() if errors else "処理に失敗しました。"
        save(job)


def worker():
    while True:
        job, extra = work_queue.get()
        try:
            run_job(job, extra)
        except Exception as e:  # 想定外の失敗でもサーバーは止めない
            with lock:
                job.update(status="error", error=str(e), finished=time.time())
            save(job)


def caption_texts(job):
    path = os.path.join(job["outdir"], "caption", "captions.json")
    if not os.path.isfile(path):
        return []
    return [c["caption"].strip() for c in json.load(open(path, encoding="utf-8")).get("captions", [])]


def job_view(job):
    base = os.path.splitext(os.path.basename(job["input"]))[0]
    stem = re.sub(r"[^\w\-]+", "_", base)
    outputs = []
    if job["status"] == "done":
        for suffix, kind in SHOWN:
            if job["mode"] == "cut" and kind == "srt":
                continue
            name = (stem if suffix == "_output.m4a" else base) + suffix
            if os.path.isfile(os.path.join(job["outdir"], name)):
                outputs.append({"name": name, "kind": kind, "url": f"/api/jobs/{job['id']}/files/{name}"})
    cues = []
    cues_path = os.path.join(job["outdir"], base + "_cues.txt")
    if job["mode"] == "telop" and os.path.isfile(cues_path):
        for line in open(cues_path, encoding="utf-8"):
            if "\t" in line:
                t, text = line.rstrip("\n").split("\t", 1)
                if text != "END":
                    cues.append({"t": float(t), "text": text})
    with lock:
        # 前にある件数（いま処理中のものも含める）
        ahead = 0 if job["status"] != "queued" else sum(
            1 for j in jobs.values() if j["status"] == "running" or (j["status"] == "queued" and j["created"] < job["created"]))
        return {k: job[k] for k in ("id", "mode", "name", "cut", "status", "error", "started", "finished")} | {
            "log": job["log"][-40:], "queue_ahead": ahead, "outputs": outputs, "cues": cues, "now": time.time(),
            "captions": caption_texts(job) if job["mode"] == "caption" and job["status"] == "done" else []}


def create_job(name, mode, silence, repeat, rfile, length, benchmark=""):
    jid = uuid.uuid4().hex[:10]
    folder = os.path.join(JOBS_DIR, jid)
    os.makedirs(folder)
    safe = os.path.basename(name).replace("/", "_") or "input"
    path = os.path.join(folder, safe)
    with open(path, "wb") as f:
        left = length
        while left > 0:
            buf = rfile.read(min(left, 1 << 20))
            if not buf:
                break
            f.write(buf)
            left -= len(buf)
    if mode == "caption":
        with open(os.path.join(folder, "benchmark.txt"), "w", encoding="utf-8") as f:
            f.write(benchmark)
    job = {"id": jid, "mode": mode, "name": safe, "cut": mode == "cut", "silence": silence, "repeat": repeat,
           "input": path, "outdir": folder,
           "status": "queued", "error": None, "log": [], "created": time.time(), "started": None, "finished": None}
    with lock:
        jobs[jid] = job
        save(job)
    work_queue.put((job, job_args(job)))
    return job


def cancel(job):
    """× で取り消す。順番待ちなら飛ばし、処理中なら auto_edit.py を止める。"""
    with lock:
        if job["status"] not in ("queued", "running"):
            return
        job.update(status="cancelled", finished=time.time())
        p = procs.get(job["id"])
        save(job)
    if p:
        p.terminate()


def rerender(job, cues):
    """編集したテロップで書き出しだけやり直す（Whisper は回さない）。"""
    base = os.path.splitext(os.path.basename(job["input"]))[0]
    cues_path = os.path.join(job["outdir"], base + "_cues.txt")
    end_line = [l for l in open(cues_path, encoding="utf-8") if l.rstrip("\n").endswith("\tEND")][-1]
    with open(cues_path, "w", encoding="utf-8") as f:
        for c in cues:
            text = "\\N".join(s.strip() for s in str(c["text"]).replace("\r", "").split("\n") if s.strip())
            if text:  # 空にした札は消す（前の札が次の札まで表示される）
                f.write(f"{float(c['t']):.2f}\t{text}\n")
        f.write(end_line)
    extra = ["--cues", cues_path, "--no-cut"]
    with lock:
        job.update(status="queued", created=time.time(), error=None)
        save(job)
    work_queue.put((job, extra))


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def make_script(self, req):
        """データページの「台本を作る」。Gemini で1本作って返す（30秒ほどかかる）。"""
        import script_gen
        theme = str(req.get("theme") or "").strip()[:200]
        avoid = [str(t)[:120] for t in (req.get("avoid") or [])][:5]
        try:
            return self.send_json(script_gen.generate(theme, avoid))
        except Exception as e:
            msg = str(e)
            friendly = ("Gemini の無料枠の上限に達しました。時間をおいてもう一度お試しください。" if "429" in msg
                        else "Gemini の API キーが正しくありません。" if "API key not valid" in msg or "API_KEY_INVALID" in msg
                        else "Gemini が混み合っています。少し待ってからもう一度お試しください。" if "503" in msg or "500" in msg
                        else "台本を作れませんでした。もう一度お試しください。（" + msg[:150] + "）")
            return self.send_json({"error": friendly}, 502)

    def is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype=None, download=False):
        ctype = ctype or {".html": "text/html; charset=utf-8", ".png": "image/png", ".mp4": "video/mp4",
                          ".m4a": "audio/mp4", ".srt": "text/plain; charset=utf-8"}.get(
            os.path.splitext(path)[1].lower(), "application/octet-stream")
        size = os.path.getsize(path)
        start, end = 0, size - 1
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if rng and (rng.group(1) or rng.group(2)):  # 動画プレーヤーのシーク用
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else size - 1
            else:
                start = size - int(rng.group(2))
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if download:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(download)}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    buf = f.read(min(left, 1 << 20))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self):
        url = urlparse(self.path)
        parts = [unquote(p) for p in url.path.strip("/").split("/")]
        if url.path in ("/", "/index.html"):
            return self.send_file(os.path.join(HERE, "index.html"))
        if url.path == "/report.html":  # データ（Instagram 監査レポート）
            return self.send_file(os.path.join(HERE, "report.html"))
        if url.path == "/api/insights/status":
            return self.send_json(insights_status)
        if parts[0] == "insights" and len(parts) == 2 and parts[1].endswith(".json"):
            path = os.path.join(HERE, "insights", os.path.basename(parts[1]))
            if os.path.isfile(path):
                return self.send_file(path, "application/json; charset=utf-8")
        if url.path == "/api/settings":
            return self.send_json({"has_key": bool(api_key()), "can_set_key": self.is_local()})
        if parts[:2] == ["api", "jobs"] and len(parts) == 3 and parts[2] in jobs:
            return self.send_json(job_view(jobs[parts[2]]))
        if parts[:2] == ["api", "jobs"] and len(parts) == 5 and parts[3] == "files" and parts[2] in jobs:
            job = jobs[parts[2]]
            name = os.path.basename(parts[4])
            path = os.path.join(job["outdir"], name)
            if os.path.isfile(path) and name not in (job["name"], "job.json"):
                qs = parse_qs(url.query)
                fname = os.path.basename(qs.get("name", [name])[0]) or name
                return self.send_file(path, download=fname if "download" in qs else None)
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        url = urlparse(self.path)
        parts = url.path.strip("/").split("/")
        length = int(self.headers.get("Content-Length") or 0)
        if url.path == "/api/jobs":
            name = unquote(self.headers.get("X-Filename", "input"))
            mode = self.headers.get("X-Mode", "telop")
            ext = os.path.splitext(name)[1].lower()
            if length > MAX_UPLOAD:
                self.close_connection = True
                return self.send_json({"error": "ファイルが大きすぎます（4GB まで）。"}, 413)
            benchmark = unquote(self.headers.get("X-Benchmark", "")).strip()
            if mode not in ("telop", "cut", "caption") or ext not in (AUDIO_EXT if mode == "cut" else VIDEO_EXT):
                self.rfile.read(length)
                return self.send_json({"error": "このファイル形式には対応していません。"}, 400)
            if mode == "caption" and (not benchmark or not api_key()):
                self.rfile.read(length)
                return self.send_json({"error": "真似したいキャプションを貼り付けてください。" if not benchmark
                                       else "Gemini の API キーが設定されていません。"}, 400)
            job = create_job(name, mode, self.headers.get("X-Silence", "1") == "1",
                             self.headers.get("X-Repeat", "1") == "1", self.rfile, length, benchmark)
            return self.send_json({"id": job["id"]})
        if url.path == "/api/script":
            return self.make_script(json.loads(self.rfile.read(length) or b"{}"))
        if url.path == "/api/settings/key":
            # API キーは、サイトを動かしている Mac の上でだけ設定できる（社内の他のパソコンからは不可）
            if not self.is_local():
                return self.send_json({"error": "API キーはサイトを動かしている Mac で設定してください。"}, 403)
            key = self.rfile.read(length).decode("utf-8", "ignore").strip()
            if len(key) < 20 or " " in key:
                return self.send_json({"error": "API キーの形式が正しくありません。"}, 400)
            fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(key)
            return self.send_json({"ok": True})
        if parts[:2] == ["api", "jobs"] and len(parts) == 4 and parts[3] == "cancel" and parts[2] in jobs:
            cancel(jobs[parts[2]])
            return self.send_json({"id": parts[2]})
        if parts[:2] == ["api", "jobs"] and len(parts) == 4 and parts[3] == "rerender" and parts[2] in jobs:
            job = jobs[parts[2]]
            if job["status"] in ("queued", "running"):
                return self.send_json({"error": "処理中です。終わってからやり直してください。"}, 409)
            rerender(job, json.loads(self.rfile.read(length) or b"[]"))
            return self.send_json({"id": job["id"]})
        self.send_json({"error": "not found"}, 404)


defdef lan_ip():
    """社内に伝える URL 用に、IP を調べる（クラウド環境用安全化）"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------- データページの自動更新
INSIGHTS_FILE = os.path.join(HERE, "insights", "tokusatsu_box.json")
INSIGHTS_EVERY = 3 * 3600  # Instagram の数字は1日単位なので、3時間おきで十分
insights_status = {"ok": None, "error": None, "checked": None}


def refresh_insights():
    r = subprocess.run([sys.executable, os.path.join(HERE, "insights_fetch.py")], cwd=HERE,
                       capture_output=True, text=True, timeout=1800)
    out = (r.stdout + r.stderr).strip().splitlines()
    insights_status["checked"] = time.time()
    if r.returncode == 0:
        insights_status.update(ok=time.time(), error=None)
    else:  # 失敗しても前回のデータはそのまま表示される
        insights_status["error"] = next((l.replace("[error]", "").strip() for l in reversed(out) if "[error]" in l),
                                        "データを更新できませんでした。")
    print(out[-1] if out else "[insights] 出力なし")


def insights_loop():
    # 起動時はデータが1時間より古ければすぐ取り直し、あとは3時間おき
    if not os.path.isfile(INSIGHTS_FILE) or time.time() - os.path.getmtime(INSIGHTS_FILE) > 3600:
        refresh_insights()
    while True:
        time.sleep(INSIGHTS_EVERY)
        try:
            refresh_insights()
        except Exception as e:  # タイムアウトなどでもサーバーは止めない
            insights_status["error"] = str(e)


def main():
    url = f"http://127.0.0.1:{PORT}/"
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:  # すでに起動している
            print(f"すでに起動しています: {url}")
            webbrowser.open(url)
            return
    os.makedirs(JOBS_DIR, exist_ok=True)
    load_jobs()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=insights_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    ip = lan_ip()
    print(f"起動しました: {url}")
    if ip:
        print(f"社員のパソコンからは、ブラウザでこのアドレスを開いてください: http://{ip}:{PORT}/")
    print("このウィンドウを閉じると止まります（このMacがスリープしても使えなくなります）。")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
