"""
UE 跑單記錄 Web UI — Google Vision API 版
手機瀏覽器開公開 URL（ngrok / Railway）
"""
import re, os, json, math, uuid, base64
import requests as http_req
from datetime import datetime, date
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template_string

load_dotenv(Path(__file__).parent / '.env')
load_dotenv(Path('/etc/secrets/.env'), override=False)
GV_KEY = os.environ.get('GOOGLE_VISION_API_KEY', '')
DS_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DS_MODEL = os.environ.get('DEEPSEEK_VISION_MODEL', 'deepseek-vl2')
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN', '')
DISCORD_CHAT_ID = os.environ.get('DISCORD_CHAT_ID', '1495033355790581794')

def notify_discord(text, image_path=None):
    if not DISCORD_TOKEN:
        return
    try:
        headers = {'Authorization': f'Bot {DISCORD_TOKEN}'}
        if image_path and Path(image_path).exists():
            with open(image_path, 'rb') as f:
                http_req.post(
                    f'https://discord.com/api/v10/channels/{DISCORD_CHAT_ID}/messages',
                    headers=headers,
                    data={'payload_json': json.dumps({'content': text})},
                    files={'files[0]': (Path(image_path).name, f, 'image/jpeg')},
                    timeout=15
                )
        else:
            http_req.post(
                f'https://discord.com/api/v10/channels/{DISCORD_CHAT_ID}/messages',
                headers={**headers, 'Content-Type': 'application/json'},
                json={'content': text},
                timeout=8
            )
    except Exception:
        pass

RECORDS_DIR = Path(__file__).parent / "ue_records"
UPLOAD_DIR = Path(__file__).parent / "ue_inbox"
RECORDS_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)

RATE = 245 / 60

app = Flask(__name__)

_skip_words = {'外送','獨享','接受','接單','取消','疊單','加碼','優先','預約','快遞','購物'}
_skip_pat = re.compile(r'(台灣|桃園|新北|台北|台中|高雄|基隆).*(路|街|巷|號)|分鐘|公里|\$|\d{3,}')

def _resize_for_ocr(path):
    """Resize image to max 1600px on longest side, return JPEG bytes (< 1MB for OCR.space)."""
    from PIL import Image as PILImage
    import io
    img = PILImage.open(path)
    w, h = img.size
    max_dim = 1600
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        resample = getattr(PILImage, 'Resampling', PILImage).LANCZOS
        img = img.resize((int(w * scale), int(h * scale)), resample)
    if img.mode in ('RGBA', 'P'):
        img = img.convert('RGB')
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=85)
    return buf.getvalue()

