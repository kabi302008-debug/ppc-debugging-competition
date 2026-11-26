"""PPC Online Python Debugging Competition - SINGLE FILE (backend + frontend + database).

RUN LOCALLY (VS Code):
  1. pip install -r requirements.txt
  2. python app.py          (default admin: admin@ppc.com / Admin@123 - change with env vars)
  3. Open http://localhost:5000
       Admin   -> /admin/login
       Student -> /student/login
  New admin / reset admin password:  python app.py create-admin EMAIL PASSWORD

RENDER:
  Start command:  gunicorn app:app --workers 1 --threads 8 --timeout 120
  Env vars: ADMIN_EMAIL, ADMIN_PASSWORD, SECRET_KEY, DB_PATH (on a PERSISTENT disk, e.g. /var/data/ppc.db)

TIME: every competition time is Indian Standard Time (IANA zone Asia/Kolkata),
  independent of the server's own timezone (Render runs in UTC).

NOTES:
  - Student code runs in a separate process with time/memory limits, not a full sandbox.
    For a real contest use Docker/Judge0.
  - Credential PDF: browser Print -> Save as PDF. Results: CSV (Excel) or Print -> PDF.
"""
import os, sys, json, sqlite3, secrets, subprocess, tempfile, time, csv, io, shutil
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, session, jsonify, redirect, Response
from werkzeug.security import generate_password_hash as H, check_password_hash as C
try:
    import resource
except ImportError:  # Windows
    resource = None

# ---------- Indian Standard Time (Tamil Nadu) ----------
try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo('Asia/Kolkata')
except Exception:  # tzdata missing: IST is a fixed +05:30 offset with no daylight saving, so this is exact
    IST = timezone(timedelta(hours=5, minutes=30), 'IST')

def now():
    """Actual current time in IST (timezone-aware). Never uses the server's local timezone."""
    return datetime.now(IST)

def fmt(dt):
    dt = dt.astimezone(IST)
    return f"{dt:%a %d %b %Y}, {int(dt.strftime('%I'))}:{dt:%M} {dt:%p} IST"

def fts(ts): return fmt(datetime.fromtimestamp(ts, IST)) if ts else ''

def t24(s):
    """Accept '19:30', '7:30 PM', '07:30pm', '12:00 AM' ... and return normalised 24h 'HH:MM'."""
    s = ' '.join(str(s or '').strip().upper().replace('.', '').split())
    for f in ('%H:%M', '%I:%M %p', '%I:%M%p', '%H:%M:%S', '%I:%M:%S %p'):
        try: return datetime.strptime(s, f).strftime('%H:%M')
        except ValueError: pass
    raise ValueError(f'Invalid time: {s!r}')

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get('DB_PATH', os.path.join(BASE, 'ppc.db'))
app = Flask(__name__)
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
_sk = os.path.join(BASE, '.secret')
if not os.path.exists(_sk):
    try: open(_sk, 'w').write(secrets.token_hex(32))
    except OSError: pass
app.secret_key = os.environ.get('SECRET_KEY') or (open(_sk).read() if os.path.exists(_sk) else secrets.token_hex(32))
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')

SCHEMA = '''
create table if not exists admins(id integer primary key,email text unique,pw text);
create table if not exists teams(id integer primary key,tid text unique,name text,pw text,m1 text,m2 text,college text,active int default 1,seen real default 0,tok text);
create table if not exists questions(id integer primary key,type text,rnd int,title text,body text,ifmt text,ofmt text,opts text,ans text,marks real,sin text,sout text,tests text,tl int default 5,pub int default 0);
create table if not exists settings(k text primary key,v text);
create table if not exists sessions(id integer primary key,tid text unique,start real,endt real,sub int default 0,subat real,r1 real default 0,r2 real default 0);
create table if not exists answers(sid int,qid int,ans text,marks real default 0,unique(sid,qid));
'''

ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'admin@ppc.com').strip().lower()
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'Admin@123')
MEM_MB = 256

def Q(sql, a=(), one=False, w=False):
    c = sqlite3.connect(DB, timeout=15); c.row_factory = sqlite3.Row
    try:
        cur = c.execute(sql, a)
        if w:
            c.commit(); return cur.lastrowid
        return cur.fetchone() if one else cur.fetchall()
    finally:
        c.close()

def L(rows): return [dict(r) for r in rows]
def err(m, c=400): return jsonify(error=m), c
DEF = {'duration': '30', 'r1min': '10', 'start': '19:30', 'last': '20:00', 'close': '20:30', 'date': '', 'status': 'draft', 'public_url': ''}
def S(k):
    r = Q('select v from settings where k=?', (k,), 1); return r['v'] if r else DEF.get(k, '')
def setS(k, v): Q('insert or replace into settings values(?,?)', (k, str(v)), w=True)

def init():
    c = sqlite3.connect(DB); c.executescript(SCHEMA)
    # ---- safe migrations: only ADD missing columns, never drop/reset data ----
    cols = {r[1] for r in c.execute('pragma table_info(questions)')}
    if 'ptitle' not in cols: c.execute("alter table questions add column ptitle text default ''")
    c.commit(); c.close()
    # Fixed admin login (set ADMIN_EMAIL / ADMIN_PASSWORD env vars)
    Q('insert into admins(email,pw) values(?,?) on conflict(email) do update set pw=excluded.pw', (ADMIN_EMAIL, H(ADMIN_PASSWORD)), w=True)
    print(f'\nRunning file: {os.path.abspath(__file__)}')
    print(f'Admin login  ->  Email: {ADMIN_EMAIL}   (password from ADMIN_PASSWORD env var / default)')
    print(f'Server time (IST): {fmt(now())}\n')

# ---------- code execution (see note: use Docker/Judge0 for strong isolation) ----------
PYBIN = sys.executable if 'python' in os.path.basename(sys.executable).lower() else (shutil.which('python3') or 'python3')

def run(code, inp='', t=5):
    t = max(1, min(int(t or 5), 15))
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, 's.py'); open(f, 'w').write(code or '')
        def lim():
            try:
                resource.setrlimit(resource.RLIMIT_CPU, (t + 1, t + 1))
                resource.setrlimit(resource.RLIMIT_AS, (MEM_MB << 20, MEM_MB << 20))
                resource.setrlimit(resource.RLIMIT_FSIZE, (1 << 20, 1 << 20))
            except Exception: pass
        try:
            p = subprocess.run([PYBIN, '-I', f], input=inp or '', capture_output=True, text=True, timeout=t, cwd=d,
                               env={'PATH': os.environ.get('PATH', '')}, preexec_fn=lim if (os.name == 'posix' and resource) else None)
            return p.stdout[:5000], p.stderr[-1500:]
        except subprocess.TimeoutExpired:
            return '', 'Time limit exceeded'
        except Exception as e:
            return '', str(e)

def norm(s): return '\n'.join(l.rstrip() for l in (s or '').strip().splitlines())
def nrm(s): return ' '.join(str(s or '').split()).casefold()
CODE = ('coding', 'debugging')

def tests_of(q):
    try: t = json.loads(q['tests'] or '[]')
    except ValueError: t = []
    return [x for x in t if isinstance(x, dict)]

def grade(q, ans):
    ans = ans or ''
    if q['type'] in CODE:
        tests = tests_of(q)   # ALL tests (visible + hidden) count for final marks
        if not tests: return 0
        ok = sum(1 for t in tests if norm(run(ans, t.get('input', ''), q['tl'])[0]) == norm(t.get('output', '')))
        return round(q['marks'] * ok / len(tests), 2)
    good = [nrm(x) for x in (q['ans'] or '').split('||')]
    return q['marks'] if nrm(ans) in good else 0

def finalize(s):
    c = sqlite3.connect(DB, timeout=15)
    cur = c.execute('update sessions set sub=1,subat=? where id=? and sub=0', (min(time.time(), s['endt']), s['id'])); c.commit(); c.close()
    if cur.rowcount == 0: return
    qs = {q['id']: q for q in Q('select * from questions where pub=1')}
    r = {1: 0, 2: 0}
    for a in Q('select * from answers where sid=?', (s['id'],)):
        q = qs.get(a['qid'])
        if not q: continue
        m = grade(q, a['ans']); r[q['rnd']] = r.get(q['rnd'], 0) + m
        Q('update answers set marks=? where sid=? and qid=?', (m, s['id'], a['qid']), w=True)
    Q('update sessions set r1=?,r2=? where id=?', (r[1], r[2], s['id']), w=True)

