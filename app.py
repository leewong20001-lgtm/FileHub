import json
import os
import secrets
import threading
import time
from datetime import datetime

from flask import Flask, abort, jsonify, render_template_string, request, send_file

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB

# Render 免費方案的磁碟是暫存的，放在 /tmp 即可
if os.name == "nt":
    STORAGE_DIR = os.path.join(os.environ.get("TEMP", os.getcwd()), "file_transfer_storage")
else:
    STORAGE_DIR = os.environ.get("STORAGE_DIR", "/tmp/file_transfer_storage")
os.makedirs(STORAGE_DIR, exist_ok=True)

META_PATH = os.path.join(STORAGE_DIR, "meta.json")
TTL_SECONDS = 24 * 60 * 60      # 檔案保留 24 小時
MAX_DOWNLOADS = 3               # 最多下載 3 次後自動刪除
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉易混淆字元

# 管理員密碼：優先讀環境變數，沒設定就用隨機密碼（啟動時印在 log）
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_hex(4)
    print("[admin] 未設定 ADMIN_PASSWORD 環境變數，本次啟動使用隨機密碼：" + ADMIN_PASSWORD)

_lock = threading.Lock()


def _load_meta():
    if os.path.exists(META_PATH):
        try:
            with open(META_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_meta(meta):
    tmp = META_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(tmp, META_PATH)


def _cleanup(meta):
    """刪除過期或超過下載次數的檔案。"""
    now = time.time()
    changed = False
    for code in list(meta.keys()):
        m = meta[code]
        if now > m["expires_at"] or m["downloads"] >= MAX_DOWNLOADS:
            try:
                os.remove(os.path.join(STORAGE_DIR, code + ".bin"))
            except OSError:
                pass
            del meta[code]
            changed = True
    if changed:
        _save_meta(meta)
    return meta


def _new_code(meta):
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if code not in meta:
            return code


@app.errorhandler(413)
def too_large(e):
    return jsonify(error="檔案超過 100 MB 上限"), 413


@app.route("/")
def index():
    return render_template_string(PAGE_HTML)


@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="沒有收到檔案"), 400
    with _lock:
        meta = _cleanup(_load_meta())
        code = _new_code(meta)
        f.save(os.path.join(STORAGE_DIR, code + ".bin"))
        meta[code] = {
            "filename": os.path.basename(f.filename),
            "size": os.path.getsize(os.path.join(STORAGE_DIR, code + ".bin")),
            "downloads": 0,
            "expires_at": time.time() + TTL_SECONDS,
        }
        _save_meta(meta)
    return jsonify(code=code)


@app.route("/info/<code>")
def info(code):
    code = code.strip().upper()
    with _lock:
        meta = _cleanup(_load_meta())
        m = meta.get(code)
    if not m or not os.path.exists(os.path.join(STORAGE_DIR, code + ".bin")):
        return jsonify(error="找不到這組提取碼，檔案可能已過期或被下載完畢"), 404
    remaining = MAX_DOWNLOADS - m["downloads"]
    return jsonify(
        filename=m["filename"],
        size=m["size"],
        remaining_downloads=remaining,
        expires=datetime.fromtimestamp(m["expires_at"]).strftime("%Y-%m-%d %H:%M"),
    )


@app.route("/d/<code>")
def download(code):
    code = code.strip().upper()
    with _lock:
        meta = _cleanup(_load_meta())
        m = meta.get(code)
    if not m or not os.path.exists(os.path.join(STORAGE_DIR, code + ".bin")):
        abort(404)
    with _lock:
        meta = _load_meta()
        if code in meta:
            meta[code]["downloads"] += 1
            _save_meta(meta)
    return send_file(
        os.path.join(STORAGE_DIR, code + ".bin"),
        as_attachment=True,
        download_name=m["filename"],
    )


@app.route("/admin")
def admin_page():
    return render_template_string(ADMIN_HTML)


def _admin_auth():
    pw = request.headers.get("X-Admin-Password", "")
    return bool(pw) and secrets.compare_digest(pw, ADMIN_PASSWORD)


@app.route("/admin/api/files")
def admin_files():
    if not _admin_auth():
        return jsonify(error="密碼錯誤"), 401
    with _lock:
        meta = _cleanup(_load_meta())
        now = time.time()
        files = [
            {
                "code": code,
                "filename": m["filename"],
                "size": m["size"],
                "downloads": m["downloads"],
                "remaining": MAX_DOWNLOADS - m["downloads"],
                "expires_in": max(0, int(m["expires_at"] - now)),
            }
            for code, m in meta.items()
        ]
    files.sort(key=lambda x: x["expires_in"], reverse=True)
    return jsonify(files=files)