def ocr_image(path):
    # Primary: OCR.space (Traditional Chinese, 25K/month free with own key)
    ocr_key = os.environ.get('OCRSPACE_API_KEY', 'helloworld')
    print(f'[OCR] key_prefix={ocr_key[:6]}', flush=True)
    try:
        img_bytes = _resize_for_ocr(path)
        print(f'[OCR] resize ok: {len(img_bytes)//1024}KB', flush=True)
        b64 = base64.b64encode(img_bytes).decode()
        r = http_req.post('https://api.ocr.space/parse/image',
                          data={'base64Image': f'data:image/jpeg;base64,{b64}',
                                'language': 'cht',
                                'isOverlayRequired': 'false',
                                'apikey': ocr_key},
                          timeout=30)
        print(f'[OCR] http_status={r.status_code}', flush=True)
        try:
            result = r.json()
        except Exception as je:
            print(f'[OCR] json_parse_err: {je} body[:200]={r.text[:200]}', flush=True)
            raise
        exit_code = result.get('OCRExitCode')
        errored = result.get('IsErroredOnProcessing')
        keys = list(result.keys())
        print(f'[OCR] space exitCode={exit_code} errored={errored} keys={keys}', flush=True)
        if result.get('ParsedResults'):
            text = result['ParsedResults'][0].get('ParsedText', '')
            print(f'[OCR] text len={len(text)} hasZh={any(chr(0x4e00)<=c<=chr(0x9fff) for c in text)}', flush=True)
            if text.strip():
                return text
        else:
            print(f'[OCR] no ParsedResults err={result.get("ErrorMessage")} raw={str(result)[:200]}', flush=True)
    except Exception as e:
        print(f'[OCR] exception: {type(e).__name__}: {e}', flush=True)
    # Fallback: Tesseract eng-only (lightweight, no chi_tra to avoid OOM on 512MB)
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(path)
        text = pytesseract.image_to_string(img, lang='eng', config='--psm 6')
        if text.strip():
            return text
    except Exception:
        pass
    # Fallback: Google Vision
    if GV_KEY:
        with open(path, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode()
        payload = {"requests": [{"image": {"content": b64}, "features": [{"type": "TEXT_DETECTION"}]}]}
        r = http_req.post(f"https://vision.googleapis.com/v1/images:annotate?key={GV_KEY}",
                          json=payload, timeout=15)
        data = r.json()
        if 'responses' in data:
            annotations = data['responses'][0].get('textAnnotations', [])
            if annotations:
                return annotations[0]['description']
    raise Exception("All OCR methods failed")

def detect_screen_type(text):
    if '接受' in text and '$' in text:
        return 'accept'
    if '您已抵達' in text or '確認訂單' in text:
        return 'arrived'
    if '完成外送' in text or '已送達' in text:
        return 'complete'
    return 'unknown'

def parse_accept(text):
    d = {}
    m = re.search(r'\+?\s*\$\s*(\d+)', text)
    if m: d['platform'] = int(m.group(1))
    m = re.search(r'總計\s*\+?\s*(\d+)\s*分', text)
    if m: d['est_min'] = int(m.group(1))
    d['bonus'] = 0
    m = re.search(r'加碼\s*\$?\s*(\d+)', text)
    if m: d['bonus'] = int(m.group(1))
    candidates = []
    for line in [l.strip() for l in text.splitlines() if l.strip()]:
        if not re.search(r'[一-鿿]{2,}', line): continue
        if _skip_pat.search(line): continue
        zh = re.findall(r'[一-鿿]+', line)
        sc = sum(1 for w in zh if w in _skip_words)
        tz = sum(len(w) for w in zh)
        if tz < 3 or sc >= len(zh): continue
        candidates.append((tz, line))
    if candidates:
        name = sorted(candidates, reverse=True)[0][1]
        d['restaurant'] = re.split(r'[|｜]', name)[0].strip()[:25]
    for line in text.splitlines():
        if re.search(r'(台灣|桃園|新北|台北|台中|高雄).*(路|街|巷|號)', line):
            d['address'] = line.strip()[:50]
            break
    return d

def parse_arrived(text):
    d = {}
    m = re.search(r'\b([A-Z0-9]{5})\b', text)
    if m: d['order_id'] = m.group(1)
    for line in text.splitlines():
        if re.search(r'[一-鿿]{3,}', line) and '確認' not in line and '抵達' not in line and '說明' not in line and '支援' not in line:
            d['restaurant'] = line.strip()[:25]
            break
    return d

def get_records(d=None):
    if d is None: d = date.today()
    f = RECORDS_DIR / f"{d.isoformat()}.json"
    return json.loads(f.read_text('utf-8')) if f.exists() else []

def save_records(records, d=None):
    if d is None: d = date.today()
    f = RECORDS_DIR / f"{d.isoformat()}.json"
    f.write_text(json.dumps(records, ensure_ascii=False, indent=2), 'utf-8')

HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>UE 補差計算器</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;600;700&display=swap">
<style>
:root {
  --bg:#0A0E1A; --surface:#131929; --elevated:#1B2238; --border:#263050;
  --text:#E8EEFF; --muted:#6B7BA4; --accent:#4DAAFF; --positive:#2EC27E;
  --amber:#F5A623; --red:#FF5F6B;
}
@media (prefers-color-scheme:light) {
  :root:not([data-theme="dark"]) {
    --bg:#F0F4FF; --surface:#FFFFFF; --elevated:#E8EEF8; --border:#C8D4EE;
    --text:#0A0E1A; --muted:#6276A4; --accent:#2270D8; --positive:#1A8F5C;
    --amber:#C47A00; --red:#D43E47;
  }
}
:root[data-theme="light"] {
  --bg:#F0F4FF; --surface:#FFFFFF; --elevated:#E8EEF8; --border:#C8D4EE;
  --text:#0A0E1A; --muted:#6276A4; --accent:#2270D8; --positive:#1A8F5C;
  --amber:#C47A00; --red:#D43E47;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;font-size:15px;line-height:1.5;min-height:100vh;padding:0 16px 32px;max-width:480px;margin:0 auto}
header{display:flex;align-items:center;justify-content:space-between;padding:14px 0 16px}
.logo{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:17px;letter-spacing:-.02em}
.logo span{color:var(--accent)}
.theme-btn{background:var(--elevated);border:1px solid var(--border);color:var(--muted);border-radius:8px;padding:6px 10px;font-size:13px;cursor:pointer}
.date-tabs{display:flex;gap:8px;margin-bottom:12px;overflow-x:auto;padding:2px 0;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.date-tabs::-webkit-scrollbar{display:none}
.date-tab{background:var(--elevated);border:1px solid var(--border);color:var(--muted);border-radius:20px;padding:5px 14px;font-size:13px;font-family:'JetBrains Mono',monospace;cursor:pointer;white-space:nowrap;flex-shrink:0}
.date-tab.active{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:18px;margin-bottom:12px}
.card-title{font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:14px}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:10px}
label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px;font-weight:500}
input[type=text],input[type=number],input[type=time],select{width:100%;background:var(--elevated);border:1px solid var(--border);border-radius:8px;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:15px;padding:9px 12px;outline:none;transition:border-color .15s;-webkit-appearance:none;appearance:none}
input:focus,select:focus{border-color:var(--accent)}
input::placeholder{color:var(--muted);opacity:.6}
.type-seg{display:flex;border:1px solid var(--border);border-radius:8px;overflow:hidden;margin-bottom:10px}
.type-seg label{flex:1;text-align:center;padding:9px;font-size:13px;cursor:pointer;color:var(--muted);background:var(--elevated)}
.type-seg input{display:none}
.type-seg input:checked+span{background:var(--accent);color:#fff;display:block}
.type-seg label span{display:block}
.result-big{text-align:center;padding:10px 0 16px}
.result-label{font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
.result-amt{font-family:'JetBrains Mono',monospace;font-size:52px;font-weight:700;line-height:1;color:var(--positive);letter-spacing:-.03em}
.result-amt.zero{color:var(--muted)}
.breakdown{border-top:1px solid var(--border);padding-top:12px;display:flex;flex-direction:column;gap:7px}
.brow{display:flex;justify-content:space-between;align-items:center;font-size:13px}
.brow .bl{color:var(--muted)}
.brow .bv{font-family:'JetBrains Mono',monospace;font-weight:600}
.bv.accent{color:var(--accent)} .bv.amber{color:var(--amber)} .bv.pos{color:var(--positive)}
.log-btn{width:100%;background:var(--accent);color:#fff;border:none;border-radius:10px;padding:13px;font-size:15px;font-weight:600;cursor:pointer;margin-top:12px;font-family:'Inter',sans-serif}
.log-btn:disabled{opacity:.35;cursor:not-allowed}
.log-entry{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 14px;margin-bottom:8px;display:grid;grid-template-columns:1fr auto;gap:6px;align-items:center}
.le-meta{font-size:12px;color:var(--muted);margin-bottom:2px}
.le-detail{font-family:'JetBrains Mono',monospace;font-size:13px;color:var(--text)}
.le-supp{font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:700;color:var(--positive);text-align:right}
.le-del{background:none;border:1px solid var(--border);color:var(--red);border-radius:6px;padding:4px 8px;font-size:11px;cursor:pointer;margin-left:6px;white-space:nowrap}
.daily-total{background:var(--elevated);border:1px solid var(--border);border-radius:10px;padding:14px;margin-top:4px}
.dt-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;font-size:13px}
.dt-row:last-child{margin-bottom:0}
.dt-row.grand{padding-top:8px;border-top:1px solid var(--border);margin-top:4px}
.dt-lbl{color:var(--muted)}
.dt-lbl.bold{font-weight:700;color:var(--text)}
.dt-amt{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:16px;color:var(--positive)}
.dt-amt.big{font-size:22px}
.dt-amt.amber{color:var(--amber)}
.dt-amt.accent{color:var(--accent)}
.empty{text-align:center;color:var(--muted);font-size:13px;padding:20px 0}
.ocr-toggle{width:100%;background:none;border:1px solid var(--border);color:var(--muted);border-radius:10px;padding:10px;font-size:13px;cursor:pointer;margin-bottom:12px;text-align:left}
.ocr-body{display:none}
.ocr-body.open{display:block}
.upload-box{border:2px dashed var(--border);border-radius:10px;padding:18px;text-align:center;cursor:pointer;color:var(--muted);font-size:14px;transition:.2s;margin-bottom:8px}
.upload-box.has{border-color:var(--accent);color:var(--accent)}
.upload-box input{display:none}
.preview-img{max-width:100%;max-height:140px;border-radius:8px;margin-top:6px;display:none}
.ocr-result{background:var(--elevated);border-radius:8px;padding:10px 12px;font-size:13px;margin-top:8px;display:none;line-height:1.8}
.ocr-result.err{border:1px solid var(--red)}
.cycle-card{background:var(--surface);border:2px solid var(--amber);border-radius:14px;padding:14px 18px;margin-bottom:12px}
</style>
</head>
<body>

<header>
  <div class="logo">UE<span>補差</span></div>
  <button class="theme-btn" onclick="document.documentElement.setAttribute('data-theme', document.documentElement.getAttribute('data-theme')==='light'?'dark':'light')">切換主題</button>
</header>

<div id="dateTabs" class="date-tabs"></div>

<!-- 計算卡 -->
<div class="card">
  <div class="card-title">新增記錄</div>
  <div class="row2">
    <div><label>接單時間</label><input type="time" id="m_accept" oninput="calc()"></div>
    <div><label>完成時間</label><input type="time" id="m_complete" oninput="calc()"></div>
  </div>
  <div class="row2">
    <div><label>平台金額</label><input type="number" id="m_platform" placeholder="$" oninput="calc()"></div>
    <div><label>加碼</label><input type="number" id="m_bonus" placeholder="0" value="0" oninput="calc()"></div>
  </div>
  <div class="row2">
    <div><label>小費 app/刷卡</label><input type="number" id="m_tip_card" placeholder="0" value="0"></div>
    <div><label>小費 現金</label><input type="number" id="m_tip_cash" placeholder="0" value="0"></div>
  </div>
  <input type="text" id="m_restaurant" placeholder="餐廳名稱（選填）" style="margin-bottom:10px">
  <div class="type-seg">
    <label><input type="radio" name="mtype" value="main" checked><span>主單</span></label>
    <label><input type="radio" name="mtype" value="stack"><span>疊單</span></label>
    <label><input type="radio" name="mtype" value="clamp"><span>夾單</span></label>
  </div>
  <div class="result-big">
    <div class="result-label">補差</div>
    <div class="result-amt zero" id="resultAmt">—</div>
  </div>
  <div class="breakdown" id="breakdown" style="display:none">
    <div class="brow"><span class="bl">時間</span><span class="bv accent" id="bMin">—</span></div>
    <div class="brow"><span class="bl">底價</span><span class="bv" id="bBase">—</span></div>
    <div class="brow"><span class="bl">分鐘收入</span><span class="bv accent" id="bMinAmt">—</span></div>
    <div class="brow" id="bBonusRow" style="display:none"><span class="bl">加碼</span><span class="bv amber" id="bBonus">—</span></div>
    <div class="brow"><span class="bl">補差</span><span class="bv pos" id="bSupp">—</span></div>
  </div>
  <button class="log-btn" id="logBtn" disabled onclick="submitManual()">記錄</button>
  <div id="logMsg" style="margin-top:8px;font-size:13px;color:var(--positive)"></div>
</div>

<!-- OCR 截圖（折疊） -->
<button class="ocr-toggle" onclick="toggleOcr(this)">📷 截圖上傳（OCR 自動辨識）▼</button>
<div class="ocr-body" id="ocrBody">
  <div class="card">
    <div class="card-title">主單截圖</div>
    <div class="upload-box" id="dz1" onclick="document.getElementById('f1').click()">點擊上傳截圖<input type="file" id="f1" accept="image/*"></div>
    <img id="prev1" class="preview-img">
    <div class="type-seg" style="margin-top:8px">
      <label><input type="radio" name="otype" value="main" checked><span>主單</span></label>
      <label><input type="radio" name="otype" value="stack"><span>疊單</span></label>
    </div>
    <div class="card-title" style="margin-top:12px">夾單截圖（選填）</div>
    <div class="upload-box" id="dz2" onclick="document.getElementById('f2').click()">點擊上傳夾單截圖<input type="file" id="f2" accept="image/*"></div>
    <img id="prev2" class="preview-img">
    <button class="log-btn" id="ocrBtn" onclick="submitOcr()" style="margin-top:10px">OCR 辨識並記錄</button>
    <div id="ocrResult" class="ocr-result"></div>
  </div>
</div>

<!-- 今日記錄 -->
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
    <div class="card-title" id="logTitle" style="margin-bottom:0">今日記錄</div>
  </div>
  <div id="logEntries"><div class="empty">尚無記錄</div></div>
</div>

<!-- 日合計 -->
<div id="dailyTotalCard" style="display:none" class="card">
  <div class="card-title">今日合計</div>
  <div id="dailyTotalBody"></div>
</div>

<!-- 週期補差 -->
<div id="cycleCard" style="display:none" class="cycle-card">
  <div class="card-title" style="color:var(--amber)">本期累積補差</div>
  <div id="cycleBody"></div>
</div>

<script>
const RATE = 245/60;
let viewDate = '';

function todayStr() {
  const d = new Date();
  return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
}

function toggleOcr(btn) {
  const body = document.getElementById('ocrBody');
  body.classList.toggle('open');
  btn.textContent = body.classList.contains('open') ? '📷 截圖上傳（OCR 自動辨識）▲' : '📷 截圖上傳（OCR 自動辨識）▼';
}

// Date tabs
async function renderDateTabs() {
  const today = todayStr();
  const r = await fetch('/dates');
  let dates = [];
  try { dates = await r.json(); } catch {}
  if (!dates.includes(today)) dates.push(today);
  dates.sort();
  if (!viewDate) viewDate = today;
  const el = document.getElementById('dateTabs');
  const recent = dates.slice(-5);
  el.innerHTML = recent.map(d => {
    const p = d.split('-');
    const lbl = d === today ? '今天' : `${parseInt(p[1])}/${parseInt(p[2])}`;
    return `<button class="date-tab${d===viewDate?' active':''}" onclick="switchDate('${d}')">${lbl}</button>`;
  }).join('');
  if (!recent.includes(viewDate)) {
    el.innerHTML = `<button class="date-tab active">${viewDate}</button>` + el.innerHTML;
  }
}

function switchDate(d) {
  viewDate = d;
  renderDateTabs();
  loadLog();
}

// Calculator
function calc() {
  const accept = document.getElementById('m_accept').value;
  const complete = document.getElementById('m_complete').value;
  const platform = parseFloat(document.getElementById('m_platform').value) || 0;
  const bonus = parseFloat(document.getElementById('m_bonus').value) || 0;
  const amtEl = document.getElementById('resultAmt');
  const bd = document.getElementById('breakdown');
  const btn = document.getElementById('logBtn');
  if (!accept || !complete || !platform) {
    amtEl.textContent = '—'; amtEl.className = 'result-amt zero';
    bd.style.display = 'none'; btn.disabled = true; return;
  }
  const [ah,am] = accept.split(':').map(Number);
  const [ch,cm] = complete.split(':').map(Number);
  let mins = (ch*60+cm)-(ah*60+am);
  if (mins < 0) mins += 1440;
  const base = platform - bonus;
  const minAmt = Math.floor(mins * RATE * 10) / 10;
  const supp = Math.max(0, Math.floor(mins * RATE) - base);
  amtEl.textContent = '$' + supp;
  amtEl.className = 'result-amt' + (supp > 0 ? '' : ' zero');
  document.getElementById('bMin').textContent = mins + '分';
  document.getElementById('bBase').textContent = '$' + base + (bonus>0?'（'+platform+'−'+bonus+'）':'');
  document.getElementById('bMinAmt').textContent = '$' + minAmt;
  if (bonus > 0) {
    document.getElementById('bBonusRow').style.display = '';
    document.getElementById('bBonus').textContent = '+$' + bonus;
  } else { document.getElementById('bBonusRow').style.display = 'none'; }
  document.getElementById('bSupp').textContent = '$' + supp;
  bd.style.display = ''; btn.disabled = false;
}

async function submitManual() {
  const accept = document.getElementById('m_accept').value;
  const complete = document.getElementById('m_complete').value;
  const platform = parseInt(document.getElementById('m_platform').value || '0');
  if (!accept || !platform) { document.getElementById('logMsg').textContent = '⚠️ 接單時間和平台金額必填'; return; }
  const body = {
    accept_time: accept, complete_time: complete,
    platform, bonus: parseInt(document.getElementById('m_bonus').value||'0'),
    tip_card: parseInt(document.getElementById('m_tip_card').value||'0'),
    tip_cash: parseInt(document.getElementById('m_tip_cash').value||'0'),
    restaurant: document.getElementById('m_restaurant').value,
    type: document.querySelector('input[name=mtype]:checked').value,
    date: viewDate
  };
  const r = await fetch('/manual', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const d = await r.json();
  if (d.ok) {
    document.getElementById('logMsg').textContent = '✅ 已記錄' + (d.supplement!=null?' 補差 $'+d.supplement:'');
    // clear inputs
    ['m_accept','m_complete','m_platform','m_restaurant'].forEach(id => document.getElementById(id).value='');
    document.getElementById('m_bonus').value='0';
    document.getElementById('m_tip_card').value='0';
    document.getElementById('m_tip_cash').value='0';
    document.querySelector('input[name=mtype][value=main]').checked=true;
    document.getElementById('resultAmt').textContent='—';
    document.getElementById('resultAmt').className='result-amt zero';
    document.getElementById('breakdown').style.display='none';
    document.getElementById('logBtn').disabled=true;
    setTimeout(()=>{document.getElementById('logMsg').textContent=''},3000);
    loadLog();
  } else {
    document.getElementById('logMsg').textContent = '❌ ' + (d.error||'失敗');
  }
}

// OCR upload
let ocrFiles = {f1:null, f2:null};
['f1','f2'].forEach(id => {
  document.getElementById(id).addEventListener('change', e => {
    const file = e.target.files[0]; if (!file) return;
    ocrFiles[id] = file;
    const n = id.slice(1);
    const dz = document.getElementById('dz'+n);
    dz.innerHTML = file.name + '<input type="file" id="'+id+'" accept="image/*" style="display:none">';
    dz.classList.add('has');
    document.getElementById(id).addEventListener('change', e2 => { if(e2.target.files[0]) ocrFiles[id]=e2.target.files[0]; });
    const prev = document.getElementById('prev'+n);
    prev.src = URL.createObjectURL(file); prev.style.display = 'block';
  });
});

async function submitOcr() {
  if (!ocrFiles.f1) { return; }
  const btn = document.getElementById('ocrBtn');
  btn.disabled = true; btn.textContent = 'OCR 辨識中…';
  const result = document.getElementById('ocrResult');
  result.style.display = 'none';
  try {
    const otype = document.querySelector('input[name=otype]:checked').value;
    const fd = new FormData(); fd.append('image', ocrFiles.f1); fd.append('otype', otype); fd.append('clamp', '0');
    const d1 = await (await fetch('/upload', {method:'POST', body:fd})).json();
    let html = renderOcrResult(d1, '主單');
    if (ocrFiles.f2) {
      const fd2 = new FormData(); fd2.append('image', ocrFiles.f2); fd2.append('otype', 'main'); fd2.append('clamp', '1');
      const d2 = await (await fetch('/upload', {method:'POST', body:fd2})).json();
      html += renderOcrResult(d2, '夾單');
    }
    result.innerHTML = html; result.style.display = '';
    result.className = 'ocr-result' + ((d1.error) ? ' err' : '');
    loadLog();
  } catch(e) {
    result.innerHTML = '錯誤: ' + e; result.style.display = ''; result.className = 'ocr-result err';
  }
  btn.disabled = false; btn.textContent = 'OCR 辨識並記錄';
}

function renderOcrResult(d, label) {
  if (d.error) return `<b>${label} 失敗</b>: ${d.error}<br>`;
  const typeStr = {accept:'接單',arrived:'抵達',complete:'完成'}[d.screen_type]||d.screen_type;
  return `<b>${label} ${typeStr}</b> ${d.restaurant||''}<br>平台 $${d.platform||'?'}　底價 $${d.platform_base||'?'}　${d.supplement!=null?'補差 $'+d.supplement:''}<br>`;
}

// Log list
async function loadLog() {
  const url = viewDate ? '/today?date='+viewDate : '/today';
  const data = await (await fetch(url)).json();
  const el = document.getElementById('logEntries');
  const titleEl = document.getElementById('logTitle');
  if (!data.records.length) {
    el.innerHTML = '<div class="empty">尚無記錄</div>';
    document.getElementById('dailyTotalCard').style.display = 'none';
    document.getElementById('cycleCard').style.display = 'none';
    titleEl.textContent = '今日記錄';
    return;
  }
  titleEl.textContent = (viewDate && viewDate !== todayStr() ? viewDate.slice(5) : '今日') + '記錄 ('+data.records.length+'筆)';
  el.innerHTML = data.records.map(rec => {
    const tl = {main:'主單',clamp:'夾單',stack:'疊單',stack_clamp:'疊夾'}[rec.type]||rec.type;
    const suppStr = rec.supplement!=null ? '$'+rec.supplement : '進行中';
    return `<div class="log-entry" id="rec-${rec.id}">
      <div>
        <div class="le-meta">${rec.accept_time}→${rec.complete_time||'?'} <span style="color:var(--accent)">${tl}</span>${rec.restaurant?' · '+rec.restaurant:''}</div>
        <div class="le-detail">平台 $${rec.platform}${rec.bonus?'(+'+rec.bonus+')':''}　${rec.total_min!=null?rec.total_min+'分　':''}<span style="color:var(--positive)">${suppStr}</span></div>
      </div>
      <div style="text-align:right">
        <div class="le-supp">${rec.supplement!=null?'$'+rec.supplement:''}</div>
        <button class="le-del" onclick="deleteRec('${rec.id}',this)">刪</button>
      </div>
    </div>`;
  }).join('');
  // Daily total
  const tipCard = data.records.reduce((s,r)=>s+(r.tip||0),0);
  const tipCash = data.records.reduce((s,r)=>s+(r.tip_cash||0),0);
  const grand = data.total_platform + data.total_supp + tipCard;
  document.getElementById('dailyTotalCard').style.display = '';
  document.getElementById('dailyTotalBody').innerHTML = `
    <div class="dt-row"><span class="dt-lbl">平台底薪</span><span class="dt-amt">$${data.total_platform}</span></div>
    <div class="dt-row"><span class="dt-lbl">補差合計</span><span class="dt-amt">$${data.total_supp}</span></div>
    ${tipCard>0?`<div class="dt-row"><span class="dt-lbl">小費(app)</span><span class="dt-amt accent">$${tipCard}</span></div>`:''}
    ${tipCash>0?`<div class="dt-row"><span class="dt-lbl">小費(現金,不計入)</span><span class="dt-amt" style="color:var(--muted)">$${tipCash}</span></div>`:''}
    <div class="dt-row grand"><span class="dt-lbl bold">今日總合</span><span class="dt-amt big">$${grand}</span></div>`;
  renderDateTabs();
}

async function deleteRec(id, btn) {
  if (btn.dataset.c !== '1') {
    btn.textContent = '確定?'; btn.style.background = 'var(--red)'; btn.style.color = '#fff'; btn.dataset.c = '1';
    setTimeout(()=>{ if(btn.dataset.c==='1'){btn.textContent='刪';btn.style.background='';btn.style.color='var(--red)';btn.dataset.c='';} }, 3000);
    return;
  }
  const r = await fetch('/delete/'+id+(viewDate?'?date='+viewDate:''), {method:'DELETE'});
  const d = await r.json();
  if (d.ok) { document.getElementById('rec-'+id)?.remove(); loadLog(); }
}

// Init
viewDate = todayStr();
renderDateTabs();
loadLog();
</script>
</body>
</html>"""

@app.route('/status')
def status():
    ocr_key = os.environ.get('OCRSPACE_API_KEY', 'helloworld')
    return jsonify({'ocr_key_prefix': ocr_key[:8], 'ocr_key_len': len(ocr_key)})

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/upload', methods=['POST'])
def upload():
    f = request.files.get('image')
    if not f:
        return jsonify({'error': '沒有收到圖片'})
    otype = request.form.get('otype', 'main')
    is_clamp = request.form.get('clamp') == '1'
    fname = UPLOAD_DIR / f"{uuid.uuid4().hex}{Path(f.filename).suffix}"
    f.save(str(fname))
    try:
        text = ocr_image(str(fname))
        screen = detect_screen_type(text)
        now = datetime.now()
        records = get_records()

        if screen == 'accept':
            parsed = parse_accept(text)
            platform = parsed.get('platform', 0)
            bonus = parsed.get('bonus', 0)
            platform_base = platform - bonus
            if otype == 'stack' and is_clamp:
                order_type = 'stack_clamp'
            elif otype == 'stack':
                order_type = 'stack'
            elif is_clamp:
                order_type = 'clamp'
            else:
                order_type = 'main'
            rec = {
                'id': now.strftime('%H%M%S'),
                'type': order_type,
                'accept_time': now.strftime('%H:%M'),
                'complete_time': None,
                'platform': platform,
                'bonus': bonus,
                'platform_base': platform_base,
                'restaurant': parsed.get('restaurant', ''),
                'address': parsed.get('address', ''),
                'est_min': parsed.get('est_min', 0),
                'total_min': None,
                'supplement': None,
                'tip': 0,
            }
            records.append(rec)
            save_records(records)
            type_label = {'main':'主單','clamp':'夾單','stack':'疊單','stack_clamp':'疊夾'}.get(order_type, order_type)
            notify_discord(f"📋 接單 [{type_label}] {rec['accept_time']}\n🍽 {rec['restaurant'] or '—'}　$**{platform}**（底 ${platform_base}）", image_path=str(fname))
            return jsonify({**parsed, 'screen_type': 'accept', 'platform_base': platform_base, 'type': order_type})

        elif screen == 'arrived':
            parsed = parse_arrived(text)
            return jsonify({**parsed, 'screen_type': 'arrived', 'restaurant': parsed.get('restaurant','')})

        elif screen == 'complete':
            # 找最新未完成的接單記錄
            pending = [r for r in records if r.get('complete_time') is None]
            if pending:
                target = pending[-1]
                target['complete_time'] = now.strftime('%H:%M')
                t0 = datetime.strptime(target['accept_time'], '%H:%M').replace(
                    year=now.year, month=now.month, day=now.day)
                if target['type'] == 'clamp':
                    prev = [r for r in records if r['id'] != target['id'] and r.get('complete_time')]
                    if prev:
                        ct_str = prev[-1]['complete_time']
                        ct = datetime.strptime(ct_str, '%H:%M').replace(year=now.year, month=now.month, day=now.day)
                        if ct > t0:
                            total_min = int((now - ct).total_seconds() / 60)
                        else:
                            total_min = int((now - t0).total_seconds() / 60)
                    else:
                        total_min = int((now - t0).total_seconds() / 60)
                else:
                    total_min = int((now - t0).total_seconds() / 60)
                target['total_min'] = total_min
                target['supplement'] = max(0, math.floor(total_min * RATE) - target['platform_base'])
                save_records(records)
                notify_discord(f"✅ 完成 {target['accept_time']}→{target['complete_time']}\n🍽 {target.get('restaurant','—')}　${target['platform']}　{total_min}分　補差 $**{target['supplement']}**", image_path=str(fname))
                return jsonify({
                    'screen_type': 'complete',
                    'restaurant': target.get('restaurant', ''),
                    'platform': target['platform'],
                    'platform_base': target['platform_base'],
                    'total_min': total_min,
                    'supplement': target['supplement']
                })
            return jsonify({'screen_type': 'complete', 'error': '找不到未完成的接單記錄'})
        else:
            return jsonify({'error': f'無法辨識截圖類型 (偵測到: {screen})\n文字: {text[:100]}'})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/manual', methods=['POST'])
def manual_entry():
    data = request.get_json()
    accept_time = data.get('accept_time', '')
    complete_time = data.get('complete_time', '')
    platform = int(data.get('platform', 0))
    bonus = int(data.get('bonus', 0))
    tip_card = int(data.get('tip_card', 0))
    tip_cash = int(data.get('tip_cash', 0))
    restaurant = data.get('restaurant', '')
    order_type = data.get('type', 'main')
    platform_base = platform - bonus
    d_str = data.get('date', '')
    try:
        target_date = date.fromisoformat(d_str) if d_str else date.today()
    except ValueError:
        target_date = date.today()
    records = get_records(target_date)
    rec_id = accept_time.replace(':', '')
    total_min = None
    supplement = None
    if complete_time:
        now = datetime.now()
        t0 = datetime.strptime(accept_time, '%H:%M').replace(year=now.year, month=now.month, day=now.day)
        t1 = datetime.strptime(complete_time, '%H:%M').replace(year=now.year, month=now.month, day=now.day)
        if t1 < t0:
            from datetime import timedelta
            t1 += timedelta(days=1)
        total_min = int((t1 - t0).total_seconds() / 60)
        supplement = max(0, math.floor(total_min * RATE) - platform_base)
    rec = {
        'id': rec_id,
        'type': order_type,
        'accept_time': accept_time,
        'complete_time': complete_time or None,
        'platform': platform,
        'bonus': bonus,
        'platform_base': platform_base,
        'restaurant': restaurant,
        'address': '',
        'est_min': 0,
        'total_min': total_min,
        'supplement': supplement,
        'tip': tip_card,
        'tip_cash': tip_cash,
    }
    records.append(rec)
    save_records(records, target_date)
    return jsonify({'ok': True, 'supplement': supplement})

@app.route('/delete/<record_id>', methods=['DELETE'])
def delete_record(record_id):
    d_str = request.args.get('date', '')
    try:
        d = date.fromisoformat(d_str) if d_str else date.today()
    except ValueError:
        d = date.today()
    records = get_records(d)
    new_records = [r for r in records if r.get('id') != record_id]
    if len(new_records) == len(records):
        return jsonify({'error': '找不到記錄'}), 404
    save_records(new_records, d)
    return jsonify({'ok': True})

@app.route('/dates')
def list_dates():
    dates = sorted([f.stem for f in RECORDS_DIR.glob('*.json')])
    return jsonify(dates)

@app.route('/today')
def today():
    d_str = request.args.get('date', '')
    try:
        d = date.fromisoformat(d_str) if d_str else date.today()
    except ValueError:
        d = date.today()
    records = get_records(d)
    tp = sum(r.get('platform', 0) for r in records)
    ts = sum(r.get('supplement') or 0 for r in records)
    return jsonify({'records': records, 'total_platform': tp, 'total_supp': ts})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5555))
    app.run(host='0.0.0.0', port=port, debug=False)
