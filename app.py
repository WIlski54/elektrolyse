import json
import os
import sqlite3
import uuid
from datetime import datetime, date
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO, emit, join_room

try:
    from google import genai
except Exception:  # pragma: no cover - optional dependency in local dry runs
    genai = None


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "elektrolyse.sqlite3"

SYSTEM_PROMPT = """Du bist ein fachlich praeziser KI-Tutor fuer Chemie in der Oberstufe.
Du hilfst beim forschenden Erschliessen der Elektrolyse, gibst aber nicht sofort fertige Loesungen.
Arbeite mit Rueckfragen, Beobachtungen und Fachbegriffen: Elektrolyse, galvanische Zelle, Anode,
Kathode, Pluspol, Minuspol, Oxidation, Reduktion, Elektronenfluss, Ionenwanderung, Elektrolyt,
Spannungsquelle, Zersetzung, Redoxreaktion, Halbreaktion, Elektronenbilanz, Oxidationsmittel,
Reduktionsmittel, Ueberspannung und Konkurrenzreaktion.
Antworte auf Deutsch, klar, anspruchsvoll und knapp. Wenn Schueler unsicher sind, fuehre sie
ueber Beobachtung -> Deutung -> Fachbegriff -> begruendetes Modell.
Nutze fuer chemische Gleichungen und Potentiale saubere LaTeX-Schreibweise mit $...$ und fuer Hervorhebungen Markdown."""

KORREKTUR_PROMPT = """Du korrigierst Schuelerantworten zur Elektrolyse auf Oberstufenniveau.
Bewerte fachlich fair, nenne richtige Begriffe und gib bei Luecken einen anspruchsvollen, hilfreichen Tipp.
Antworte nur als JSON ohne Markdown:
{"correct": bool, "feedback": "dein Text", "hint": "optionaler Tipp"}"""


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")
ACTIVE_TEACHER_TOKENS = set()