def _delete_one(meta, code):
    try:
        os.remove(os.path.join(STORAGE_DIR, code + ".bin"))
    except OSError:
        pass
    del meta[code]


@app.route("/admin/api/delete", methods=["POST"])
def admin_delete():
    if not _admin_auth():
        return jsonify(error="密碼錯誤"), 401
    body = request.get_json(silent=True) or {}
    code = str(body.get("code", "")).strip().upper()
    with _lock:
        meta = _load_meta()
        if code not in meta:
            return jsonify(error="找不到這組提取碼"), 404
        _delete_one(meta, code)
        _save_meta(meta)
    return jsonify(ok=True)


@app.route("/admin/api/delete_all", methods=["POST"])
def admin_delete_all():
    if not _admin_auth():
        return jsonify(error="密碼錯誤"), 401
    with _lock:
        meta = _load_meta()
        count = 0
        for code in list(meta.keys()):
            _delete_one(meta, code)
            count += 1
        if count:
            _save_meta(meta)
    return jsonify(ok=True, deleted=count)


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>檔案中繼站</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, "Segoe UI", "Microsoft JhengHei", sans-serif;
    background: #f2f4f8; color: #1f2933; min-height: 100vh;
    display: flex; align-items: center; justify-content: center; padding: 24px;
  }
  .card {
    background: #fff; border-radius: 12px; padding: 32px;
    box-shadow: 0 2px 12px rgba(0,0,0,.08); width: 100%; max-width: 480px;
  }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .sub { color: #6b7785; font-size: 13px; margin-bottom: 20px; }
  .tabs { display: flex; gap: 8px; margin-bottom: 20px; }
  .tab {
    flex: 1; padding: 10px; border: 1px solid #d5dbe3; background: #fff;
    border-radius: 8px; cursor: pointer; font-size: 14px; color: #52606d;
  }
  .tab.active { background: #2563eb; border-color: #2563eb; color: #fff; }
  .panel { display: none; }
  .panel.active { display: block; }
  .dropzone {
    border: 2px dashed #c3ccd6; border-radius: 10px; padding: 36px 16px;
    text-align: center; color: #6b7785; cursor: pointer; font-size: 14px;
    transition: border-color .15s, background .15s;
  }
  .dropzone:hover, .dropzone.drag { border-color: #2563eb; background: #eff6ff; }
  .dropzone input { display: none; }
  .filename { margin-top: 12px; font-size: 14px; color: #1f2933; word-break: break-all; }
  .bar { height: 8px; background: #e4e9f0; border-radius: 4px; margin-top: 14px; overflow: hidden; }
  .bar > div { height: 100%; width: 0; background: #2563eb; transition: width .2s; }
  .status { margin-top: 8px; font-size: 13px; color: #6b7785; min-height: 18px; }
  .status.err { color: #d0342c; }
  button.primary {
    margin-top: 16px; width: 100%; padding: 12px; border: 0; border-radius: 8px;
    background: #2563eb; color: #fff; font-size: 15px; cursor: pointer;
  }
  button.primary:disabled { background: #9db4e8; cursor: not-allowed; }
  .result { margin-top: 20px; display: none; text-align: center; }
  .code {
    font-size: 32px; letter-spacing: 8px; font-weight: 700; color: #1f2933;
    background: #f2f4f8; border-radius: 8px; padding: 12px; margin: 10px 0;
  }
  .link {
    font-size: 13px; color: #2563eb; word-break: break-all; margin: 8px 0;
  }
  .copy {
    margin-top: 8px; padding: 8px 16px; border: 1px solid #d5dbe3; background: #fff;
    border-radius: 8px; cursor: pointer; font-size: 13px;
  }
  .copy:active { background: #eff6ff; }
  input.codeinput {
    width: 100%; padding: 14px; font-size: 24px; letter-spacing: 8px; text-align: center;
    text-transform: uppercase; border: 1px solid #d5dbe3; border-radius: 8px; outline: none;
  }
  input.codeinput:focus { border-color: #2563eb; }
  .fileinfo {
    margin-top: 16px; padding: 14px; background: #f2f4f8; border-radius: 8px;
    font-size: 14px; display: none; line-height: 1.8;
  }
  .fileinfo b { word-break: break-all; }
  .note { margin-top: 20px; font-size: 12px; color: #9aa5b1; line-height: 1.6; }
</style>
</head>
<body>
<div class="card">
  <h1>檔案中繼站</h1>
  <div class="sub">跨網路傳檔：一台電腦上傳，另一台用提取碼下載</div>

  <div class="tabs">
    <button class="tab active" id="tab-up" onclick="switchTab('up')">上傳檔案</button>
    <button class="tab" id="tab-dl" onclick="switchTab('dl')">下載檔案</button>
  </div>

  <div class="panel active" id="panel-up">
    <div class="dropzone" id="dropzone" onclick="document.getElementById('fileinput').click()">
      點擊選擇檔案，或把檔案拖進來<br><span style="font-size:12px">上限 100 MB</span>
      <input type="file" id="fileinput">
    </div>
    <div class="filename" id="filename"></div>
    <div class="bar" id="bar" style="display:none"><div id="barfill"></div></div>
    <div class="status" id="upstatus"></div>
    <button class="primary" id="uploadbtn" disabled onclick="doUpload()">上傳並取得提取碼</button>

    <div class="result" id="result">
      <div style="font-size:13px;color:#6b7785">提取碼（24 小時內有效，可下載 3 次）</div>
      <div class="code" id="showcode"></div>
      <div class="link" id="showlink"></div>
      <button class="copy" onclick="copyLink()">複製下載連結</button>
    </div>
  </div>

  <div class="panel" id="panel-dl">
    <input class="codeinput" id="dlcode" maxlength="6" placeholder="輸入提取碼" oninput="this.value=this.value.toUpperCase().replace(/[^A-Z0-9]/g,'')">
    <button class="primary" id="checkbtn" onclick="checkCode()">查詢檔案</button>
    <div class="status" id="dlstatus"></div>
    <div class="fileinfo" id="fileinfo"></div>
    <button class="primary" id="dlbtn" style="display:none" onclick="doDownload()">下載檔案</button>
  </div>

  <div class="note">注意：檔案存放在伺服器暫存空間，24 小時或下載 3 次後自動刪除。請上傳後盡快下載，不要當作長期保存空間。<br><a href="/admin" style="color:#b7c0cc">管理</a></div>
</div>

<script>
let pickedFile = null;
let currentUrl = "";
let currentCode = "";

function switchTab(t) {
  document.getElementById("panel-up").classList.toggle("active", t === "up");
  document.getElementById("panel-dl").classList.toggle("active", t === "dl");
  document.getElementById("tab-up").classList.toggle("active", t === "up");
  document.getElementById("tab-dl").classList.toggle("active", t === "dl");
}

const dz = document.getElementById("dropzone");
const fi = document.getElementById("fileinput");
dz.addEventListener("dragover", e => { e.preventDefault(); dz.classList.add("drag"); });
dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
dz.addEventListener("drop", e => {
  e.preventDefault(); dz.classList.remove("drag");
  if (e.dataTransfer.files.length) pickFile(e.dataTransfer.files[0]);
});
fi.addEventListener("change", () => { if (fi.files.length) pickFile(fi.files[0]); });

function pickFile(f) {
  if (f.size > 100 * 1024 * 1024) {
    setStatus("upstatus", "檔案超過 100 MB 上限", true);
    return;
  }
  pickedFile = f;
  document.getElementById("filename").textContent = f.name + "（" + fmtSize(f.size) + "）";
  document.getElementById("uploadbtn").disabled = false;
  document.getElementById("result").style.display = "none";
}

function doUpload() {
  if (!pickedFile) return;
  const fd = new FormData();
  fd.append("file", pickedFile);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/upload");
  document.getElementById("bar").style.display = "block";
  document.getElementById("uploadbtn").disabled = true;
  setStatus("upstatus", "上傳中...");
  xhr.upload.onprogress = e => {
    if (e.lengthComputable) {
      const pct = Math.round(e.loaded / e.total * 100);
      document.getElementById("barfill").style.width = pct + "%";
      setStatus("upstatus", "上傳中 " + pct + "%");
    }
  };
  xhr.onload = () => {
    document.getElementById("bar").style.display = "none";
    if (xhr.status === 200) {
      const data = JSON.parse(xhr.responseText);
      currentCode = data.code;
      currentUrl = location.origin + "/d/" + data.code;
      document.getElementById("showcode").textContent = data.code;
      document.getElementById("showlink").textContent = currentUrl;
      document.getElementById("result").style.display = "block";
      setStatus("upstatus", "上傳完成。把提取碼或連結給另一台電腦即可下載。");
      document.getElementById("uploadbtn").disabled = false;
    } else {
      let msg = "上傳失敗（" + xhr.status + "）";
      try { msg = JSON.parse(xhr.responseText).error || msg; } catch (e) {}
      setStatus("upstatus", msg, true);
      document.getElementById("uploadbtn").disabled = false;
    }
  };
  xhr.onerror = () => {
    document.getElementById("bar").style.display = "none";
    setStatus("upstatus", "網路錯誤，請重試", true);
    document.getElementById("uploadbtn").disabled = false;
  };
  xhr.send(fd);
}

function copyLink() {
  navigator.clipboard.writeText(currentUrl).then(() => {
    setStatus("upstatus", "已複製下載連結");
  });
}

function checkCode() {
  const code = document.getElementById("dlcode").value.trim().toUpperCase();
  if (code.length !== 6) { setStatus("dlstatus", "提取碼必須是 6 個字元", true); return; }
  setStatus("dlstatus", "查詢中...");
  fetch("/info/" + code).then(r => r.json().then(d => ({ ok: r.ok, d }))).then(({ ok, d }) => {
    if (!ok) { setStatus("dlstatus", d.error || "查無此檔案", true); hideInfo(); return; }
    setStatus("dlstatus", "");
    const box = document.getElementById("fileinfo");
    box.innerHTML = "<b>" + esc(d.filename) + "</b><br>" +
      "大小：" + fmtSize(d.size) + "<br>" +
      "剩餘下載次數：" + d.remaining_downloads + "<br>" +
      "有效期限：" + d.expires;
    box.style.display = "block";
    const btn = document.getElementById("dlbtn");
    btn.style.display = "block";
    currentCode = code;
  }).catch(() => setStatus("dlstatus", "網路錯誤，請重試", true));
}

function doDownload() {
  window.location.href = "/d/" + currentCode;
}

function hideInfo() {
  document.getElementById("fileinfo").style.display = "none";
  document.getElementById("dlbtn").style.display = "none";
}

function setStatus(id, msg, isErr) {
  const el = document.getElementById(id);
  el.textContent = msg;
  el.classList.toggle("err", !!isErr);
}

function fmtSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

document.getElementById("dlcode").addEventListener("keydown", e => {
  if (e.key === "Enter") checkCode();
});
</script>
</body>
</html>
"""

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>管理 - 檔案中繼站</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, "Segoe UI", "Microsoft JhengHei", sans-serif;
    background: #f2f4f8; color: #1f2933; min-height: 100vh;
    display: flex; align-items: center; justify-content: center; padding: 24px;
  }
  .card {
    background: #fff; border-radius: 12px; padding: 32px;
    box-shadow: 0 2px 12px rgba(0,0,0,.08); width: 100%; max-width: 720px;
  }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .sub { color: #6b7785; font-size: 13px; margin-bottom: 20px; }
  input[type=password] {
    width: 100%; padding: 12px; font-size: 15px; border: 1px solid #d5dbe3;
    border-radius: 8px; outline: none;
  }
  input[type=password]:focus { border-color: #2563eb; }
  button.primary {
    margin-top: 14px; width: 100%; padding: 12px; border: 0; border-radius: 8px;
    background: #2563eb; color: #fff; font-size: 15px; cursor: pointer;
  }
  .status { margin-top: 8px; font-size: 13px; color: #6b7785; min-height: 18px; }
  .status.err { color: #d0342c; }
  #listwrap { display: none; }
  .summary { font-size: 13px; color: #6b7785; margin-bottom: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: #6b7785; font-weight: 600; padding: 8px 6px; border-bottom: 2px solid #e4e9f0; }
  td { padding: 10px 6px; border-bottom: 1px solid #eef1f5; vertical-align: top; }
  td.fname { word-break: break-all; max-width: 220px; }
  code { background: #f2f4f8; padding: 2px 6px; border-radius: 4px; letter-spacing: 2px; }
  .del {
    padding: 5px 12px; border: 1px solid #e3b8b5; background: #fff; color: #d0342c;
    border-radius: 6px; cursor: pointer; font-size: 12px;
  }
  .del:hover { background: #fdf0ef; }
  .empty { text-align: center; color: #9aa5b1; padding: 28px 0; }
  .toolbar { margin-top: 18px; display: flex; gap: 10px; }
  .toolbar button {
    padding: 9px 16px; border-radius: 8px; font-size: 13px; cursor: pointer;
    border: 1px solid #d5dbe3; background: #fff; color: #52606d;
  }
  .toolbar button.danger { border-color: #e3b8b5; color: #d0342c; }
  .toolbar button.danger:hover { background: #fdf0ef; }
  .back { display: inline-block; margin-top: 18px; font-size: 12px; color: #b7c0cc; }
</style>
</head>
<body>
<div class="card">
  <h1>管理頁面</h1>
  <div class="sub">輸入管理密碼，查看與刪除伺服器上的檔案</div>

  <div id="loginbox">
    <input type="password" id="pw" placeholder="管理密碼" onkeydown="if(event.key==='Enter')login()">
    <button class="primary" onclick="login()">登入</button>
    <div class="status" id="status"></div>
  </div>

  <div id="listwrap">
    <div class="summary" id="summary"></div>
    <table>
      <thead><tr><th>提取碼</th><th>檔名</th><th>大小</th><th>下載/剩餘</th><th>剩餘時間</th><th></th></tr></thead>
      <tbody id="tbody"></tbody>
    </table>
    <div class="empty" id="empty" style="display:none">目前沒有任何檔案</div>
    <div class="toolbar">
      <button onclick="refresh()">重新整理</button>
      <button class="danger" onclick="deleteAll()">全部清空</button>
    </div>
  </div>

  <a class="back" href="/">回到首頁</a>
</div>

<script>
let pw = "";

function setStatus(msg, isErr) {
  const el = document.getElementById("status");
  el.textContent = msg;
  el.classList.toggle("err", !!isErr);
}

async function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({"X-Admin-Password": pw}, opts.headers || {});
  if (opts.body && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers["Content-Type"] = "application/json";
  }
  const r = await fetch(path, opts);
  const d = await r.json().catch(() => ({}));
  return { ok: r.ok, d };
}

async function login() {
  pw = document.getElementById("pw").value;
  if (!pw) { setStatus("請輸入密碼", true); return; }
  const { ok, d } = await api("/admin/api/files");
  if (!ok) { setStatus(d.error || "登入失敗", true); return; }
  setStatus("");
  document.getElementById("loginbox").style.display = "none";
  document.getElementById("listwrap").style.display = "block";
  render(d.files);
}

async function refresh() {
  const { ok, d } = await api("/admin/api/files");
  if (!ok) { setStatus(d.error || "讀取失敗", true); return; }
  render(d.files);
}

function render(files) {
  const tbody = document.getElementById("tbody");
  tbody.innerHTML = "";
  let totalSize = 0;
  files.forEach(f => {
    totalSize += f.size;
    const tr = document.createElement("tr");
    tr.innerHTML =
      "<td><code>" + f.code + "</code></td>" +
      "<td class='fname'>" + esc(f.filename) + "</td>" +
      "<td>" + fmtSize(f.size) + "</td>" +
      "<td>" + f.downloads + " / " + f.remaining + "</td>" +
      "<td>" + fmtRemaining(f.expires_in) + "</td>" +
      "<td><button class='del' onclick=\"delOne('" + f.code + "')\">刪除</button></td>";
    tbody.appendChild(tr);
  });
  document.getElementById("summary").textContent =
    files.length + " 個檔案，合計 " + fmtSize(totalSize);
  document.getElementById("empty").style.display = files.length ? "none" : "block";
}

async function delOne(code) {
  if (!confirm("確定刪除 " + code + " 這個檔案？")) return;
  const { ok, d } = await api("/admin/api/delete", { method: "POST", body: { code: code } });
  if (!ok) { setStatus(d.error || "刪除失敗", true); return; }
  refresh();
}

async function deleteAll() {
  if (!confirm("確定清空所有檔案？")) return;
  const { ok, d } = await api("/admin/api/delete_all", { method: "POST" });
  if (!ok) { setStatus(d.error || "刪除失敗", true); return; }
  refresh();
}

function fmtSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

function fmtRemaining(sec) {
  if (sec <= 0) return "即將過期";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h > 0 ? h + " 小時 " + m + " 分" : m + " 分鐘";
}

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