# ---------- competition schedule (all IST) ----------
def schedule():
    """(entry opens, last entry, closes) as IST-aware datetimes, or None if no date is set.
    Times that fall before the previous one roll over to the next day (e.g. 11:30 PM start, 12:30 AM close)."""
    d = S('date')
    if not d: return None
    try:
        day = datetime.strptime(d, '%Y-%m-%d')
        def at(k):
            h, m = map(int, t24(S(k)).split(':')); return day.replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
        a, b, c = at('start'), at('last'), at('close')
    except ValueError:
        return None
    if b < a: b += timedelta(days=1)
    if c < b: c += timedelta(days=1)
    return a, b, c

def close_epoch():
    sc = schedule(); return sc[2].timestamp() if sc else None

def window(n=None):
    n = n or now()
    st = S('status')
    if st == 'stopped': return False, 'The competition has ended.'
    if st != 'live': return False, 'The competition is not live yet. Please wait for the organisers.'
    sc = schedule()
    if sc:
        a, b, c = sc
        if n < a: return False, f'Test opens at {fmt(a)}.'
        if n >= c: return False, f'The competition closed at {fmt(c)}.'
        if n >= b: return False, f'Entry closed. No new attempts after {fmt(b)}.'
    return True, ''

def summary():
    sc = schedule()
    if not sc: return 'No competition date set: students can enter at any time while the competition is Published.'
    return f'Opens: {fmt(sc[0])}\nLast entry: {fmt(sc[1])}\nCloses: {fmt(sc[2])}'

