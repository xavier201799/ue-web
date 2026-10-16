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

def ocr_image(path):
    with open(path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = {"requests": [{"image": {"content": b64}, "features": [{"type": "TEXT_DETECTION"}]}]}
    r = http_req.post(f"https://vision.googleapis.com/v1/images:annotate?key={GV_KEY}",
                      json=payload, timeout=15)
    annotations = r.json()['responses'][0].get('textAnnotations', [])
    return annotations[0]['description'] if annotations else ''

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
<title>UE 跑單記錄</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, sans-serif; background: #f5f5f5; color: #111; }
.hdr { background: #06C167; color: #fff; padding: 14px 16px; font-size: 18px; font-weight: bold; }
.card { background: #fff; border-radius: 12px; padding: 16px; margin: 12px; box-shadow: 0 1px 4px rgba(0,0,0,.1); }
.section-title { font-size: 15px; font-weight: bold; margin-bottom: 12px; color: #222; }
.section-sub { font-size: 12px; color: #888; margin-bottom: 8px; }
.upload-box { border: 2px dashed #ccc; border-radius: 10px; padding: 20px; text-align: center; cursor: pointer; color: #888; font-size: 14px; transition: .2s; }
.upload-box.has-file { border-color: #06C167; color: #06C167; background: #f0faf5; }
.upload-box input[type=file] { display: none; }
.preview-img { max-width: 100%; max-height: 160px; border-radius: 8px; margin-top: 8px; display: none; }
.seg { display: flex; border: 1px solid #ddd; border-radius: 8px; overflow: hidden; margin-top: 10px; }
.seg label { flex: 1; text-align: center; padding: 10px; font-size: 14px; cursor: pointer; }
.seg input { display: none; }
.seg input:checked + span { background: #06C167; color: #fff; display: block; }
.seg label span { display: block; }
.divider { border: none; border-top: 2px dashed #e0e0e0; margin: 16px 0; }
.clamp-section { background: #fff8f0; border: 1px solid #ffd699; border-radius: 10px; padding: 14px; margin-top: 4px; }
.clamp-title { font-size: 14px; font-weight: bold; color: #e67e22; margin-bottom: 8px; }
.btn { width: 100%; padding: 14px; background: #06C167; color: #fff; border: none; border-radius: 10px; font-size: 16px; font-weight: bold; cursor: pointer; margin-top: 12px; }
.btn:disabled { background: #aaa; }
.result { border-left: 4px solid #06C167; background: #e8f9ef; border-radius: 8px; padding: 12px; margin: 12px; font-size: 14px; line-height: 1.8; }
.result.err { border-left-color: #e74c3c; background: #fdecea; }
.row-item { padding: 10px 0; border-bottom: 1px solid #f0f0f0; font-size: 14px; }
.row-item:last-child { border-bottom: none; }
.badge { display: inline-block; border-radius: 4px; padding: 1px 6px; font-size: 11px; margin-left: 4px; }
.badge.main { background: #e8f9ef; color: #06C167; }
.badge.clamp { background: #fff3e0; color: #e67e22; }
.badge.stack { background: #e8eaf6; color: #3f51b5; }
.badge.stack_clamp { background: #fce4ec; color: #c2185b; }
.total-bar { background: #06C167; color: #fff; padding: 12px 16px; margin: 0 12px 12px; border-radius: 10px; display: flex; justify-content: space-between; font-size: 14px; }
</style>
</head>
<body>
<div class="hdr">UE 跑單記錄</div>

<div class="card">
  <!-- 欄位一：主單 / 疊單 -->
  <div class="section-title">欄位一：主單截圖</div>
  <div class="section-sub">接到新單時的接單畫面（有「接受」按鈕）</div>
  <div class="upload-box" id="dz1" onclick="document.getElementById('f1').click()">
    點擊上傳截圖
    <input type="file" id="f1" accept="image/*">
  </div>
  <img id="prev1" class="preview-img">
  <div class="seg">
    <label><input type="radio" name="otype" value="main" checked><span>主單</span></label>
    <label><input type="radio" name="otype" value="stack"><span>疊單</span></label>
  </div>

  <hr class="divider">

  <!-- 欄位二：夾單 -->
  <div class="clamp-section">
    <div class="clamp-title">欄位二：夾單截圖（選填）</div>
    <div class="section-sub">在送第一單途中又接了新單，上傳那張接單截圖</div>
    <div class="upload-box" id="dz2" onclick="document.getElementById('f2').click()">
      點擊上傳夾單截圖
      <input type="file" id="f2" accept="image/*">
    </div>
    <img id="prev2" class="preview-img">
  </div>

  <div id="amountOverride" style="display:none; margin-top:12px; background:#fff3cd; border-radius:8px; padding:12px;">
    <div style="font-size:13px; color:#856404; margin-bottom:6px;">⚠️ OCR 金額讀取失敗，請手動輸入：</div>
    <input id="manualAmount" type="number" placeholder="主單金額" style="width:48%; padding:8px; border:1px solid #ccc; border-radius:6px; font-size:14px;">
    <input id="manualAmountClamp" type="number" placeholder="夾單金額" style="width:48%; padding:8px; border:1px solid #ccc; border-radius:6px; font-size:14px; margin-left:4%;">
  </div>

  <button class="btn" id="submitBtn" onclick="submitAll()">送出記錄</button>
</div>

<div id="resultArea"></div>

<div class="card" id="todayCard">
  <div class="section-title" id="todayTitle">今日記錄</div>
  <div id="todayList">載入中…</div>
</div>

<script>
let files = {f1: null, f2: null};

['f1','f2'].forEach(id => {
  document.getElementById(id).addEventListener('change', e => {
    const file = e.target.files[0];
    if (!file) return;
    files[id] = file;
    const dz = document.getElementById('dz'+id.slice(1));
    dz.querySelector('input[type=file]') || null;
    dz.innerHTML = file.name + '<input type="file" id="'+id+'" accept="image/*" style="display:none">';
    dz.classList.add('has-file');
    document.getElementById('f'+id.slice(1)).addEventListener('change', e => {
      const f2 = e.target.files[0]; if (f2) { files[id] = f2; }
    });
    const prev = document.getElementById('prev'+id.slice(1));
    prev.src = URL.createObjectURL(file);
    prev.style.display = 'block';
  });
});

async function uploadOne(file, otype, isClamp) {
  const fd = new FormData();
  fd.append('image', file);
  fd.append('otype', otype);
  fd.append('clamp', isClamp ? '1' : '0');
  const r = await fetch('/upload', {method:'POST', body: fd});
  return r.json();
}

async function submitAll() {
  if (!files.f1) { alert('請先上傳主單截圖'); return; }
  const btn = document.getElementById('submitBtn');
  btn.disabled = true; btn.textContent = 'OCR 處理中…';
  const otype = document.querySelector('input[name=otype]:checked').value;
  let html = '';
  try {
    const d1 = await uploadOne(files.f1, otype, false);
    html += renderResult(d1, '主單');
    if (files.f2) {
      const d2 = await uploadOne(files.f2, 'main', true);
      html += renderResult(d2, '夾單');
    }
    document.getElementById('resultArea').innerHTML = html;
    loadToday();
  } catch(e) {
    document.getElementById('resultArea').innerHTML = `<div class="result err">錯誤: ${e}</div>`;
  }
  btn.disabled = false; btn.textContent = '送出記錄';
}

function renderResult(data, label) {
  if (data.error) return `<div class="result err"><b>${label} 錯誤</b>: ${data.error}</div>`;
  if (data.screen_type === 'accept' && (!data.platform || data.platform === 0)) {
    document.getElementById('amountOverride').style.display = 'block';
  }
  const type = data.screen_type === 'accept' ? '接單' : data.screen_type === 'arrived' ? '抵達' : '完成';
  return `<div class="result">
    <b>${label} — ${type}</b><br>
    餐廳：${data.restaurant || '—'}<br>
    平台：$${data.platform || '⚠️需手動填'}　底價：$${data.platform_base || '—'}<br>
    ${data.est_min ? '預估：'+data.est_min+'分' : ''}
    ${data.total_min != null ? '　實際：'+data.total_min+'分　補差：$'+data.supplement : ''}
  </div>`;
}

async function loadToday() {
  const r = await fetch('/today');
  const data = await r.json();
  const list = document.getElementById('todayList');
  if (!data.records.length) { list.innerHTML = '<div style="color:#888;font-size:13px">尚無記錄</div>'; return; }
  let html = '';
  for (const rec of data.records) {
    const typeLabel = {main:'主單',clamp:'夾單',stack:'疊單',stack_clamp:'疊夾'}[rec.type]||rec.type;
    const supp = rec.supplement != null ? `補差 $${rec.supplement}` : '進行中';
    html += `<div class="row-item">
      ${rec.accept_time}→${rec.complete_time||'?'}
      <span class="badge ${rec.type}">${typeLabel}</span>
      　$${rec.platform} ${supp}<br>
      <span style="color:#888;font-size:12px">${rec.restaurant||''}</span>
    </div>`;
  }
  document.getElementById('todayTitle').textContent = `今日記錄（${data.records.length}筆）`;
  list.innerHTML = html;
  document.querySelector('.total-bar') && document.querySelector('.total-bar').remove();
  document.getElementById('todayCard').insertAdjacentHTML('afterend',
    `<div class="total-bar"><span>平台 $${data.total_platform}</span><span>補差 $${data.total_supp}</span><span>合計 $${data.total_platform+data.total_supp}</span></div>`);
}
loadToday();
</script>
</body>
</html>"""

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

@app.route('/today')
def today():
    records = get_records()
    tp = sum(r.get('platform', 0) for r in records)
    ts = sum(r.get('supplement') or 0 for r in records)
    return jsonify({'records': records, 'total_platform': tp, 'total_supp': ts})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5555))
    app.run(host='0.0.0.0', port=port, debug=False)
