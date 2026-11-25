"""PPC Online Python Debugging Competition - SINGLE FILE (backend + frontend + database).

RUN IN VS CODE:
  1. pip install flask gunicorn
  2. python app.py          (default admin: admin@ppc.com / Admin@123 - change at top of file)
  3. Open http://localhost:5000
       Admin   -> /admin/login
       Student -> /student/login
  New admin / reset admin password:  python app.py create-admin EMAIL PASSWORD

MAKE THE STUDENT LINK PUBLIC (localhost only works on YOUR laptop):
  Deploy this one file to a host (PythonAnywhere / Render / Railway), install flask, start with:
      gunicorn app:app --workers 1 --threads 8
  Set env vars ADMIN_EMAIL, ADMIN_PASSWORD, SECRET_KEY, and DB_PATH on a PERSISTENT disk.
  Then Admin -> Competition -> type your public address -> Save -> Publish.
  Share the Student Portal URL / QR shown there. Students enter Team Name + Team Leader Name.

NOTES:
  - Student code runs in a separate process with time/memory limits, not a full sandbox.
    For a real contest use Docker/Judge0.
  - Credential PDF: browser Print -> Save as PDF. Results: CSV (Excel) or Print -> PDF.
"""
import os, sys, json, sqlite3, secrets, subprocess, tempfile, time, csv, io
from datetime import datetime
from functools import wraps
from flask import Flask, request, session, jsonify, redirect, Response
from werkzeug.security import generate_password_hash as H, check_password_hash as C

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get('DB_PATH', os.path.join(BASE, 'ppc.db'))
app = Flask(__name__)
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
_sk = os.path.join(BASE, '.secret')
if not os.path.exists(_sk):
    open(_sk, 'w').write(secrets.token_hex(32))
app.secret_key = os.environ.get('SECRET_KEY') or open(_sk).read()
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
    c = sqlite3.connect(DB); c.executescript(SCHEMA); c.commit(); c.close()
    # Fixed admin login (edit these two lines, or set ADMIN_EMAIL / ADMIN_PASSWORD env vars)
    Q('insert into admins(email,pw) values(?,?) on conflict(email) do update set pw=excluded.pw', (ADMIN_EMAIL, H(ADMIN_PASSWORD)), w=True)
    print(f'\nRunning file: {os.path.abspath(__file__)}')
    print(f'Admin login  ->  Email: {ADMIN_EMAIL}   Password: {ADMIN_PASSWORD}\n')

# ---------- code execution (see README note: use Docker/Judge0 for strong isolation) ----------
import shutil
PYBIN = sys.executable if 'python' in os.path.basename(sys.executable).lower() else (shutil.which('python3') or 'python3')

def run(code, inp='', t=5):
    t = max(1, min(int(t or 5), 15))
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, 's.py'); open(f, 'w').write(code or '')
        def lim():
            try:
                import resource
                resource.setrlimit(resource.RLIMIT_CPU, (t + 1, t + 1))
                resource.setrlimit(resource.RLIMIT_AS, (300 << 20, 300 << 20))
                resource.setrlimit(resource.RLIMIT_FSIZE, (1 << 20, 1 << 20))
            except Exception: pass
        try:
            p = subprocess.run([PYBIN, '-I', f], input=inp or '', capture_output=True, text=True, timeout=t, cwd=d,
                               env={'PATH': os.environ.get('PATH', '')}, preexec_fn=lim if os.name == 'posix' else None)
            return p.stdout[:5000], p.stderr[-1500:]
        except subprocess.TimeoutExpired:
            return '', 'Time limit exceeded'
        except Exception as e:
            return '', str(e)

def norm(s): return '\n'.join(l.rstrip() for l in (s or '').strip().splitlines())

