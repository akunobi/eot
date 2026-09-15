"""
SD EOT Exam — Corrector
------------------------
App Flask de un solo archivo, pensada para correr EN LOCAL (tu propio PC),
no desplegada en ningún servicio en la nube. Corrige las respuestas del
formulario de Google "SD EOT Exam": marca preguntas falladas, decide
Aprobado/Suspendido y genera el mensaje final a partir de las plantillas
formatpassed.txt / formatfailed.txt.

Configuración: pon tus datos en un archivo ".env" junto a este archivo
(hay una plantilla en .env.example) o expórtalos como variables de entorno.
Ver README.md para el paso a paso.
"""

import os
import re
import json
import sqlite3
import secrets
from functools import wraps
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, request, redirect, jsonify, make_response

from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleAuthRequest

# --------------------------------------------------------------------------
# Configuración — todo pensado para localhost
# --------------------------------------------------------------------------

# Render define automáticamente RENDER_EXTERNAL_URL con la URL pública del
# servicio (p. ej. "https://sd-eot-exam.onrender.com"). Si existe, la usamos
# para construir la redirect URI por defecto en vez de localhost.
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
IS_RENDER = bool(os.environ.get("RENDER")) or bool(RENDER_EXTERNAL_URL)

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
_default_redirect_uri = (
    "https://eot.devs.surf/oauth2callback" if IS_RENDER else "http://localhost:5000/oauth2callback"
)
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", _default_redirect_uri)
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")
FORM_TITLE = os.environ.get("FORM_TITLE", "SD EOT Exam")
# Render inyecta PORT automáticamente y espera que el proceso escuche en 0.0.0.0.
HOST = os.environ.get("HOST", "0.0.0.0" if IS_RENDER else "127.0.0.1")
PORT = int(os.environ.get("PORT", os.environ.get("LOCAL_PORT", "5000")))

# El flujo OAuth exige HTTPS salvo que se marque explícitamente lo contrario.
# Solo relajamos esto para desarrollo local por http://; en Render la URL
# externa ya es https, así que esta rama no debería activarse ahí.
if GOOGLE_REDIRECT_URI.startswith("http://"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/forms.body.readonly",
    "https://www.googleapis.com/auth/forms.responses.readonly",
]

CLIENT_CONFIG = {
    "web": {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [GOOGLE_REDIRECT_URI],
    }
}

# En Render, el filesystem es efímero salvo que adjuntes un "Persistent
# Disk" (requiere un plan de pago) y montes DB_PATH dentro de él, p. ej.
# DB_PATH=/var/data/eot_ledger.db con el disco montado en /var/data. Sin
# disco persistente, este archivo se resetea en cada redeploy y cada vez
# que la instancia gratuita se "duerme" y vuelve a arrancar.
DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "eot_ledger.db"),
)
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
# Render (y cualquier PaaS con proxy delante) termina el TLS y reenvía por
# HTTP interno, añadiendo cabeceras X-Forwarded-*. Con ProxyFix, Flask sabe
# que la petición original era https y request.is_secure se comporta bien
# (importante para las cookies "secure" de abajo).
try:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
except ImportError:
    pass

# Sesiones en memoria del proceso: sid (cookie) -> {"credentials": Credentials, "email": str}
# Sencillo a propósito: esta app la usa una sola persona.
SESSIONS = {}


# --------------------------------------------------------------------------
# Base de datos (ledger de exámenes ya procesados)
# --------------------------------------------------------------------------

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS ledger (
            response_id     TEXT PRIMARY KEY,
            username        TEXT,
            result          TEXT,      -- 'pass' | 'fail' | 'discarded'
            wrong_questions TEXT,      -- JSON list de títulos de pregunta
            message         TEXT,      -- texto final generado
            graded_at       TEXT,      -- ISO timestamp UTC
            archived        INTEGER DEFAULT 0
        )
        """
    )
    db.commit()
    db.close()


init_db()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(s):
    return datetime.fromisoformat(s)


# --------------------------------------------------------------------------
# Plantillas de mensaje (idénticas a formatfailed.txt / formatpassed.txt,
# con {username} y {bullets} como marcadores)
# --------------------------------------------------------------------------

FAIL_TEMPLATE = """Greetings {username},

I am writing to inform you of the result of your "EOT" application:
> **STATUS:** Reviewed
> **RESULT:** Failed

