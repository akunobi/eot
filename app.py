"""
SD EOT Exam — Grader
------------------------
Single-file Flask app, meant to run LOCALLY (on your own PC),
not deployed to any cloud service. Grades the responses to the Google
"SD EOT Exam" form: flags wrong questions, decides Pass/Fail, and
generates the final message from the formatpassed.txt / formatfailed.txt
templates.

Setup: put your credentials in a ".env" file next to this file
(there's a template in .env.example) or export them as environment
variables. See README.md for the step-by-step guide.
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
# Configuration — everything designed for localhost
# --------------------------------------------------------------------------

# Render automatically sets RENDER_EXTERNAL_URL to the service's public
# URL (e.g. "https://sd-eot-exam.onrender.com"). If it's set, we use it
# to build the default redirect URI instead of localhost.
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
IS_RENDER = bool(os.environ.get("RENDER")) or bool(RENDER_EXTERNAL_URL)
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
_default_redirect_uri = (
    "https://eot.devs.surf/oauth2callback" if IS_RENDER else "http://localhost:5000/oauth2callback"
)
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", _default_redirect_uri)
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")
FORM_TITLE = os.environ.get("FORM_TITLE", "SD EOT Exam")
# Render injects PORT automatically and expects the process to listen on 0.0.0.0.
HOST = os.environ.get("HOST", "0.0.0.0" if IS_RENDER else "127.0.0.1")
PORT = int(os.environ.get("PORT", os.environ.get("LOCAL_PORT", "5000")))

# The OAuth flow requires HTTPS unless explicitly told otherwise.
# We only relax this for local development over http://; on Render the
# external URL is already https, so this branch shouldn't trigger there.
if GOOGLE_REDIRECT_URI.startswith("http://"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/forms.body",
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

# On Render, the filesystem is ephemeral unless you attach a "Persistent
# Disk" (requires a paid plan) and mount DB_PATH inside it, e.g.
# DB_PATH=/var/data/eot_ledger.db with the disk mounted at /var/data.
# Without a persistent disk, this file resets on every redeploy and every
# time the free instance "sleeps" and spins back up.
DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "eot_ledger.db"),
)
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
# Render (and any PaaS with a proxy in front) terminates TLS and forwards
# over internal HTTP, adding X-Forwarded-* headers. With ProxyFix, Flask
# knows the original request was https and request.is_secure behaves
# correctly (important for the "secure" cookies below).
try:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
except ImportError:
    pass

# In-process in-memory sessions: sid (cookie) -> {"credentials": Credentials, "email": str}
# Deliberately simple: this app is used by a single person.
SESSIONS = {}


# --------------------------------------------------------------------------
# Database (ledger of already-processed exams)
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
            wrong_questions TEXT,      -- JSON list of question titles
            message         TEXT,      -- final generated text
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
# Message templates (identical to formatfailed.txt / formatpassed.txt,
# with {username} and {bullets} as placeholders)
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
    username = username or "(unknown user)"
    if wrong_questions:
        bullets = "\n".join(f"- {q}" for q in wrong_questions)
    else:
        bullets = "- (No question was marked as wrong)"
    template = FAIL_TEMPLATE if result == "fail" else PASS_TEMPLATE
    return template.replace("{username}", username).replace("{bullets}", bullets)


# --------------------------------------------------------------------------
# Authentication
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
        return "Missing GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET environment variables.", 500
    flow = build_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="select_account consent",
    )
    # PKCE: the verifier is generated by this Flow instance and needs to
    # be reused in /oauth2callback (which creates another instance), so it
    # travels in a short-lived cookie alongside the "state".
    resp = make_response(redirect(auth_url))
    resp.set_cookie("oauth_state", state, httponly=True, samesite="Lax", secure=request.is_secure, max_age=600)
    resp.set_cookie("oauth_cv", flow.code_verifier, httponly=True, samesite="Lax", secure=request.is_secure, max_age=600)
    return resp


@app.route("/oauth2callback")
def oauth2callback():
    expected_state = request.cookies.get("oauth_state")
    code_verifier = request.cookies.get("oauth_cv")
    if not expected_state or request.args.get("state") != expected_state:
        return "Invalid OAuth state, please try again from /login.", 400
    if not code_verifier:
        return "Missing PKCE verifier (cookie expired), please try again from /login.", 400

    flow = build_flow(code_verifier=code_verifier)
    try:
        flow.fetch_token(authorization_response=request.url)
    except Exception as exc:  # noqa: BLE001
        return f"Could not complete sign-in: {exc}", 400

    creds = flow.credentials
    try:
        oauth2_service = build("oauth2", "v2", credentials=creds)
        userinfo = oauth2_service.userinfo().get().execute()
        email = userinfo.get("email", "")
    except Exception as exc:  # noqa: BLE001
        return f"Could not verify the Google account: {exc}", 400

    if ADMIN_EMAIL and email.lower() != ADMIN_EMAIL.lower():
        return f"The account {email} is not authorized to use this tool.", 403

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
# Google Forms / Drive helpers
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
    """Returns (dict questionId -> title, ordered list of questionId,
    dict questionId -> {"type", "options", "correct"} for choice questions)."""
    qmap = {}
    order = []
    qmeta = {}
    for item in form.get("items", []):
        qi = item.get("questionItem")
        if not qi:
            continue
        q = qi.get("question", {})
        qid = q.get("questionId")
        if not qid:
            continue
        title = item.get("title") or "(untitled question)"
        qmap[qid] = title
        order.append(qid)

        meta = {"type": None, "options": [], "correct": None, "points": None}
        choice = q.get("choiceQuestion")
        if choice:
            meta["type"] = choice.get("type")  # RADIO | CHECKBOX | DROP_DOWN
            meta["options"] = [o.get("value", "") for o in choice.get("options", []) if o.get("value")]
        grading = q.get("grading") or {}
        correct_answers = grading.get("correctAnswers")
        if correct_answers:
            meta["correct"] = [a.get("value", "") for a in correct_answers.get("answers", []) if a.get("value")]
        if "pointValue" in grading:
            meta["points"] = grading.get("pointValue")
        qmeta[qid] = meta
    return qmap, order, qmeta


def find_item_by_question_id(form, question_id):
    """Returns (item_index, item_dict) for the item holding this questionId, or (None, None)."""
    for idx, item in enumerate(form.get("items", [])):
        qi = item.get("questionItem")
        if qi and qi.get("question", {}).get("questionId") == question_id:
            return idx, item
    return None, None


def extract_answer_text(answer):
    if not answer:
        return "(no answer)"
    text_answers = answer.get("textAnswers")
    if text_answers:
        values = [a.get("value", "") for a in text_answers.get("answers", [])]
        joined = ", ".join(v for v in values if v)
        return joined or "(no answer)"
    return "(unsupported answer type)"


def extract_answer_list(answer):
    """Raw list of selected values (unjoined), used to match against form options."""
    if not answer:
        return []
    text_answers = answer.get("textAnswers")
    if text_answers:
        return [a.get("value", "") for a in text_answers.get("answers", []) if a.get("value")]
    return []


USERNAME_HINT = re.compile(r"usuario|username|discord|roblox", re.IGNORECASE)


def detect_username(qmap, order, answers):
    for qid in order:
        title = qmap.get(qid, "")
        if USERNAME_HINT.search(title) and qid in answers:
            val = extract_answer_text(answers[qid])
            if val and val != "(no answer)":
                return val
    for qid in order:
        if qid in answers:
            val = extract_answer_text(answers[qid])
            if val and val != "(no answer)":
                return val
    return "(unknown user)"


def graded_response_ids():
    db = get_db()
    rows = db.execute("SELECT response_id FROM ledger").fetchall()
    db.close()
    return {r["response_id"] for r in rows}


# --------------------------------------------------------------------------
# API: pending exams / detail / grade / delete / recent
# --------------------------------------------------------------------------

@app.route("/api/pending")
@login_required
def api_pending():
    creds = request.google_creds
    form_id = find_form_id(creds, FORM_TITLE)
    if not form_id:
        return jsonify({
            "error": f'No Google Form named "{FORM_TITLE}" was found that this account has access to.',
            "pending": [],
        }), 200

    try:
        responses = list_all_responses(creds, form_id)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Could not read the form's responses: {exc}", "pending": []}), 200

    form = get_form(creds, form_id)
    qmap, order, _qmeta = question_map(form)

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
        return jsonify({"error": f'Form "{FORM_TITLE}" not found.'}), 404

    form = get_form(creds, form_id)
    qmap, order, qmeta = question_map(form)

    try:
        responses = list_all_responses(creds, form_id)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500

    target = next((r for r in responses if r.get("responseId") == response_id), None)
    if not target:
        return jsonify({"error": "That response was not found (it may no longer exist)."}), 404

    answers = target.get("answers", {})
    username = detect_username(qmap, order, answers)

    questions = []
    for qid in order:
        title = qmap.get(qid, "(untitled question)")
        if USERNAME_HINT.search(title):
            continue  # don't show the identification question as a question to grade
        raw_answer = answers.get(qid)
        answer_text = extract_answer_text(raw_answer)
        meta = qmeta.get(qid, {})
        questions.append({
            "id": qid,
            "title": title,
            "answer": answer_text,
            "type": meta.get("type"),          # RADIO | CHECKBOX | DROP_DOWN | None
            "options": meta.get("options") or [],  # all options as they appear in the form
            "correct": meta.get("correct"),    # list of correct values, or None if not exposed
            "selected": extract_answer_list(raw_answer),  # raw selected values
            "points": meta.get("points"),      # current point value in the form, or None
        })

    return jsonify({
        "error": None,
        "response_id": response_id,
        "username": username,
        "submitted_at": target.get("lastSubmittedTime") or target.get("createTime") or "",
        "questions": questions,
    })


@app.route("/api/set_points", methods=["POST"])
@login_required
def api_set_points():
    data = request.get_json(force=True, silent=True) or {}
    question_id = data.get("question_id")
    points = data.get("points")
    if not question_id or points is None:
        return jsonify({"error": "Missing question_id or points."}), 400
    try:
        points = int(points)
    except (TypeError, ValueError):
        return jsonify({"error": "Points must be a whole number."}), 400
    if points < 0:
        return jsonify({"error": "Points can't be negative."}), 400

    creds = request.google_creds
    form_id = find_form_id(creds, FORM_TITLE)
    if not form_id:
        return jsonify({"error": f'Form "{FORM_TITLE}" not found.'}), 404

    form = get_form(creds, form_id)
    idx, item = find_item_by_question_id(form, question_id)
    if item is None:
        return jsonify({"error": "That question was not found in the form."}), 404

    # Keep any existing grading fields (correctAnswers, whenRight/whenWrong)
    # and only change the point value.
    grading = dict(item["questionItem"]["question"].get("grading") or {})
    grading["pointValue"] = points

    update_body = {
        "requests": [
            {
                "updateItem": {
                    "item": {
                        "questionItem": {
                            "question": {
                                "questionId": question_id,
                                "grading": grading,
                            }
                        }
                    },
                    "location": {"index": idx},
                    "updateMask": "questionItem.question.grading",
                }
            }
        ]
    }

    try:
        forms = build("forms", "v1", credentials=creds)
        forms.forms().batchUpdate(formId=form_id, body=update_body).execute()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Could not update the form: {exc}"}), 500

    return jsonify({"error": None, "points": points})


@app.route("/api/grade", methods=["POST"])
@login_required
def api_grade():
    data = request.get_json(force=True, silent=True) or {}
    response_id = data.get("response_id")
    username = data.get("username", "")
    wrong_questions = data.get("wrong_questions", [])
    result = data.get("result")

    if not response_id or result not in ("pass", "fail"):
        return jsonify({"error": "Incomplete data."}), 400

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
        return jsonify({"error": "Missing response_id."}), 400

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
# Main page (single-file SPA)
# --------------------------------------------------------------------------

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" id="theme-color-meta" content="#f7f8fa">
<title>SD EOT Exam — Grader</title>
<script>
// Applied before first paint to avoid a light/dark flash on load.
(function(){
  try {
    var stored = localStorage.getItem('eot-theme');
    var theme = stored || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', theme);
  } catch (e) {}
})();
</script>
<style>
:root{
  --bg:#f7f8fa;
  --card:#ffffff;
  --border:#e3e6ea;
  --text:#1f2430;
  --text-dim:#707685;
  --blue:#2563eb;
  --blue-dim:#eaf0fe;
  --green:#1a9c56;
  --green-dim:#e9f8ef;
  --red:#d63b3b;
  --red-dim:#fdecec;
  --amber:#b6650a;
  --toast-bg:#1f2430;
  --toast-text:#ffffff;
  --wrong-answer-bg:#ffffff;
  --radius:10px;
}
[data-theme="dark"]{
  --bg:#12151c;
  --card:#1a1f2b;
  --border:#2b3140;
  --text:#e7e9ee;
  --text-dim:#96a0b3;
  --blue:#5b93ff;
  --blue-dim:#1c2a47;
  --green:#3ddc84;
  --green-dim:#123524;
  --red:#ff6b6b;
  --red-dim:#3a1a1a;
  --amber:#e0a458;
  --toast-bg:#e7e9ee;
  --toast-text:#12151c;
  --wrong-answer-bg:#1a1f2b;
}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;}
body{
  background:var(--bg);
  color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  font-size:15px;
  line-height:1.5;
  min-height:100vh;
  transition:background-color .15s ease, color .15s ease;
}
h1,h2,h3{margin:0;font-weight:600;}
button,textarea{font-family:inherit;}
:focus-visible{outline:2px solid var(--blue);outline-offset:2px;}
@media (prefers-reduced-motion: reduce){*{animation:none!important;transition:none!important;}}

/* ---------- theme toggle ---------- */
.theme-toggle{
  position:fixed;top:18px;right:20px;z-index:70;
  width:38px;height:38px;border-radius:50%;
  background:var(--card);border:1px solid var(--border);color:var(--text);
  display:flex;align-items:center;justify-content:center;cursor:pointer;
  transition:background-color .15s, border-color .15s, color .15s;
}
.theme-toggle:hover{border-color:var(--blue);color:var(--blue);}
.theme-toggle .icon-moon{display:none;}
[data-theme="dark"] .theme-toggle .icon-sun{display:none;}
[data-theme="dark"] .theme-toggle .icon-moon{display:block;}

/* ---------- layout ---------- */
.app{max-width:1040px;margin:0 auto;padding:32px 20px 120px;}
header.top{
  display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;
  margin-bottom:28px;
}
header.top h1{font-size:1.35rem;}
header.top .sub{color:var(--text-dim);font-size:.9rem;margin-top:4px;}
.acct{display:flex;align-items:center;gap:12px;font-size:.85rem;color:var(--text-dim);}
.acct button{
  background:var(--card);border:1px solid var(--border);color:var(--text);border-radius:8px;
  padding:7px 14px;cursor:pointer;
}
.acct button:hover{border-color:var(--red);color:var(--red);}

.grid{display:grid;grid-template-columns:300px 1fr;gap:20px;align-items:start;}
@media (max-width:820px){.grid{grid-template-columns:1fr;}}

.panel{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:20px;transition:background-color .15s, border-color .15s;}
.panel h2{font-size:1rem;margin:0 0 14px;display:flex;justify-content:space-between;align-items:center;}
.panel h2 .count{
  color:var(--text-dim);font-weight:500;font-size:.85rem;background:var(--bg);
  border-radius:20px;padding:2px 10px;
}

/* ---------- pending list ---------- */
.pending-item{
  border:1px solid var(--border);border-radius:8px;padding:12px 14px;margin-bottom:8px;
  cursor:pointer;transition:border-color .15s, background .15s;
}
.pending-item:hover{border-color:var(--blue);background:var(--blue-dim);}
.pending-item.active{border-color:var(--blue);background:var(--blue-dim);}
.pending-item .u{color:var(--text);font-weight:600;}
.pending-item .t{color:var(--text-dim);font-size:.82rem;margin-top:2px;}
.empty-note{color:var(--text-dim);font-size:.9rem;line-height:1.5;}

/* ---------- grading panel ---------- */
.grading-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:18px;flex-wrap:wrap;}
.grading-head .u{font-size:1.15rem;font-weight:600;}
.grading-head .t{color:var(--text-dim);font-size:.85rem;margin-top:3px;}
.link-btn{background:none;border:none;color:var(--text-dim);text-decoration:underline;cursor:pointer;font-size:.85rem;padding:0;}
.link-btn:hover{color:var(--red);}

.qrow{
  border:1px solid var(--border);border-radius:8px;padding:14px 16px;margin-bottom:10px;
  cursor:pointer;transition:.12s;
}
.qrow:hover{border-color:var(--blue);}
.qrow.wrong{border-color:var(--red);background:var(--red-dim);}

.qrow-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px;}
.qrow .tag{font-size:.76rem;color:var(--text-dim);font-weight:700;text-transform:uppercase;letter-spacing:.03em;}
.qrow .wrong-flag{display:none;font-size:.8rem;font-weight:600;color:var(--red);flex:none;}
.qrow.wrong .wrong-flag{display:inline;}
.points-edit{display:flex;align-items:center;gap:4px;}
.points-input{width:48px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:3px 6px;font-size:.8rem;}
.points-save{padding:3px 6px;}

.qsection{margin-bottom:10px;}
.qsection:last-child{margin-bottom:0;}
.qsection-label{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--text-dim);margin-bottom:4px;}
.qsection-label-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px;}
.qsection-label-row .qsection-label{margin-bottom:0;}
.view-form-btn{padding:3px 7px;}
.fv-options{display:flex;flex-direction:column;gap:7px;margin:14px 0;}
.fv-opt{display:flex;align-items:center;gap:10px;padding:9px 11px;border:1px solid var(--border);border-radius:8px;font-size:.9rem;}
.fv-opt .fv-mark{width:16px;flex:none;text-align:center;color:var(--text-dim);}
.fv-selected{border-color:var(--text-dim);}
.fv-right{border-color:var(--green);background:var(--green-dim);color:var(--green);}
.fv-right .fv-mark{color:var(--green);}
.fv-wrong{border-color:var(--red);background:var(--red-dim);color:var(--red);}
.fv-wrong .fv-mark{color:var(--red);}
.fv-missed{border-color:var(--green);border-style:dashed;color:var(--green);}
.fv-note{color:var(--text-dim);font-size:.8rem;margin-top:2px;}
.qsection-body{font-size:.95rem;line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere;}
.q-question .qsection-body{color:var(--text);}
.q-answer{padding:9px 12px;background:var(--bg);border-left:3px solid var(--border);border-radius:0 6px 6px 0;}
.q-answer .qsection-body{color:var(--text-dim);}
.qrow.wrong .q-answer{border-left-color:var(--red);background:var(--wrong-answer-bg);}
.qrow.wrong .q-answer .qsection-body{color:var(--red);text-decoration:line-through;}
.hint{color:var(--text-dim);font-size:.85rem;margin-top:14px;}

.placeholder{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:240px;color:var(--text-dim);text-align:center;gap:10px;}
.placeholder svg{opacity:.5;}

/* ---------- recent ---------- */
.recent-item{
  display:flex;justify-content:space-between;align-items:center;gap:10px;
  border:1px solid var(--border);border-radius:8px;padding:10px 14px;margin-bottom:8px;flex-wrap:wrap;
}
.recent-item .u{font-weight:600;}
.recent-item .meta{font-size:.8rem;color:var(--text-dim);margin-top:2px;}
.badge{font-size:.78rem;font-weight:600;padding:4px 10px;border-radius:20px;}
.badge.pass{color:var(--green);background:var(--green-dim);}
.badge.fail{color:var(--red);background:var(--red-dim);}
.iconbtn{background:none;border:1px solid var(--border);border-radius:8px;color:var(--text-dim);padding:6px 9px;cursor:pointer;display:inline-flex;}
.iconbtn:hover{border-color:var(--blue);color:var(--blue);}

/* ---------- finish button ---------- */
.fab{
  position:fixed;right:24px;bottom:24px;z-index:20;
  display:flex;align-items:center;gap:8px;
  background:var(--blue);color:#fff;border:none;border-radius:30px;
  padding:14px 22px;font-size:.95rem;font-weight:600;cursor:pointer;
  box-shadow:0 4px 14px rgba(37,99,235,.35);
  transition:opacity .15s, transform .15s, background .15s;
}
.fab:hover{background:#1d4fd1;}
.fab[disabled]{opacity:.35;pointer-events:none;box-shadow:none;}

/* ---------- reference doc floating tab ---------- */
.doc-tab{
  position:fixed;top:50%;right:0;z-index:40;
  transform:translateY(-50%);
  display:flex;align-items:center;gap:8px;
  background:var(--card);color:var(--text);border:1px solid var(--border);border-right:none;
  border-radius:10px 0 0 10px;
  padding:12px 10px;cursor:pointer;
  box-shadow:-2px 4px 14px rgba(0,0,0,.12);
  writing-mode:vertical-rl;text-orientation:mixed;
  font-size:.85rem;font-weight:600;letter-spacing:.02em;
  transition:background-color .15s, border-color .15s, color .15s, right .2s ease;
}
.doc-tab:hover{color:var(--blue);border-color:var(--blue);}
.doc-tab svg{transform:rotate(90deg);flex:none;}
.doc-tab.hidden{right:-999px;}

.doc-drawer{
  position:fixed;top:0;right:0;height:100vh;width:min(480px, 100vw);
  background:var(--card);border-left:1px solid var(--border);
  box-shadow:-4px 0 24px rgba(0,0,0,.18);
  z-index:55;
  display:flex;flex-direction:column;
  transform:translateX(100%);
  transition:transform .22s ease;
}
.doc-drawer.show{transform:translateX(0);}
.doc-drawer-head{
  display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:14px 16px;border-bottom:1px solid var(--border);flex:none;
}
.doc-drawer-head h3{font-size:.95rem;}
.doc-drawer-head .actions{display:flex;align-items:center;gap:6px;}
.doc-drawer-head a.iconbtn{text-decoration:none;}
.doc-drawer iframe{flex:1;width:100%;border:none;background:#fff;}
.doc-drawer-backdrop{
  position:fixed;inset:0;background:rgba(10,12,16,.4);z-index:54;
  opacity:0;pointer-events:none;transition:opacity .2s ease;
}
.doc-drawer-backdrop.show{opacity:1;pointer-events:auto;}
@media (max-width:640px){
  .doc-drawer{width:100vw;}
}

/* ---------- modals ---------- */
.overlay{position:fixed;inset:0;background:rgba(10,12,16,.55);display:none;align-items:center;justify-content:center;z-index:50;padding:20px;}
.overlay.show{display:flex;}
.modal{
  background:var(--card);border-radius:14px;max-width:440px;width:100%;padding:26px;
  box-shadow:0 10px 40px rgba(0,0,0,.25);
}
.modal h3{font-size:1.1rem;margin:0 0 10px;}
.modal p{color:var(--text-dim);font-size:.92rem;line-height:1.5;margin:0;}
.modal .row{display:flex;gap:10px;margin-top:22px;flex-wrap:wrap;}
.btn{
  font-size:.9rem;font-weight:600;
  padding:11px 18px;border:1px solid var(--border);border-radius:8px;background:var(--card);color:var(--text);cursor:pointer;flex:1;
  transition:.15s;
}
.btn:hover{background:var(--bg);}
.btn.primary{background:var(--blue);border-color:var(--blue);color:#fff;}
.btn.primary:hover{background:#1d4fd1;}
.btn.ghost{color:var(--text-dim);}
.btn.green{border-color:var(--green);color:var(--green);}
.btn.green:hover{background:var(--green-dim);}
.btn.red{border-color:var(--red);color:var(--red);}
.btn.red:hover{background:var(--red-dim);}
.msgbox{
  width:100%;min-height:220px;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);
  font-family:inherit;font-size:.9rem;padding:12px;resize:vertical;margin-top:4px;
}

.toast{
  position:fixed;left:24px;bottom:24px;z-index:60;background:var(--toast-bg);
  color:var(--toast-text);border-radius:8px;padding:10px 16px;font-size:.88rem;opacity:0;transform:translateY(8px);
  transition:.2s;pointer-events:none;
}
.toast.show{opacity:1;transform:translateY(0);}

/* ---------- home / sign-in ---------- */
.home-wrap{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;position:relative;overflow:hidden;}
.home-wrap::before{
  content:"";position:absolute;inset:-25% -10% auto -10%;height:460px;
  background:radial-gradient(circle at 28% 30%, var(--blue-dim), transparent 60%),
             radial-gradient(circle at 76% 62%, var(--green-dim), transparent 55%);
  filter:blur(6px);pointer-events:none;opacity:.9;
}
.home-card{
  position:relative;z-index:1;width:100%;max-width:380px;text-align:center;
  background:var(--card);border:1px solid var(--border);border-radius:18px;
  padding:38px 30px 30px;box-shadow:0 20px 60px rgba(0,0,0,.12);
}
.home-badge{
  width:52px;height:52px;border-radius:14px;margin:0 auto 18px;
  display:flex;align-items:center;justify-content:center;
  background:var(--blue-dim);color:var(--blue);
}
.home-card h1{font-size:1.25rem;margin-bottom:8px;}
.home-card p{color:var(--text-dim);font-size:.9rem;line-height:1.5;margin:0 0 26px;}
.google-btn{
  display:flex;align-items:center;justify-content:center;gap:10px;
  width:100%;padding:12px 18px;border-radius:10px;border:1px solid var(--border);
  background:var(--card);color:var(--text);font-weight:600;font-size:.92rem;
  text-decoration:none;transition:.15s;
}
.google-btn:hover{border-color:var(--blue);box-shadow:0 4px 14px rgba(37,99,235,.18);transform:translateY(-1px);}
.home-links{margin-top:22px;font-size:.8rem;color:var(--text-dim);}
.home-links a{color:var(--text-dim);text-decoration:none;}
.home-links a:hover{color:var(--blue);text-decoration:underline;}
.home-links span{margin:0 6px;}
</style>
</head>
<body>

<button class="theme-toggle" id="theme-toggle" title="Toggle dark mode" aria-label="Toggle dark mode">
  <svg class="icon-sun" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>
  <svg class="icon-moon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
</button>

<div id="app" class="app" style="display:none;">
  <header class="top">
    <div>
      <h1>SD EOT Exam — Grader</h1>
      <div class="sub">Grading attempts · SD | Company</div>
    </div>
    <div class="acct">
      <span id="acct-email">&nbsp;</span>
      <button id="logout-btn">Log out</button>
    </div>
  </header>

  <div class="grid">
    <div class="panel">
      <h2>Pending <span class="count" id="pending-count">0</span></h2>
      <div id="pending-list"></div>
      <div id="pending-empty" class="empty-note" style="display:none;">There are no exams pending review right now.</div>
      <div id="pending-error" class="empty-note" style="display:none;color:var(--red);"></div>
    </div>

    <div class="panel" id="grading-panel">
      <div id="placeholder" class="placeholder">
        <svg width="42" height="42" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M4 4h16v16H4z"/><path d="M8 9h8M8 13h5"/></svg>
        <div>Select a pending exam to start grading it.</div>
      </div>

      <div id="grading-body" style="display:none;">
        <div class="grading-head">
          <div>
            <div class="u" id="g-username"></div>
            <div class="t" id="g-submitted"></div>
          </div>
          <button class="link-btn" id="discard-btn">Discard without grading</button>
        </div>
        <div id="questions"></div>
        <div class="hint">Tap a question to mark it as wrong. Questions you don't tap are considered correct.</div>
      </div>
    </div>
  </div>

  <div class="panel" style="margin-top:20px;">
    <h2>Recently graded <span class="count" id="recent-count">0</span></h2>
    <div id="recent-list"></div>
    <div id="recent-empty" class="empty-note" style="display:none;">You haven't graded any exams in the last 2 hours.</div>
  </div>
</div>

<div id="login-view" class="home-wrap" style="display:none;">
  <div class="home-card">
    <div class="home-badge">
      <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2 2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>
    </div>
    <h1>SD EOT Exam — Grader</h1>
    <p>Sign in with the authorized Google account to find the form and grade pending attempts.</p>
    <a class="google-btn" href="/login">
      <svg width="18" height="18" viewBox="0 0 48 48"><path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.3 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.9 1.2 8 3.1l5.7-5.7C34.5 6.1 29.5 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.7-.4-3.5z"/><path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.6 15.9 18.9 13 24 13c3.1 0 5.9 1.2 8 3.1l5.7-5.7C34.5 6.1 29.5 4 24 4c-7.6 0-14.1 4.3-17.4 10.7z"/><path fill="#4CAF50" d="M24 44c5.3 0 10.1-2 13.7-5.4l-6.3-5.3C29.4 34.9 26.8 36 24 36c-5.3 0-9.7-3.3-11.3-8l-6.6 5.1C9.6 39.6 16.2 44 24 44z"/><path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.3-2.3 4.3-4.2 5.7l6.3 5.3C39.7 37.5 44 31.3 44 24c0-1.3-.1-2.7-.4-3.5z"/></svg>
      Sign in with Google
    </a>
    <div class="home-links">
      <a href="/privacy">Privacy Policy</a>
      <span>·</span>
      <a href="/terms">Terms of Service</a>
    </div>
  </div>
</div>

<button class="fab" id="fab" disabled title="Finish grading">
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M20 6L9 17l-5-5"/></svg>
  Finish grading
</button>

<!-- floating reference doc tab -->
<button class="doc-tab" id="doc-tab" title="Open reference doc">
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
  Reference doc
</button>

<div class="doc-drawer-backdrop" id="doc-drawer-backdrop"></div>
<div class="doc-drawer" id="doc-drawer">
  <div class="doc-drawer-head">
    <h3>Reference doc</h3>
    <div class="actions">
      <a class="iconbtn" id="doc-drawer-open" href="https://docs.google.com/document/d/1-7eOxE_8KYL_oKWcgIJEryZWyrHDVkxAsXq-dt0hSFg/edit?usp=sharing" target="_blank" rel="noopener" title="Open in new tab">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6"/><path d="M10 14L21 3"/></svg>
      </a>
      <button class="iconbtn" id="doc-drawer-close" title="Close">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18M6 6l12 12"/></svg>
      </button>
    </div>
  </div>
  <iframe id="doc-drawer-frame" src="" loading="lazy" title="Reference document"></iframe>
</div>

<!-- modal: confirm finish -->
<div class="overlay" id="ov-confirm">
  <div class="modal">
    <h3>Finished grading?</h3>
    <p>Questions you didn't mark are considered correct. This will save the changes for this exam.</p>
    <div class="row">
      <button class="btn ghost" data-close="ov-confirm">Cancel</button>
      <button class="btn primary" id="confirm-yes">Yes, finish</button>
    </div>
  </div>
</div>

<!-- modal: pass / fail -->
<div class="overlay" id="ov-result">
  <div class="modal">
    <h3>Pass or fail?</h3>
    <p>The corresponding message will be generated to send them.</p>
    <div class="row">
      <button class="btn red" id="result-fail">✕ Fail</button>
      <button class="btn green" id="result-pass">✓ Pass</button>
    </div>
  </div>
</div>

<!-- modal: generated message -->
<div class="overlay" id="ov-message">
  <div class="modal" style="max-width:560px;">
    <h3 id="msg-title">Generated message</h3>
    <p>Review the text, then copy or download it to send.</p>
    <textarea class="msgbox" id="msg-text" readonly></textarea>
    <div class="row">
      <button class="btn ghost" id="msg-copy">Copy</button>
      <button class="btn ghost" id="msg-download">Download .txt</button>
      <button class="btn primary" data-close="ov-message">Close</button>
    </div>
  </div>
</div>


<!-- modal: view a choice question as it looks in the form -->
<div class="overlay" id="ov-formview">
  <div class="modal" style="max-width:520px;">
    <h3 id="fv-title">Question</h3>
    <div id="fv-options" class="fv-options"></div>
    <div id="fv-note" class="fv-note" style="display:none;"></div>
    <div class="row">
      <button class="btn primary" data-close="ov-formview">Close</button>
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

/* ---------- theme toggle ---------- */
function applyTheme(theme){
  document.documentElement.setAttribute('data-theme', theme);
  try{ localStorage.setItem('eot-theme', theme); }catch(e){}
  const meta = $('#theme-color-meta');
  if(meta) meta.setAttribute('content', theme === 'dark' ? '#12151c' : '#f7f8fa');
}
$('#theme-toggle').addEventListener('click', ()=>{
  const current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  applyTheme(current === 'dark' ? 'light' : 'dark');
});
applyTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light');

/* ---------- reference doc drawer ---------- */
const DOC_PREVIEW_URL = 'https://docs.google.com/document/d/1-7eOxE_8KYL_oKWcgIJEryZWyrHDVkxAsXq-dt0hSFg/preview';
let docDrawerLoaded = false;
function openDocDrawer(){
  if(!docDrawerLoaded){
    $('#doc-drawer-frame').src = DOC_PREVIEW_URL;
    docDrawerLoaded = true;
  }
  $('#doc-drawer').classList.add('show');
  $('#doc-drawer-backdrop').classList.add('show');
  $('#doc-tab').classList.add('hidden');
}
function closeDocDrawer(){
  $('#doc-drawer').classList.remove('show');
  $('#doc-drawer-backdrop').classList.remove('show');
  $('#doc-tab').classList.remove('hidden');
}
$('#doc-tab').addEventListener('click', openDocDrawer);
$('#doc-drawer-close').addEventListener('click', closeDocDrawer);
$('#doc-drawer-backdrop').addEventListener('click', closeDocDrawer);
document.addEventListener('keydown', (e)=>{
  if(e.key === 'Escape' && $('#doc-drawer').classList.contains('show')) closeDocDrawer();
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
  if(mins < 1) return 'just now';
  if(mins < 60) return `${mins} min ago`;
  const h = Math.floor(mins/60);
  return `${h}h ${mins%60}min ago`;
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
  $('#g-submitted').textContent = 'Submitted ' + timeAgo(currentResponse.submitted_at);
  const box = $('#questions');
  box.innerHTML='';
  currentResponse.questions.forEach((q, idx)=>{
    const hasChoices = !!(q.type && q.options && q.options.length);
    const row = el(`<div class="qrow" data-id="${q.id}">
        <div class="qrow-head">
          <span class="tag">Question ${idx+1}</span>
          <div class="points-edit">
            <input type="number" class="points-input" min="0" step="1" placeholder="pts" value="${q.points != null ? q.points : ''}">
            <button type="button" class="iconbtn points-save" title="Save point value to the form">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M20 6L9 17l-5-5"/></svg>
            </button>
          </div>
          <span class="wrong-flag">Wrong</span>
        </div>
        <div class="qsection q-question">
          <div class="qsection-label">Question</div>
          <div class="qsection-body qt"></div>
        </div>
        <div class="qsection q-answer">
          <div class="qsection-label-row">
            <div class="qsection-label">Answer</div>
            ${hasChoices ? `<button type="button" class="iconbtn view-form-btn" title="View as it looks in the form">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg>
            </button>` : ''}
          </div>
          <div class="qsection-body qa"></div>
        </div>
      </div>`);
    row.querySelector('.qt').textContent = q.title;
    row.querySelector('.qa').textContent = q.answer;
    if(hasChoices){
      row.querySelector('.view-form-btn').addEventListener('click', (e)=>{
        e.stopPropagation();
        openFormView(q);
      });
    }
    const pointsInput = row.querySelector('.points-input');
    const pointsSaveBtn = row.querySelector('.points-save');
    pointsInput.addEventListener('click', (e)=>e.stopPropagation());
    pointsSaveBtn.addEventListener('click', async (e)=>{
      e.stopPropagation();
      const val = pointsInput.value;
      if(val === ''){ toast('Enter a point value first'); return; }
      const points = parseInt(val, 10);
      if(isNaN(points) || points < 0){ toast('Points must be 0 or more'); return; }
      const res = await apiPost('/api/set_points', {question_id: q.id, points});
      if(res.error){ toast(res.error); return; }
      q.points = points;
      toast('Points saved to the form');
    });
    row.addEventListener('click', ()=>{
      if(wrongIds.has(q.id)){ wrongIds.delete(q.id); row.classList.remove('wrong'); }
      else { wrongIds.add(q.id); row.classList.add('wrong'); }
    });
    box.appendChild(row);
  });
}

function openFormView(q){
  $('#fv-title').textContent = q.title;
  const box = $('#fv-options');
  box.innerHTML = '';
  const selected = new Set(q.selected || []);
  const correct = q.correct ? new Set(q.correct) : null;
  const isCheckbox = q.type === 'CHECKBOX';
  q.options.forEach(opt=>{
    const isSelected = selected.has(opt);
    const isCorrect = correct ? correct.has(opt) : null;
    let cls = 'fv-opt';
    if(isSelected && isCorrect === true) cls += ' fv-right';
    else if(isSelected && isCorrect === false) cls += ' fv-wrong';
    else if(isSelected) cls += ' fv-selected';
    else if(isCorrect === true) cls += ' fv-missed';
    const row = el(`<div class="${cls}"><span class="fv-mark"></span><span class="fv-text"></span></div>`);
    row.querySelector('.fv-mark').textContent = isSelected ? (isCheckbox ? '☑' : '●') : (isCheckbox ? '☐' : '○');
    row.querySelector('.fv-text').textContent = opt;
    box.appendChild(row);
  });
  const note = $('#fv-note');
  if(correct === null){
    note.style.display = 'block';
    note.textContent = "This form doesn't expose correct answers here — only what was selected is shown.";
  } else {
    note.style.display = 'none';
  }
  openModal('ov-formview');
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
  $('#msg-title').textContent = result==='fail' ? 'Exam failed' : 'Exam passed';
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
  toast('Message copied to clipboard');
});
$('#msg-download').addEventListener('click', ()=>{
  const result = $('#msg-title').textContent.includes('failed') ? 'formatfailed' : 'formatpassed';
  const blob = new Blob([$('#msg-text').value], {type:'text/plain'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = result + '.txt';
  a.click();
});

$('#discard-btn').addEventListener('click', async ()=>{
  if(!currentResponse) return;
  if(!confirm('Delete this exam without grading it? It cannot be graded later.')) return;
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
          <span class="badge ${r.result==='pass'?'pass':'fail'}">${r.result==='pass'?'Pass':'Fail'}</span>
          <button class="iconbtn" data-act="view" title="View message">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg>
          </button>
          <button class="iconbtn" data-act="del" title="Delete">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>
          </button>
        </div>
      </div>`);
    row.querySelector('.u').textContent = r.username;
    row.querySelector('.meta').textContent = `${timeAgo(r.graded_at)} · hides in ${Math.floor(r.minutes_left/60)}h ${r.minutes_left%60}min`;
    row.querySelector('[data-act="view"]').addEventListener('click', ()=>{
      $('#msg-title').textContent = r.result==='fail' ? 'Exam failed' : 'Exam passed';
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
    const r = await fetch('/api/me');
    if(r.status === 401) throw new Error('unauthenticated');
    const me = await r.json();
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
        return INDEX_HTML  # the JS shows the home/sign-in view when /api/me is unauthenticated
    return INDEX_HTML

# --------------------------------------------------------------------------
# Rutas de Políticas Legales (Cumplimiento de OAuth de Google)
# --------------------------------------------------------------------------

LEGAL_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__ - SD EOT Exam</title>
<script>
(function(){
  try {
    var stored = localStorage.getItem('eot-theme');
    var theme = stored || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', theme);
  } catch (e) {}
})();
</script>
<style>
:root{
  --bg:#f7f8fa; --card:#ffffff; --border:#e3e6ea; --text:#1f2430; --text-dim:#707685;
  --blue:#2563eb; --radius:14px;
}
[data-theme="dark"]{
  --bg:#12151c; --card:#1a1f2b; --border:#2b3140; --text:#e7e9ee; --text-dim:#96a0b3;
  --blue:#5b93ff;
}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;}
body{
  background:var(--bg); color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  line-height:1.6;
}
.legal-topbar{display:flex;align-items:center;justify-content:space-between;max-width:720px;margin:0 auto;padding:22px 20px 0;}
.legal-topbar a{color:var(--text-dim);text-decoration:none;font-size:.85rem;font-weight:600;display:flex;align-items:center;gap:6px;}
.legal-topbar a:hover{color:var(--blue);}
.theme-toggle{background:var(--card);border:1px solid var(--border);border-radius:8px;color:var(--text-dim);padding:6px;cursor:pointer;display:flex;align-items:center;justify-content:center;}
.theme-toggle:hover{border-color:var(--blue);color:var(--blue);}
.theme-toggle .icon-moon{display:none;}
[data-theme="dark"] .theme-toggle .icon-sun{display:none;}
[data-theme="dark"] .theme-toggle .icon-moon{display:block;}
.legal-wrap{max-width:720px;margin:0 auto;padding:28px 20px 70px;}
.legal-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:38px 34px;}
.legal-card h1{font-size:1.5rem;margin:0 0 6px;}
.legal-updated{color:var(--text-dim);font-size:.82rem;margin:0 0 26px;}
.legal-card h2{font-size:1.02rem;margin:28px 0 10px;padding-top:20px;border-top:1px solid var(--border);}
.legal-card h2:first-of-type{border-top:none;padding-top:0;margin-top:0;}
.legal-card p, .legal-card li{color:var(--text-dim);font-size:.92rem;}
.legal-card ul{padding-left:20px;margin:8px 0;}
.legal-card li{margin-bottom:4px;}
.legal-card a{color:var(--blue);}
.legal-card strong{color:var(--text);}
.legal-card code{background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:1px 5px;font-size:.85em;}
.legal-footer{display:flex;justify-content:center;gap:10px;margin-top:26px;font-size:.85rem;}
.legal-footer a{color:var(--text-dim);text-decoration:none;padding:8px 14px;border:1px solid var(--border);border-radius:20px;transition:.15s;}
.legal-footer a:hover{border-color:var(--blue);color:var(--blue);}
</style>
</head>
<body>
<div class="legal-topbar">
  <a href="/">&larr; Back to SD EOT Exam</a>
  <button class="theme-toggle" id="theme-toggle" title="Toggle dark mode" aria-label="Toggle dark mode">
    <svg class="icon-sun" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>
    <svg class="icon-moon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
  </button>
</div>
<div class="legal-wrap">
  <div class="legal-card">
    <h1>__TITLE__</h1>
    <p class="legal-updated">Last updated: __UPDATED__</p>
    __BODY__
  </div>
  <div class="legal-footer">
    <a href="/">Home</a>
    <a href="/privacy">Privacy Policy</a>
    <a href="/terms">Terms of Service</a>
  </div>
</div>
<script>
document.getElementById('theme-toggle').addEventListener('click', function(){
  var current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  var next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try{ localStorage.setItem('eot-theme', next); }catch(e){}
});
</script>
</body>
</html>"""