def grade(q, ans):
    ans = ans or ''
    if q['type'] in ('coding', 'debugging'):
        try: tests = json.loads(q['tests'] or '[]')
        except ValueError: tests = []
        if not tests: return 0
        ok = sum(1 for t in tests if norm(run(ans, t.get('input', ''), q['tl'])[0]) == norm(t.get('output', '')))
        return round(q['marks'] * ok / len(tests), 2)
    good = [x.strip().lower() for x in (q['ans'] or '').split('||')]
    return q['marks'] if ans.strip().lower() in good else 0

def finalize(s):
    c = sqlite3.connect(DB, timeout=15)
    cur = c.execute('update sessions set sub=1,subat=? where id=? and sub=0', (min(time.time(), s['endt']), s['id'])); c.commit(); c.close()
    if cur.rowcount == 0: return
    qs = {q['id']: q for q in Q('select * from questions where pub=1')}
    r = {1: 0, 2: 0}
    for a in Q('select * from answers where sid=?', (s['id'],)):
        q = qs.get(a['qid'])
        if not q: continue
        m = grade(q, a['ans']); r[q['rnd']] += m
        Q('update answers set marks=? where sid=? and qid=?', (m, s['id'], a['qid']), w=True)
    Q('update sessions set r1=?,r2=? where id=?', (r[1], r[2], s['id']), w=True)

def close_epoch():
    try: return datetime.strptime(S('date') + ' ' + S('close'), '%Y-%m-%d %H:%M').timestamp()
    except ValueError: return None

