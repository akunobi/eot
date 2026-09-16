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
import urllib.request
import urllib.error
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

# Optional: URL + shared secret for the Apps Script "grading bridge" web app.
# The Forms REST API cannot write a per-response score (only per-question max
# points, handled by /api/set_points below) — that write only exists through
# Apps Script's FormApp.submitGrades(). See APPS_SCRIPT_BRIDGE_SETUP.md for
# how to deploy it. Left blank, /api/set_response_score just returns an error.
APPS_SCRIPT_BRIDGE_URL = os.environ.get("APPS_SCRIPT_BRIDGE_URL", "").rstrip("/")
APPS_SCRIPT_BRIDGE_SECRET = os.environ.get("APPS_SCRIPT_BRIDGE_SECRET", "")
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
            archived        INTEGER DEFAULT 0,
            points_assigned TEXT DEFAULT '{}'  -- JSON {questionId: pointsAwarded}, the base/max value always comes live from the form
        )
        """
    )
    # Migration for DBs created before this column existed.
    try:
        db.execute("ALTER TABLE ledger ADD COLUMN points_assigned TEXT DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass
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
        grade = (raw_answer or {}).get("grade") or {}
        questions.append({
            "id": qid,
            "title": title,
            "answer": answer_text,
            "type": meta.get("type"),          # RADIO | CHECKBOX | DROP_DOWN | None
            "options": meta.get("options") or [],  # all options as they appear in the form
            "correct": meta.get("correct"),    # list of correct values, or None if not exposed
            "selected": extract_answer_list(raw_answer),  # raw selected values
            "points": meta.get("points"),      # current max point value in the form, or None
            "awarded": grade.get("score"),     # score currently recorded on THIS response in
                                                # the Form (read-only via API), or None if never graded
            "form_correct": grade.get("correct"),  # Forms' own right/wrong flag for this response, or None
        })

    return jsonify({
        "error": None,
        "response_id": response_id,
        "username": username,
        "submitted_at": target.get("lastSubmittedTime") or target.get("createTime") or "",
        "total_score": target.get("totalScore"),  # sum of awarded scores as Forms currently has it
        "questions": questions,
    })


@app.route("/api/set_points", methods=["POST"])
@login_required
def api_set_points():
    """Pushes a new point value to the Form itself. This is the only kind of
    point-related write the Forms API supports: it's a per-question value,
    shared by every response (past and future auto-grading), not a per-student
    score. Kept as an explicit, separate action from the local per-response
    'points assigned' note."""
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


def push_score_to_bridge(form_id, response_id, question_id, question_title, score,
                          response_created_time=None, expected_answer_text=None):
    """Calls the Apps Script bridge web app to record a per-response score.
    Returns (ok, result_dict_or_error_string).

    response_created_time / expected_answer_text are required in practice:
    the REST API's responseId and Apps Script's FormApp response IDs live in
    different ID spaces, so the bridge falls back to matching by submission
    timestamp (and, if needed, by the text of this answer) — see bridge.gs.
    """
    if not APPS_SCRIPT_BRIDGE_URL or not APPS_SCRIPT_BRIDGE_SECRET:
        return False, (
            "The Apps Script bridge isn't configured. Set APPS_SCRIPT_BRIDGE_URL and "
            "APPS_SCRIPT_BRIDGE_SECRET (see APPS_SCRIPT_BRIDGE_SETUP.md)."
        )

    payload = json.dumps({
        "secret": APPS_SCRIPT_BRIDGE_SECRET,
        "form_id": form_id,
        "response_id": response_id,
        "question_id": question_id,
        "question_title": question_title,
        "score": score,
        "response_created_time": response_created_time,
        "expected_answer_text": expected_answer_text,
    }).encode("utf-8")

    req = urllib.request.Request(
        APPS_SCRIPT_BRIDGE_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # Apps Script web apps sometimes 302-redirect through accounts.google.com
        # when access isn't set to "Anyone" — surface that clearly.
        return False, f"The bridge returned HTTP {exc.code}. Check the deployment's access setting."
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not reach the Apps Script bridge: {exc}"

    try:
        result = json.loads(body)
    except (TypeError, ValueError):
        return False, "The bridge returned something that wasn't valid JSON (check the deployment URL/logs)."

    if not result.get("ok"):
        err = result.get("error") or "The Form rejected the score."
        debug_bits = []
        for key in (
            "opened_form_id", "opened_form_title", "requested_response_id",
            "response_count_on_opened_form", "sample_ids_on_opened_form",
        ):
            if key in result:
                debug_bits.append(f"{key}={result[key]}")
        if debug_bits:
            err += " [" + ", ".join(debug_bits) + "]"
        return False, err
    return True, result


@app.route("/api/set_response_score", methods=["POST"])
@login_required
def api_set_response_score():
    """Pushes the points-awarded ('left number') for ONE question on ONE
    response into the actual Google Form, via the Apps Script bridge (the
    Forms REST API has no write for this — see push_score_to_bridge)."""
    data = request.get_json(force=True, silent=True) or {}
    response_id = data.get("response_id")
    question_id = data.get("question_id")
    score = data.get("score")

    if not response_id or not question_id or score is None:
        return jsonify({"error": "Missing response_id, question_id or score."}), 400
    try:
        score = float(score)
    except (TypeError, ValueError):
        return jsonify({"error": "Score must be a number."}), 400
    if score < 0:
        return jsonify({"error": "Score can't be negative."}), 400

    creds = request.google_creds
    form_id = find_form_id(creds, FORM_TITLE)
    if not form_id:
        return jsonify({"error": f'Form "{FORM_TITLE}" not found.'}), 404

    form = get_form(creds, form_id)
    qmap, _order, _qmeta = question_map(form)
    question_title = qmap.get(question_id)
    if question_title is None:
        return jsonify({"error": "That question was not found in the form."}), 404

    # The bridge can't resolve response_id directly (REST API and Apps
    # Script use different ID spaces for the same response — see
    # bridge.gs), so it falls back to matching by submission timestamp,
    # disambiguating with the answer text if needed. Look both up here
    # from the same REST API response the UI already showed the grader.
    try:
        responses = list_all_responses(creds, form_id)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500

    target = next((r for r in responses if r.get("responseId") == response_id), None)
    if not target:
        return jsonify({"error": "That response was not found (it may no longer exist)."}), 404

    response_created_time = target.get("createTime")
    expected_answer_text = extract_answer_text(target.get("answers", {}).get(question_id))

    ok, result = push_score_to_bridge(
        form_id, response_id, question_id, question_title, score,
        response_created_time=response_created_time,
        expected_answer_text=expected_answer_text,
    )
    if not ok:
        return jsonify({"error": result}), 502

    return jsonify({"error": None, "score": result.get("score", score)})


@app.route("/api/grade", methods=["POST"])
@login_required
def api_grade():
    data = request.get_json(force=True, silent=True) or {}
    response_id = data.get("response_id")
    username = data.get("username", "")
    wrong_questions = data.get("wrong_questions", [])
    points_assigned = data.get("points_assigned", {})
    result = data.get("result")

    if not response_id or result not in ("pass", "fail"):
        return jsonify({"error": "Incomplete data."}), 400
    if not isinstance(points_assigned, dict):
        points_assigned = {}

    message = build_message(result, username, wrong_questions)

    db = get_db()
    db.execute(
        """
        INSERT INTO ledger (response_id, username, result, wrong_questions, message, graded_at, archived, points_assigned)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?)
        ON CONFLICT(response_id) DO UPDATE SET
            username=excluded.username,
            result=excluded.result,
            wrong_questions=excluded.wrong_questions,
            message=excluded.message,
            graded_at=excluded.graded_at,
            archived=0,
            points_assigned=excluded.points_assigned
        """,
        (
            response_id, username, result,
            json.dumps(wrong_questions, ensure_ascii=False),
            message, now_iso(),
            json.dumps(points_assigned, ensure_ascii=False),
        ),
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
            "points_assigned": json.loads(r["points_assigned"] or "{}"),
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
<meta name="theme-color" id="theme-color-meta" content="#e9ebef">
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
  --bg:#e9ebef;
  --card:#ffffff;
  --border:#d5d9e0;
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
  --shadow:0 1px 2px rgba(16,24,40,.04), 0 1px 8px rgba(16,24,40,.05);
  --card-glow:inset 0 1px 0 0 rgba(255,255,255,.6);
  --card-border:var(--border);
}
[data-theme="dark"]{
  --bg:#0b0d12;
  --card:#141824;
  --border:#242a38;
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
  --shadow:0 1px 2px rgba(0,0,0,.45), 0 8px 28px rgba(0,0,0,.4);
  --card-glow:inset 0 1px 0 0 rgba(255,255,255,.06);
  --card-border:rgba(255,255,255,.1);
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
.app{
  max-width:1400px;margin:0 auto;
  height:100vh;height:100dvh;
  padding:24px 28px calc(18px + env(safe-area-inset-bottom));
  display:grid;grid-template-rows:auto 1fr auto;gap:18px;
}
header.top{
  display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;
}
header.top h1{font-size:1.35rem;}
header.top .sub{color:var(--text-dim);font-size:.9rem;margin-top:4px;}
.acct{display:flex;align-items:center;gap:12px;font-size:.85rem;color:var(--text-dim);}
.acct button{
  background:var(--card);border:1px solid var(--border);color:var(--text);border-radius:8px;
  padding:7px 14px;cursor:pointer;
}
.acct button:hover{border-color:var(--red);color:var(--red);}

.grid{display:grid;grid-template-columns:300px 1fr;gap:20px;align-items:stretch;min-height:0;}
@media (max-width:820px){.grid{grid-template-columns:1fr;}.app{height:auto;min-height:100vh;min-height:100dvh;}}

.panel{background:var(--card);border:1px solid var(--card-border);border-radius:var(--radius);padding:20px;box-shadow:var(--shadow), var(--card-glow);transition:background-color .15s, border-color .15s;display:flex;flex-direction:column;min-height:0;}
.recent-panel{max-height:38vh;}
.recent-panel #recent-list{overflow-y:auto;}
#pending-list{flex:1;min-height:0;overflow-y:auto;padding-right:2px;}
.panel h2{font-size:1rem;margin:0 0 14px;display:flex;justify-content:space-between;align-items:center;}
.panel h2 .count{
  color:var(--text-dim);font-weight:500;font-size:.85rem;background:var(--bg);
  border-radius:20px;padding:2px 10px;
}

/* ---------- pending list ---------- */
.pending-item{
  display:flex;align-items:center;gap:12px;
  border:1px solid var(--border);border-radius:8px;padding:10px 14px;margin-bottom:8px;
  cursor:pointer;transition:border-color .15s, background .15s;
}
.pending-item:hover{border-color:var(--blue);background:var(--blue-dim);transform:translateY(-1px);}
.pending-item.active{border-color:var(--blue);background:var(--blue-dim);box-shadow:0 0 0 1px var(--blue) inset;}
.pending-item .pi-info{flex:1;min-width:0;}
.pending-item .u{color:var(--text);font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.pending-item .t{display:flex;align-items:center;gap:6px;color:var(--text-dim);font-size:.82rem;margin-top:2px;}
.pending-item:active{transform:scale(.99);}
.empty-note{color:var(--text-dim);font-size:.9rem;line-height:1.5;}

/* ---------- avatars ---------- */
.avatar{
  flex:none;width:34px;height:34px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;
  font-size:.76rem;font-weight:700;color:#fff;letter-spacing:.02em;
}

/* ---------- urgency dots on timestamps ---------- */
.tdot{width:6px;height:6px;border-radius:50%;flex:none;display:inline-block;}
.tdot.ok{background:var(--green);}
.tdot.warn{background:var(--amber);}
.tdot.urgent{background:var(--red);}

/* ---------- loading skeletons ---------- */
@keyframes shimmer{0%{background-position:-220px 0;}100%{background-position:calc(220px + 100%) 0;}}
.skeleton{
  border-radius:8px;height:50px;margin-bottom:8px;
  background:linear-gradient(90deg, var(--bg) 0px, var(--border) 60px, var(--bg) 120px);
  background-size:220px 100%;animation:shimmer 1.3s ease-in-out infinite;
}
@media (prefers-reduced-motion: reduce){.skeleton{animation:none;opacity:.6;}}

/* ---------- entrance animation for list rows ---------- */
@keyframes itemIn{from{opacity:0;transform:translateY(3px);}to{opacity:1;transform:translateY(0);}}
.pending-item,.recent-item{animation:itemIn .16s ease both;}

/* ---------- small status affordances ---------- */
.unsaved-dot{width:7px;height:7px;border-radius:50%;background:var(--amber);display:inline-block;margin-left:7px;vertical-align:middle;}
.wrong-count{font-size:.8rem;color:var(--text-dim);font-weight:600;white-space:nowrap;}
.wrong-count.has-wrong{color:var(--red);}
.toast.error{background:var(--red);color:#fff;}
.toast.success{background:var(--green);color:#fff;}
.kbd{
  display:inline-block;min-width:16px;text-align:center;font-size:.72rem;font-weight:600;
  border:1px solid var(--border);border-bottom-width:2px;border-radius:4px;padding:0 4px;
  color:var(--text-dim);background:var(--bg);font-family:inherit;
}

/* ---------- grading panel ---------- */
#grading-body{display:flex;flex-direction:column;flex:1;min-height:0;}
#questions{flex:1;overflow-y:auto;padding-right:2px;}
.grading-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:18px;flex-wrap:wrap;flex:none;}
.grading-head .u{font-size:1.15rem;font-weight:600;}
.grading-head .t{color:var(--text-dim);font-size:.85rem;margin-top:3px;}
.link-btn{background:none;border:none;color:var(--text-dim);text-decoration:underline;cursor:pointer;font-size:.85rem;padding:0;}
.link-btn:hover{color:var(--red);}
.link-btn.neutral:hover{color:var(--blue);}

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
.points-edit{display:flex;align-items:center;gap:5px;}
.points-input{width:40px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:3px 6px;font-size:.8rem;}
.points-base-wrap{display:flex;align-items:center;gap:3px;}
.points-base-view{color:var(--text-dim);font-size:.8rem;font-weight:600;}
.points-base-input{width:34px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:2px 4px;font-size:.78rem;}
.points-base-wrap .iconbtn{padding:2px 4px;}
.points-sync-btn{padding:2px 4px;}
.points-sync-btn.is-synced{color:#2fae5c;}
.points-sync-status{font-size:.68rem;color:var(--text-dim);margin-left:2px;white-space:nowrap;}
.points-sync-status.ok{color:#2fae5c;}
.points-sync-status.err{color:#e5484d;}

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

.placeholder{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:200px;color:var(--text-dim);text-align:center;gap:14px;}
.placeholder .ph-illustration{opacity:.9;}
.placeholder .ph-title{color:var(--text);font-weight:600;font-size:.95rem;}
.placeholder .ph-sub{font-size:.85rem;max-width:280px;}

/* ---------- grading-pane skeleton loader ---------- */
.grading-skeleton{flex:1;display:none;flex-direction:column;gap:10px;padding-top:4px;}
.grading-skeleton .sk-line{border-radius:8px;}
.grading-skeleton .sk-head{height:22px;width:40%;}
.grading-skeleton .sk-sub{height:14px;width:25%;margin-bottom:8px;}
.grading-skeleton .sk-row{height:64px;}

/* ---------- recent ---------- */
.recent-item{
  display:flex;justify-content:space-between;align-items:center;gap:10px;
  border:1px solid var(--border);border-radius:8px;padding:10px 14px;margin-bottom:8px;flex-wrap:wrap;
}
.recent-item .ri-left{display:flex;align-items:center;gap:10px;min-width:0;}
.recent-item .ri-info{min-width:0;}
.recent-item .u{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.recent-item .meta{font-size:.8rem;color:var(--text-dim);margin-top:2px;}
.badge{font-size:.78rem;font-weight:600;padding:4px 10px;border-radius:20px;}
.badge.pass{color:var(--green);background:var(--green-dim);}
.badge.fail{color:var(--red);background:var(--red-dim);}
.iconbtn{background:none;border:1px solid var(--border);border-radius:8px;color:var(--text-dim);padding:6px 9px;cursor:pointer;display:inline-flex;}
.iconbtn:hover{border-color:var(--blue);color:var(--blue);}

/* ---------- grading toolbar & finish button ---------- */
.grading-toolbar{
  display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;
  flex:none;margin-top:14px;padding-top:14px;border-top:1px solid var(--border);
}
.grading-toolbar .hint{margin:0;flex:1;min-width:200px;}
.fab{
  display:flex;align-items:center;gap:8px;flex:none;
  background:var(--blue);color:#fff;border:none;border-radius:10px;
  padding:11px 20px;font-size:.9rem;font-weight:600;cursor:pointer;
  box-shadow:0 4px 14px rgba(37,99,235,.3);
  transition:opacity .15s, transform .15s, background .15s;
}
.fab:hover{background:#1d4fd1;}
.fab[disabled]{opacity:.35;pointer-events:none;box-shadow:none;}

@media (max-width:480px){
  .app{padding-left:14px;padding-right:14px;}
  header.top h1{font-size:1.15rem;}
  .grading-toolbar{justify-content:stretch;}
  .grading-toolbar .fab{width:100%;justify-content:center;}
}

/* ---------- reference doc nav button ---------- */
.doc-tab{
  display:flex;align-items:center;gap:7px;
  background:var(--card);color:var(--text);border:1px solid var(--border);
  border-radius:8px;
  padding:7px 13px;cursor:pointer;
  font-size:.85rem;font-weight:600;
  transition:background-color .15s, border-color .15s, color .15s, opacity .15s;
}
.doc-tab:hover{color:var(--blue);border-color:var(--blue);}
.doc-tab svg{flex:none;}
.doc-tab.hidden{opacity:.4;}

.doc-drawer{
  position:fixed;top:0;right:0;height:100vh;width:min(480px, 100vw);
  background:var(--card);border-left:1px solid var(--card-border);
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
  background:var(--card);border:1px solid var(--card-border);border-radius:14px;max-width:440px;width:100%;padding:26px;
  box-shadow:0 10px 40px rgba(0,0,0,.25), var(--card-glow);
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
  content:"";position:absolute;inset:0;
  background:radial-gradient(circle at 24% 18%, var(--blue-dim), transparent 42%),
             radial-gradient(circle at 74% 30%, var(--green-dim), transparent 38%),
             radial-gradient(circle at 50% 8%, var(--green-dim), transparent 46%),
             radial-gradient(circle at 50% 50%, transparent 55%, var(--bg) 82%);
  filter:blur(38px);pointer-events:none;opacity:.85;
}
.home-card{
  position:relative;z-index:1;width:100%;max-width:380px;text-align:center;
  background:var(--card);border:1px solid var(--card-border);border-radius:18px;
  padding:38px 30px 30px;box-shadow:0 20px 60px rgba(0,0,0,.16), var(--card-glow);
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
      <button class="doc-tab" id="doc-tab" title="Open reference doc">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
        Reference doc
      </button>
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
        <svg class="ph-illustration" width="120" height="88" viewBox="0 0 120 88" fill="none" xmlns="http://www.w3.org/2000/svg">
          <rect x="24" y="6" width="60" height="76" rx="8" fill="var(--blue-dim)"/>
          <rect x="34" y="20" width="40" height="5" rx="2.5" fill="var(--blue)" opacity=".55"/>
          <rect x="34" y="31" width="40" height="5" rx="2.5" fill="var(--border)"/>
          <rect x="34" y="42" width="26" height="5" rx="2.5" fill="var(--border)"/>
          <circle cx="86" cy="58" r="22" fill="var(--card)" stroke="var(--green)" stroke-width="3"/>
          <path d="M77 58l6 6 12-13" stroke="var(--green)" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <div class="ph-title">No exam selected</div>
        <div class="ph-sub">Choose a pending exam from the list to start grading it.</div>
      </div>

      <div id="grading-skeleton" class="grading-skeleton">
        <div class="skeleton sk-line sk-head"></div>
        <div class="skeleton sk-line sk-sub"></div>
        <div class="skeleton sk-line sk-row"></div>
        <div class="skeleton sk-line sk-row"></div>
        <div class="skeleton sk-line sk-row"></div>
      </div>

      <div id="grading-body" style="display:none;">
        <div class="grading-head">
          <div>
            <div class="u" id="g-username"></div>
            <div class="t" id="g-submitted"></div>
          </div>
          <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;">
            <span class="wrong-count" id="wrong-count">0 marked wrong</span>
            <button class="link-btn neutral" id="clear-marks-btn" title="Unmark every question in this exam">Clear marks</button>
            <button class="link-btn" id="discard-btn">Discard without grading</button>
          </div>
        </div>
        <div id="questions"></div>
        <div class="grading-toolbar">
          <div class="hint">Tap a question to mark it as wrong · press <span class="kbd">1</span>–<span class="kbd">9</span> to toggle · questions you don't mark are considered correct.</div>
          <button class="fab" id="fab" disabled title="Finish grading">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M20 6L9 17l-5-5"/></svg>
            Finish grading
          </button>
        </div>
      </div>
    </div>
  </div>

  <div class="panel recent-panel">
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

<!-- modal: generic reusable confirm (discard, delete, switch-with-unsaved-changes) -->
<div class="overlay" id="ov-generic-confirm">
  <div class="modal">
    <h3 id="gc-title">Are you sure?</h3>
    <p id="gc-body"></p>
    <div class="row">
      <button class="btn ghost" id="gc-cancel-btn" data-close="ov-generic-confirm">Cancel</button>
      <button class="btn red" id="gc-confirm-btn">Confirm</button>
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
function $$(sel){return Array.from(document.querySelectorAll(sel));}
function el(html){const t=document.createElement('template');t.innerHTML=html.trim();return t.content.firstChild;}

function toast(msg, type){
  const t = $('#toast');
  clearTimeout(t._hideTimer);
  t.textContent = msg;
  t.className = 'toast show' + (type ? ' ' + type : '');
  t._hideTimer = setTimeout(()=>t.classList.remove('show'), 2400);
}

function openModal(id){ $('#'+id).classList.add('show'); }
function closeModal(id){ $('#'+id).classList.remove('show'); }
document.querySelectorAll('[data-close]').forEach(b=>{
  b.addEventListener('click', ()=>closeModal(b.dataset.close));
});

/* ---------- generic confirm dialog (promise-based, replaces native confirm()) ---------- */
let _confirmResolve = null;
function confirmDialog(title, body, confirmLabel, tone){
  return new Promise(resolve=>{
    $('#gc-title').textContent = title;
    $('#gc-body').textContent = body;
    const btn = $('#gc-confirm-btn');
    btn.textContent = confirmLabel || 'Confirm';
    btn.className = 'btn ' + (tone || 'red');
    _confirmResolve = resolve;
    openModal('ov-generic-confirm');
  });
}
$('#gc-confirm-btn').addEventListener('click', ()=>{
  closeModal('ov-generic-confirm');
  if(_confirmResolve){ _confirmResolve(true); _confirmResolve = null; }
});
$('#gc-cancel-btn').addEventListener('click', ()=>{
  if(_confirmResolve){ _confirmResolve(false); _confirmResolve = null; }
});

/* ---------- theme toggle ---------- */
function applyTheme(theme){
  document.documentElement.setAttribute('data-theme', theme);
  try{ localStorage.setItem('eot-theme', theme); }catch(e){}
  const meta = $('#theme-color-meta');
  if(meta) meta.setAttribute('content', theme === 'dark' ? '#0b0d12' : '#e9ebef');
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
  if(e.key === 'Escape'){
    if($('#doc-drawer').classList.contains('show')){ closeDocDrawer(); return; }
    const openOverlay = $$('.overlay.show')[0];
    if(openOverlay){ closeModal(openOverlay.id); return; }
    return;
  }
  // Number-key shortcuts to mark questions wrong while grading — skip while
  // typing in a field or while any modal/drawer is open.
  if(!currentResponse) return;
  const tag = (document.activeElement && document.activeElement.tagName) || '';
  if(tag === 'INPUT' || tag === 'TEXTAREA') return;
  if($$('.overlay.show').length || $('#doc-drawer').classList.contains('show')) return;
  if(e.key >= '1' && e.key <= '9'){
    const rows = $$('.qrow');
    const row = rows[parseInt(e.key, 10) - 1];
    if(row) row.click();
  }
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
  const hours = Math.floor(mins/60);
  if(hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours/24);
  if(days < 7) return `${days} day${days===1?'':'s'} ago`;
  const weeks = Math.floor(days/7);
  if(weeks < 5) return `${weeks} week${weeks===1?'':'s'} ago`;
  const months = Math.floor(days/30);
  return `${months} month${months===1?'':'s'} ago`;
}

// grading-urgency dot for a pending item: green while fresh, amber once it's
// been waiting a while, red once it's been waiting a long time.
function urgencyLevel(iso){
  if(!iso) return 'ok';
  const mins = Math.max(0, Math.round((Date.now()-new Date(iso).getTime())/60000));
  if(mins < 120) return 'ok';
  if(mins < 1440) return 'warn';
  return 'urgent';
}

const AVATAR_COLORS = ['#2563eb','#7c5cff','#0f9d6e','#d63b3b','#b6650a','#0891b2','#c2410c','#65a30d'];
function avatarColor(name){
  let h = 0;
  for(let i=0;i<(name||'').length;i++){ h = (h*31 + name.charCodeAt(i)) >>> 0; }
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}
function initialsOf(name){
  const parts = (name||'').trim().split(/\\s+/).filter(Boolean);
  if(parts.length===0) return '?';
  if(parts.length===1) return parts[0].slice(0,2).toUpperCase();
  return (parts[0][0]+parts[parts.length-1][0]).toUpperCase();
}

let pendingInitialLoad = true;
async function loadPending(){
  const list = $('#pending-list');
  const errBox = $('#pending-error');
  const emptyBox = $('#pending-empty');
  if(pendingInitialLoad){
    list.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
    emptyBox.style.display = 'none';
  }
  let data;
  try{
    data = await apiGet('/api/pending');
  }catch(e){
    if(e.message === 'auth') return;
    errBox.style.display='block';
    errBox.textContent = "Couldn't reach the server — retrying shortly.";
    return;
  }
  pendingInitialLoad = false;
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
        <div class="avatar"></div>
        <div class="pi-info">
          <div class="u"></div>
          <div class="t"><span class="tdot"></span><span class="t-label"></span></div>
        </div>
      </div>`);
    const avatar = item.querySelector('.avatar');
    avatar.textContent = initialsOf(p.username);
    avatar.style.background = avatarColor(p.username || '');
    item.querySelector('.u').textContent = p.username;
    item.querySelector('.tdot').classList.add(urgencyLevel(p.submitted_at));
    item.querySelector('.t-label').textContent = timeAgo(p.submitted_at);
    item.addEventListener('click', ()=>selectResponse(p.response_id));
    list.appendChild(item);
  });
}

function hasUnsavedMarks(){
  return !!currentResponse && wrongIds.size > 0;
}

async function selectResponse(rid){
  if(currentResponse && currentResponse.response_id === rid) return;
  if(hasUnsavedMarks()){
    const ok = await confirmDialog(
      'Switch exams?',
      `You've marked ${wrongIds.size} question${wrongIds.size===1?'':'s'} wrong for ${currentResponse.username} that hasn't been saved yet. Switching now will discard those marks.`,
      'Switch anyway', 'red'
    );
    if(!ok) return;
  }
  $('#placeholder').style.display='none';
  $('#grading-body').style.display='none';
  $('#grading-skeleton').style.display='flex';
  let data;
  try{
    data = await apiGet('/api/response/'+encodeURIComponent(rid));
  }catch(e){
    $('#grading-skeleton').style.display='none';
    if(e.message === 'auth') return;
    if(!currentResponse) $('#placeholder').style.display='flex';
    toast("Couldn't load that exam — check your connection.", 'error');
    return;
  }
  if(data.error){
    $('#grading-skeleton').style.display='none';
    if(!currentResponse) $('#placeholder').style.display='flex';
    toast(data.error, 'error');
    return;
  }
  currentResponse = data;
  wrongIds = new Set();
  renderGrading();
  loadPending();
  $('#fab').removeAttribute('disabled');
}

function renderGrading(){
  $('#placeholder').style.display='none';
  $('#grading-skeleton').style.display='none';
  $('#grading-body').style.display='flex';
  $('#g-username').textContent = currentResponse.username;
  $('#g-submitted').textContent = 'Submitted ' + timeAgo(currentResponse.submitted_at);
  updateWrongCount();
  const box = $('#questions');
  box.innerHTML='';
  currentResponse.questions.forEach((q, idx)=>{
    const hasChoices = !!(q.type && q.options && q.options.length);
    const row = el(`<div class="qrow" data-id="${q.id}">
        <div class="qrow-head">
          <span class="tag">Question ${idx+1}</span>
          <div class="points-edit" title="Points awarded to THIS student for this question">
            <input type="number" class="points-input" min="0" step="1" placeholder="pts" value="${q.awarded != null ? q.awarded : (q.points != null ? q.points : '')}">
            <button type="button" class="iconbtn points-sync-btn" title="Push this score to the Form for this response">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M20 6L9 17l-5-5"/></svg>
            </button>
            <span class="points-sync-status"></span>
            <div class="points-base-wrap">
              <span class="points-base-view">/<span class="points-base-value">${q.points != null ? q.points : '—'}</span></span>
              <input type="number" class="points-base-input" min="0" step="1" value="${q.points != null ? q.points : ''}" style="display:none;">
              <button type="button" class="iconbtn points-base-edit" title="Edit the max point value for this question in the Form (affects every response)">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>
              </button>
              <button type="button" class="iconbtn points-base-save" title="Push this value to the Form (changes it for everyone)" style="display:none;">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M20 6L9 17l-5-5"/></svg>
              </button>
            </div>
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
    const syncBtn = row.querySelector('.points-sync-btn');
    const syncStatus = row.querySelector('.points-sync-status');
    pointsInput.addEventListener('click', (e)=>e.stopPropagation());
    pointsInput.addEventListener('input', ()=>{
      syncBtn.classList.remove('is-synced');
      syncStatus.textContent = '';
      syncStatus.className = 'points-sync-status';
    });
    if(q.awarded != null){
      syncBtn.classList.add('is-synced');
      syncStatus.textContent = 'Synced';
      syncStatus.className = 'points-sync-status ok';
    }
    syncBtn.addEventListener('click', async (e)=>{
      e.stopPropagation();
      const val = pointsInput.value;
      if(val === ''){ toast('Enter a score first', 'error'); return; }
      const newScore = parseFloat(val);
      if(isNaN(newScore) || newScore < 0){ toast('Score must be 0 or more', 'error'); return; }
      syncBtn.disabled = true;
      syncStatus.textContent = 'Syncing…';
      syncStatus.className = 'points-sync-status';
      try{
        const res = await apiPost('/api/set_response_score', {
          response_id: currentResponse.response_id,
          question_id: q.id,
          score: newScore
        });
        if(res.error){
          syncStatus.textContent = 'Failed';
          syncStatus.className = 'points-sync-status err';
          toast(res.error, 'error');
          return;
        }
        q.awarded = res.score;
        syncBtn.classList.add('is-synced');
        syncStatus.textContent = 'Synced';
        syncStatus.className = 'points-sync-status ok';
        toast('Score pushed to the Form', 'success');
      }catch(err){
        syncStatus.textContent = 'Failed';
        syncStatus.className = 'points-sync-status err';
        if(err.message !== 'auth') toast("Couldn't reach the server — check your connection.", 'error');
      }finally{
        syncBtn.disabled = false;
      }
    });

    const baseView = row.querySelector('.points-base-view');
    const baseValueSpan = row.querySelector('.points-base-value');
    const baseInput = row.querySelector('.points-base-input');
    const baseEditBtn = row.querySelector('.points-base-edit');
    const baseSaveBtn = row.querySelector('.points-base-save');
    baseInput.addEventListener('click', (e)=>e.stopPropagation());
    baseEditBtn.addEventListener('click', (e)=>{
      e.stopPropagation();
      baseView.style.display = 'none';
      baseEditBtn.style.display = 'none';
      baseInput.style.display = 'inline-block';
      baseSaveBtn.style.display = 'inline-flex';
      baseInput.focus();
      baseInput.select();
    });
    baseSaveBtn.addEventListener('click', async (e)=>{
      e.stopPropagation();
      const val = baseInput.value;
      if(val === ''){ toast('Enter a point value first', 'error'); return; }
      const newPoints = parseInt(val, 10);
      if(isNaN(newPoints) || newPoints < 0){ toast('Points must be 0 or more', 'error'); return; }
      const res = await apiPost('/api/set_points', {question_id: q.id, points: newPoints});
      if(res.error){ toast(res.error, 'error'); return; }
      q.points = newPoints;
      baseValueSpan.textContent = newPoints;
      baseInput.style.display = 'none';
      baseSaveBtn.style.display = 'none';
      baseView.style.display = 'inline';
      baseEditBtn.style.display = 'inline-flex';
      toast('Point value saved to the Form', 'success');
    });
    row.addEventListener('click', ()=>{
      if(wrongIds.has(q.id)){ wrongIds.delete(q.id); row.classList.remove('wrong'); }
      else { wrongIds.add(q.id); row.classList.add('wrong'); }
      updateWrongCount();
    });
    box.appendChild(row);
  });
}

function updateWrongCount(){
  const badge = $('#wrong-count');
  if(!badge) return;
  const n = wrongIds.size;
  badge.textContent = n + (n === 1 ? ' marked wrong' : ' marked wrong');
  badge.classList.toggle('has-wrong', n > 0);
}

$('#clear-marks-btn').addEventListener('click', ()=>{
  if(wrongIds.size === 0) return;
  wrongIds.clear();
  $$('.qrow.wrong').forEach(r=>r.classList.remove('wrong'));
  updateWrongCount();
  toast('Cleared all marks for this exam');
});

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

window.addEventListener('beforeunload', (e)=>{
  if(hasUnsavedMarks()){
    e.preventDefault();
    e.returnValue = '';
  }
});

function resetGradingPanel(){
  currentResponse = null;
  wrongIds = new Set();
  $('#grading-body').style.display='none';
  $('#grading-skeleton').style.display='none';
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

let submittingGrade = false;
async function submitGrade(result){
  if(submittingGrade) return;
  submittingGrade = true;
  closeModal('ov-result');
  const savedResponse = currentResponse;
  const wrongTitles = savedResponse.questions
    .filter(q=>wrongIds.has(q.id))
    .map(q=>q.title);
  const pointsAssigned = {};
  $$('.qrow').forEach(row=>{
    const qid = row.dataset.id;
    const input = row.querySelector('.points-input');
    if(input && input.value !== '') pointsAssigned[qid] = parseInt(input.value, 10);
  });
  try{
    const data = await apiPost('/api/grade', {
      response_id: savedResponse.response_id,
      username: savedResponse.username,
      wrong_questions: wrongTitles,
      points_assigned: pointsAssigned,
      result
    });
    if(data.error){ toast(data.error, 'error'); return; }
    $('#msg-title').textContent = result==='fail' ? 'Exam failed' : 'Exam passed';
    $('#msg-text').value = data.message;
    openModal('ov-message');
    resetGradingPanel();
    loadPending();
    loadRecent();
  }catch(e){
    if(e.message !== 'auth') toast("Couldn't save the grade — check your connection and try again.", 'error');
  }finally{
    submittingGrade = false;
  }
}
$('#result-fail').addEventListener('click', ()=>submitGrade('fail'));
$('#result-pass').addEventListener('click', ()=>submitGrade('pass'));

$('#msg-copy').addEventListener('click', async ()=>{
  try{
    await navigator.clipboard.writeText($('#msg-text').value);
    toast('Message copied to clipboard', 'success');
  }catch(e){
    toast("Couldn't copy — select the text and copy manually.", 'error');
  }
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
  const target = currentResponse;
  const ok = await confirmDialog(
    'Discard without grading?',
    `${target.username}'s exam will be removed from the pending list and can't be graded later.`,
    'Discard', 'red'
  );
  if(!ok) return;
  try{
    await apiPost('/api/delete', {response_id: target.response_id, username: target.username});
    resetGradingPanel();
    loadPending();
    toast('Exam discarded', 'success');
  }catch(e){
    if(e.message !== 'auth') toast("Couldn't discard the exam — check your connection.", 'error');
  }
});

let recentInitialLoad = true;
async function loadRecent(){
  const list = $('#recent-list');
  if(recentInitialLoad){
    list.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
    $('#recent-empty').style.display = 'none';
  }
  let data;
  try{
    data = await apiGet('/api/recent');
  }catch(e){
    return; // silent — this list is non-critical and will retry on the next poll
  }
  recentInitialLoad = false;
  list.innerHTML='';
  const items = data.recent || [];
  $('#recent-count').textContent = items.length;
  $('#recent-empty').style.display = items.length===0 ? 'block':'none';
  items.forEach(r=>{
    const row = el(`<div class="recent-item">
        <div class="ri-left">
          <div class="avatar"></div>
          <div class="ri-info">
            <div class="u"></div>
            <div class="meta"></div>
          </div>
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
    const ravatar = row.querySelector('.avatar');
    ravatar.textContent = initialsOf(r.username);
    ravatar.style.background = avatarColor(r.username || '');
    row.querySelector('.u').textContent = r.username;
    row.querySelector('.meta').textContent = `${timeAgo(r.graded_at)} · hides in ${Math.floor(r.minutes_left/60)}h ${r.minutes_left%60}min`;
    row.querySelector('[data-act="view"]').addEventListener('click', ()=>{
      $('#msg-title').textContent = r.result==='fail' ? 'Exam failed' : 'Exam passed';
      $('#msg-text').value = r.message;
      openModal('ov-message');
    });
    row.querySelector('[data-act="del"]').addEventListener('click', async ()=>{
      const ok = await confirmDialog(
        'Delete this record?',
        `This removes ${r.username}'s graded record from the recently graded list.`,
        'Delete', 'red'
      );
      if(!ok) return;
      try{
        await apiPost('/api/delete', {response_id: r.response_id});
        loadRecent();
        toast('Record deleted', 'success');
      }catch(e){
        if(e.message !== 'auth') toast("Couldn't delete — check your connection.", 'error');
      }
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
    $('#app').style.display='grid';
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
  --bg:#e9ebef; --card:#ffffff; --border:#d5d9e0; --text:#1f2430; --text-dim:#707685;
  --blue:#2563eb; --accent:#6d28d9; --radius:14px;
}
[data-theme="dark"]{
  --bg:#0b0d12; --card:#141824; --border:#242a38; --text:#e7e9ee; --text-dim:#96a0b3;
  --blue:#5b93ff; --accent:#a78bfa;
}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;}
body{
  background:var(--bg); color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  line-height:1.6;
}
.legal-topbar{display:flex;align-items:center;justify-content:space-between;max-width:980px;margin:0 auto;padding:22px 20px 0;}
.legal-topbar a{color:var(--text-dim);text-decoration:none;font-size:.85rem;font-weight:600;display:flex;align-items:center;gap:6px;}
.legal-topbar a:hover{color:var(--accent);}
.theme-toggle{background:var(--card);border:1px solid var(--border);border-radius:8px;color:var(--text-dim);padding:6px;cursor:pointer;display:flex;align-items:center;justify-content:center;}
.theme-toggle:hover{border-color:var(--accent);color:var(--accent);}
.theme-toggle .icon-moon{display:none;}
[data-theme="dark"] .theme-toggle .icon-sun{display:none;}
[data-theme="dark"] .theme-toggle .icon-moon{display:block;}
.legal-shell{max-width:980px;margin:0 auto;padding:28px 20px 70px;display:grid;grid-template-columns:200px 1fr;gap:32px;align-items:start;}
.legal-toc{position:sticky;top:24px;display:flex;flex-direction:column;gap:1px;}
.legal-toc-title{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--text-dim);margin:0 0 8px 10px;}
.legal-toc a{color:var(--text-dim);text-decoration:none;font-size:.85rem;line-height:1.35;padding:6px 10px;border-radius:8px;border-left:2px solid transparent;transition:.15s;}
.legal-toc a:hover{color:var(--text);background:var(--card);}
.legal-toc a.active{color:var(--accent);border-left-color:var(--accent);background:var(--card);font-weight:600;}
.legal-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:38px 34px;min-width:0;}
.legal-card h1{font-size:1.5rem;margin:0 0 6px;}
.legal-updated{color:var(--text-dim);font-size:.82rem;margin:0 0 26px;}
.legal-card h2{font-size:1.02rem;margin:28px 0 10px;padding-top:20px;border-top:1px solid var(--border);scroll-margin-top:24px;}
.legal-card h2:first-of-type{border-top:none;padding-top:0;margin-top:0;}
.legal-card p, .legal-card li{color:var(--text-dim);font-size:.92rem;overflow-wrap:anywhere;}
.legal-card ul{padding-left:20px;margin:8px 0;}
.legal-card li{margin-bottom:4px;}
.legal-card a{color:var(--accent);}
.legal-card a:hover{text-decoration:none;}
.legal-card strong{color:var(--text);}
.legal-card code{
  background:#161a24;color:#c9d1e0;border:1px solid rgba(255,255,255,.08);
  border-radius:6px;padding:2px 7px;font-size:.85em;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
.legal-footer{display:flex;justify-content:center;gap:10px;margin-top:26px;font-size:.85rem;}
.legal-footer a{color:var(--text-dim);text-decoration:none;padding:8px 14px;border:1px solid var(--border);border-radius:20px;transition:.15s;}
.legal-footer a:hover{border-color:var(--accent);color:var(--accent);}
@media (max-width:760px){
  .legal-shell{grid-template-columns:1fr;padding-top:20px;}
  .legal-toc{position:static;flex-direction:row;flex-wrap:wrap;gap:6px;}
  .legal-toc-title{display:none;}
  .legal-toc a{border-left:none;border-bottom:2px solid transparent;}
  .legal-toc a.active{border-left-color:transparent;border-bottom-color:var(--accent);}
  .legal-card{padding:26px 20px;}
}
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
<div class="legal-shell">
  <nav class="legal-toc" id="legal-toc">
    <div class="legal-toc-title">On this page</div>
  </nav>
  <div>
    <div class="legal-card" id="legal-card">
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
</div>
<script>
document.getElementById('theme-toggle').addEventListener('click', function(){
  var current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  var next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try{ localStorage.setItem('eot-theme', next); }catch(e){}
});
// Build the table of contents from the section headings, and highlight
// whichever section is currently in view as the user scrolls.
(function(){
  var card = document.getElementById('legal-card');
  var toc = document.getElementById('legal-toc');
  var heads = Array.prototype.slice.call(card.querySelectorAll('h2'));
  var links = heads.map(function(h, i){
    var id = 'sec-' + i;
    h.id = id;
    var a = document.createElement('a');
    a.href = '#' + id;
    a.textContent = h.textContent;
    toc.appendChild(a);
    return a;
  });
  if(!links.length) return;
  var setActive = function(id){
    links.forEach(function(a){ a.classList.toggle('active', a.getAttribute('href') === '#' + id); });
  };
  setActive(heads[0].id);
  if('IntersectionObserver' in window){
    var obs = new IntersectionObserver(function(entries){
      entries.forEach(function(entry){
        if(entry.isIntersecting){ setActive(entry.target.id); }
      });
    }, {rootMargin:'-15% 0px -70% 0px'});
    heads.forEach(function(h){ obs.observe(h); });
  }
})();
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
        <li><strong>Google Forms & Drive Metadata:</strong> Used to fetch student submissions from linked Google Forms for automatic response evaluation, and, only when an administrator explicitly chooses to, to update a question's point value in the form.</li>
    </ul>

    <h2>2. Data Usage & Sharing</h2>
    <p>Accessed data is processed only to evaluate exam answers, flag incorrect questions, generate text feedback for applicants, and, when explicitly requested by an administrator, update a question's point value on the linked form. We do not sell, rent, or share user data with any third parties.</p>

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