def render_legal_page(title, updated, body_html):
    return (
        LEGAL_PAGE_TEMPLATE
        .replace("__TITLE__", title)
        .replace("__UPDATED__", updated)
        .replace("__BODY__", body_html)
    )


@app.route("/privacy")
def privacy():
    body = """
    <p><strong>SD EOT Exam</strong> ("the Application", "we") respects your privacy. This Privacy Policy explains how information is handled when using our web service (<code>https://eot.devs.surf</code>).</p>

    <h2>1. Information Accessed</h2>
    <p>When authenticating through Google OAuth, the Application requests permissions to access:</p>
    <ul>
        <li><strong>Google Account Email:</strong> Used exclusively to authenticate authorized application managers.</li>
        <li><strong>Google Forms & Drive Metadata:</strong> Used to fetch student submissions from linked Google Forms for automatic response evaluation, and to update a question's point value in the form when an administrator edits it within the app.</li>
    </ul>

    <h2>2. Data Usage & Sharing</h2>
    <p>Accessed data is processed only to evaluate exam answers, flag incorrect questions, generate text feedback for applicants, and update point values on the linked form. We do not sell, rent, or share user data with any third parties.</p>

    <h2>3. Google API Limited Use Disclosure</h2>
    <p>SD EOT Exam's use and transfer to any other app of information received from Google APIs will adhere to the <a href="https://developers.google.com/terms/api-services-user-data-policy" target="_blank">Google API Services User Data Policy</a>, including the Limited Use requirements.</p>

    <h2>4. Data Retention</h2>
    <p>Grading logs (user identifier, pass/fail state, and wrong question titles) are saved locally within a restricted database. Raw Google Form files are never duplicated or permanently stored on our servers.</p>

    <h2>5. Contact Us</h2>
    <p>If you have any questions regarding this Privacy Policy or data processing, please contact the developer at: <strong>bankainobi@gmail.com</strong>.</p>
    """
    return render_legal_page("Privacy Policy", "September 15, 2026", body)