def get_db():
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS schueler (
                id TEXT PRIMARY KEY,
                pseudonym TEXT NOT NULL,
                klasse TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS antworten (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schueler_id TEXT NOT NULL,
                aufgabe_nr TEXT NOT NULL,
                niveau TEXT NOT NULL,
                antwort TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (schueler_id) REFERENCES schueler(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS fortschritt (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schueler_id TEXT NOT NULL,
                aufgabe_nr TEXT NOT NULL,
                niveau TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (schueler_id, aufgabe_nr, niveau),
                FOREIGN KEY (schueler_id) REFERENCES schueler(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS ki_anfragen (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schueler_id TEXT NOT NULL,
                status TEXT NOT NULL,
                grund TEXT NOT NULL,
                created_at TEXT NOT NULL,
                decided_at TEXT,
                FOREIGN KEY (schueler_id) REFERENCES schueler(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS token_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schueler_id TEXT NOT NULL,
                tokens INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schueler_id TEXT NOT NULL,
                request_id INTEGER,
                role TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (schueler_id) REFERENCES schueler(id) ON DELETE CASCADE
            );
            """
        )


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def require_student():
    sid = session.get("schueler_id")
    if not sid:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM schueler WHERE id = ?", (sid,)).fetchone()
        if row:
            db.execute("UPDATE schueler SET last_seen = ? WHERE id = ?", (now_iso(), sid))
        return row


def require_teacher():
    return bool(session.get("lehrer"))


def issue_teacher_action_token():
    token = session.get("teacher_action_token")
    if not token or token not in ACTIVE_TEACHER_TOKENS:
        token = uuid.uuid4().hex
        ACTIVE_TEACHER_TOKENS.add(token)
        session["teacher_action_token"] = token
    return token


def revoke_teacher_action_token(token=None):
    token = token or request.headers.get("X-Teacher-Token", "") or request.form.get("teacher_token", "")
    token = token or session.get("teacher_action_token")
    if token:
        ACTIVE_TEACHER_TOKENS.discard(token)


def require_teacher_action():
    token = request.headers.get("X-Teacher-Token", "") or request.form.get("teacher_token", "")
    return require_teacher() or token in ACTIVE_TEACHER_TOKENS


def row_to_dict(row):
    return dict(row) if row else None


def clear_classroom_activity():
    with get_db() as db:
        student_ids = [row["id"] for row in db.execute("SELECT id FROM schueler").fetchall()]
        for table in ("chat_messages", "token_log", "ki_anfragen", "fortschritt", "antworten", "schueler"):
            db.execute(f"DELETE FROM {table}")
    for sid in student_ids:
        socketio.emit(
            "session_reset",
            {"message": "Die Unterrichtssitzung wurde durch die Lehrkraft beendet."},
            to=f"schueler_{sid}",
        )
    socketio.emit("classroom_reset", {"message": "Alle Schueleraktivitaeten wurden geloescht."}, to="lehrer_room")


def get_student_row(sid):
    with get_db() as db:
        row = db.execute(
            """
            SELECT s.*,
                   COUNT(DISTINCT f.aufgabe_nr || ':' || f.niveau) AS erledigt,
                   (SELECT COUNT(*) FROM ki_anfragen k
                    WHERE k.schueler_id = s.id AND k.status = 'offen') AS offene_ki,
                   (SELECT COUNT(*) FROM chat_messages c
                    WHERE c.schueler_id = s.id) AS chat_count
            FROM schueler s
            LEFT JOIN fortschritt f ON f.schueler_id = s.id AND f.status = 'gespeichert'
            WHERE s.id = ?
            GROUP BY s.id
            """,
            (sid,),
        ).fetchone()
    return row_to_dict(row)


def get_student_summary():
    with get_db() as db:
        students = db.execute(
            """
            SELECT s.*,
                   COUNT(DISTINCT f.aufgabe_nr || ':' || f.niveau) AS erledigt,
                   (SELECT COUNT(*) FROM ki_anfragen k
                    WHERE k.schueler_id = s.id AND k.status = 'offen') AS offene_ki,
                   (SELECT COUNT(*) FROM chat_messages c
                    WHERE c.schueler_id = s.id) AS chat_count
            FROM schueler s
            LEFT JOIN fortschritt f ON f.schueler_id = s.id AND f.status = 'gespeichert'
            GROUP BY s.id
            ORDER BY s.last_seen DESC
            """
        ).fetchall()
        open_requests = db.execute("SELECT COUNT(*) AS n FROM ki_anfragen WHERE status = 'offen'").fetchone()["n"]
        today = date.today().isoformat()
        tokens_today = db.execute(
            "SELECT COALESCE(SUM(tokens), 0) AS n FROM token_log WHERE created_at LIKE ?",
            (f"{today}%",),
        ).fetchone()["n"]
    return [row_to_dict(s) for s in students], open_requests, tokens_today


def get_open_ki_requests():
    with get_db() as db:
        rows = db.execute(
            """
            SELECT k.*, s.pseudonym, s.klasse
            FROM ki_anfragen k
            JOIN schueler s ON s.id = k.schueler_id
            WHERE k.status = 'offen'
            ORDER BY k.created_at DESC
            """
        ).fetchall()
    return [row_to_dict(r) for r in rows]


def gemini_text(prompt, history=None):
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key or genai is None:
        return (
            "Ich kann gerade ohne hinterlegten GEMINI_API_KEY nur als Platzhalter antworten. "
            "Fachlicher Tipp: Beschreibe erst, was du an den Elektroden siehst, und ordne dann "
            "Elektronenfluss, Ionenwanderung, Oxidation und Reduktion zu."
        ), 0

    client = genai.Client(api_key=api_key)
    history_text = ""
    if history:
        history_text = "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in history[-6:])
    response = client.models.generate_content(
        model=os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview"),
        contents=f"{SYSTEM_PROMPT}\n\nBisheriger Chat:\n{history_text}\n\nSchuelerfrage:\n{prompt}",
    )
    text = getattr(response, "text", "") or "Ich brauche noch eine genauere Beobachtung von dir."
    token_count = len(text.split()) + len(prompt.split())
    return text, token_count


@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pseudonym = request.form.get("pseudonym", "").strip()
        klasse = request.form.get("klasse", "").strip()
        if not pseudonym or not klasse:
            return render_template("login.html", error="Bitte Pseudonym und Klasse eintragen.")
        sid = str(uuid.uuid4())
        with get_db() as db:
            db.execute(
                "INSERT INTO schueler (id, pseudonym, klasse, created_at, last_seen) VALUES (?, ?, ?, ?, ?)",
                (sid, pseudonym[:40], klasse[:20], now_iso(), now_iso()),
            )
        session.clear()
        session["schueler_id"] = sid
        socketio.emit("student_joined", {"student": get_student_row(sid)}, to="lehrer_room")
        return redirect(url_for("arbeitsblatt"))
    return render_template("login.html")


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/arbeitsblatt", methods=["GET", "POST"])
def arbeitsblatt():
    student = require_student()
    if not student:
        return redirect(url_for("login"))
    return render_template("index.html", student=student)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/lehrer", methods=["GET", "POST"])
def lehrer_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == os.environ.get("LEHRER_PASSWORD", "gsm"):
            session["lehrer"] = True
            issue_teacher_action_token()
            return redirect(url_for("dashboard"))
        return render_template("lehrer_login.html", error="Das Passwort stimmt nicht.")
    return render_template("lehrer_login.html")


@app.route("/dashboard")
def dashboard():
    if not require_teacher():
        return redirect(url_for("lehrer_login"))
    teacher_action_token = issue_teacher_action_token()
    students, open_requests, tokens_today = get_student_summary()
    return render_template(
        "dashboard.html",
        students=students,
        open_requests=open_requests,
        ki_requests=get_open_ki_requests(),
        teacher_action_token=teacher_action_token,
        tokens_today=tokens_today,
        total_tasks=6,
    )


@app.post("/api/reset-classroom")
def reset_classroom():
    if not require_teacher_action():
        return jsonify({"blocked": True}), 401
    clear_classroom_activity()
    return jsonify({"ok": True})


@app.route("/lehrer/logout", methods=["POST", "GET"])
def lehrer_logout():
    if require_teacher_action():
        clear_classroom_activity()
    revoke_teacher_action_token()
    session.clear()
    return redirect(url_for("lehrer_login"))


@app.route("/schueler/<sid>")
def schueler_detail(sid):
    if not require_teacher():
        return redirect(url_for("lehrer_login"))
    teacher_action_token = issue_teacher_action_token()
    with get_db() as db:
        student = db.execute("SELECT * FROM schueler WHERE id = ?", (sid,)).fetchone()
        answers = db.execute(
            "SELECT * FROM antworten WHERE schueler_id = ? ORDER BY created_at DESC",
            (sid,),
        ).fetchall()
        requests = db.execute(
            "SELECT * FROM ki_anfragen WHERE schueler_id = ? ORDER BY created_at DESC",
            (sid,),
        ).fetchall()
        chats = db.execute(
            "SELECT * FROM chat_messages WHERE schueler_id = ? ORDER BY created_at DESC",
            (sid,),
        ).fetchall()
    if not student:
        return redirect(url_for("dashboard"))
    return render_template(
        "schueler_detail.html",
        student=student,
        answers=[row_to_dict(a) for a in answers],
        requests=[row_to_dict(r) for r in requests],
        chats=[row_to_dict(c) for c in chats],
        teacher_action_token=teacher_action_token,
    )


@app.post("/api/answer")
def save_answer():
    student = require_student()
    if not student:
        return jsonify({"blocked": True, "error": "Nicht angemeldet."}), 401
    data = request.get_json(force=True)
    task = str(data.get("task", ""))[:20]
    niveau = str(data.get("niveau", "B"))[:1]
    answer = str(data.get("answer", "")).strip()
    if not task or not answer:
        return jsonify({"ok": False, "error": "Bitte erst eine Antwort eintragen."}), 400
    with get_db() as db:
        db.execute(
            "INSERT INTO antworten (schueler_id, aufgabe_nr, niveau, antwort, created_at) VALUES (?, ?, ?, ?, ?)",
            (student["id"], task, niveau, answer, now_iso()),
        )
        db.execute(
            """
            INSERT INTO fortschritt (schueler_id, aufgabe_nr, niveau, status, updated_at)
            VALUES (?, ?, ?, 'gespeichert', ?)
            ON CONFLICT(schueler_id, aufgabe_nr, niveau)
            DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at
            """,
            (student["id"], task, niveau, now_iso()),
        )
    payload = {
        "student": get_student_row(student["id"]),
        "task": task,
        "niveau": niveau,
        "answer": answer,
        "created_at": now_iso(),
    }
    socketio.emit("student_answer", payload, to="lehrer_room")
    return jsonify({"ok": True})


@app.get("/api/progress")
def progress():
    student = require_student()
    if not student:
        return jsonify({"blocked": True}), 401
    with get_db() as db:
        rows = db.execute(
            "SELECT aufgabe_nr, niveau, status FROM fortschritt WHERE schueler_id = ?",
            (student["id"],),
        ).fetchall()
    return jsonify({"items": [row_to_dict(r) for r in rows]})


@app.post("/api/ki-anfrage")
def request_ki():
    student = require_student()
    if not student:
        return jsonify({"blocked": True}), 401
    data = request.get_json(force=True)
    reason = str(data.get("reason", "Ich brauche Hilfe bei der Elektrolyse."))[:240]
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO ki_anfragen (schueler_id, status, grund, created_at) VALUES (?, 'offen', ?, ?)",
            (student["id"], reason, now_iso()),
        )
        request_id = cur.lastrowid
    socketio.emit(
        "ki_request",
        {"id": request_id, "student": row_to_dict(student), "grund": reason, "created_at": now_iso()},
        to="lehrer_room",
    )
    return jsonify({"ok": True, "request_id": request_id, "status": "offen"})


@app.post("/api/ki-anfrage/<int:request_id>/<decision>")
def decide_ki(request_id, decision):
    if not require_teacher_action():
        return jsonify({"blocked": True}), 401
    status = "genehmigt" if decision == "approve" else "abgelehnt"
    with get_db() as db:
        row = db.execute("SELECT * FROM ki_anfragen WHERE id = ?", (request_id,)).fetchone()
        if not row:
            return jsonify({"ok": False}), 404
        db.execute(
            "UPDATE ki_anfragen SET status = ?, decided_at = ? WHERE id = ?",
            (status, now_iso(), request_id),
        )
    student = get_student_row(row["schueler_id"])
    socketio.emit(
        "ki_decision",
        {"request_id": request_id, "status": status},
        to=f"schueler_{row['schueler_id']}",
    )
    socketio.emit(
        "ki_request_decided",
        {"request_id": request_id, "status": status, "student": student},
        to="lehrer_room",
    )
    return jsonify({"ok": True, "status": status, "student": student})


@app.post("/api/chat")
def chat():
    student = require_student()
    if not student:
        return jsonify({"blocked": True}), 401
    data = request.get_json(force=True)
    request_id = data.get("request_id")
    with get_db() as db:
        approved = db.execute(
            "SELECT * FROM ki_anfragen WHERE id = ? AND schueler_id = ? AND status = 'genehmigt'",
            (request_id, student["id"]),
        ).fetchone()
    if not approved:
        return jsonify({"blocked": True, "error": "Die KI-Hilfe muss erst freigegeben werden."}), 403
    answer, tokens = gemini_text(str(data.get("message", "")), data.get("history", []))
    if tokens:
        with get_db() as db:
            db.execute(
                "INSERT INTO token_log (schueler_id, tokens, created_at) VALUES (?, ?, ?)",
                (student["id"], tokens, now_iso()),
            )
    created_at = now_iso()
    with get_db() as db:
        db.execute(
            "INSERT INTO chat_messages (schueler_id, request_id, role, message, created_at) VALUES (?, ?, 'user', ?, ?)",
            (student["id"], request_id, str(data.get("message", ""))[:2000], created_at),
        )
        db.execute(
            "INSERT INTO chat_messages (schueler_id, request_id, role, message, created_at) VALUES (?, ?, 'assistant', ?, ?)",
            (student["id"], request_id, answer[:4000], now_iso()),
        )
    socketio.emit(
        "ki_chat_message",
        {
            "student": get_student_row(student["id"]),
            "request_id": request_id,
            "user_message": str(data.get("message", ""))[:2000],
            "assistant_message": answer[:4000],
            "created_at": created_at,
        },
        to="lehrer_room",
    )
    return jsonify({"ok": True, "answer": answer})


@app.post("/api/check-answer")
def check_answer():
    student = require_student()
    if not student:
        return jsonify({"blocked": True}), 401
    data = request.get_json(force=True)
    prompt = (
        f"{KORREKTUR_PROMPT}\n\nAufgabe: {data.get('question', '')}\n"
        f"Kontext: {data.get('context', '')}\nSchuelerantwort: {data.get('answer', '')}"
    )
    text, _tokens = gemini_text(prompt, [])
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = {
            "correct": False,
            "feedback": "Ich kann deine Antwort noch nicht sicher bewerten.",
            "hint": "Pruefe, ob du Beobachtung und Deutung getrennt hast.",
        }
    return jsonify(parsed)


@socketio.on("connect")
def socket_connect():
    sid = session.get("schueler_id")
    if session.get("lehrer"):
        join_room("lehrer_room")
        emit("connected", {"room": "lehrer_room"})
    if sid:
        join_room(f"schueler_{sid}")
        emit("connected", {"room": f"schueler_{sid}"})


@socketio.on("watch_student")
def watch_student(data):
    if not session.get("lehrer"):
        return
    sid = str(data.get("sid", ""))
    if sid:
        join_room(f"watch_{sid}")
        emit("watching", {"sid": sid})


if __name__ == "__main__":
    init_db()
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=debug,
        allow_unsafe_werkzeug=True,
    )