def window():
    if S('status') != 'live': return False, 'The competition is not live yet. Please wait for the organisers.'
    d = S('date')
    if d:
        try:
            n = datetime.now()
            a = datetime.strptime(d + ' ' + S('start'), '%Y-%m-%d %H:%M'); b = datetime.strptime(d + ' ' + S('last'), '%Y-%m-%d %H:%M')
            if n < a: return False, f'Test opens at {S("start")} on {d}.'
            if n > b: return False, f'Entry closed. No new attempts after {S("last")}.'
        except ValueError: pass
    return True, ''

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
button.alt{background:#eaf4ff;color:#1b5fa8}button.red{background:#e5484d}
input,select,textarea{width:100%;padding:9px;border:1px solid #bcd6f0;border-radius:8px;font:inherit;margin:4px 0 10px}
input[type=radio]{width:auto;margin-right:8px}table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:8px;border-bottom:1px solid #e3effb;text-align:left}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}.big{font-size:28px;font-weight:700;color:#2f8fe8}
.center{max-width:380px;margin:10vh auto;padding:0 16px}.err{color:#c62828;font-size:14px;min-height:20px}.tbl{overflow-x:auto}
pre{background:#f4f9ff;padding:10px;border-radius:8px;white-space:pre-wrap;overflow-x:auto}.warn{background:#fff3cd;color:#7a5b00}
.CodeMirror{border:1px solid #bcd6f0;border-radius:8px;height:300px;font-size:14px}</style>'''
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
        print(f"[LOGIN FAILED] email typed = {d.get('a')!r}  (server expects {ADMIN_EMAIL!r})"); return err('Invalid credentials', 401)
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
    n = time.time()
    return jsonify(teams=Q('select count(*) c from teams', one=True)['c'], questions=Q('select count(*) c from questions', one=True)['c'],
        active=Q('select count(*) c from sessions where sub=0 and endt>?', (n,), 1)['c'], done=Q('select count(*) c from sessions where sub=1', one=True)['c'], status=S('status'))

QF = ['type', 'rnd', 'title', 'body', 'ifmt', 'ofmt', 'opts', 'ans', 'marks', 'sin', 'sout', 'tests', 'tl', 'pub']
@app.get('/api/a/questions')
@adm
def a_qs(): return jsonify(L(Q('select * from questions order by rnd,id')))

def qvals(d):
    if d.get('type') in ('coding', 'debugging'):
        try: assert isinstance(json.loads(d.get('tests') or '[]'), list)
        except Exception: raise ValueError('Test cases must be valid JSON list')
    if not (d.get('title') or '').strip(): raise ValueError('Title required')
    return [d.get(k, '') if k not in ('rnd', 'tl', 'pub', 'marks') else (float(d.get(k) or 0) if k == 'marks' else int(d.get(k) or 0)) for k in QF]

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
def a_qpub(i): Q('update questions set pub=? where id=?', (int(request.json.get('pub', 0)), i), w=True); return jsonify(ok=1)

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

@app.route('/api/a/settings', methods=['GET', 'POST'])
@adm
def a_set():
    if request.method == 'POST':
        d = request.json or {}
        for k in ('date', 'start', 'last', 'close', 'duration', 'r1min', 'public_url'):
            if k in d: setS(k, str(d[k]).strip())
        if d.get('status') in ('draft', 'live', 'stopped'):
            setS('status', d['status'])
            if d['status'] == 'stopped':
                for s in Q('select * from sessions where sub=0'): finalize(s)
    return jsonify({k: S(k) for k in DEF})

def results():
    for s in Q('select * from sessions where sub=0 and endt<=?', (time.time(),)): finalize(s)
    out = []
    for r in Q('select t.tid,t.name,s.start,s.subat,s.sub,s.r1,s.r2 from teams t left join sessions s on s.tid=t.tid'):
        r = dict(r); r['r1'] = r['r1'] or 0; r['r2'] = r['r2'] or 0; r['total'] = r['r1'] + r['r2']
        r['time'] = round(r['subat'] - r['start']) if r['sub'] else None
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
    w.writerow(['Rank', 'Team Name', 'Team ID', 'Round 1', 'Round 2', 'Total', 'Completion Time (s)', 'Status'])
    for x in r: w.writerow([x.get('rank', ''), x['name'], x['tid'], x['r1'], x['r2'], x['total'], x['time'] if x['time'] is not None else '', x['status']])
    return Response(o.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=results.csv'})

@app.get('/admin/dashboard')
def admin_dash():
    if not session.get('admin'): return redirect('/admin/login')
    return page('Admin Dashboard', '''<div class=top><b>PPC Admin Dashboard</b><a class="btn alt" href=/api/a/logout>Logout</a></div><div class=wrap><div id=tabs></div><div id=v></div></div><script src=https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js></script>''', ADMIN_JS)

ADMIN_JS = r'''<script>
let V='Overview',QL=[],TL=[];const TABS=['Overview','Questions','Teams','Competition','Results'];
const fail=e=>{if(e.status==401)location='/admin/login';else alert(e.message)};
function nav(){E('tabs').innerHTML=TABS.map(t=>`<button class="${t==V?'':'alt'}" onclick="go('${t}')">${t}</button>`).join('')}
function go(t){V=t;nav();({Overview:ov,Questions:qs,Teams:tm,Competition:cp,Results:rs})[t]().catch(fail)}
async function ov(){const o=await api('/api/a/overview');E('v').innerHTML='<div class=grid>'+[['Registered Teams',o.teams],['Total Questions',o.questions],['Active Participants',o.active],['Completed Tests',o.done],['Competition Status',o.status.toUpperCase()]].map(x=>`<div class=card><div>${x[0]}</div><div class=big>${x[1]}</div></div>`).join('')+'</div>'}
async function qs(){QL=await api('/api/a/questions');E('v').innerHTML=`<button onclick="qf()">+ Add Question</button><div id=qform></div><div id=pv></div><div class=tbl><table><tr><th>#<th>Round<th>Type<th>Title<th>Marks<th>Status<th></tr>${QL.map(q=>`<tr><td>${q.id}<td>${q.rnd}<td>${q.type}<td>${esc(q.title)}<td>${q.marks}<td>${q.pub?'Published':'Draft'}<td><button class=alt onclick="pvw(${q.id})">Preview</button> <button class=alt onclick="qf(${q.id})">Edit</button> <button class=alt onclick="tp(${q.id},${q.pub?0:1})">${q.pub?'Unpublish':'Publish'}</button> <button class=red onclick="dq(${q.id})">Delete</button></tr>`).join('')}</table></div>`}
function qf(id){const q=QL.find(x=>x.id==id)||{type:'mcq',rnd:1,marks:1,tl:5,title:'',body:'',ifmt:'',ofmt:'',opts:'',ans:'',sin:'',sout:'',tests:'[]'};
const T=[['mcq','Multiple Choice'],['oneword','One-Word Answer'],['output','Predict the Output'],['error','Find the Error'],['blank','Fill in the Blanks'],['coding','Python Coding'],['debugging','Python Debugging']];
E('qform').innerHTML=`<div class=card><h3>${id?'Edit':'New'} Question</h3><div class=grid><div>Type<select id=f_type>${T.map(t=>`<option value=${t[0]} ${t[0]==q.type?'selected':''}>${t[1]}</option>`).join('')}</select></div><div>Round<select id=f_rnd><option value=1 ${q.rnd==1?'selected':''}>Round 1</option><option value=2 ${q.rnd==2?'selected':''}>Round 2</option></select></div><div>Marks<input id=f_marks type=number step=0.5 value="${q.marks}"></div><div>Time limit (sec, code)<input id=f_tl type=number value="${q.tl}"></div></div>Title<input id=f_title value="${esc(q.title)}">Question / Problem Description<textarea id=f_body rows=5>${esc(q.body)}</textarea><div class=grid><div>Input Format<textarea id=f_ifmt rows=2>${esc(q.ifmt)}</textarea></div><div>Output Format<textarea id=f_ofmt rows=2>${esc(q.ofmt)}</textarea></div><div>Sample Input<textarea id=f_sin rows=2>${esc(q.sin)}</textarea></div><div>Sample Output<textarea id=f_sout rows=2>${esc(q.sout)}</textarea></div></div>MCQ options (one per line) OR starter/buggy code for coding &amp; debugging<textarea id=f_opts rows=6>${esc(q.opts)}</textarea>Correct answer (non-code; use || between accepted alternatives)<input id=f_ans value="${esc(q.ans)}">Hidden test cases (coding/debugging) as JSON list of input/output objects<textarea id=f_tests rows=4>${esc(q.tests)}</textarea><button onclick="sq(${id||0},0)">Save Draft</button> <button onclick="sq(${id||0},1)">Save &amp; Publish</button> <button class=alt onclick="E('qform').innerHTML=''">Cancel</button></div>`;scrollTo(0,0)}
async function sq(id,pub){const g=k=>E('f_'+k).value;const b={type:g('type'),rnd:+g('rnd'),marks:+g('marks'),tl:+g('tl'),title:g('title'),body:g('body'),ifmt:g('ifmt'),ofmt:g('ofmt'),sin:g('sin'),sout:g('sout'),opts:g('opts'),ans:g('ans'),tests:g('tests'),pub};try{await api(id?'/api/a/questions/'+id:'/api/a/questions',id?'PUT':'POST',b);qs()}catch(e){alert(e.message)}}
async function tp(id,p){await api(`/api/a/questions/${id}/pub`,'POST',{pub:p});qs()}
async function dq(id){if(confirm('Delete this question?')){await api('/api/a/questions/'+id,'DELETE');qs()}}
function pvw(id){const q=QL.find(x=>x.id==id),c=/coding|debugging/.test(q.type);E('pv').innerHTML=`<div class=card><h3>Preview: ${esc(q.title)} (${q.marks} marks)</h3><pre>${esc(q.body)}</pre>${q.ifmt?'<b>Input:</b><pre>'+esc(q.ifmt)+'</pre>':''}${q.ofmt?'<b>Output:</b><pre>'+esc(q.ofmt)+'</pre>':''}${q.sin?'<b>Sample Input:</b><pre>'+esc(q.sin)+'</pre><b>Sample Output:</b><pre>'+esc(q.sout)+'</pre>':''}${q.opts?'<pre>'+esc(q.opts)+'</pre>':''}<small>Answer key: ${esc(q.ans)} ${c?'| tests: '+esc(q.tests):''}</small> <button class=alt onclick="E('pv').innerHTML=''">Close</button></div>`}
function sheet(n,leader){const w=window.open('','_blank');w.document.write(`<html><body style="font-family:sans-serif;padding:40px"><h1>Programmers Paradise Club</h1><h3>Python Debugging Competition</h3><hr><p>Team Name: <b>${esc(n)}</b></p><p>Team Leader: <b>${esc(leader)}</b></p><p>Student Portal: <b>${esc(pubUrl())}</b></p><p>Open the link, enter your Team Name and Team Leader Name exactly as above.</p><script>setTimeout(()=>print(),400)<\/script></body></html>`);w.document.close()}
async function tm(){TL=await api('/api/a/teams');E('v').innerHTML=`<div class=card><h3 id=th>Add Team</h3><div class=grid><div>Team ID (blank = auto)<input id=t_tid></div><div>Team Name<input id=t_name></div><div>Team Leader Name<input id=t_m1></div></div><input type=hidden id=t_id><button onclick="st()">Save Team</button> <button class=alt onclick="tm()">Clear</button></div><div class=tbl><table><tr><th>Team ID<th>Team Name<th>Team Leader<th>Active<th></tr>${TL.map(t=>`<tr><td>${esc(t.tid)}<td>${esc(t.name)}<td>${esc(t.m1)}<td>${t.active?'Yes':'No'}<td><button class=alt onclick="et(${t.id})">Edit</button> <button class=alt onclick="rp(${t.id})">Credential PDF</button> <button class=alt onclick="ta(${t.id},${t.active?0:1})">${t.active?'Deactivate':'Activate'}</button> <button class=red onclick="dt(${t.id})">Delete</button></tr>`).join('')}</table></div>`}
function et(i){const t=TL.find(x=>x.id==i);t_name.value=t.name;t_tid.value=t.tid;t_m1.value=t.m1;t_id.value=i;th.textContent='Edit Team';scrollTo(0,0)}
async function st(){try{const b={name:t_name.value,tid:t_tid.value,m1:t_m1.value};
if(t_id.value){await api('/api/a/teams/'+t_id.value,'PUT',b);tm()}else{await api('/api/a/teams','POST',b);tm()}}catch(e){fail(e)}}
function rp(i){const t=TL.find(x=>x.id==i);sheet(t.name,t.m1)}
async function ta(i,a){await api('/api/a/teams/'+i,'PUT',{active:a});tm()}
async function dt(i){if(confirm('Delete team and its submissions?')){await api('/api/a/teams/'+i,'DELETE');tm()}}
let SET={};
function pubUrl(){return (SET.public_url||location.origin).replace(/\/+$/,'')+'/student/login'}
async function cp(){SET=await api('/api/a/settings');const u=pubUrl(),bad=/localhost|127\.|192\.168\.|\b10\.\d+\.|\.local\b/.test(u),s=SET;
E('v').innerHTML=`<div class=card><h3>Competition status: <span class=big>${({draft:'DRAFT',live:'PUBLISHED',stopped:'STOPPED'})[s.status]}</span></h3><div class=grid><div>Date<input id=c_date type=date value="${s.date}"></div><div>Entry opens<input id=c_start type=time value="${s.start}"></div><div>Last entry time<input id=c_last type=time value="${s.last}"></div><div>Closing time<input id=c_close type=time value="${s.close}"></div><div>Test duration (min)<input id=c_duration type=number value="${s.duration}"></div><div>Round 1 duration (min)<input id=c_r1min type=number value="${s.r1min}"></div></div>Public website address (your deployed URL, e.g. https://myapp.onrender.com)<input id=c_public_url value="${esc(s.public_url)}" placeholder="https://..."><button onclick="ss('draft')">Save as Draft</button> <button class=alt onclick=prev()>Preview</button> <button onclick="pub()">Publish Competition</button> <button class=alt onclick="ss('draft')">Unpublish Competition</button> <button class=red onclick="ss('stopped')">Stop Competition</button><p><small>Draft/Unpublished: students can open the link but see a waiting message. Published: students can start between the entry and last-entry times. Stop force-submits all running tests.</small></p></div>
<div class=card><h3>Student Portal URL (same for all teams)</h3>${bad?'<p class=err><b>This is a local address - only your own laptop can open it.</b> Deploy the app online, then type the public address in the box above and save.</p>':''}<input id=su readonly value="${esc(u)}"><button onclick=cpy()>Copy Link</button> <button class=alt onclick=shr()>Share Link</button><div id=qr style="margin:10px 0"></div></div>`;
E('qr').innerHTML='';if(window.QRCode)new QRCode(E('qr'),{text:u,width:180,height:180})}
async function ss(st){const b={};['date','start','last','close','duration','r1min','public_url'].forEach(k=>b[k]=E('c_'+k).value);if(st)b.status=st;try{await api('/api/a/settings','POST',b);cp()}catch(e){fail(e)}}
function pub(){const u=(E('c_public_url').value||location.origin);if(/localhost|127\.|192\.168\./.test(u)&&!confirm('The student link is a local address and will NOT open for other people. Publish anyway?'))return;ss('live')}
function cpy(){const i=E('su');i.select();try{navigator.clipboard.writeText(i.value).catch(()=>document.execCommand('copy'))}catch(e){document.execCommand('copy')}alert('Link copied')}
function shr(){const u=E('su').value,t='PPC Python Debugging Competition - open this link to enter: '+u;if(navigator.share)navigator.share({title:'PPC Competition',text:t,url:u});else window.open('https://wa.me/?text='+encodeURIComponent(t),'_blank')}
async function prev(){const q=await api('/api/a/questions');const w=window.open('','_blank');w.document.write('<body style="font-family:sans-serif;max-width:800px;margin:auto"><h2>Preview - what students will see</h2>'+q.map(x=>`<hr><b>Round ${x.rnd}: ${esc(x.title)}</b> (${x.marks} marks) <i>[${x.pub?'Published':'Draft - hidden from students'}]</i><pre style="white-space:pre-wrap">${esc(x.body)}\n${esc(x.opts)}</pre>`).join(''));w.document.close()}
async function rs(){E('v').innerHTML='Evaluating...';const r=await api('/api/a/results');const m=x=>x==null?'-':Math.floor(x/60)+'m '+x%60+'s';E('v').innerHTML=`<p>${r.final?'<b>Final rankings</b> (competition closed)':'<i>Provisional rankings - final after the competition is stopped.</i>'}</p><a class=btn href=/api/a/results.csv>Download Excel/CSV</a> <button class=alt onclick=print()>Print / Save PDF</button> <button class=alt onclick=rs()>Refresh</button><div class=tbl><table><tr><th>Rank<th>Team<th>ID<th>Round 1<th>Round 2<th>Total<th>Completion<th>Status</tr>${r.rows.map(x=>`<tr><td>${x.rank||'-'}<td>${esc(x.name)}<td>${esc(x.tid)}<td>${x.r1}<td>${x.r2}<td><b>${x.total}</b><td>${m(x.time)}<td>${x.status}</tr>`).join('')}</table></div>`}
nav();api('/api/a/settings').then(x=>SET=x).catch(()=>{});go('Overview')</script>'''

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
    o = {'team': t['name'], 'dur': S('duration'), 'r1': S('r1min')}
    if not s:
        ok, m = window(); o.update(state='ready' if ok else 'wait', msg=m); return jsonify(o)
    if s['sub']: o['state'] = 'done'; return jsonify(o)
    saved = {a['qid']: a['ans'] for a in Q('select qid,ans from answers where sid=?', (s['id'],))}
    qs = []
    for q in Q('select * from questions where pub=1 order by rnd,id'):
        q = dict(q); isc = q['type'] in ('coding', 'debugging')
        qs.append({'id': q['id'], 'type': q['type'], 'rnd': q['rnd'], 'title': q['title'], 'body': q['body'], 'ifmt': q['ifmt'], 'ofmt': q['ofmt'], 'marks': q['marks'],
                   'sin': q['sin'], 'sout': q['sout'], 'opts': q['opts'], 'saved': saved.get(q['id'], q['opts'] if isc else '')})
    n = time.time()
    o.update(state='running', rem=max(0, s['endt'] - n), r1rem=max(0, s['start'] + int(S('r1min')) * 60 - n), qs=qs); return jsonify(o)

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
    return jsonify(ok=1)

@app.post('/api/s/run')
@stud
def s_run(t):
    s = live(t)
    if not s: return err('Test is not active', 403)
    if time.time() - session.get('lr', 0) < 2: return err('Please wait a moment between runs', 429)
    session['lr'] = time.time(); d = request.json or {}
    q = Q('select * from questions where id=? and pub=1 and rnd=2', (d.get('qid'),), 1)
    if not q: return err('Invalid question', 404)
    o, e = run(str(d.get('code', ''))[:20000], d.get('stdin', q['sin']), q['tl']); return jsonify(out=o, err=e)

@app.post('/api/s/submit')
@stud
def s_submit(t):
    s = sess(t)
    if s and not s['sub']: finalize(s)
    return jsonify(ok=1)

STUD_JS = r'''<script>
let ST,endAt=0,built=false,ED={},cur=0,tmr={},warn=0;const R=1000;
const fail=e=>{if(e.status==401)location='/student/login';else alert(e.message)};
async function load(){try{const j=await api('/api/s/state');const was=ST&&ST.state;ST=j;if(j.state=='running')endAt=Date.now()+j.rem*R;if(!built||was!=j.state)draw()}catch(e){fail(e)}}
function draw(){built=true;const a=E('app');E('tm').textContent='';
if(ST.state=='done'){a.innerHTML=`<div class=card><h2>Test submitted</h2><p>Thank you, ${esc(ST.team)}. Your answers are locked and are being evaluated. Rankings will be announced by the organisers.</p></div>`;return}
if(ST.state!='running'){a.innerHTML=`<div class=card><h2>Welcome, ${esc(ST.team)}!</h2><h3>Instructions</h3><ul><li>Each team has ${ST.dur} minutes.</li><li>There are two rounds: Round 1 (Python Basics, ${ST.r1} min) and Round 2 (Python Debugging).</li><li>Read each question carefully.</li><li>Submit all answers before the timer ends.</li><li>The test auto-submits when the timer reaches zero.</li><li>Refreshing the page does NOT reset the timer.</li><li>Only one active session is allowed per team.</li></ul>${ST.state=='wait'?`<p class=err>${esc(ST.msg)}</p>`:''}<button onclick=start() ${ST.state=='wait'?'':''}>Start Test</button> <button class=alt onclick=load()>Refresh status</button></div>`;return}
const r1=ST.qs.filter(q=>q.rnd==1),r2=ST.qs.filter(q=>q.rnd==2);
a.innerHTML=`<div id=wn class="card warn" style="display:none"></div><div><button id=b1 onclick="rd(1)">Round 1 - Basics</button> <button id=b2 class=alt onclick="rd(2)">Round 2 - Debugging</button></div>
<div id=R1>${r1.map((q,i)=>qhtml(q,i)).join('')||'<p>No questions.</p>'}</div>
<div id=R2 style="display:none"><div>${r2.map((q,i)=>`<button class=alt onclick="cq(${i})">Question ${i+1}</button>`).join(' ')}</div>${r2.map((q,i)=>chtml(q,i)).join('')}</div>
<div class=card><button class=red onclick=fin()>Finish &amp; Submit Test</button></div>`;
r2.forEach((q,i)=>{ED[q.id]=CodeMirror.fromTextArea(E('ta'+q.id),{mode:'python',lineNumbers:true,indentUnit:4,tabSize:4,indentWithTabs:false,extraKeys:{Tab:c=>c.replaceSelection('    ')}});ED[q.id].on('change',()=>deb(q.id,()=>ED[q.id].getValue()))});cq(0)}
function qhtml(q,i){let inp;if(q.type=='mcq')inp=q.opts.split('\n').filter(x=>x.trim()).map(o=>`<label style="display:block;margin:6px 0"><input type=radio name=q${q.id} value="${esc(o)}" ${q.saved==o?'checked':''} onchange="sv(${q.id},this.value)">${esc(o)}</label>`).join('');
else inp=`<input value="${esc(q.saved)}" placeholder="Your answer" oninput="deb(${q.id},()=>this.value)">`;
return `<div class=card><b>Q${i+1}. ${esc(q.title)}</b> <small>(${q.marks} marks)</small><pre>${esc(q.body)}</pre>${inp}</div>`}
function chtml(q,i){return `<div class=card id=C${i} style="display:none"><b>${i==0?'Easy':'Medium'} - ${esc(q.title)}</b> <small>(${q.marks} marks)</small><pre>${esc(q.body)}</pre>${q.ifmt?'<b>Input:</b> '+esc(q.ifmt)+'<br>':''}${q.ofmt?'<b>Output:</b> '+esc(q.ofmt)+'<br>':''}${q.sin?`<b>Sample Input</b><pre>${esc(q.sin)}</pre><b>Sample Output</b><pre>${esc(q.sout)}</pre>`:''}<textarea id=ta${q.id}>${esc(q.saved)}</textarea><button onclick="runc(${q.id})">&#9654; Run Code</button> <button class=alt onclick="subc(${q.id})">Submit Code</button> <span id=ss${q.id}></span><p>Custom input (stdin)</p><textarea id=in${q.id} rows=2>${esc(q.sin)}</textarea><b>Output</b><pre id=o${q.id}></pre><b>Errors</b><pre id=e${q.id} style="color:#c62828"></pre></div>`}
function rd(n){E('R1').style.display=n==1?'':'none';E('R2').style.display=n==2?'':'none';E('b1').className=n==1?'':'alt';E('b2').className=n==2?'':'alt';if(n==2)cq(cur)}
function cq(i){cur=i;document.querySelectorAll('[id^=C]').forEach(e=>{if(/^C\d+$/.test(e.id))e.style.display='none'});const c=E('C'+i);if(c){c.style.display='';const q=ST.qs.filter(x=>x.rnd==2)[i];if(q)setTimeout(()=>ED[q.id].refresh(),20)}}
async function sv(id,v){try{await api('/api/s/save','POST',{qid:id,answer:v})}catch(e){if(e.status==403)load();else console.log(e.message)}}
function deb(id,f){clearTimeout(tmr[id]);tmr[id]=setTimeout(()=>sv(id,f()),800)}
async function runc(id){E('o'+id).textContent='Running...';E('e'+id).textContent='';try{await sv(id,ED[id].getValue());const r=await api('/api/s/run','POST',{qid:id,code:ED[id].getValue(),stdin:E('in'+id).value});E('o'+id).textContent=r.out;E('e'+id).textContent=r.err}catch(e){E('o'+id).textContent='';E('e'+id).textContent=e.message}}
async function subc(id){await sv(id,ED[id].getValue());E('ss'+id).textContent='Saved as your final code (evaluated on hidden tests at submission).'}
async function start(){try{await api('/api/s/start','POST');built=false;load()}catch(e){fail(e)}}
async function fin(){if(!confirm('Submit the test now? You cannot edit afterwards.'))return;for(const id in ED)await sv(+id,ED[id].getValue());E('app').innerHTML='<div class=card>Submitting and evaluating...</div>';try{await api('/api/s/submit','POST')}catch(e){}built=false;load()}
setInterval(()=>{if(!ST||ST.state!='running')return;const s=Math.max(0,Math.round((endAt-Date.now())/R));E('tm').textContent='\u23F1 '+String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');
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