@app.route("/terms")
def terms():
    body = """
    <p>By accessing or using <strong>SD EOT Exam</strong> (<code>https://eot.devs.surf</code>), you agree to be bound by these Terms of Service. If you do not agree, please do not use the application.</p>

    <h2>1. Service Description</h2>
    <p>SD EOT Exam is an automated grading and feedback utility designed to review student submissions from authorized Google Forms.</p>

    <h2>2. Authorized Access & Authentication</h2>
    <p>Authentication is processed via Google OAuth. You are responsible for maintaining the security of your account credentials and ensuring you have legitimate authorization to access and edit linked exam forms.</p>

    <h2>3. Third-Party Integration Disclaimer</h2>
    <p>This application integrates with services provided by Google LLC (Google Forms, Google Drive, and Google OAuth). SD EOT Exam is an independent tool and is not affiliated with, sponsored by, or endorsed by Google LLC.</p>

    <h2>4. Disclaimer of Warranties & Limitation of Liability</h2>
    <p>The application is provided on an "AS IS" and "AS AVAILABLE" basis. The developer shall not be held liable for any indirect damages, grading errors, or service interruptions resulting from the use of this service.</p>

    <h2>5. Service Modifications</h2>
    <p>We reserve the right to modify, suspend, or terminate access to the application at any time without prior notice.</p>

    <h2>6. Contact Information</h2>
    <p>If you have any questions about these Terms of Service, please contact the developer at: <strong>bankainobi@gmail.com</strong>.</p>
    """
    return render_legal_page("Terms of Service", "September 15, 2026", body)

if __name__ == "__main__":
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not ADMIN_EMAIL:
        print("⚠  Missing GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / ADMIN_EMAIL.")
        print("   Create a .env file (see .env.example) or export them before starting.")
    print(f"➡  Listening on http://{HOST}:{PORT}")
    print(f"   Configured redirect URI: {GOOGLE_REDIRECT_URI}")
    # Note: on Render, the process is started by gunicorn (see Procfile), not this branch.
    app.run(host=HOST, port=PORT, debug=False)