# ---------- auth helpers ----------
def adm(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get('admin'): return err('Unauthorized', 401)
        return f(*a, **k)
    return w

def stud(f):
    @wraps(f)
    def w(*a, **k):
        t = Q('select * from teams where tid=?', (session.get('team'),), 1)
        if not t or not t['tok'] or t['tok'] != session.get('tok') or not t['active']:
            session.clear(); return err('Session invalid. Please login again.', 401)
        Q('update teams set seen=? where id=?', (time.time(), t['id']), w=True)
        return f(t, *a, **k)
    return w

def sess(t): return Q('select * from sessions where tid=?', (t['tid'],), 1)
def live(t):
    s = sess(t)
    if not s or s['sub']: return None
    if time.time() >= s['endt']: finalize(s); return None
    return s

@app.after_request
def hdr(r):
    r.headers['X-Frame-Options'] = 'DENY'; r.headers['Cache-Control'] = 'no-store'; return r

# ---------- pages ----------
CSS = '''<meta name=viewport content="width=device-width,initial-scale=1"><style>
*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,Segoe UI,sans-serif;background:#fff;color:#12263f}
.top{background:#eaf4ff;padding:14px 20px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #cfe5fb;gap:10px;flex-wrap:wrap}
.wrap{max-width:1100px;margin:0 auto;padding:16px}.card{border:1px solid #d6e8fa;border-radius:12px;padding:16px;margin:12px 0;background:#fff}
button,.btn{background:#2f8fe8;color:#fff;border:0;border-radius:8px;padding:9px 16px;cursor:pointer;font-size:14px;text-decoration:none;display:inline-block;margin:2px 0}
button.alt{background:#eaf4ff;color:#1b5fa8}button.red{background:#e5484d}button:disabled{opacity:.5;cursor:not-allowed}
input,select,textarea{width:100%;padding:9px;border:1px solid #bcd6f0;border-radius:8px;font:inherit;margin:4px 0 10px}
textarea{resize:vertical}input[type=radio]{width:auto;margin-right:8px}table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:8px;border-bottom:1px solid #e3effb;text-align:left}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}.big{font-size:28px;font-weight:700;color:#2f8fe8}
.center{max-width:380px;margin:10vh auto;padding:0 16px}.err{color:#c62828;font-size:14px;min-height:20px}.tbl{overflow-x:auto}
pre{background:#f4f9ff;padding:10px;border-radius:8px;white-space:pre-wrap;overflow-x:auto}.warn{background:#fff3cd;color:#7a5b00}
.ok{color:#1b7f46}.clock{font-weight:600;color:#1b5fa8}.sel{display:flex;gap:4px}.sel select{margin:4px 0 10px}
.CodeMirror{border:1px solid #bcd6f0;border-radius:8px;height:300px;font-size:14px}
@media(max-width:600px){.wrap{padding:10px}.top{padding:10px}.card{padding:12px}}</style>'''
JS = '''<script>const E=s=>document.getElementById(s);
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(u,m,b){const r=await fetch(u,{method:m||'GET',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined});
const j=await r.json().catch(()=>({}));if(!r.ok){const e=new Error(j.error||'Error');e.status=r.status;throw e}return j}</script>'''
def page(title, body, extra=''): return Response(f'<!doctype html><title>{title}</title>{CSS}{JS}{body}{extra}')

@app.get('/')
def home():
    return page('PPC Competition', '''<div class=center style="text-align:center;max-width:460px"><h1>Programmers Paradise Club</h1>
<p>Department of Computer Science and Engineering<br>Python Debugging Competition</p>
<a class=btn style="display:block;padding:16px;margin:12px 0" href=/admin/login>Admin Portal</a>
<a class=btn style="display:block;padding:16px;margin:12px 0" href=/student/login>Student Portal</a></div>''')

def login_page(title, sub, fields, api_url, goto, extra=''):
    return page(title, f'''<div class=center><div class=card><h2>{title}</h2><p>{sub}</p>{fields}<div class=err id=e></div>
<button style="width:100%" onclick=go()>Login</button>{extra}</div></div>''', f'''<script>
async function go(){{try{{await api('{api_url}','POST',{{a:E('a').value,pw:E('p').value}});location='{goto}'}}catch(x){{E('e').textContent=x.message}}}}
document.addEventListener('keydown',e=>{{if(e.key=='Enter')go()}})</script>''')

@app.get('/admin/login')
def admin_login():
    if session.get('admin'): return redirect('/admin/dashboard')
    return login_page('Admin Login', 'Authorised administrators only', 'Email<input id=a type=email autocomplete=username>Password<input id=p type=password autocomplete=current-password>',
        '/api/a/login', '/admin/dashboard', '<p><a href=# onclick="alert(\'Ask the server owner to run: python app.py create-admin EMAIL NEWPASSWORD\');return false">Forgot Password?</a></p>')

@app.get('/student/login')
def student_login():
    if session.get('team'): return redirect('/student/dashboard')
    return login_page('Team Verification', 'PPC Python Debugging Competition', 'Team Name<input id=a autocomplete=off>Team Leader Name<input id=p autocomplete=off>',
        '/api/s/login', '/student/dashboard')

@app.get('/api/a/logout')
def a_logout(): session.clear(); return redirect('/admin/login')
@app.get('/api/s/logout')
def s_logout():
    Q('update teams set tok=null where tid=?', (session.get('team'),), w=True); session.clear(); return redirect('/student/login')

@app.post('/api/a/login')
def a_login():
    d = request.json or {}; a = Q('select * from admins where email=?', ((d.get('a') or '').strip().lower(),), 1)
    if not a or not C(a['pw'], (d.get('pw') or '').strip()):
        print(f"[LOGIN FAILED] email typed = {d.get('a')!r}"); return err('Invalid credentials', 401)
    session.clear(); session['admin'] = a['id']; return jsonify(ok=1)

@app.post('/api/s/login')
def s_login():
    d = request.json or {}; nm = lambda x: ' '.join((x or '').split()).casefold()
    t = next((r for r in Q('select * from teams') if nm(d.get('pw')) and nm(r['name']) == nm(d.get('a')) and nm(r['m1']) == nm(d.get('pw'))), None)
    if not t: time.sleep(1); return err('Team Name and Team Leader Name do not match a registered team', 401)
    if not t['active']: return err('This team is deactivated. Contact the organisers.', 403)
    if t['tok'] and time.time() - t['seen'] < 45: return err('This team already has an active session on another device.', 409)
    tok = secrets.token_hex(16); Q('update teams set tok=?,seen=? where id=?', (tok, time.time(), t['id']), w=True)
    session.clear(); session['team'] = t['tid']; session['tok'] = tok; return jsonify(ok=1)

# ---------- admin APIs ----------
@app.get('/api/a/overview')
@adm
def a_ov():
    n = time.time(); cnt = lambda r, p=None: Q('select count(*) c from questions where rnd=?' + (' and pub=1' if p else ''), (r,), 1)['c']
    return jsonify(teams=Q('select count(*) c from teams', one=True)['c'], questions=Q('select count(*) c from questions', one=True)['c'],
        r1=cnt(1), r1p=cnt(1, 1), r2=cnt(2), r2p=cnt(2, 1),
        active=Q('select count(*) c from sessions where sub=0 and endt>?', (n,), 1)['c'], done=Q('select count(*) c from sessions where sub=1', one=True)['c'], status=S('status'),
        now_ms=int(time.time() * 1000))

QF = ['type', 'rnd', 'title', 'body', 'ifmt', 'ofmt', 'opts', 'ans', 'marks', 'sin', 'sout', 'tests', 'tl', 'pub', 'ptitle']
TYPES = ('mcq', 'oneword', 'output', 'error', 'blank', 'coding', 'debugging')

@app.get('/api/a/questions')
@adm
def a_qs(): return jsonify(L(Q('select * from questions order by rnd,id')))

def qvals(d):
    d = d or {}
    typ = d.get('type') or 'oneword'
    if typ not in TYPES: raise ValueError('Invalid question type')
    try: rnd = int(d.get('rnd') or 1)
    except (TypeError, ValueError): raise ValueError('Invalid round')
    if rnd not in (1, 2): raise ValueError('Round must be 1 or 2')
    if not (d.get('title') or '').strip(): raise ValueError('Title required')
    try: marks = float(d.get('marks') or 0)
    except (TypeError, ValueError): raise ValueError('Marks must be a number')
    if marks < 0: raise ValueError('Marks cannot be negative')
    try: tl = max(1, min(int(d.get('tl') or 5), 15))
    except (TypeError, ValueError): raise ValueError('Time limit must be a number of seconds')
    pub = 1 if int(d.get('pub') or 0) else 0
    tests = d.get('tests') or '[]'
    if typ in CODE:
        try:
            tl_ = json.loads(tests); assert isinstance(tl_, list)
        except Exception: raise ValueError('Test cases must be a valid list')
        clean = []
        for t in tl_:
            if not isinstance(t, dict): raise ValueError('Invalid test case')
            if not str(t.get('output', '')).strip(): raise ValueError('Every test case needs an expected output')
            hid = bool(t.get('hidden', True))
            clean.append({'input': str(t.get('input', '')), 'output': str(t.get('output', '')), 'hidden': hid, 'kind': t.get('kind') or ('hidden' if hid else 'extra')})
        tests = json.dumps(clean)
        if pub and not clean: raise ValueError('Add at least one test case before publishing')
    elif not (d.get('ans') or '').strip():
        raise ValueError('Correct answer required')
    g = lambda k: str(d.get(k) or '')
    return [typ, rnd, g('title').strip(), g('body'), g('ifmt'), g('ofmt'), g('opts'), g('ans').strip(), marks, g('sin'), g('sout'), tests, tl, pub, g('ptitle').strip()]

@app.post('/api/a/questions')
@adm
def a_qadd():
    try: v = qvals(request.json)
    except ValueError as e: return err(str(e))
    return jsonify(id=Q(f'insert into questions({",".join(QF)}) values({",".join("?" * len(QF))})', v, w=True))

@app.put('/api/a/questions/<int:i>')
@adm
def a_qput(i):
    try: v = qvals(request.json)
    except ValueError as e: return err(str(e))
    Q(f'update questions set {",".join(k + "=?" for k in QF)} where id=?', v + [i], w=True); return jsonify(ok=1)

@app.delete('/api/a/questions/<int:i>')
@adm
def a_qdel(i): Q('delete from questions where id=?', (i,), w=True); Q('delete from answers where qid=?', (i,), w=True); return jsonify(ok=1)

@app.post('/api/a/questions/<int:i>/pub')
@adm
def a_qpub(i):
    p = int((request.json or {}).get('pub', 0)); q = Q('select * from questions where id=?', (i,), 1)
    if not q: return err('Question not found', 404)
    if p:
        if q['type'] in CODE and not tests_of(q): return err('Add at least one test case before publishing')
        if q['type'] not in CODE and not (q['ans'] or '').strip(): return err('Correct answer required before publishing')
    Q('update questions set pub=? where id=?', (p, i), w=True); return jsonify(ok=1)

@app.get('/api/a/teams')
@adm
def a_ts(): return jsonify(L(Q('select id,tid,name,m1,m2,college,active from teams order by id')))

@app.post('/api/a/teams')
@adm
def a_tadd():
    d = request.json or {}; n = Q('select count(*) c from teams', one=True)['c'] + 1
    tid = (d.get('tid') or '').strip() or f'PPC{n:03d}'; pw = (d.get('pw') or '').strip() or secrets.token_urlsafe(6)
    if not (d.get('name') or '').strip(): return err('Team name required')
    if not (d.get('m1') or '').strip(): return err('Member 1 (Team Leader) name required')
    try: Q('insert into teams(tid,name,pw,m1,m2,college) values(?,?,?,?,?,?)', (tid, d['name'], H(pw), d.get('m1', ''), d.get('m2', ''), d.get('college', '')), w=True)
    except sqlite3.IntegrityError: return err('Team ID already exists')
    return jsonify(tid=tid, name=d['name'], pw=pw)

@app.put('/api/a/teams/<int:i>')
@adm
def a_tput(i):
    d = request.json or {}
    for k in ('name', 'm1'):
        if k in d and not str(d[k]).strip(): return err('Team Name and Team Leader Name cannot be empty')
    nt = (d.get('tid') or '').strip()
    old = Q('select tid from teams where id=?', (i,), 1)
    if old and nt and nt != old['tid']:
        try:
            Q('update teams set tid=? where id=?', (nt, i), w=True)
            Q('update sessions set tid=? where tid=?', (nt, old['tid']), w=True)
        except sqlite3.IntegrityError: return err('Team ID already exists')
    for k in ('name', 'm1', 'm2', 'college', 'active'):
        if k in d: Q(f'update teams set {k}=? where id=?', (d[k].strip() if isinstance(d[k], str) else d[k], i), w=True)
    if 'active' in d and not d['active']: Q('update teams set tok=null where id=?', (i,), w=True)
    pw = None
    if d.get('reset'):
        pw = secrets.token_urlsafe(6); Q('update teams set pw=?,tok=null where id=?', (H(pw), i), w=True)
    return jsonify(pw=pw)

@app.delete('/api/a/teams/<int:i>')
@adm
def a_tdel(i):
    t = Q('select tid from teams where id=?', (i,), 1)
    if t:
        s = Q('select id from sessions where tid=?', (t['tid'],), 1)
        if s: Q('delete from answers where sid=?', (s['id'],), w=True)
        Q('delete from sessions where tid=?', (t['tid'],), w=True); Q('delete from teams where id=?', (i,), w=True)
    return jsonify(ok=1)

def settings_out():
    o = {k: S(k) for k in DEF}
    o.update(now_ms=int(time.time() * 1000), now_ist=fmt(now()), tz='Asia/Kolkata', summary=summary()); return o

@app.route('/api/a/settings', methods=['GET', 'POST'])
@adm
def a_set():
    if request.method == 'POST':
        d = request.json or {}
        try:
            for k in ('start', 'last', 'close'):
                if k in d: d[k] = t24(d[k])
            if str(d.get('date') or '').strip(): datetime.strptime(str(d['date']).strip(), '%Y-%m-%d')
            if 'duration' in d and int(float(d['duration'])) < 1: raise ValueError('Test duration must be at least 1 minute')
            if 'r1min' in d and int(float(d['r1min'])) < 0: raise ValueError('Round 1 duration cannot be negative')
            for k in ('duration', 'r1min'):
                if k in d: d[k] = int(float(d[k]))
        except ValueError as e:
            return err(str(e) if 'must' in str(e) or 'Invalid time' in str(e) else 'Invalid date or number in settings')
        for k in ('date', 'start', 'last', 'close', 'duration', 'r1min', 'public_url'):
            if k in d: setS(k, str(d[k]).strip())
        if d.get('status') in ('draft', 'live', 'stopped'):
            setS('status', d['status'])
            if d['status'] == 'stopped':
                for s in Q('select * from sessions where sub=0'): finalize(s)
    return jsonify(settings_out())

def results():
    for s in Q('select * from sessions where sub=0 and endt<=?', (time.time(),)): finalize(s)
    out = []
    for r in Q('select t.tid,t.name,s.start,s.subat,s.sub,s.r1,s.r2 from teams t left join sessions s on s.tid=t.tid'):
        r = dict(r); r['r1'] = r['r1'] or 0; r['r2'] = r['r2'] or 0; r['total'] = r['r1'] + r['r2']
        r['time'] = round(r['subat'] - r['start']) if r['sub'] else None
        r['submitted'] = fts(r['subat']) if r['sub'] else ''; r['started'] = fts(r['start']) if r['start'] else ''
        r['status'] = 'Submitted' if r['sub'] else ('In progress' if r['start'] else 'Not started'); out.append(r)
    done = sorted([r for r in out if r['sub']], key=lambda r: (-r['total'], r['time']))
    for i, r in enumerate(done): r['rank'] = i + 1
    return sorted(out, key=lambda r: (r.get('rank') or 9999, r['name'])), S('status') == 'stopped'

@app.get('/api/a/results')
@adm
def a_res():
    r, fin = results(); return jsonify(rows=r, final=fin)

@app.get('/api/a/results.csv')
@adm
def a_csv():
    r, _ = results(); o = io.StringIO(); w = csv.writer(o)
    w.writerow(['Rank', 'Team Name', 'Team ID', 'Round 1', 'Round 2', 'Total', 'Completion Time (s)', 'Status', 'Started (IST)', 'Submitted (IST)'])
    for x in r: w.writerow([x.get('rank', ''), x['name'], x['tid'], x['r1'], x['r2'], x['total'], x['time'] if x['time'] is not None else '', x['status'], x['started'], x['submitted']])
    return Response(o.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=results.csv'})

@app.get('/api/a/submissions/<tid>')
@adm
def a_sub(tid):
    t = Q('select * from teams where tid=?', (tid,), 1); s = sess({'tid': tid}) if t else None
    if not s: return err('No submission for this team', 404)
    ans = {a['qid']: a for a in Q('select * from answers where sid=?', (s['id'],))}
    items = [{'rnd': q['rnd'], 'type': q['type'], 'title': q['ptitle'] or q['title'] if 'ptitle' in q.keys() else q['title'], 'max': q['marks'],
              'ans': ans[q['id']]['ans'] if q['id'] in ans else '', 'marks': ans[q['id']]['marks'] if q['id'] in ans else 0}
             for q in Q('select * from questions where pub=1 order by rnd,id')]
    return jsonify(team=t['name'], sub=s['sub'], items=items)

@app.get('/admin/dashboard')
def admin_dash():
    if not session.get('admin'): return redirect('/admin/login')
    return page('Admin Dashboard', '''<div class=top><b>PPC Admin Dashboard</b><a class="btn alt" href=/api/a/logout>Logout</a></div><div class=wrap><div id=tabs class=tabs></div><div id=v></div></div><script src=https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js></script>''', ADMIN_JS)

ADMIN_JS = r'''<script>
let V='Overview',QL=[],TL=[],SET={},SKEW=0,RS=[];
const TABS=['Overview','Round 1','Round 2','Teams','Submissions','Competition','Results'];
const fail=e=>{if(e.status==401)location='/admin/login';else alert(e.message)};
const ist=ms=>new Date(ms).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',weekday:'short',day:'2-digit',month:'short',year:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit',hour12:true})+' IST';
const isCode=q=>q&&(q.type=='coding'||q.type=='debugging');
function nav(){E('tabs').innerHTML=TABS.map(t=>`<button class="${t==V?'':'alt'}" onclick="go('${t}')">${t}</button>`).join(' ')}
function go(t){V=t;nav();({'Overview':ov,'Round 1':()=>rq(1),'Round 2':()=>rq(2),Teams:tm,Submissions:sb,Competition:cp,Results:rs})[t]().catch(fail)}
setInterval(()=>{const c=E('clk');if(c)c.textContent=ist(Date.now()+SKEW)},1000);

async function ov(){const o=await api('/api/a/overview');SKEW=o.now_ms-Date.now();E('v').innerHTML='<div class=grid>'+[['Registered Teams',o.teams],['Round 1 Questions (published)',o.r1+' ('+o.r1p+')'],['Round 2 Questions (published)',o.r2+' ('+o.r2p+')'],['Active Participants',o.active],['Completed Tests',o.done],['Competition Status',o.status.toUpperCase()]].map(x=>`<div class=card><div>${x[0]}</div><div class=big>${x[1]}</div></div>`).join('')+`</div><div class=card>Current Indian Standard Time (Tamil Nadu): <span class=clock id=clk></span></div>`}

/* ---------- questions (per round) ---------- */
async function rq(r){QL=(await api('/api/a/questions')).filter(q=>q.rnd==r);
E('v').innerHTML=`<h3>Round ${r} - ${r==1?'One Word Questions':'Programming and Debugging Questions'}</h3><button onclick="qf(${r})">+ Add Question</button><div id=qform></div><div id=pv></div><div class=tbl><table><tr><th>No.<th>${r==2?'Program / Question':'Question'}<th>Type<th>Marks<th>Status<th></tr>${QL.map((q,i)=>`<tr><td>${i+1}<td>${esc(q.ptitle?q.ptitle+' - ':'')}${esc(q.title)}<td>${q.type}<td>${q.marks}<td>${q.pub?'Published':'Draft'}<td><button class=alt onclick="pvw(${q.id})">Preview</button> <button class=alt onclick="qf(${r},${q.id})">Edit</button> <button class=alt onclick="tp(${q.id},${q.pub?0:1},${r})">${q.pub?'Unpublish':'Publish'}</button> <button class=red onclick="dq(${q.id},${r})">Delete</button></tr>`).join('')||'<tr><td colspan=6>No questions yet. Click "+ Add Question".</tr>'}</table></div>`}
function useCode(r,q){return r==2&&(!q||isCode(q))}
function qf(r,id){const q=QL.find(x=>x.id==id);E('pv').innerHTML='';E('qform').innerHTML=useCode(r,q)?f2(q):f1(r,q);scrollTo(0,0)}
function f1(r,q){q=q||{title:'',body:'',ans:'',marks:1};const id=q.id||0;
return `<div class=card><h3>${id?'Edit':'New'} Round ${r} Question</h3>Question Title<input id=f_title value="${esc(q.title)}">Question Description<textarea id=f_body rows=4>${esc(q.body)}</textarea>Correct ${r==1?'One-Word ':''}Answer <small>(use || between accepted alternatives; not case sensitive)</small><input id=f_ans value="${esc(q.ans)}">Marks<input id=f_marks type=number step=0.5 min=0 value="${q.marks}"><button onclick="sq(${r},${id},0)">Save Draft</button> <button onclick="sq(${r},${id},1)">Save &amp; Publish</button> <button class=alt onclick="E('qform').innerHTML=''">Cancel</button></div>`}
function f2(q){q=q||{type:'coding',marks:5,tl:5,ptitle:'',title:'',body:'',ifmt:'',ofmt:'',sin:'',sout:'',opts:'',tests:'[]'};const id=q.id||0;let T=[];try{T=JSON.parse(q.tests||'[]')}catch(e){}
const vis=T.filter(t=>t.hidden===false),by=k=>vis.find(t=>t.kind==k)||{input:'',output:''},x=vis.filter(t=>t.kind!='tc1'&&t.kind!='tc2'),h=T.filter(t=>t.hidden!==false);
setTimeout(()=>{x.forEach(t=>addT('xt',t.input,t.output));h.forEach(t=>addT('ht',t.input,t.output))},0);
return `<div class=card><h3>${id?'Edit':'New'} Round 2 Question</h3><div class=grid><div>Type<select id=f_type><option value=coding ${q.type=='coding'?'selected':''}>Python Coding</option><option value=debugging ${q.type=='debugging'?'selected':''}>Python Debugging</option></select></div><div>Marks<input id=f_marks type=number step=0.5 min=0 value="${q.marks}"></div><div>Time limit (seconds per test, 1-15)<input id=f_tl type=number min=1 max=15 value="${q.tl}"></div></div>
Program Title<input id=f_ptitle value="${esc(q.ptitle)}">Question Title<input id=f_title value="${esc(q.title)}">Problem Description<textarea id=f_body rows=5>${esc(q.body)}</textarea>
<div class=grid><div>Input Format<textarea id=f_ifmt rows=2>${esc(q.ifmt)}</textarea></div><div>Output Format<textarea id=f_ofmt rows=2>${esc(q.ofmt)}</textarea></div><div>Sample Input<textarea id=f_sin rows=3>${esc(q.sin)}</textarea></div><div>Sample Output<textarea id=f_sout rows=3>${esc(q.sout)}</textarea></div></div>
<h4>Test Case 1 (visible to students)</h4><div class=grid><div>Input<textarea id=f_t1i rows=2>${esc(by('tc1').input)}</textarea></div><div>Expected Output<textarea id=f_t1o rows=2>${esc(by('tc1').output)}</textarea></div></div>
<h4>Test Case 2 (visible to students)</h4><div class=grid><div>Input<textarea id=f_t2i rows=2>${esc(by('tc2').input)}</textarea></div><div>Expected Output<textarea id=f_t2o rows=2>${esc(by('tc2').output)}</textarea></div></div>
<h4>Additional Test Cases (visible to students)</h4><div id=xt></div><button class=alt type=button onclick="addT('xt')">+ Add Test Case</button>
<h4>Hidden Test Cases (never shown to students - used for final marks)</h4><div id=ht></div><button class=alt type=button onclick="addT('ht')">+ Add Hidden Test Case</button>
<p><small>Marks are shared equally across all test cases (visible + hidden). Run Code on the student side checks only the sample and visible test cases.</small></p>
Starter / buggy code (optional - students start with this in the editor)<textarea id=f_opts rows=6 style="font-family:monospace">${esc(q.opts)}</textarea>
<button onclick="sq(2,${id},0)">Save Draft</button> <button onclick="sq(2,${id},1)">Save &amp; Publish</button> <button class=alt onclick="E('qform').innerHTML=''">Cancel</button></div>`}
function addT(box,i,o){const d=document.createElement('div');d.className='card';d.innerHTML='<div class=grid><div>Input<textarea class=ti rows=2></textarea></div><div>Expected Output<textarea class=to rows=2></textarea></div></div><button class=red type=button onclick="this.parentNode.remove()">Remove</button>';d.querySelector('.ti').value=i||'';d.querySelector('.to').value=o||'';E(box).appendChild(d)}
function gt(box,hidden,kind){return [...E(box).querySelectorAll('.card')].map(d=>({input:d.querySelector('.ti').value,output:d.querySelector('.to').value,hidden:hidden,kind:kind})).filter(t=>t.input.trim()||t.output.trim())}
async function sq(r,id,pub){const g=k=>E('f_'+k)?E('f_'+k).value:'';const q=QL.find(x=>x.id==id);let b;
if(!useCode(r,q))b={type:q?q.type:'oneword',rnd:r,marks:+g('marks'),tl:q?q.tl:5,title:g('title'),body:g('body'),ifmt:q?q.ifmt:'',ofmt:q?q.ofmt:'',opts:q?q.opts:'',ans:g('ans'),sin:q?q.sin:'',sout:q?q.sout:'',tests:q?q.tests:'[]',ptitle:q?q.ptitle:'',pub};
else{const T=[],one=(i,o,k)=>{if(i.trim()||o.trim())T.push({input:i,output:o,hidden:false,kind:k})};one(g('t1i'),g('t1o'),'tc1');one(g('t2i'),g('t2o'),'tc2');gt('xt',false,'extra').forEach(x=>T.push(x));gt('ht',true,'hidden').forEach(x=>T.push(x));
b={type:g('type'),rnd:2,marks:+g('marks'),tl:+g('tl'),title:g('title'),ptitle:g('ptitle'),body:g('body'),ifmt:g('ifmt'),ofmt:g('ofmt'),sin:g('sin'),sout:g('sout'),opts:g('opts'),ans:'',tests:JSON.stringify(T),pub}}
try{await api(id?'/api/a/questions/'+id:'/api/a/questions',id?'PUT':'POST',b);rq(r)}catch(e){alert(e.message)}}
async function tp(id,p,r){try{await api(`/api/a/questions/${id}/pub`,'POST',{pub:p});rq(r)}catch(e){alert(e.message)}}
async function dq(id,r){if(confirm('Delete this question? Student answers to it are deleted too.')){await api('/api/a/questions/'+id,'DELETE');rq(r)}}
function pvw(id){const q=QL.find(x=>x.id==id);let T=[];try{T=JSON.parse(q.tests||'[]')}catch(e){}
E('pv').innerHTML=`<div class=card><h3>Preview: ${esc(q.ptitle?q.ptitle+' - ':'')}${esc(q.title)} (${q.marks} marks)</h3><pre>${esc(q.body)}</pre>${q.ifmt?'<b>Input Format:</b><pre>'+esc(q.ifmt)+'</pre>':''}${q.ofmt?'<b>Output Format:</b><pre>'+esc(q.ofmt)+'</pre>':''}${q.sin?'<b>Sample Input:</b><pre>'+esc(q.sin)+'</pre><b>Sample Output:</b><pre>'+esc(q.sout)+'</pre>':''}${isCode(q)?(q.opts?'<b>Starter code:</b><pre>'+esc(q.opts)+'</pre>':'')+'<small>Time limit '+q.tl+'s | '+T.filter(t=>t.hidden===false).length+' visible and '+T.filter(t=>t.hidden!==false).length+' hidden test cases</small>':(q.opts?'<pre>'+esc(q.opts)+'</pre>':'')+'<small>Answer key: '+esc(q.ans)+'</small>'} <button class=alt onclick="E('pv').innerHTML=''">Close</button></div>`;E('pv').scrollIntoView()}

/* ---------- teams ---------- */
function sheet(n,leader){const w=window.open('','_blank');w.document.write(`<html><body style="font-family:sans-serif;padding:40px"><h1>Programmers Paradise Club</h1><h3>Python Debugging Competition</h3><hr><p>Team Name: <b>${esc(n)}</b></p><p>Team Leader: <b>${esc(leader)}</b></p><p>Student Portal: <b>${esc(pubUrl())}</b></p><p>Open the link, enter your Team Name and Team Leader Name exactly as above.</p><script>setTimeout(()=>print(),400)<\/script></body></html>`);w.document.close()}
async function tm(){TL=await api('/api/a/teams');E('v').innerHTML=`<div class=card><h3 id=th>Add Team</h3><div class=grid><div>Team ID (blank = auto)<input id=t_tid></div><div>Team Name<input id=t_name></div><div>Team Leader Name<input id=t_m1></div></div><input type=hidden id=t_id><button onclick="st()">Save Team</button> <button class=alt onclick="tm()">Clear</button></div><div class=tbl><table><tr><th>Team ID<th>Team Name<th>Team Leader<th>Active<th></tr>${TL.map(t=>`<tr><td>${esc(t.tid)}<td>${esc(t.name)}<td>${esc(t.m1)}<td>${t.active?'Yes':'No'}<td><button class=alt onclick="et(${t.id})">Edit</button> <button class=alt onclick="rp(${t.id})">Credential PDF</button> <button class=alt onclick="ta(${t.id},${t.active?0:1})">${t.active?'Deactivate':'Activate'}</button> <button class=red onclick="dt(${t.id})">Delete</button></tr>`).join('')}</table></div>`}
function et(i){const t=TL.find(x=>x.id==i);t_name.value=t.name;t_tid.value=t.tid;t_m1.value=t.m1;t_id.value=i;th.textContent='Edit Team';scrollTo(0,0)}
async function st(){try{const b={name:t_name.value,tid:t_tid.value,m1:t_m1.value};
if(t_id.value){await api('/api/a/teams/'+t_id.value,'PUT',b);tm()}else{await api('/api/a/teams','POST',b);tm()}}catch(e){fail(e)}}
function rp(i){const t=TL.find(x=>x.id==i);sheet(t.name,t.m1)}
async function ta(i,a){await api('/api/a/teams/'+i,'PUT',{active:a});tm()}
async function dt(i){if(confirm('Delete team and its submissions?')){await api('/api/a/teams/'+i,'DELETE');tm()}}

/* ---------- submissions ---------- */
async function sb(){const r=await api('/api/a/results');RS=r.rows;E('v').innerHTML=`<h3>Student Submissions</h3><div class=tbl><table><tr><th>Team<th>ID<th>Status<th>Submitted (IST)<th>Total<th></tr>${RS.map((x,i)=>`<tr><td>${esc(x.name)}<td>${esc(x.tid)}<td>${x.status}<td>${esc(x.submitted||'-')}<td>${x.total}<td>${x.start?`<button class=alt onclick="sd(${i})">View answers</button>`:''}</tr>`).join('')}</table></div><div id=sdv></div>`}
async function sd(i){try{const r=await api('/api/a/submissions/'+encodeURIComponent(RS[i].tid));E('sdv').innerHTML=`<div class=card><h3>${esc(r.team)} - ${r.sub?'Submitted':'In progress (marks are calculated when the test is submitted)'}</h3>${r.items.map(x=>`<div class=card><b>Round ${x.rnd}: ${esc(x.title)}</b> - Marks: ${x.marks} / ${x.max}<pre>${esc(x.ans||'(no answer)')}</pre></div>`).join('')}<button class=alt onclick="E('sdv').innerHTML=''">Close</button></div>`;E('sdv').scrollIntoView()}catch(e){fail(e)}}

/* ---------- competition (IST, 12-hour AM/PM) ---------- */
function tsel(id,v){const m=/^(\d{1,2}):(\d{2})/.exec(v||''),H=m?+m[1]:19,M=m?+m[2]:30,ap=H>=12?'PM':'AM',h=H%12||12;
let o='<div class=sel><select id='+id+'_h>';for(let i=1;i<=12;i++)o+=`<option ${i==h?'selected':''}>${i}</option>`;
o+='</select><select id='+id+'_m>';for(let i=0;i<60;i++){const s=String(i).padStart(2,'0');o+=`<option ${i==M?'selected':''}>${s}</option>`}
return o+'</select><select id='+id+'_p><option '+(ap=='AM'?'selected':'')+'>AM</option><option '+(ap=='PM'?'selected':'')+'>PM</option></select></div>'}
function tval(id){let h=(+E(id+'_h').value)%12;if(E(id+'_p').value=='PM')h+=12;return String(h).padStart(2,'0')+':'+E(id+'_m').value}
function pubUrl(){return (SET.public_url||location.origin).replace(/\/+$/,'')+'/student/login'}
async function cp(){SET=await api('/api/a/settings');SKEW=SET.now_ms-Date.now();const u=pubUrl(),bad=/localhost|127\.|192\.168\.|\b10\.\d+\.|\.local\b/.test(u),s=SET;
E('v').innerHTML=`<div class=card><h3>Competition status: <span class=big>${({draft:'DRAFT',live:'PUBLISHED',stopped:'STOPPED'})[s.status]}</span></h3><p>Current Indian Standard Time (Tamil Nadu): <span class=clock id=clk></span></p><div class=grid><div>Competition date<input id=c_date type=date value="${s.date}"></div><div>Entry opens (IST)${tsel('c_start',s.start)}</div><div>Last entry time (IST)${tsel('c_last',s.last)}</div><div>Closing time (IST)${tsel('c_close',s.close)}</div><div>Test duration (min)<input id=c_duration type=number min=1 value="${s.duration}"></div><div>Round 1 duration (min)<input id=c_r1min type=number min=0 value="${s.r1min}"></div></div><p class=ok><b>Saved schedule (IST - Tamil Nadu time):</b><br>${esc(s.summary).replace(/\n/g,'<br>')}</p>Public website address (your deployed URL, e.g. https://myapp.onrender.com)<input id=c_public_url value="${esc(s.public_url)}" placeholder="https://..."><button onclick="ss('draft')">Save as Draft</button> <button class=alt onclick=prev()>Preview</button> <button onclick="pub()">Publish Competition</button> <button class=alt onclick="ss('draft')">Unpublish Competition</button> <button class=red onclick="ss('stopped')">Stop Competition</button><p><small>All times are Indian Standard Time (Asia/Kolkata), whatever the server's timezone. Draft/Unpublished: students see a waiting message. Published: students can start from the entry time until the last-entry time. Stop force-submits all running tests.</small></p></div>
<div class=card><h3>Student Portal URL (same for all teams)</h3>${bad?'<p class=err><b>This is a local address - only your own laptop can open it.</b> Deploy the app online, then type the public address in the box above and save.</p>':''}<input id=su readonly value="${esc(u)}"><button onclick=cpy()>Copy Link</button> <button class=alt onclick=shr()>Share Link</button><div id=qr style="margin:10px 0"></div></div>`;
E('qr').innerHTML='';if(window.QRCode)new QRCode(E('qr'),{text:u,width:180,height:180})}
async function ss(st){const b={};['date','duration','r1min','public_url'].forEach(k=>b[k]=E('c_'+k).value);['start','last','close'].forEach(k=>b[k]=tval('c_'+k));if(st)b.status=st;try{await api('/api/a/settings','POST',b);cp()}catch(e){fail(e)}}
function pub(){const u=(E('c_public_url').value||location.origin);if(/localhost|127\.|192\.168\./.test(u)&&!confirm('The student link is a local address and will NOT open for other people. Publish anyway?'))return;ss('live')}
function cpy(){const i=E('su');i.select();try{navigator.clipboard.writeText(i.value).catch(()=>document.execCommand('copy'))}catch(e){document.execCommand('copy')}alert('Link copied')}
function shr(){const u=E('su').value,t='PPC Python Debugging Competition - open this link to enter: '+u;if(navigator.share)navigator.share({title:'PPC Competition',text:t,url:u});else window.open('https://wa.me/?text='+encodeURIComponent(t),'_blank')}
async function prev(){const q=await api('/api/a/questions');const w=window.open('','_blank');w.document.write('<body style="font-family:sans-serif;max-width:800px;margin:auto"><h2>Preview - what students will see</h2>'+q.map(x=>`<hr><b>Round ${x.rnd}: ${esc(x.ptitle?x.ptitle+' - ':'')}${esc(x.title)}</b> (${x.marks} marks) <i>[${x.pub?'Published':'Draft - hidden from students'}]</i><pre style="white-space:pre-wrap">${esc(x.body)}${/coding|debugging/.test(x.type)?'':'\n'+esc(x.opts)}</pre>`).join(''));w.document.close()}

/* ---------- results ---------- */
async function rs(){E('v').innerHTML='Evaluating...';const r=await api('/api/a/results');const m=x=>x==null?'-':Math.floor(x/60)+'m '+x%60+'s';E('v').innerHTML=`<p>${r.final?'<b>Final rankings</b> (competition closed)':'<i>Provisional rankings - final after the competition is stopped.</i>'}</p><a class=btn href=/api/a/results.csv>Download Excel/CSV</a> <button class=alt onclick=print()>Print / Save PDF</button> <button class=alt onclick=rs()>Refresh</button><div class=tbl><table><tr><th>Rank<th>Team<th>ID<th>Round 1<th>Round 2<th>Total<th>Completion<th>Status<th>Submitted (IST)</tr>${r.rows.map(x=>`<tr><td>${x.rank||'-'}<td>${esc(x.name)}<td>${esc(x.tid)}<td>${x.r1}<td>${x.r2}<td><b>${x.total}</b><td>${m(x.time)}<td>${x.status}<td>${esc(x.submitted||'-')}</tr>`).join('')}</table></div>`}
nav();api('/api/a/settings').then(x=>{SET=x;SKEW=x.now_ms-Date.now()}).catch(()=>{});go('Overview')</script>'''

# ---------- student APIs ----------
@app.get('/student/dashboard')
def stud_dash():
    if not session.get('team'): return redirect('/student/login')
    return page('Student Portal', '''<link rel=stylesheet href=https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.css>
<div class=top><b>PPC Debugging Competition</b><span><b id=tm></b> <a class="btn alt" href=/api/s/logout>Logout</a></span></div><div class=wrap id=app>Loading...</div>
<script src=https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js></script><script src=https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/python/python.min.js></script>''', STUD_JS)

@app.get('/api/s/state')
@stud
def s_state(t):
    s = sess(t)
    if s and not s['sub'] and time.time() >= s['endt']: finalize(s); s = sess(t)
    o = {'team': t['name'], 'dur': S('duration'), 'r1': S('r1min'), 'ist': fmt(now()), 'mem': MEM_MB}
    if not s:
        ok, m = window(); o.update(state='ready' if ok else 'wait', msg=m); return jsonify(o)
    if s['sub']: o['state'] = 'done'; return jsonify(o)
    saved = {a['qid']: a['ans'] for a in Q('select qid,ans from answers where sid=?', (s['id'],))}
    n = time.time(); r1rem = max(0, s['start'] + int(S('r1min')) * 60 - n)
    qs = []
    for q in Q('select * from questions where pub=1 order by rnd,id'):
        q = dict(q); isc = q['type'] in CODE
        if q['rnd'] == 1 and r1rem <= 0: continue   # Round 1 is only available during its own time
        # hidden tests, answer keys and the raw test list are never sent to students
        qs.append({'id': q['id'], 'type': q['type'], 'rnd': q['rnd'], 'title': q['title'], 'ptitle': q.get('ptitle') or '', 'body': q['body'], 'ifmt': q['ifmt'], 'ofmt': q['ofmt'],
                   'marks': q['marks'], 'tl': q['tl'], 'sin': q['sin'], 'sout': q['sout'], 'opts': q['opts'], 'saved': saved.get(q['id'], q['opts'] if isc else '')})
    o.update(state='running', rem=max(0, s['endt'] - n), r1rem=r1rem, qs=qs); return jsonify(o)

@app.post('/api/s/start')
@stud
def s_start(t):
    if sess(t): return err('Test already started')
    ok, m = window()
    if not ok: return err(m, 403)
    n = time.time(); e = n + int(S('duration')) * 60; c = close_epoch()
    if c: e = min(e, c)
    try: Q('insert into sessions(tid,start,endt) values(?,?,?)', (t['tid'], n, e), w=True)
    except sqlite3.IntegrityError: pass
    return jsonify(ok=1)

@app.post('/api/s/save')
@stud
def s_save(t):
    s = live(t)
    if not s: return err('Test is not active', 403)
    d = request.json or {}; q = Q('select * from questions where id=? and pub=1', (d.get('qid'),), 1)
    if not q: return err('Invalid question', 404)
    if q['rnd'] == 1 and time.time() > s['start'] + int(S('r1min')) * 60: return err('Round 1 time is over', 403)
    Q('insert into answers(sid,qid,ans) values(?,?,?) on conflict(sid,qid) do update set ans=excluded.ans', (s['id'], q['id'], str(d.get('answer', ''))[:20000]), w=True)
    return jsonify(ok=1, at=fmt(now()))

def visible_cases(q):
    """Sample + visible test cases only. Hidden test cases are never returned."""
    vis = [t for t in tests_of(q) if t.get('hidden') is False]
    cases, extra = [], 0
    for t in vis:
        k = t.get('kind')
        if k == 'tc1': lab = 'Test Case 1'
        elif k == 'tc2': lab = 'Test Case 2'
        else: extra += 1; lab = f'Additional Test Case {extra}'
        cases.append((lab, t.get('input', ''), t.get('output', '')))
    si, so = q['sin'] or '', q['sout'] or ''
    if (si.strip() or so.strip()) and not any(norm(c[1]) == norm(si) and norm(c[2]) == norm(so) for c in cases):
        cases.insert(0, ('Sample', si, so))
    return cases[:12]

@app.post('/api/s/run')
@stud
def s_run(t):
    s = live(t)
    if not s: return err('Test is not active', 403)
    if time.time() - session.get('lr', 0) < 2: return err('Please wait a moment between runs', 429)
    session['lr'] = time.time(); d = request.json or {}
    q = Q("select * from questions where id=? and pub=1 and type in ('coding','debugging')", (d.get('qid'),), 1)
    if not q: return err('Invalid question', 404)
    code = str(d.get('code', ''))[:20000]
    if d.get('custom'):
        o, e = run(code, str(d.get('stdin', ''))[:5000], q['tl']); return jsonify(out=o, err=e)
    res = []
    for lab, i, exp in visible_cases(q):
        o, e = run(code, i, q['tl'])
        res.append({'label': lab, 'ok': norm(o) == norm(exp) and not (e and not o and 'Time limit' in e), 'input': i, 'expected': exp, 'got': o, 'err': e})
    return jsonify(results=res, passed=sum(1 for r in res if r['ok']), total=len(res))

@app.post('/api/s/submit')
@stud
def s_submit(t):
    s = sess(t)
    if s and not s['sub']: finalize(s)
    return jsonify(ok=1)

STUD_JS = r'''<script>
let ST,endAt=0,r1At=0,built=false,ED={},cur=0,tmr={},warn=0,r1x=false,R2Q=[];const R=1000;
const fail=e=>{if(e.status==401)location='/student/login';else alert(e.message)};
const code=q=>q.type=='coding'||q.type=='debugging';
const tnow=()=>new Date().toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour12:true})+' IST';
async function load(){try{const j=await api('/api/s/state');const was=ST&&ST.state;ST=j;if(j.state=='running'){endAt=Date.now()+j.rem*R;r1At=Date.now()+j.r1rem*R}
if(!built||was!=j.state||j.state!='running')draw()}catch(e){fail(e)}}
async function refresh(){for(const id in ED)try{await sv(+id,ED[id].getValue())}catch(e){}built=false;await load()}
function draw(){built=true;ED={};const a=E('app');E('tm').textContent='';
if(ST.state=='done'){a.innerHTML=`<div class=card><h2>Test submitted</h2><p>Thank you, ${esc(ST.team)}. Your answers are locked and are being evaluated. Rankings will be announced by the organisers.</p></div>`;return}
if(ST.state!='running'){a.innerHTML=`<div class=card><h2>Welcome, ${esc(ST.team)}!</h2><p>Current time: <b>${esc(ST.ist)}</b> (Indian Standard Time)</p><h3>Instructions</h3><ul><li>Each team has ${ST.dur} minutes.</li><li>There are two rounds: Round 1 (One Word Questions, ${ST.r1} min) and Round 2 (Programming &amp; Debugging).</li><li>Read each question carefully.</li><li>Submit all answers before the timer ends.</li><li>The test auto-submits when the timer reaches zero.</li><li>Refreshing the page does NOT reset the timer.</li><li>Only one active session is allowed per team.</li></ul>${ST.state=='wait'?`<p class=err>${esc(ST.msg)}</p>`:''}<button onclick=start() ${ST.state=='wait'?'disabled':''}>Start Test</button> <button class=alt onclick=load()>Refresh status</button></div>`;return}
const r1=ST.qs.filter(q=>q.rnd==1),r2=ST.qs.filter(q=>q.rnd==2);R2Q=r2;
const fb='<div class=card><button class=red onclick=fin()>Finish &amp; Submit Test</button></div>';
if(!r1.length&&!r2.length){a.innerHTML='<div class=card>No questions are available right now. Please wait or contact the organisers.</div>'+fb;return}
a.innerHTML=`<div id=wn class="card warn" style="display:none"></div><div>${r1.length?'<button id=b1 onclick="rd(1)">Round 1 - One Word Questions</button> ':''}${r2.length?'<button id=b2 class=alt onclick="rd(2)">Round 2 - Programming &amp; Debugging</button>':''}</div>
${r1.length?`<div id=R1><div class=card><b>Round 1 closes in <span id=r1t></span></b></div>${r1.map((q,i)=>qhtml(q,i)).join('')}<div class=card><button onclick="sv1()">Save Round 1 Answers</button> <span id=s1></span></div></div>`:''}
${r2.length?`<div id=R2 style="display:none"><div>${r2.map((q,i)=>`<button class=alt id=nb${i} onclick="cq(${i})">Question ${i+1}</button>`).join(' ')}</div>${r2.map((q,i)=>panel(q,i)).join('')}</div>`:''}${fb}`;
r2.forEach(q=>{if(code(q)){ED[q.id]=CodeMirror.fromTextArea(E('ta'+q.id),{mode:'python',lineNumbers:true,indentUnit:4,tabSize:4,indentWithTabs:false,extraKeys:{Tab:c=>c.replaceSelection('    ')}});ED[q.id].on('change',()=>deb(q.id,()=>ED[q.id].getValue()))}});
cur=0;rd(r1.length?1:2)}
function qhtml(q,i){let inp;if(q.type=='mcq')inp=q.opts.split('\n').filter(x=>x.trim()).map(o=>`<label style="display:block;margin:6px 0"><input type=radio name=q${q.id} value="${esc(o)}" ${q.saved==o?'checked':''} onchange="sv(${q.id},this.value)">${esc(o)}</label>`).join('');
else inp=`<input id=a${q.id} value="${esc(q.saved)}" placeholder="Your answer" autocomplete=off oninput="deb(${q.id},()=>this.value)">`;
return `<div class=card><b>Q${i+1}. ${esc(q.title)}</b> <small>(${q.marks} marks)</small><pre>${esc(q.body)}</pre>${inp}</div>`}
function panel(q,i){if(!code(q))return `<div class=pn id=P${i} style="display:none">${qhtml(q,i)}</div>`;
return `<div class="card pn" id=P${i} style="display:none"><h3>${esc(q.ptitle||q.title)}</h3><b>${esc(q.title)}</b> <small>(${q.marks} marks | time limit ${q.tl}s per test | memory ${ST.mem} MB)</small><pre>${esc(q.body)}</pre>${q.ifmt?'<b>Input Format</b><pre>'+esc(q.ifmt)+'</pre>':''}${q.ofmt?'<b>Output Format</b><pre>'+esc(q.ofmt)+'</pre>':''}${q.sin?`<b>Sample Input</b><pre>${esc(q.sin)}</pre><b>Sample Output</b><pre>${esc(q.sout)}</pre>`:''}<textarea id=ta${q.id}>${esc(q.saved)}</textarea><p><button onclick="runc(${q.id})">&#9654; Run Code</button> <button class=alt onclick="subc(${q.id})">Submit Code</button> <span id=ss${q.id}></span></p><div id=rr${q.id}></div><details><summary>Run with custom input</summary><textarea id=in${q.id} rows=3>${esc(q.sin)}</textarea><button class=alt onclick="runx(${q.id})">Run with this input</button><b> Output</b><pre id=o${q.id}></pre><b>Errors</b><pre id=e${q.id} style="color:#c62828"></pre></details></div>`}
function rd(n){if(E('R1'))E('R1').style.display=n==1?'':'none';if(E('R2'))E('R2').style.display=n==2?'':'none';if(E('b1'))E('b1').className=n==1?'':'alt';if(E('b2'))E('b2').className=n==2?'':'alt';if(n==2)cq(cur)}
function cq(i){cur=i;document.querySelectorAll('.pn').forEach(e=>e.style.display='none');R2Q.forEach((q,j)=>{const b=E('nb'+j);if(b)b.className=j==i?'':'alt'});const c=E('P'+i);if(c){c.style.display='';const q=R2Q[i];if(q&&ED[q.id])setTimeout(()=>ED[q.id].refresh(),20)}}
async function sv(id,v){try{return await api('/api/s/save','POST',{qid:id,answer:v})}catch(e){if(e.status==403)load();else console.log(e.message)}}
function deb(id,f){clearTimeout(tmr[id]);tmr[id]=setTimeout(()=>sv(id,f()),800)}
function r1val(q){if(q.type=='mcq'){const c=document.querySelector('input[name=q'+q.id+']:checked');return c?c.value:null}const i=E('a'+q.id);return i?i.value:null}
async function sv1(){if(E('s1'))E('s1').textContent='Saving...';for(const q of ST.qs.filter(x=>x.rnd==1)){const v=r1val(q);if(v!==null)await sv(q.id,v)}if(E('s1'))E('s1').textContent='Saved at '+tnow()}
function rres(r){return r.results.map(x=>`<div class=card style="border-left:5px solid ${x.ok?'#2e9e5b':'#e5484d'}"><b>${esc(x.label)}: ${x.ok?'Passed':'Failed'}</b>${x.ok?'':`<pre>Input:\n${esc(x.input)}\nExpected output:\n${esc(x.expected)}\nYour output:\n${esc(x.got)}</pre>`}${x.err?`<pre style="color:#c62828">${esc(x.err)}</pre>`:''}</div>`).join('')+`<p><b>${r.passed} / ${r.total} visible tests passed</b></p>`}
async function runc(id){const o=E('rr'+id);o.innerHTML='Running...';try{await sv(id,ED[id].getValue());o.innerHTML=rres(await api('/api/s/run','POST',{qid:id,code:ED[id].getValue()}))}catch(e){o.innerHTML='<pre style="color:#c62828">'+esc(e.message)+'</pre>'}}
async function runx(id){E('o'+id).textContent='Running...';E('e'+id).textContent='';try{const r=await api('/api/s/run','POST',{qid:id,code:ED[id].getValue(),stdin:E('in'+id).value,custom:1});E('o'+id).textContent=r.out;E('e'+id).textContent=r.err}catch(e){E('o'+id).textContent='';E('e'+id).textContent=e.message}}
async function subc(id){E('ss'+id).textContent='Saving...';await sv(id,ED[id].getValue());E('ss'+id).textContent='Code submitted at '+tnow()+' (final marks use hidden tests when the test ends).'}
async function start(){try{await api('/api/s/start','POST');built=false;load()}catch(e){fail(e)}}
async function fin(){if(!confirm('Submit the test now? You cannot edit afterwards.'))return;for(const id in ED)await sv(+id,ED[id].getValue());if(E('R1'))await sv1();E('app').innerHTML='<div class=card>Submitting and evaluating...</div>';try{await api('/api/s/submit','POST')}catch(e){}built=false;load()}
setInterval(()=>{if(!ST||ST.state!='running')return;const s=Math.max(0,Math.round((endAt-Date.now())/R));E('tm').textContent='\u23F1 '+String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');
const t1=E('r1t');if(t1){const q=Math.max(0,Math.round((r1At-Date.now())/R));t1.textContent=String(Math.floor(q/60)).padStart(2,'0')+':'+String(q%60).padStart(2,'0');if(q<=0&&!r1x){r1x=true;refresh().then(()=>{r1x=false})}}
const w=E('wn');if(w){if(s<=300&&s>0){w.style.display='';w.textContent='Only 5 minutes remaining!'}}
if(s<=0){ST.state='x';fin2()}},500);
async function fin2(){try{await api('/api/s/submit','POST')}catch(e){}built=false;load()}
setInterval(()=>{if(ST&&ST.state!='done')load()},15000);
document.addEventListener('visibilitychange',()=>{if(document.hidden&&ST&&ST.state=='running'){warn++;alert('Warning: you left the test tab ('+warn+'). This may be reviewed by the organisers.')}});
load()</script>'''

if __name__ == '__main__':
    init()
    if len(sys.argv) >= 4 and sys.argv[1] == 'create-admin':
        em = sys.argv[2].lower()
        Q('insert into admins(email,pw) values(?,?) on conflict(email) do update set pw=excluded.pw', (em, H(sys.argv[3])), w=True); print('Admin saved:', em)
    else:
        app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), threaded=True)
else:
    init()