Below, you will find several resources that may help you improve your performance and increase your chances of passing in the future:
> Read the [**SD | Company Handbook**](https://docs.google.com/document/d/1PCm83DNfWZi5Y_vbmf1UQ_VBRhVdCyIwC2rPAEl21Hg/edit?usp=sharing)
> Read the [**BARC Speeder Coruscant Highway Code**](https://docs.google.com/document/d/1ipYHflZUKuCPSLSSKUBIoHE8DWk1QyrywslJRKRSgYs/edit?tab=t.0)
> Read the [**BARC Speeder CHC Punishment Trello**](https://trello.com/b/yHJXst5D/barc-chc-punishments)
> Read the [**Universal Vehicle Coruscant Highway Code**](https://docs.google.com/document/d/1ipYHflZUKuCPSLSSKUBIoHE8DWk1QyrywslJRKRSgYs/edit?tab=t.xctuw7toxgvj)
> Read the [**BARC General Information Document**](https://docs.google.com/document/d/1Pdr1jv-2M46iHEYB0kQzICrB18WSmFhj17tO1Jqare0/edit?usp=sharing)

Here are the questions you need to work on.
{bullets}

If you have any questions regarding your application or EOT in general, please feel free to send a DM to any Educator or CS+. However, it is recommended that you contact an Educator first.

Best regards,
ogmhabas"""

PASS_TEMPLATE = """Greetings {username},

I am writing to inform you of the result of your "EOT" application:
> **STATUS:** Reviewed
> **RESULT:** Passed

If you have any questions regarding EOT or the next steps of the process, please feel free to send a DM to any Educator or CS+. However, it is recommended that you contact an Educator first.

Here are the questions you need to work on for future reference. 
{bullets}

Best regards,
ogmhabas"""


def build_message(result, username, wrong_questions):
    username = username or "(usuario desconocido)"
    if wrong_questions:
        bullets = "\n".join(f"- {q}" for q in wrong_questions)
    else:
        bullets = "- (No se marcó ninguna pregunta como fallada)"
    template = FAIL_TEMPLATE if result == "fail" else PASS_TEMPLATE
    return template.replace("{username}", username).replace("{bullets}", bullets)


# --------------------------------------------------------------------------
# Autenticación
# --------------------------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        sid = request.cookies.get("sid")
        sess = SESSIONS.get(sid) if sid else None
        if not sess:
            if request.path.startswith("/api/"):
                return jsonify({"error": "auth_required"}), 401
            return redirect("/login")
        request.google_creds = sess["credentials"]
        request.user_email = sess["email"]
        return view(*args, **kwargs)
    return wrapped


def build_flow(code_verifier=None):
    return Flow.from_client_config(
        CLIENT_CONFIG,
        scopes=SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI,
        code_verifier=code_verifier,
    )


@app.route("/login")
def login():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return "Faltan GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET en las variables de entorno.", 500
    flow = build_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="select_account consent",
    )
    # PKCE: el verificador lo genera esta instancia de Flow y hace falta
    # reutilizarlo en /oauth2callback (que crea otra instancia), así que
    # viaja en una cookie de corta duración junto al "state".
    resp = make_response(redirect(auth_url))
    resp.set_cookie("oauth_state", state, httponly=True, samesite="Lax", secure=request.is_secure, max_age=600)
    resp.set_cookie("oauth_cv", flow.code_verifier, httponly=True, samesite="Lax", secure=request.is_secure, max_age=600)
    return resp


@app.route("/oauth2callback")
def oauth2callback():
    expected_state = request.cookies.get("oauth_state")
    code_verifier = request.cookies.get("oauth_cv")
    if not expected_state or request.args.get("state") != expected_state:
        return "Estado de OAuth inválido, vuelve a intentarlo desde /login.", 400
    if not code_verifier:
        return "Falta el verificador de PKCE (cookie expirada), vuelve a intentarlo desde /login.", 400

    flow = build_flow(code_verifier=code_verifier)
    try:
        flow.fetch_token(authorization_response=request.url)
    except Exception as exc:  # noqa: BLE001
        return f"No se pudo completar el inicio de sesión: {exc}", 400

    creds = flow.credentials
    try:
        oauth2_service = build("oauth2", "v2", credentials=creds)
        userinfo = oauth2_service.userinfo().get().execute()
        email = userinfo.get("email", "")
    except Exception as exc:  # noqa: BLE001
        return f"No se pudo verificar la cuenta de Google: {exc}", 400

    if ADMIN_EMAIL and email.lower() != ADMIN_EMAIL.lower():
        return f"La cuenta {email} no está autorizada a usar esta herramienta.", 403

    sid = secrets.token_urlsafe(32)
    SESSIONS[sid] = {"credentials": creds, "email": email}

    resp = make_response(redirect("/"))
    resp.set_cookie("sid", sid, httponly=True, samesite="Lax", secure=request.is_secure, max_age=60 * 60 * 12)
    resp.delete_cookie("oauth_state")
    resp.delete_cookie("oauth_cv")
    return resp


@app.route("/logout")
def logout():
    sid = request.cookies.get("sid")
    SESSIONS.pop(sid, None)
    resp = make_response(redirect("/"))
    resp.delete_cookie("sid")
    return resp


@app.route("/api/me")
@login_required
def api_me():
    return jsonify({"email": request.user_email, "form_title": FORM_TITLE})


# --------------------------------------------------------------------------
# Ayudantes de Google Forms / Drive
# --------------------------------------------------------------------------

def find_form_id(creds, title):
    drive = build("drive", "v3", credentials=creds)
    safe_title = title.replace("'", "\\'")
    query = f"name = '{safe_title}' and mimeType = 'application/vnd.google-apps.form' and trashed = false"
    res = drive.files().list(q=query, fields="files(id, name)", pageSize=5).execute()
    files = res.get("files", [])
    if not files:
        return None
    return files[0]["id"]


def get_form(creds, form_id):
    forms = build("forms", "v1", credentials=creds)
    return forms.forms().get(formId=form_id).execute()


def list_all_responses(creds, form_id):
    forms = build("forms", "v1", credentials=creds)
    responses = []
    page_token = None
    while True:
        kwargs = {"formId": form_id}
        if page_token:
            kwargs["pageToken"] = page_token
        res = forms.forms().responses().list(**kwargs).execute()
        responses.extend(res.get("responses", []))
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    return responses


def question_map(form):
    """Devuelve (dict questionId -> título, lista ordenada de questionId)."""
    qmap = {}
    order = []
    for item in form.get("items", []):
        qi = item.get("questionItem")
        if not qi:
            continue
        q = qi.get("question", {})
        qid = q.get("questionId")
        if not qid:
            continue
        title = item.get("title") or "(pregunta sin título)"
        qmap[qid] = title
        order.append(qid)
    return qmap, order


def extract_answer_text(answer):
    if not answer:
        return "(sin respuesta)"
    text_answers = answer.get("textAnswers")
    if text_answers:
        values = [a.get("value", "") for a in text_answers.get("answers", [])]
        joined = ", ".join(v for v in values if v)
        return joined or "(sin respuesta)"
    return "(tipo de respuesta no soportado)"


USERNAME_HINT = re.compile(r"usuario|username|discord|roblox", re.IGNORECASE)


def detect_username(qmap, order, answers):
    for qid in order:
        title = qmap.get(qid, "")
        if USERNAME_HINT.search(title) and qid in answers:
            val = extract_answer_text(answers[qid])
            if val and val != "(sin respuesta)":
                return val
    for qid in order:
        if qid in answers:
            val = extract_answer_text(answers[qid])
            if val and val != "(sin respuesta)":
                return val
    return "(usuario desconocido)"


def graded_response_ids():
    db = get_db()
    rows = db.execute("SELECT response_id FROM ledger").fetchall()
    db.close()
    return {r["response_id"] for r in rows}


# --------------------------------------------------------------------------
# API: exámenes pendientes / detalle / calificar / eliminar / recientes
# --------------------------------------------------------------------------

@app.route("/api/pending")
@login_required
def api_pending():
    creds = request.google_creds
    form_id = find_form_id(creds, FORM_TITLE)
    if not form_id:
        return jsonify({
            "error": f'No se encontró un formulario de Google llamado "{FORM_TITLE}" al que tenga acceso esta cuenta.',
            "pending": [],
        }), 200

    try:
        responses = list_all_responses(creds, form_id)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"No se pudieron leer las respuestas del formulario: {exc}", "pending": []}), 200

    form = get_form(creds, form_id)
    qmap, order = question_map(form)

    already = graded_response_ids()
    pending = []
    for r in responses:
        rid = r.get("responseId")
        if not rid or rid in already:
            continue
        answers = r.get("answers", {})
        username = detect_username(qmap, order, answers)
        submitted = r.get("lastSubmittedTime") or r.get("createTime") or ""
        pending.append({"response_id": rid, "username": username, "submitted_at": submitted})

    pending.sort(key=lambda p: p["submitted_at"])
    return jsonify({"error": None, "pending": pending})


@app.route("/api/response/<response_id>")
@login_required
def api_response_detail(response_id):
    creds = request.google_creds
    form_id = find_form_id(creds, FORM_TITLE)
    if not form_id:
        return jsonify({"error": f'No se encontró el formulario "{FORM_TITLE}".'}), 404

    form = get_form(creds, form_id)
    qmap, order = question_map(form)

    try:
        responses = list_all_responses(creds, form_id)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500

    target = next((r for r in responses if r.get("responseId") == response_id), None)
    if not target:
        return jsonify({"error": "No se encontró esa respuesta (puede que ya no exista)."}), 404

    answers = target.get("answers", {})
    username = detect_username(qmap, order, answers)

    questions = []
    for qid in order:
        title = qmap.get(qid, "(pregunta sin título)")
        if USERNAME_HINT.search(title):
            continue  # no mostramos la pregunta de identificación como pregunta a corregir
        answer_text = extract_answer_text(answers.get(qid))
        questions.append({"id": qid, "title": title, "answer": answer_text})

    return jsonify({
        "error": None,
        "response_id": response_id,
        "username": username,
        "submitted_at": target.get("lastSubmittedTime") or target.get("createTime") or "",
        "questions": questions,
    })


@app.route("/api/grade", methods=["POST"])
@login_required
def api_grade():
    data = request.get_json(force=True, silent=True) or {}
    response_id = data.get("response_id")
    username = data.get("username", "")
    wrong_questions = data.get("wrong_questions", [])
    result = data.get("result")

    if not response_id or result not in ("pass", "fail"):
        return jsonify({"error": "Datos incompletos."}), 400

    message = build_message(result, username, wrong_questions)

    db = get_db()
    db.execute(
        """
        INSERT INTO ledger (response_id, username, result, wrong_questions, message, graded_at, archived)
        VALUES (?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(response_id) DO UPDATE SET
            username=excluded.username,
            result=excluded.result,
            wrong_questions=excluded.wrong_questions,
            message=excluded.message,
            graded_at=excluded.graded_at,
            archived=0
        """,
        (response_id, username, result, json.dumps(wrong_questions, ensure_ascii=False), message, now_iso()),
    )
    db.commit()
    db.close()

    return jsonify({"error": None, "message": message})


@app.route("/api/delete", methods=["POST"])
@login_required
def api_delete():
    data = request.get_json(force=True, silent=True) or {}
    response_id = data.get("response_id")
    username = data.get("username", "")
    if not response_id:
        return jsonify({"error": "Falta response_id."}), 400

    db = get_db()
    row = db.execute("SELECT response_id FROM ledger WHERE response_id = ?", (response_id,)).fetchone()
    if row:
        db.execute("UPDATE ledger SET archived = 1 WHERE response_id = ?", (response_id,))
    else:
        db.execute(
            "INSERT INTO ledger (response_id, username, result, wrong_questions, message, graded_at, archived) "
            "VALUES (?, ?, 'discarded', '[]', '', ?, 1)",
            (response_id, username, now_iso()),
        )
    db.commit()
    db.close()
    return jsonify({"error": None})


@app.route("/api/recent")
@login_required
def api_recent():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    db = get_db()
    rows = db.execute(
        "SELECT * FROM ledger WHERE archived = 0 AND result IN ('pass','fail') AND graded_at >= ? "
        "ORDER BY graded_at DESC",
        (cutoff,),
    ).fetchall()
    db.close()

    now = datetime.now(timezone.utc)
    items = []
    for r in rows:
        graded_at = parse_iso(r["graded_at"])
        minutes_elapsed = int((now - graded_at).total_seconds() // 60)
        minutes_left = max(0, 120 - minutes_elapsed)
        items.append({
            "response_id": r["response_id"],
            "username": r["username"],
            "result": r["result"],
            "wrong_questions": json.loads(r["wrong_questions"] or "[]"),
            "message": r["message"],
            "graded_at": r["graded_at"],
            "minutes_left": minutes_left,
        })
    return jsonify({"error": None, "recent": items})


# --------------------------------------------------------------------------
# Página principal (SPA de un solo archivo)
# --------------------------------------------------------------------------

INDEX_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SD EOT Exam — Corrector</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Share+Tech+Mono&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#05060a;
  --bg-panel:#0b0d15;
  --bg-panel-2:#0e111b;
  --cyan:#00f6ff;
  --magenta:#ff2ec4;
  --green:#39ff9e;
  --red:#ff3d5e;
  --amber:#ffcf4d;
  --text:#d9f7ff;
  --text-dim:#5f7c8a;
  --border:rgba(0,246,255,.35);
  --border-soft:rgba(0,246,255,.15);
}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;}
body{
  background:var(--bg);
  color:var(--text);
  font-family:'Share Tech Mono', monospace;
  min-height:100vh;
  overflow-x:hidden;
  position:relative;
}
h1,h2,h3,.display{
  font-family:'Orbitron', sans-serif;
  letter-spacing:.06em;
  text-transform:uppercase;
}
::selection{background:var(--magenta);color:#000;}

/* ---------- fondo ambiental ---------- */
.ambient{
  position:fixed; inset:0; z-index:0; pointer-events:none; overflow:hidden;
  background-image:
    linear-gradient(rgba(0,246,255,.05) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,246,255,.05) 1px, transparent 1px);
  background-size:42px 42px;
}
.blob{position:absolute; border-radius:50%; filter:blur(90px); opacity:.28;}
.blob.cyan{width:520px;height:520px; background:var(--cyan); top:-160px; left:-140px; animation:drift1 22s ease-in-out infinite;}
.blob.magenta{width:480px;height:480px; background:var(--magenta); bottom:-160px; right:-120px; animation:drift2 26s ease-in-out infinite;}
@keyframes drift1{0%,100%{transform:translate(0,0);}50%{transform:translate(60px,40px);}}
@keyframes drift2{0%,100%{transform:translate(0,0);}50%{transform:translate(-50px,-60px);}}
.scanlines{
  position:fixed; inset:0; z-index:1; pointer-events:none;
  background:repeating-linear-gradient(to bottom, rgba(0,246,255,.05) 0px, rgba(0,246,255,.05) 1px, transparent 1px, transparent 3px);
  animation:scan 9s linear infinite;
  opacity:.5;
}
@keyframes scan{0%{background-position-y:0;}100%{background-position-y:600px;}}

.cut{clip-path:polygon(16px 0,100% 0,100% calc(100% - 16px),calc(100% - 16px) 100%,0 100%,0 16px);}
.cut-sm{clip-path:polygon(9px 0,100% 0,100% calc(100% - 9px),calc(100% - 9px) 100%,0 100%,0 9px);}

/* ---------- layout ---------- */
.app{position:relative;z-index:2;max-width:1180px;margin:0 auto;padding:28px 20px 140px;}
header.top{
  display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;
  border-bottom:1px solid var(--border-soft); padding-bottom:16px; margin-bottom:26px;
}
header.top h1{font-size:1.5rem;color:var(--cyan);margin:0;text-shadow:0 0 14px rgba(0,246,255,.5);}
header.top .sub{color:var(--text-dim);font-size:.8rem;margin-top:4px;}
.acct{display:flex;align-items:center;gap:10px;font-size:.78rem;color:var(--text-dim);}
.acct button{background:none;border:1px solid var(--border-soft);color:var(--text-dim);padding:6px 12px;cursor:pointer;font-family:inherit;}
.acct button:hover{border-color:var(--red);color:var(--red);}

.grid{display:grid;grid-template-columns:320px 1fr;gap:22px;align-items:start;}
@media (max-width:860px){.grid{grid-template-columns:1fr;}}

.panel{background:var(--bg-panel);border:1px solid var(--border-soft);padding:18px;}
.panel h2{font-size:.9rem;color:var(--cyan);margin:0 0 14px;display:flex;justify-content:space-between;align-items:center;}
.panel h2 .count{color:var(--amber);}

/* ---------- lista pendientes ---------- */
.pending-item{
  background:var(--bg-panel-2);border:1px solid var(--border-soft);padding:12px 14px;margin-bottom:10px;
  cursor:pointer;transition:border-color .15s, transform .15s;
}
.pending-item:hover{border-color:var(--cyan);transform:translateX(2px);}
.pending-item.active{border-color:var(--magenta);box-shadow:0 0 18px rgba(255,46,196,.25);}
.pending-item .u{color:var(--text);font-weight:bold;font-size:.86rem;}
.pending-item .t{color:var(--text-dim);font-size:.68rem;margin-top:3px;}
.empty-note{color:var(--text-dim);font-size:.78rem;line-height:1.5;}

/* ---------- panel de corrección ---------- */
.grading-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:18px;flex-wrap:wrap;}
.grading-head .u{font-size:1.1rem;color:var(--magenta);text-shadow:0 0 10px rgba(255,46,196,.4);}
.grading-head .t{color:var(--text-dim);font-size:.72rem;margin-top:4px;}
.link-btn{background:none;border:none;color:var(--text-dim);text-decoration:underline;cursor:pointer;font-family:inherit;font-size:.72rem;}
.link-btn:hover{color:var(--red);}

.qrow{
  background:var(--bg-panel-2);border:1px solid var(--border-soft);padding:14px 16px;margin-bottom:10px;
  cursor:pointer;transition:.15s;position:relative;
}
.qrow .tag{font-size:.65rem;color:var(--text-dim);letter-spacing:.1em;}
.qrow .qt{font-size:.86rem;color:var(--text);margin:6px 0 8px;}
.qrow .qa{font-size:.82rem;color:var(--cyan);}
.qrow:hover{border-color:var(--cyan);}
.qrow.wrong{border-color:var(--red);background:linear-gradient(90deg, rgba(255,61,94,.12), transparent 60%);}
.qrow.wrong .qa{color:var(--red);text-decoration:line-through;text-decoration-color:rgba(255,61,94,.7);}
.qrow.wrong::after{
  content:'FALLADA'; position:absolute; top:10px; right:14px; color:var(--red);
  font-family:'Orbitron',sans-serif; font-size:.62rem; letter-spacing:.1em;
}
.hint{color:var(--text-dim);font-size:.7rem;margin-top:14px;}

.placeholder{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:260px;color:var(--text-dim);text-align:center;gap:10px;}
.placeholder svg{opacity:.4;}

/* ---------- recientes ---------- */
.recent-item{
  display:flex;justify-content:space-between;align-items:center;gap:10px;
  background:var(--bg-panel-2);border:1px solid var(--border-soft);padding:10px 14px;margin-bottom:8px;flex-wrap:wrap;
}
.recent-item .u{font-size:.82rem;}
.recent-item .meta{font-size:.66rem;color:var(--text-dim);margin-top:2px;}
.badge{font-family:'Orbitron',sans-serif;font-size:.6rem;letter-spacing:.08em;padding:3px 8px;}
.badge.pass{color:var(--green);border:1px solid var(--green);}
.badge.fail{color:var(--red);border:1px solid var(--red);}
.iconbtn{background:none;border:1px solid var(--border-soft);color:var(--text-dim);padding:5px 8px;cursor:pointer;display:inline-flex;}
.iconbtn:hover{border-color:var(--magenta);color:var(--magenta);}

/* ---------- botón flotante ---------- */
.fab{
  position:fixed;right:26px;bottom:26px;z-index:20;width:66px;height:66px;border-radius:50%;
  background:radial-gradient(circle at 35% 30%, #10151f, #05060a 70%);
  border:2px solid var(--cyan);color:var(--cyan);display:flex;align-items:center;justify-content:center;
  cursor:pointer;box-shadow:0 0 18px rgba(0,246,255,.45), inset 0 0 12px rgba(0,246,255,.15);
  animation:pulse 2.4s ease-in-out infinite;
  transition:opacity .2s, transform .15s;
}
.fab:hover{transform:scale(1.06);}
.fab[disabled]{opacity:.3;pointer-events:none;animation:none;}
@keyframes pulse{0%,100%{box-shadow:0 0 12px rgba(0,246,255,.35), inset 0 0 10px rgba(0,246,255,.12);}50%{box-shadow:0 0 26px rgba(0,246,255,.65), inset 0 0 14px rgba(0,246,255,.25);}}
.fab-label{
  position:fixed;right:26px;bottom:98px;z-index:20;color:var(--text-dim);font-size:.65rem;
  letter-spacing:.08em;text-align:right;
}

/* ---------- modales ---------- */
.overlay{position:fixed;inset:0;background:rgba(2,3,6,.78);backdrop-filter:blur(3px);display:none;align-items:center;justify-content:center;z-index:50;padding:20px;}
.overlay.show{display:flex;}
.modal{
  background:var(--bg-panel);border:1px solid var(--cyan);max-width:440px;width:100%;padding:26px;
  box-shadow:0 0 40px rgba(0,246,255,.2);
  animation:pop .18s ease-out;
}
@keyframes pop{from{opacity:0;transform:scale(.94);}to{opacity:1;transform:scale(1);}}
.modal h3{color:var(--cyan);margin:0 0 12px;font-size:1rem;}
.modal p{color:var(--text-dim);font-size:.82rem;line-height:1.5;}
.modal .row{display:flex;gap:10px;margin-top:20px;flex-wrap:wrap;}
.btn{
  font-family:'Orbitron',sans-serif;font-size:.72rem;letter-spacing:.06em;text-transform:uppercase;
  padding:11px 18px;border:1px solid var(--border);background:transparent;color:var(--text);cursor:pointer;flex:1;
  transition:.15s;
}
.btn:hover{background:rgba(0,246,255,.08);}
.btn.ghost{border-color:var(--border-soft);color:var(--text-dim);}
.btn.green{border-color:var(--green);color:var(--green);}
.btn.green:hover{background:rgba(57,255,158,.1);}
.btn.red{border-color:var(--red);color:var(--red);}
.btn.red:hover{background:rgba(255,61,94,.1);}
.msgbox{
  width:100%;min-height:220px;background:#03040a;border:1px solid var(--border-soft);color:var(--text);
  font-family:'Share Tech Mono',monospace;font-size:.76rem;padding:12px;resize:vertical;
}

.toast{
  position:fixed;left:26px;bottom:26px;z-index:60;background:var(--bg-panel);border:1px solid var(--cyan);
  color:var(--cyan);padding:10px 16px;font-size:.75rem;opacity:0;transform:translateY(8px);
  transition:.25s;pointer-events:none;
}
.toast.show{opacity:1;transform:translateY(0);}

/* ---------- login ---------- */
.login-wrap{position:relative;z-index:2;min-height:100vh;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:18px;text-align:center;padding:20px;}
.login-wrap h1{font-size:1.7rem;color:var(--cyan);text-shadow:0 0 16px rgba(0,246,255,.5);}
.login-wrap p{color:var(--text-dim);max-width:380px;font-size:.85rem;}
.login-wrap a.btn{display:inline-block;text-decoration:none;border-color:var(--magenta);color:var(--magenta);padding:13px 26px;}
.login-wrap a.btn:hover{background:rgba(255,46,196,.1);}
</style>
</head>
<body>
<div class="ambient"><div class="blob cyan"></div><div class="blob magenta"></div></div>
<div class="scanlines"></div>

<div id="app" class="app" style="display:none;">
  <header class="top">
    <div>
      <h1>SD EOT Exam — Corrector</h1>
      <div class="sub">Corrección de intentos &middot; SD | Company</div>
    </div>
    <div class="acct">
      <span id="acct-email">&nbsp;</span>
      <button id="logout-btn">Cerrar sesión</button>
    </div>
  </header>

  <div class="grid">
    <div class="panel cut">
      <h2>Pendientes <span class="count" id="pending-count">0</span></h2>
      <div id="pending-list"></div>
      <div id="pending-empty" class="empty-note" style="display:none;">No hay exámenes pendientes por corregir ahora mismo.</div>
      <div id="pending-error" class="empty-note" style="display:none;color:var(--red);"></div>
    </div>

    <div class="panel cut" id="grading-panel">
      <div id="placeholder" class="placeholder">
        <svg width="46" height="46" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M4 4h16v16H4z"/><path d="M8 9h8M8 13h5"/></svg>
        <div>Selecciona un examen pendiente para empezar a corregirlo.</div>
      </div>

      <div id="grading-body" style="display:none;">
        <div class="grading-head">
          <div>
            <div class="u" id="g-username"></div>
            <div class="t" id="g-submitted"></div>
          </div>
          <button class="link-btn" id="discard-btn">Eliminar sin corregir</button>
        </div>
        <div id="questions"></div>
        <div class="hint">Toca una pregunta para marcarla como fallada. Las que no toques quedan como correctas.</div>
      </div>
    </div>
  </div>

  <div class="panel cut" style="margin-top:22px;">
    <h2>Corregidos recientemente <span class="count" id="recent-count">0</span></h2>
    <div id="recent-list"></div>
    <div id="recent-empty" class="empty-note" style="display:none;">Aún no has corregido ningún examen en las últimas 2 horas.</div>
  </div>
</div>

<div id="login-view" class="login-wrap" style="display:none;">
  <h1>SD EOT Exam — Corrector</h1>
  <p>Inicia sesión con la cuenta de Google autorizada para buscar el formulario y corregir los intentos pendientes.</p>
  <a class="btn" href="/login">Iniciar sesión con Google</a>
</div>

<button class="fab" id="fab" disabled title="Finalizar corrección">
  <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>
</button>
<div class="fab-label">FINALIZAR<br>CORRECCIÓN</div>

<!-- modal: confirmar fin -->
<div class="overlay" id="ov-confirm">
  <div class="modal cut-sm">
    <h3>¿Terminaste de corregir?</h3>
    <p>Las preguntas que no marcaste se consideran correctas. Esto guardará los cambios de este examen.</p>
    <div class="row">
      <button class="btn ghost" data-close="ov-confirm">Cancelar</button>
      <button class="btn" id="confirm-yes">Sí, finalizar</button>
    </div>
  </div>
</div>

<!-- modal: aprobado / suspendido -->
<div class="overlay" id="ov-result">
  <div class="modal cut-sm">
    <h3>¿Aprobó o suspendió?</h3>
    <p>Se generará el mensaje correspondiente para enviárselo.</p>
    <div class="row">
      <button class="btn red" id="result-fail">✕ Suspendido</button>
      <button class="btn green" id="result-pass">✓ Aprobado</button>
    </div>
  </div>
</div>

<!-- modal: mensaje generado -->
<div class="overlay" id="ov-message">
  <div class="modal cut-sm" style="max-width:560px;">
    <h3 id="msg-title">Mensaje generado</h3>
    <textarea class="msgbox" id="msg-text" readonly></textarea>
    <div class="row">
      <button class="btn ghost" id="msg-copy">Copiar</button>
      <button class="btn ghost" id="msg-download">Descargar .txt</button>
      <button class="btn" data-close="ov-message">Cerrar</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let currentResponse = null;   // {response_id, username, questions:[{id,title,answer}]}
let wrongIds = new Set();
let pendingCache = [];

function $(sel){return document.querySelector(sel);}
function el(html){const t=document.createElement('template');t.innerHTML=html.trim();return t.content.firstChild;}

function toast(msg){
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), 2200);
}

function openModal(id){ $('#'+id).classList.add('show'); }
function closeModal(id){ $('#'+id).classList.remove('show'); }
document.querySelectorAll('[data-close]').forEach(b=>{
  b.addEventListener('click', ()=>closeModal(b.dataset.close));
});

async function apiGet(url){
  const r = await fetch(url);
  if(r.status === 401){ window.location.href = '/login'; throw new Error('auth'); }
  return r.json();
}
async function apiPost(url, body){
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body||{})});
  if(r.status === 401){ window.location.href = '/login'; throw new Error('auth'); }
  return r.json();
}

function timeAgo(iso){
  if(!iso) return '';
  const d = new Date(iso);
  const mins = Math.max(0, Math.round((Date.now()-d.getTime())/60000));
  if(mins < 1) return 'justo ahora';
  if(mins < 60) return `hace ${mins} min`;
  const h = Math.floor(mins/60);
  return `hace ${h}h ${mins%60}min`;
}

async function loadPending(){
  const data = await apiGet('/api/pending');
  const list = $('#pending-list');
  const errBox = $('#pending-error');
  const emptyBox = $('#pending-empty');
  list.innerHTML = '';
  if(data.error){
    errBox.style.display='block'; errBox.textContent = data.error;
    emptyBox.style.display='none';
  } else {
    errBox.style.display='none';
  }
  pendingCache = data.pending || [];
  $('#pending-count').textContent = pendingCache.length;
  emptyBox.style.display = (!data.error && pendingCache.length===0) ? 'block':'none';
  pendingCache.forEach(p=>{
    const active = currentResponse && currentResponse.response_id===p.response_id;
    const item = el(`<div class="pending-item ${active?'active':''}">
        <div class="u"></div>
        <div class="t"></div>
      </div>`);
    item.querySelector('.u').textContent = p.username;
    item.querySelector('.t').textContent = timeAgo(p.submitted_at);
    item.addEventListener('click', ()=>selectResponse(p.response_id));
    list.appendChild(item);
  });
}

async function selectResponse(rid){
  const data = await apiGet('/api/response/'+encodeURIComponent(rid));
  if(data.error){ toast(data.error); return; }
  currentResponse = data;
  wrongIds = new Set();
  renderGrading();
  loadPending();
  $('#fab').removeAttribute('disabled');
}

function renderGrading(){
  $('#placeholder').style.display='none';
  $('#grading-body').style.display='block';
  $('#g-username').textContent = currentResponse.username;
  $('#g-submitted').textContent = 'Enviado ' + timeAgo(currentResponse.submitted_at);
  const box = $('#questions');
  box.innerHTML='';
  currentResponse.questions.forEach((q, idx)=>{
    const row = el(`<div class="qrow" data-id="${q.id}">
        <div class="tag">PREGUNTA ${String(idx+1).padStart(2,'0')}</div>
        <div class="qt"></div>
        <div class="qa"></div>
      </div>`);
    row.querySelector('.qt').textContent = q.title;
    row.querySelector('.qa').textContent = q.answer;
    row.addEventListener('click', ()=>{
      if(wrongIds.has(q.id)){ wrongIds.delete(q.id); row.classList.remove('wrong'); }
      else { wrongIds.add(q.id); row.classList.add('wrong'); }
    });
    box.appendChild(row);
  });
}

function resetGradingPanel(){
  currentResponse = null;
  wrongIds = new Set();
  $('#grading-body').style.display='none';
  $('#placeholder').style.display='flex';
  $('#fab').setAttribute('disabled','disabled');
}

$('#fab').addEventListener('click', ()=>{
  if(!currentResponse) return;
  openModal('ov-confirm');
});

$('#confirm-yes').addEventListener('click', ()=>{
  closeModal('ov-confirm');
  openModal('ov-result');
});

async function submitGrade(result){
  closeModal('ov-result');
  const wrongTitles = currentResponse.questions
    .filter(q=>wrongIds.has(q.id))
    .map(q=>q.title);
  const data = await apiPost('/api/grade', {
    response_id: currentResponse.response_id,
    username: currentResponse.username,
    wrong_questions: wrongTitles,
    result
  });
  if(data.error){ toast(data.error); return; }
  $('#msg-title').textContent = result==='fail' ? 'Suspendido — mensaje generado' : 'Aprobado — mensaje generado';
  $('#msg-text').value = data.message;
  openModal('ov-message');
  resetGradingPanel();
  loadPending();
  loadRecent();
}
$('#result-fail').addEventListener('click', ()=>submitGrade('fail'));
$('#result-pass').addEventListener('click', ()=>submitGrade('pass'));

$('#msg-copy').addEventListener('click', async ()=>{
  await navigator.clipboard.writeText($('#msg-text').value);
  toast('Mensaje copiado al portapapeles');
});
$('#msg-download').addEventListener('click', ()=>{
  const result = $('#msg-title').textContent.includes('Suspendido') ? 'formatfailed' : 'formatpassed';
  const blob = new Blob([$('#msg-text').value], {type:'text/plain'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = result + '.txt';
  a.click();
});

$('#discard-btn').addEventListener('click', async ()=>{
  if(!currentResponse) return;
  if(!confirm('¿Eliminar este examen sin corregirlo? No se podrá corregir más adelante.')) return;
  await apiPost('/api/delete', {response_id: currentResponse.response_id, username: currentResponse.username});
  resetGradingPanel();
  loadPending();
});

async function loadRecent(){
  const data = await apiGet('/api/recent');
  const list = $('#recent-list');
  list.innerHTML='';
  const items = data.recent || [];
  $('#recent-count').textContent = items.length;
  $('#recent-empty').style.display = items.length===0 ? 'block':'none';
  items.forEach(r=>{
    const row = el(`<div class="recent-item">
        <div>
          <div class="u"></div>
          <div class="meta"></div>
        </div>
        <div style="display:flex;align-items:center;gap:8px;">
          <span class="badge ${r.result==='pass'?'pass':'fail'}">${r.result==='pass'?'APROBADO':'SUSPENDIDO'}</span>
          <button class="iconbtn" data-act="view" title="Ver mensaje">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg>
          </button>
          <button class="iconbtn" data-act="del" title="Eliminar">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>
          </button>
        </div>
      </div>`);
    row.querySelector('.u').textContent = r.username;
    row.querySelector('.meta').textContent = `${timeAgo(r.graded_at)} · se oculta en ${Math.floor(r.minutes_left/60)}h ${r.minutes_left%60}min`;
    row.querySelector('[data-act="view"]').addEventListener('click', ()=>{
      $('#msg-title').textContent = r.result==='fail' ? 'Suspendido — mensaje generado' : 'Aprobado — mensaje generado';
      $('#msg-text').value = r.message;
      openModal('ov-message');
    });
    row.querySelector('[data-act="del"]').addEventListener('click', async ()=>{
      await apiPost('/api/delete', {response_id: r.response_id});
      loadRecent();
    });
    list.appendChild(row);
  });
}

$('#logout-btn').addEventListener('click', ()=>{ window.location.href='/logout'; });

async function init(){
  try{
    const me = await apiGet('/api/me');
    $('#app').style.display='block';
    $('#login-view').style.display='none';
    $('#acct-email').textContent = me.email;
    await loadPending();
    await loadRecent();
    setInterval(loadPending, 45000);
    setInterval(loadRecent, 30000);
  }catch(e){
    $('#app').style.display='none';
    $('#login-view').style.display='flex';
  }
}
init();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    sid = request.cookies.get("sid")
    if not sid or sid not in SESSIONS:
        return INDEX_HTML  # el JS detecta 401 en /api/me y muestra la vista de login
    return INDEX_HTML


if __name__ == "__main__":
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not ADMIN_EMAIL:
        print("⚠  Faltan GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / ADMIN_EMAIL.")
        print("   Crea un archivo .env (mira .env.example) o expórtalas antes de arrancar.")
    print(f"➡  Escuchando en http://{HOST}:{PORT}")
    print(f"   Redirect URI configurada: {GOOGLE_REDIRECT_URI}")
    # Nota: en Render, el proceso lo arranca gunicorn (ver Procfile), no esta rama.
    app.run(host=HOST, port=PORT, debug=False)
