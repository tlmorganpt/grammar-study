#!/usr/bin/env python3
"""
Essential Grammar in Use — Método de Estudo em 4 Passos
Servidor local que abre no browser. Sem dependências além de Python 3.
"""

import http.server, socketserver, urllib.parse, zipfile, sqlite3, json
import mimetypes, os, shutil, tempfile, threading, webbrowser, signal, sys, re
import hashlib, uuid
from datetime import date

APKG   = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "Anki", "English_Essential_Grammar_In_Use_Theory.apkg")
APKG_EX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "Anki", "English_Grammar_In_Use_Exercises.apkg")
TRACKER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       ".llm", "UNIT_TRACKER.md")
_BASE     = os.path.dirname(os.path.abspath(__file__))
_DATA     = os.environ.get('DATA_DIR', _BASE)
DB_FILE   = os.path.join(_DATA, '.anki_users.db')
OUT_DIR   = os.path.join(_DATA, 'anki_output')
PORT      = int(os.environ.get('PORT', 7654))
_LOCAL    = not os.environ.get('PORT')  # False when running on Render/Fly
TMP       = None
TMP_EX    = None
DECK      = {}
DECK_EX   = {}
CUSTOM_EX = {}
SESSIONS  = {}  # token -> username


def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            hash     TEXT NOT NULL,
            created  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS progress (
            username TEXT PRIMARY KEY REFERENCES users(username) ON DELETE CASCADE,
            done     TEXT NOT NULL DEFAULT '[]'
        );
    """)
    conn.commit()
    conn.close()


def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def hash_pwd(pwd):
    return hashlib.sha256(pwd.encode("utf-8")).hexdigest()


def get_token_user(handler):
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return SESSIONS.get(auth[7:])
    return None


# ─── Carregamento ─────────────────────────────────────────────────────────────

def load_deck(apkg_path):
    global TMP
    TMP = tempfile.mkdtemp(prefix="anki_reader_")
    with zipfile.ZipFile(apkg_path, "r") as z:
        z.extractall(TMP)

    db_path = next(
        (os.path.join(TMP, n) for n in
         ("collection.anki2", "collection.anki21", "collection.anki21b")
         if os.path.exists(os.path.join(TMP, n))),
        None
    )
    if not db_path:
        sys.exit("❌  Base de dados Anki não encontrada no .apkg")

    with open(os.path.join(TMP, "media")) as f:
        media_map = json.load(f)

    fname_to_key = {v: k for k, v in media_map.items()}
    mime_for_key = {k: (mimetypes.guess_type(v)[0] or "application/octet-stream")
                    for k, v in media_map.items()}

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT flds FROM notes").fetchall()
    conn.close()

    units = {}
    for (flds,) in rows:
        f = flds.split("\x1f")
        if len(f) < 10:
            continue
        num  = int(f[4]) if f[4].isdigit() else 0
        name = f[3].strip()
        page = f[7].strip()
        key  = f"Unidade {num:03d} — {name}"

        img_m = re.search(r'src="([^"]+)"', f[8])
        aud_m = re.search(r'\[sound:([^\]]+)\]', f[9])
        img_k = fname_to_key.get(img_m.group(1)) if img_m else None
        aud_k = fname_to_key.get(aud_m.group(1)) if aud_m else None

        units.setdefault(key, {"notes": [], "page": page, "num": num, "name": name})
        units[key]["notes"].append({"sub": f[5], "img": img_k, "aud": aud_k})

    return {
        "units":  units,
        "labels": sorted(units.keys()),
        "mime":   mime_for_key,
    }


def load_exercise_deck(apkg_path):
    global TMP_EX
    TMP_EX = tempfile.mkdtemp(prefix="anki_ex_")
    with zipfile.ZipFile(apkg_path, "r") as z:
        z.extractall(TMP_EX)

    db_path = next(
        (os.path.join(TMP_EX, n) for n in
         ("collection.anki21b", "collection.anki21", "collection.anki2")
         if os.path.exists(os.path.join(TMP_EX, n))),
        None
    )
    if not db_path:
        return {"exercises": {}, "units": [], "mime": {}}

    with open(os.path.join(TMP_EX, "media")) as f:
        media_map = json.load(f)

    fname_to_key = {v: k for k, v in media_map.items()}
    mime_for_key = {k: (mimetypes.guess_type(v)[0] or "application/octet-stream")
                    for k, v in media_map.items()}

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT flds FROM notes").fetchall()
    conn.close()

    # Fields: Id(0), Category(1), Unit(2), Rubric(3), Question(4), Choices(5), Image(6), Audio(7), AudioText(8)
    exercises = {}
    for (flds,) in rows:
        f = flds.split("\x1f")
        if len(f) < 5:
            continue
        unit_str = f[2].strip() if len(f) > 2 else ""
        rubric   = f[3].strip() if len(f) > 3 else ""
        question = f[4].strip() if len(f) > 4 else ""
        choices  = f[5].strip() if len(f) > 5 else ""
        img_raw  = f[6].strip() if len(f) > 6 else ""
        aud_raw  = f[7].strip() if len(f) > 7 else ""

        if not question:
            continue

        img_m = re.search(r'src="([^"]+)"', img_raw)
        aud_m = re.search(r'\[sound:([^\]]+)\]', aud_raw)
        img_k = fname_to_key.get(img_m.group(1)) if img_m else None
        aud_k = fname_to_key.get(aud_m.group(1)) if aud_m else None

        card = {
            "unit":     unit_str,
            "rubric":   rubric,
            "question": question,
            "choices":  [c.strip() for c in choices.split("/") if c.strip()] if choices else [],
            "img":      img_k,
            "aud":      aud_k,
        }
        exercises.setdefault(unit_str, []).append(card)

    return {"exercises": exercises, "units": sorted(exercises.keys()), "mime": mime_for_key}


# Mapeamento explícito: teoria (1-115) → prefixo(s) do grupo de exercícios no deck
# Grupos disponíveis (English Grammar In Use — intermédio, 27 grupos):
#  "Unit 1:"       Present continuous (67 cards)
#  "Unit 2:"       Present simple (55 cards)
#  "Unit 3:"       Present continuous and present simple 1 (38 cards)
#  "Unit 4:"       Present continuous and present simple 2 (1 card)
#  "Unit 7:"       Present perfect 1 (15 cards)
#  "Unit 13:"      Present perfect and past 1 (15 cards)
#  "Unit 19:"      I am doing / I do for the future (15 cards)
#  "Unit 26:"      can, could and (be) able (23 cards)
#  "Unit 32:"      must, mustn't and needn't (21 cards)
#  "Unit 38:"      If I do ... and if I did ... (28 cards)
#  "Unit 39:"      If I knew ... / I wish I knew ... (6 cards)
#  "Unit 47:"      Reported speech 1 (22 cards)
#  "Unit 53:"      Verb + -ing (30 cards)
#  "Unit 54:"      Verb + to ... (1 card)
#  "Unit 61:"      I'm used to ... (26 cards)
#  "Unit 69:"      Countable and uncountable 1 (35 cards)
#  "Unit 77:"      Names with and without the 1 (21 cards)
#  "Unit 82:"      myself/yourself/themselves etc. (37 cards)
#  "Unit 83:"      a friend of mine / my own house (5 cards)
#  "Unit 92:"      Relative clauses 1: who/that/which (19 cards)
#  "Unit 98:"      boring/bored, interesting/interested (27 cards)
#  "Unit 105:"     Comparison 1 (30 cards)
#  "Unit 113:"     although and in spite of (25 cards)
#  "Unit 121:"     at, on, in (time) (33 cards)
#  "Unit 129:"     Noun + preposition (28 cards)
#  "Unit 137:"     Phrasal verbs 1 (28 cards)
#  "Present and past"  review set — Units 1-18 do livro intermédio (31 cards)
#
# Unidades sem grupo correspondente ficam sem exercícios (melhor nenhum que errado):
#  21, 22 (passive), 35 (imperative/let's), 36 (I used to ≠ I'm used to),
#  56-58 (get/do/make/have), 74-75 (this/that, one/ones), 82 (both/either/neither),
#  93, 96 (word order)
_UNIT_MAP = {
    # Present continuous — Unit 1: "I am doing"
    3:   ["Unit 1:"],
    4:   ["Unit 1:"],

    # Present simple — Unit 2: "I do"
    5:   ["Unit 2:"],
    6:   ["Unit 2:"],
    7:   ["Unit 2:"],

    # Present continuous + simple — Unit 3+4
    8:   ["Unit 3:", "Unit 4:"],

    # Past tenses — "Present and past" review set
    10:  ["Present and past"],
    11:  ["Present and past"],
    12:  ["Present and past"],
    13:  ["Present and past"],
    14:  ["Present and past"],

    # Present perfect — Unit 7
    15:  ["Unit 7:"],
    16:  ["Unit 7:"],
    17:  ["Unit 7:"],
    18:  ["Unit 7:"],
    19:  ["Unit 7:"],

    # Present perfect vs past — Unit 13
    20:  ["Unit 13:"],

    # Future (present continuous + going to) — Unit 19
    # will/shall (27,28) nao estao cobertos neste deck
    25:  ["Unit 19:"],
    26:  ["Unit 19:"],

    # can and could — Unit 26
    # might (29) nao esta coberto; should/have to (32,33) nao estao cobertos
    30:  ["Unit 26:"],

    # must / mustn't / needn't — Unit 32
    31:  ["Unit 32:"],

    # Reported speech — Unit 47
    50:  ["Unit 47:"],

    # Verb + -ing (enjoy doing / stop doing) — Unit 53
    # Unidade 52 cobre ambos os lados (to/ing): Unit 53 para o lado -ing
    51:  ["Unit 53:"],
    52:  ["Unit 53:"],

    # Possessive pronouns (mine/yours/ours) — Unit 83
    # "a friend of mine" testa directamente pronomes possessivos
    61:  ["Unit 83:"],
    62:  ["Unit 83:"],

    # myself/yourself/themselves — Unit 82
    63:  ["Unit 82:"],

    # Countable and uncountable — Unit 69
    67:  ["Unit 69:"],
    68:  ["Unit 69:"],

    # the (artigo definido) — Unit 77: nomes com e sem "the"
    70:  ["Unit 77:"],
    73:  ["Unit 77:"],

    # Adjectives -ed/-ing (boring/bored) — Unit 98
    85:  ["Unit 98:"],

    # Comparatives and superlatives — Unit 105
    87:  ["Unit 105:"],
    88:  ["Unit 105:"],
    89:  ["Unit 105:"],
    90:  ["Unit 105:"],

    # Conditionals — Unit 38 + Unit 39
    99:  ["Unit 38:"],
    100: ["Unit 38:", "Unit 39:"],

    # Relative clauses — Unit 92
    101: ["Unit 92:"],
    102: ["Unit 92:"],

    # Prepositions de tempo — Unit 121
    103: ["Unit 121:"],
    104: ["Unit 121:"],
    105: ["Unit 121:"],

    # Phrasal verbs — Unit 137
    114: ["Unit 137:"],
    115: ["Unit 137:"],
}


def find_exercises_for_unit(theory_label):
    m = re.match(r"Unidade (\d+)", theory_label)
    if not m:
        return []

    num = int(m.group(1))

    # Try deck exercises first (solid mappings only)
    prefixes = _UNIT_MAP.get(num, [])
    if prefixes and DECK_EX.get("exercises"):
        cards = []
        for prefix in prefixes:
            for ex_unit, ex_cards in DECK_EX["exercises"].items():
                if ex_unit.startswith(prefix):
                    cards.extend(ex_cards)
        if cards:
            return cards[:20]

    # Fall back to custom exercises for units not covered by the deck
    return CUSTOM_EX.get(num, [])


# ─── Tracker ──────────────────────────────────────────────────────────────────

def mark_unit_complete(label: str):
    if not os.path.exists(TRACKER):
        return
    # label: "Unidade 005 — I do/work/like etc. (present simple)"
    num_match = re.match(r"Unidade (\d+)", label)
    if not num_match:
        return
    num = int(num_match.group(1))
    with open(TRACKER, encoding="utf-8") as f:
        text = f.read()
    # Replace "- [ ] X —" where X == num
    updated = re.sub(
        rf"(- )\[ \]( {num} —)",
        r"\1[x]\2",
        text
    )
    with open(TRACKER, "w", encoding="utf-8") as f:
        f.write(updated)


# ─── HTML ─────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Essential Grammar in Use</title>
<style>
:root {
  --bg:       #0f1112;
  --surface:  #181b1d;
  --panel:    #1f2224;
  --border:   #2a2d30;
  --accent:   #c41e2a;
  --accent-d: #9b1520;
  --green:    #22c55e;
  --amber:    #f59e0b;
  --text:     #e8eaed;
  --muted:    #6b7280;
  --radius:   8px;
}
* { box-sizing:border-box; margin:0; padding:0; }
body {
  display:flex; flex-direction:column; height:100vh;
  background:var(--bg); color:var(--text);
  font-family:-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size:14px; overflow:hidden;
}

/* ── Header ── */
header {
  display:flex; align-items:center; gap:12px;
  padding:0 20px; height:54px;
  background:var(--accent); border-bottom:3px solid var(--accent-d);
  flex-shrink:0; overflow:hidden;
}
.hdr-book { display:flex; flex-direction:column; gap:1px; flex-shrink:0; }
.hdr-title {
  font-size:13px; font-weight:800; color:#fff;
  letter-spacing:.04em; text-transform:uppercase; line-height:1;
}
.hdr-sub {
  font-size:9px; color:rgba(255,255,255,.55);
  font-weight:500; letter-spacing:.06em; text-transform:uppercase;
}
.hdr-sep { width:1px; height:26px; background:rgba(255,255,255,.22); flex-shrink:0; }
#hdr-unit {
  font-size:12px; color:rgba(255,255,255,.80); flex:1; min-width:0;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
#btn-back-map {
  background:rgba(0,0,0,.22); border:1px solid rgba(255,255,255,.25);
  color:#fff; border-radius:6px; padding:5px 12px;
  font-size:12px; font-weight:600; cursor:pointer; white-space:nowrap; flex-shrink:0;
}
#btn-back-map:hover { background:rgba(0,0,0,.38); }
@media (max-width:520px) {
  header { gap:8px; padding:0 12px; }
  .hdr-sub { display:none; }
  .hdr-sep { display:none; }
  .hdr-title { font-size:12px; }
  .lang-btn { padding:3px 5px; font-size:9px; }
  #hdr-user-name { display:none; }
  #btn-back-map { padding:5px 10px; font-size:11px; }
}

/* ── Map view (full page) ── */
#map-view {
  flex:1; overflow-y:auto;
  display:flex; flex-direction:column; align-items:center;
  padding-bottom:80px;
  background-image:radial-gradient(rgba(255,255,255,.025) 1px, transparent 1px);
  background-size:30px 30px;
}
#map-view::-webkit-scrollbar { width:5px; }
#map-view::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }

/* Progress bar */
.map-progress { width:100%; max-width:600px; padding:22px 24px 6px; }
.map-prog-bar {
  height:8px; background:rgba(255,255,255,.05); border-radius:6px;
  margin-bottom:8px; overflow:hidden;
  box-shadow:inset 0 2px 4px rgba(0,0,0,.4);
}
.map-prog-fill {
  height:100%; border-radius:6px;
  background:linear-gradient(90deg,#166534,#22c55e,#4ade80,#22c55e,#166534);
  background-size:300% 100%;
  transition:width 1s cubic-bezier(.4,0,.2,1);
  animation:progFlow 3s linear infinite;
  box-shadow:0 0 10px rgba(34,197,94,.35);
}
@keyframes progFlow {
  0%   { background-position:200% 0; }
  100% { background-position:-200% 0; }
}
.map-prog-label {
  font-size:11px; color:var(--muted);
  display:flex; justify-content:space-between; align-items:center;
}
.map-prog-pct { font-weight:800; color:var(--green); font-size:13px; }

/* Map canvas */
#map-container {
  position:relative; margin:0 auto;
  animation:mapIn .55s cubic-bezier(.4,0,.2,1) both;
}
@keyframes mapIn {
  from { opacity:0; }
  to   { opacity:1; }
}

/* Zone banner (absolutely positioned in container) */
.mz-banner {
  position:absolute; left:0; right:0;
  display:flex; align-items:center; gap:9px;
  padding:0 14px; border-radius:12px;
  border-left-width:3px; border-left-style:solid;
}
.mz-banner-emoji { font-size:20px; line-height:1; }
.mz-banner-name  {
  font-size:11px; font-weight:800; letter-spacing:.10em;
  text-transform:uppercase; flex:1;
}
.mz-banner-cnt { font-size:10px; font-weight:700; margin-left:auto; }

/* ── Map nodes ── */
@keyframes nodeIn {
  0%   { opacity:0; transform:translate(-50%,-50%) scale(.25); }
  72%  { opacity:1; transform:translate(-50%,-50%) scale(1.10); }
  100% { opacity:1; transform:translate(-50%,-50%) scale(1); }
}
@keyframes mnPulse {
  0%   { box-shadow:0 7px 0 var(--mn-shadow), 0 0 0 0 rgba(196,30,42,.8), 0 10px 30px rgba(196,30,42,.4); }
  60%  { box-shadow:0 7px 0 var(--mn-shadow), 0 0 0 18px rgba(196,30,42,0), 0 10px 30px rgba(196,30,42,.1); }
  100% { box-shadow:0 7px 0 var(--mn-shadow), 0 0 0 0 rgba(196,30,42,0), 0 10px 30px rgba(196,30,42,.4); }
}
@keyframes doneGlow {
  0%,100% { box-shadow:0 7px 0 #0a3018, 0 0 14px rgba(34,197,94,.3), 0 10px 24px rgba(0,0,0,.5); }
  50%     { box-shadow:0 7px 0 #0a3018, 0 0 28px rgba(34,197,94,.55), 0 10px 24px rgba(0,0,0,.5); }
}
@keyframes shine {
  0%,100% { left:-90%; opacity:0; }
  10%     { opacity:1; }
  55%     { left:170%; opacity:0; }
}
@keyframes lockShake {
  0%,100% { transform:translate(-50%,-50%) rotate(0deg); }
  20%     { transform:translate(-50%,-50%) rotate(-10deg); }
  40%     { transform:translate(-50%,-50%) rotate(10deg); }
  60%     { transform:translate(-50%,-50%) rotate(-6deg); }
  80%     { transform:translate(-50%,-50%) rotate(6deg); }
}

.mn {
  position:absolute;
  transform:translate(-50%,-50%);
  width:var(--mn-sz,84px); height:var(--mn-sz,84px); border-radius:50%;
  display:flex; flex-direction:column; align-items:center; justify-content:center;
  border:3px solid var(--border);
  background:var(--panel); color:var(--muted);
  font-family:inherit; cursor:pointer; overflow:hidden;
  animation:nodeIn .5s cubic-bezier(0.34,1.3,0.64,1) both;
  transition:transform .22s cubic-bezier(0.34,1.56,0.64,1), filter .18s;
  z-index:5;
}
.mn:hover:not(.mn-locked) {
  transform:translate(-50%,-58%) scale(1.15);
  filter:brightness(1.2);
  z-index:20;
}
.mn-num { font-size:var(--mn-fs,17px); font-weight:900; line-height:1; letter-spacing:-.5px; }
.mn-sub { font-size:var(--mn-ss,12px); line-height:1; margin-top:4px; }
.mn-lbl {
  position:absolute; transform:translateX(-50%);
  width:var(--mn-lw,108px); font-size:var(--mn-ls,9.5px);
  color:#8b95a3; text-align:center;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  pointer-events:none; z-index:4; line-height:1.2;
}
.mn-lbl.mn-lbl-locked { opacity:.28; }

/* Done node */
.mn.mn-done {
  --mn-shadow:#083020;
  background:linear-gradient(150deg,#1e7a40 0%,#145228 100%);
  border-color:#22c55e; color:#4ade80;
  box-shadow:0 7px 0 #083020, 0 0 14px rgba(34,197,94,.3), 0 10px 24px rgba(0,0,0,.5);
  animation:nodeIn .5s cubic-bezier(0.34,1.3,0.64,1) both, doneGlow 3.5s ease-in-out infinite;
}
.mn.mn-done::before {
  content:''; position:absolute; top:-60%; left:-90%;
  width:60%; height:220%; transform:rotate(20deg);
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.20),transparent);
  animation:shine 5s ease-in-out infinite;
}

/* Active node */
.mn.mn-active {
  --mn-shadow:#2d0509;
  background:linear-gradient(150deg,#6b1015 0%,#450a0e 100%);
  border-color:#ef4444; color:#fca5a5;
  box-shadow:0 7px 0 #2d0509, 0 0 0 0 rgba(196,30,42,.8), 0 10px 30px rgba(196,30,42,.4);
  animation:nodeIn .5s cubic-bezier(0.34,1.3,0.64,1) both, mnPulse 2s ease-out infinite;
}
.mn.mn-active .mn-num { text-shadow:0 0 14px rgba(252,165,165,.7); }

/* Locked node */
.mn.mn-locked {
  background:#0c0e0f; border-color:#1a1d20;
  color:#202428; cursor:not-allowed; opacity:.45;
  box-shadow:0 4px 0 #080a0b;
  animation:nodeIn .5s cubic-bezier(0.34,1.3,0.64,1) both;
}
.mn.mn-locked:active { animation:lockShake .35s ease forwards !important; }

/* ── Module view (full page) ── */
#module-view { flex:1; display:flex; flex-direction:column; overflow:hidden; }

/* ── Steps bar ── */
.steps-bar {
  display:flex; align-items:center; gap:0;
  padding:10px 24px; border-bottom:1px solid var(--border);
  flex-shrink:0; background:var(--surface);
}
.step-btn {
  display:flex; align-items:center; gap:7px; padding:6px 14px;
  border-radius:6px; cursor:pointer; border:none;
  font-size:12px; font-weight:600; background:none; color:var(--muted);
  transition:all .15s;
}
.step-btn .n {
  width:20px; height:20px; border-radius:50%;
  display:flex; align-items:center; justify-content:center;
  font-size:10px; font-weight:700; background:var(--border); color:var(--muted);
}
.step-btn.active { color:var(--text); background:var(--panel); }
.step-btn.active .n { background:var(--accent); color:#fff; }
.step-btn.done .n { background:var(--green); color:#fff; }
.step-btn.done { color:var(--muted); }
.step-sep { width:20px; height:1px; background:var(--border); margin:0 2px; }
.step-btn:disabled { cursor:not-allowed; opacity:.35; }
.step-time { font-size:9.5px; font-weight:700; color:var(--green); margin-left:3px; opacity:.85; font-variant-numeric:tabular-nums; }
#step-timer {
  margin-left:auto; font-size:13px; font-weight:700;
  font-variant-numeric:tabular-nums; letter-spacing:.5px;
  color:var(--accent); background:rgba(196,30,42,.1);
  padding:3px 10px; border-radius:6px; min-width:46px; text-align:center;
  transition:color .3s, background .3s;
}
#step-timer.warning { color:#f59e0b; background:rgba(245,158,11,.12); }
#step-timer.urgent  { color:#ef4444; background:rgba(239,68,68,.15);
                      animation:timerPulse 1s ease-in-out infinite; }
@keyframes timerPulse { 0%,100%{opacity:1} 50%{opacity:.4} }

/* ── Step content ── */
.step-content { flex:1; overflow-y:auto; padding:28px; }
.step-content::-webkit-scrollbar { width:5px; }
.step-content::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }

.step-header { margin-bottom:24px; padding-bottom:16px; border-bottom:1px solid var(--border); }
.step-header h2 { font-size:16px; font-weight:700; color:var(--text); }
.step-header p  { font-size:13px; color:var(--muted); margin-top:5px; line-height:1.5; }

/* ── Step 1: Theory ── */
#img-nav { display:flex; align-items:center; gap:10px; margin-bottom:14px; }
#img-nav button { background:var(--panel); border:1px solid var(--border); color:var(--text); border-radius:6px; padding:5px 14px; font-size:13px; cursor:pointer; }
#img-nav button:disabled { opacity:.30; cursor:default; }
#img-nav button:hover:not(:disabled) { border-color:var(--accent); }
#img-counter { font-size:12px; color:var(--muted); }
#btn-audio { margin-left:auto; background:#14532d; color:var(--green); border:1px solid #166534; border-radius:6px; padding:5px 16px; font-size:13px; cursor:pointer; }
#btn-audio:disabled { opacity:.30; cursor:default; }
#img-box { background:#000; border-radius:var(--radius); overflow:hidden; display:flex; align-items:center; justify-content:center; min-height:400px; max-height:60vh; border:1px solid var(--border); }
#img-box img { max-width:100%; max-height:60vh; object-fit:contain; }

/* ── Step 2: Book ── */
.exercise-card { background:var(--panel); border:1px solid var(--border); border-radius:var(--radius); padding:20px; margin-bottom:14px; }
.exercise-card h3 { font-size:14px; font-weight:700; margin-bottom:8px; }
.exercise-card p  { color:var(--muted); font-size:13px; line-height:1.6; }
.page-badge { display:inline-block; background:#3d0a0e; color:#fca5a5; border-radius:5px; padding:2px 10px; font-size:12px; font-weight:700; margin-top:8px; }

/* ── Step 3: Sentences ── */
.ctx-row { display:flex; align-items:center; gap:10px; margin-bottom:20px; }
.ctx-row label { font-size:12px; color:var(--muted); white-space:nowrap; }
.ctx-row input { flex:1; background:var(--panel); border:1px solid var(--border); color:var(--text); border-radius:6px; padding:7px 12px; font-size:13px; outline:none; }
.ctx-row input:focus { border-color:var(--accent); }
.done-badge { background:var(--panel); border:1px solid var(--border); color:var(--muted); border-radius:5px; padding:3px 12px; font-size:12px; white-space:nowrap; transition:all .3s; }
.done-badge.all-done { background:#14532d; border-color:#166534; color:var(--green); }

.sentence-card { background:var(--panel); border:1.5px solid var(--border); border-radius:var(--radius); padding:18px; margin-bottom:12px; transition:border-color .2s; }
.sentence-card.partial  { border-color:#92400e; }
.sentence-card.complete { border-color:#166534; }
.sc-head  { display:flex; align-items:center; justify-content:space-between; margin-bottom:14px; }
.sc-rail  { display:flex; align-items:center; gap:5px; }
.rail-s   { font-size:11px; color:var(--muted); padding:3px 8px; border-radius:4px; border:1px solid transparent; transition:all .2s; white-space:nowrap; }
.rail-s.active { color:var(--accent); border-color:var(--accent); background:rgba(196,30,42,.08); }
.rail-s.done   { color:var(--green); border-color:#166534; background:rgba(34,197,94,.08); }
.rail-arr { color:var(--border); font-size:12px; }
.sc-status { font-size:11px; font-weight:600; white-space:nowrap; }
.sc-status.ok   { color:var(--green); }
.sc-status.warn { color:var(--amber); }
.sc-field { display:flex; flex-direction:column; gap:4px; margin-bottom:12px; }
.sc-field label { font-size:11px; color:var(--muted); }
.sc-ta { width:100%; background:var(--bg); border:1.5px solid var(--border); color:var(--text); border-radius:6px; padding:10px 12px; font-size:14px; line-height:1.6; outline:none; font-family:inherit; resize:none; min-height:56px; overflow:hidden; }
.sc-ta:focus { border-color:var(--accent); }
.tokens-wrap { margin:0 0 12px; }
.tokens-lbl  { font-size:11px; color:var(--muted); margin-bottom:8px; }
.tokens      { display:flex; flex-wrap:wrap; gap:6px; }
@keyframes popIn { from{opacity:0;transform:scale(.8) translateY(-4px)} to{opacity:1;transform:scale(1) translateY(0)} }
.token { padding:4px 12px; border-radius:6px; cursor:pointer; border:1.5px solid var(--border); background:var(--bg); color:#94a3b8; font-size:13px; font-family:inherit; transition:border-color .12s, color .12s, background .12s, box-shadow .15s; animation:popIn .16s ease both; }
.token:hover { border-color:var(--accent); color:var(--accent); background:rgba(196,30,42,.06); }
.token.sel   { border-color:var(--amber); background:#1c0d00; color:var(--amber); font-weight:700; box-shadow:0 0 0 3px rgba(245,158,11,.15); }
.tok-p       { color:var(--muted); font-size:11px; }
.hint-section { margin-bottom:10px; }
.hint-bar     { display:flex; align-items:center; gap:8px; background:#071a07; border:1px solid #166534; border-radius:6px; padding:8px 14px; margin-bottom:8px; }
.hint-lbl     { font-size:14px; flex-shrink:0; }
.hint-bar input { flex:1; background:transparent; border:none; color:var(--text); font-size:13px; outline:none; }
.sug-btn { background:transparent; border:1px solid #166534; color:var(--green); border-radius:5px; padding:3px 10px; font-size:11px; cursor:pointer; white-space:nowrap; }
.sug-btn:hover { background:#14532d; }
.sc-preview { background:var(--bg); border:1px solid var(--border); border-radius:6px; padding:9px 12px; font-size:12px; color:#94a3b8; font-family:"SF Mono", monospace; word-break:break-all; line-height:1.7; min-height:36px; }
.cloze-hi { color:var(--amber); font-weight:700; }
.reset-btn { background:none; border:none; color:var(--muted); font-size:11px; cursor:pointer; padding:6px 0 0; display:block; }
.reset-btn:hover { color:#f87171; }

/* ── Step 3: Exercise Review ── */
.ex-topbar { display:flex; align-items:center; gap:12px; margin-bottom:20px; }
.ex-prog-wrap { flex:1; height:5px; background:var(--border); border-radius:3px; }
.ex-prog-fill { height:100%; background:var(--accent); border-radius:3px; transition:width .4s; }
.ex-counter { font-size:12px; color:var(--muted); white-space:nowrap; }
.ex-score   { font-size:12px; font-weight:700; white-space:nowrap; }
.ex-unit-badge { background:#3d0a0e; color:#fca5a5; border-radius:5px; padding:3px 12px; font-size:11px; font-weight:700; margin-bottom:12px; display:inline-block; }
.ex-rubric { font-size:13px; color:var(--muted); margin-bottom:16px; line-height:1.5; font-style:italic; }
.ex-card { background:var(--panel); border:1.5px solid var(--border); border-radius:var(--radius); padding:24px; margin-bottom:16px; }
.ex-question { font-size:19px; color:var(--text); line-height:2; margin-bottom:20px; word-break:break-word; }
.cloze-blank { display:inline-block; min-width:72px; border-bottom:2.5px solid var(--accent); color:transparent; background:rgba(196,30,42,.08); border-radius:4px 4px 0 0; padding:0 8px; vertical-align:bottom; }
.cloze-ans { color:var(--amber); font-weight:700; border-bottom:2.5px solid var(--amber); padding:0 4px; }
.ex-choices { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:16px; }
.ex-choice { padding:7px 16px; border-radius:6px; border:1.5px solid var(--border); background:var(--bg); color:var(--text); font-size:14px; font-family:inherit; cursor:pointer; transition:all .15s; }
.ex-choice:hover:not(:disabled) { border-color:var(--accent); color:var(--accent); background:rgba(196,30,42,.06); }
.ex-choice:disabled { cursor:default; opacity:.85; }
.ex-choice-correct { border-color:#16a34a !important; background:#14532d !important; color:#4ade80 !important; }
.ex-choice-wrong   { border-color:#991b1b !important; background:#3b0d0d !important; color:#f87171 !important; }
.ex-btn-reveal { background:var(--accent); color:#fff; border:none; border-radius:6px; padding:9px 24px; font-size:14px; font-weight:600; cursor:pointer; }
.ex-btn-reveal:hover { opacity:.88; }
.ex-btn-check { background:var(--accent); color:#fff; border:none; border-radius:6px; padding:9px 24px; font-size:14px; font-weight:600; cursor:pointer; margin-top:4px; }
.ex-btn-check:hover { opacity:.88; }
.cloze-input { min-width:90px; border:none; border-bottom:2.5px solid var(--accent); background:rgba(196,30,42,.08); border-radius:4px 4px 0 0; padding:2px 8px; vertical-align:bottom; color:var(--text); font-size:inherit; font-family:inherit; outline:none; }
.cloze-input.correct { border-color:var(--green); background:rgba(34,197,94,.15); color:var(--green); }
.cloze-input.wrong   { border-color:#ef4444; background:rgba(239,68,68,.12); color:#f87171; }
.ex-judge { display:flex; gap:10px; margin-top:16px; }
.ex-btn-knew    { background:#14532d; color:#4ade80; border:1px solid #16a34a; border-radius:6px; padding:9px 24px; font-size:14px; font-weight:600; cursor:pointer; }
.ex-btn-unknown { background:#3b0d0d; color:#f87171; border:1px solid #991b1b; border-radius:6px; padding:9px 24px; font-size:14px; font-weight:600; cursor:pointer; }
.ex-btn-knew:hover    { opacity:.88; }
.ex-btn-unknown:hover { opacity:.88; }
.ex-no-cards { display:flex; flex-direction:column; align-items:center; gap:12px; padding:40px 20px; text-align:center; color:var(--muted); }
.ex-no-cards .ex-nc-icon { font-size:40px; }
.ex-no-cards p { font-size:13px; max-width:300px; line-height:1.6; }
.ex-summary { background:var(--panel); border:1px solid var(--border); border-radius:var(--radius); padding:32px; text-align:center; }
.ex-summary h3 { font-size:22px; color:#fff; margin-bottom:8px; }
.ex-summary .ex-sub { font-size:13px; color:var(--muted); margin-bottom:24px; }
.ex-stats { display:flex; justify-content:center; gap:40px; margin-bottom:28px; }
.ex-stat { display:flex; flex-direction:column; align-items:center; gap:4px; }
.ex-stat-val { font-size:38px; font-weight:700; }
.ex-stat-lbl { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.06em; }
.ex-stat.known   .ex-stat-val { color:var(--green); }
.ex-stat.unknown .ex-stat-val { color:#f87171; }
.ex-retry-btn { background:var(--panel); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:9px 24px; font-size:14px; font-weight:600; cursor:pointer; }
.ex-retry-btn:hover { border-color:var(--accent); }

/* ── Step 4 ── */
.anki-empty-state { display:flex; flex-direction:column; align-items:center; justify-content:center; gap:10px; padding:40px 20px; background:var(--panel); border:1px dashed var(--border); border-radius:var(--radius); text-align:center; margin-bottom:20px; }
.anki-empty-state .aes-icon { font-size:36px; }
.anki-empty-state p { font-size:13px; color:var(--muted); max-width:280px; line-height:1.6; }
.anki-empty-state p b { color:var(--text); }
.aes-btn { background:var(--accent); color:#fff; border:none; border-radius:6px; padding:8px 20px; font-size:13px; font-weight:600; cursor:pointer; margin-top:6px; }
.aes-btn:hover { opacity:.85; }
.anki-preview { background:var(--bg); border:1px solid var(--border); border-radius:var(--radius); padding:16px; margin-bottom:20px; font-family:"SF Mono", monospace; font-size:11px; color:#94a3b8; max-height:260px; overflow-y:auto; white-space:pre-wrap; word-break:break-all; line-height:1.6; }
.anki-row { display:flex; gap:10px; flex-wrap:wrap; }
.anki-row button { padding:9px 20px; border-radius:6px; font-size:13px; font-weight:600; cursor:pointer; border:none; transition:opacity .15s; }
.anki-row button:hover { opacity:.85; }
#btn-copy { background:var(--accent); color:#fff; }
#btn-save { background:var(--panel); border:1px solid var(--border); color:var(--text); }
#btn-done { background:#14532d; color:var(--green); border:1px solid #166534; }
#save-msg { font-size:13px; color:var(--green); padding:9px 0; align-self:center; }

/* ── Nav footer ── */
.nav-footer { display:flex; justify-content:space-between; align-items:center; padding:12px 24px; border-top:1px solid var(--border); flex-shrink:0; background:var(--surface); }
.nav-footer button { background:var(--panel); border:1px solid var(--border); color:var(--text); border-radius:6px; padding:8px 20px; font-size:13px; font-weight:600; cursor:pointer; }
.nav-footer button:hover { border-color:var(--accent); }
.nav-footer button:disabled { opacity:.30; cursor:default; }
#btn-next-step { background:var(--accent); border-color:var(--accent); color:#fff; }
#btn-next-step:hover { opacity:.88; }

/* ── Login view ── */
#login-view {
  flex:1; display:flex; align-items:center; justify-content:center;
  padding:24px;
}
.login-card {
  background:var(--surface); border:1px solid var(--border);
  border-radius:14px; padding:36px 32px; width:100%; max-width:380px;
  display:flex; flex-direction:column; gap:0;
}
.login-logo { text-align:center; margin-bottom:24px; }
.login-logo-title { font-size:18px; font-weight:800; color:#fff; letter-spacing:.04em; }
.login-logo-sub { font-size:11px; color:var(--muted); margin-top:3px; }
.login-tabs { display:flex; background:var(--panel); border-radius:8px; padding:3px; margin-bottom:24px; }
.login-tab {
  flex:1; padding:7px; border:none; background:none; color:var(--muted);
  font-size:13px; font-weight:600; border-radius:6px; cursor:pointer; transition:all .15s;
}
.login-tab.active { background:var(--accent); color:#fff; }
.login-field { display:flex; flex-direction:column; gap:6px; margin-bottom:14px; }
.login-field label { font-size:11px; color:var(--muted); font-weight:600; text-transform:uppercase; letter-spacing:.05em; }
.login-field input {
  background:var(--panel); border:1px solid var(--border); color:var(--text);
  border-radius:8px; padding:10px 14px; font-size:14px; outline:none; font-family:inherit;
}
.login-field input:focus { border-color:var(--accent); }
.login-err { font-size:12px; color:#f87171; min-height:18px; margin-bottom:8px; text-align:center; }
.login-btn {
  background:var(--accent); color:#fff; border:none; border-radius:8px;
  padding:12px; font-size:14px; font-weight:700; cursor:pointer; width:100%;
  margin-top:4px; transition:opacity .15s;
}
.login-btn:hover { opacity:.88; }

/* Language switcher */
.lang-sw { display:flex; gap:2px; }
.lang-btn {
  background:none; border:none; color:rgba(255,255,255,.40);
  font-size:10px; font-weight:700; cursor:pointer; padding:3px 7px;
  border-radius:4px; letter-spacing:.04em; transition:all .12s;
}
.lang-btn:hover { color:#fff; background:rgba(255,255,255,.1); }
.lang-btn.active { color:#fff; background:rgba(255,255,255,.18); }

/* Header user chip */
#hdr-user {
  display:flex; align-items:center; gap:8px;
  background:rgba(0,0,0,.22); border:1px solid rgba(255,255,255,.18);
  border-radius:20px; padding:4px 12px 4px 8px;
}
#hdr-user-name { font-size:12px; color:#fff; font-weight:600; }
#btn-logout {
  background:none; border:none; color:rgba(255,255,255,.55);
  font-size:11px; cursor:pointer; padding:0; line-height:1;
}
#btn-logout:hover { color:#fff; }
</style>
</head>
<body>

<header>
  <div class="hdr-book">
    <div class="hdr-title">Essential Grammar in Use</div>
    <div class="hdr-sub">Raymond Murphy · Cambridge University Press</div>
  </div>
  <div class="hdr-sep"></div>
  <span id="hdr-unit"></span>
  <button id="btn-back-map" onclick="returnToMap()" style="display:none">← Mapa</button>
  <div class="lang-sw">
    <button class="lang-btn active" data-lang="pt" onclick="setLang('pt')">PT</button>
    <button class="lang-btn" data-lang="en" onclick="setLang('en')">EN</button>
    <button class="lang-btn" data-lang="es" onclick="setLang('es')">ES</button>
    <button class="lang-btn" data-lang="fr" onclick="setLang('fr')">FR</button>
  </div>
  <div id="hdr-user" style="display:none">
    <span style="font-size:15px">👤</span>
    <span id="hdr-user-name"></span>
    <button id="btn-logout" onclick="logout()" title="Terminar sessão">✕</button>
  </div>
</header>

<!-- Login view -->
<div id="login-view">
  <div class="login-card">
    <div class="login-logo">
      <div class="login-logo-title">Essential Grammar in Use</div>
      <div class="login-logo-sub" id="login-sub-text">Inicia sessão para continuar o teu progresso</div>
    </div>
    <div class="login-tabs">
      <button class="login-tab active" id="tab-login" onclick="switchTab('login')">Entrar</button>
      <button class="login-tab" id="tab-register" onclick="switchTab('register')">Registar</button>
    </div>
    <div class="login-field">
      <label id="lbl-username">Utilizador</label>
      <input type="text" id="login-username" autocomplete="username" placeholder="nome de utilizador" onkeydown="if(event.key==='Enter')loginSubmit()">
    </div>
    <div class="login-field">
      <label id="lbl-password">Palavra-passe</label>
      <input type="password" id="login-password" autocomplete="current-password" placeholder="••••••••" onkeydown="if(event.key==='Enter')loginSubmit()">
    </div>
    <div class="login-err" id="login-err"></div>
    <button class="login-btn" id="login-submit-btn" onclick="loginSubmit()">Entrar</button>
    <p id="login-disclaimer" style="margin-top:20px;padding-top:16px;border-top:1px solid var(--border);font-size:11px;color:var(--muted);text-align:center;line-height:1.6">
      Auxiliar de estudo para o livro <em style="color:#94a3b8">Essential Grammar in Use</em>
      de Raymond Murphy — a experiência é mais completa com o livro físico.
    </p>
  </div>
</div>

<!-- Map view (full page, hidden until login) -->
<div id="map-view" style="display:none">
  <div class="map-progress">
    <div class="map-prog-bar"><div id="map-prog-fill" class="map-prog-fill" style="width:0%"></div></div>
    <div class="map-prog-label"><span id="map-prog-label">0 / 115 unidades completas</span><span class="map-prog-pct" id="map-prog-pct">0%</span></div>
  </div>
  <div id="map-scale-outer" style="flex-shrink:0;position:relative;width:100%">
    <div id="map-container"></div>
  </div>
</div>

<!-- Module view (full page, hidden initially) -->
<div id="module-view" style="display:none">
  <div class="steps-bar">
    <button class="step-btn" id="sb1" onclick="goStep(1)"><span class="n">1</span> Teoria<span class="step-time" id="st1"></span></button>
    <div class="step-sep"></div>
    <button class="step-btn" id="sb2" onclick="goStep(2)"><span class="n">2</span> Livro<span class="step-time" id="st2"></span></button>
    <div class="step-sep"></div>
    <button class="step-btn" id="sb3" onclick="goStep(3)"><span class="n">3</span> Praticar<span class="step-time" id="st3"></span></button>
    <div class="step-sep"></div>
    <button class="step-btn" id="sb4" onclick="goStep(4)"><span class="n">4</span> Concluir<span class="step-time" id="st4"></span></button>
    <span id="step-timer" style="display:none">0:00</span>
  </div>
  <div class="step-content" id="step-content"></div>
  <div class="nav-footer" id="nav-footer">
    <button id="btn-prev-step" onclick="prevStep()" disabled>← Anterior</button>
    <button id="btn-next-step" onclick="nextStep()">Seguinte →</button>
  </div>
</div>

<script>
// ─── State ────────────────────────────────────────────────────────────────────
const S = {
  units: [],
  unit: null,
  step: 1,
  noteIdx: 0,
  done: new Set(),
  context: localStorage.getItem('ctx') || 'Engenharia de Software',
  sentences: [
    {text:'', tokens:[], selIdx:-1, hide:'', hint:''},
    {text:'', tokens:[], selIdx:-1, hide:'', hint:''},
    {text:'', tokens:[], selIdx:-1, hide:'', hint:''},
  ],
  audioEl: null,
  exCards: [],
  exIdx: 0,
  exRevealed: false,
  exKnown: 0,
  exUnknown: 0,
  exDone: false,
  stepTimes: [0, 0, 0, 0],
  stepStart: 0,
  timerInt: null,
  user: null,
  token: null,
  lang: localStorage.getItem('lang') || 'pt',
};

// ─── i18n ─────────────────────────────────────────────────────────────────────
var LANGS = {
  pt: {
    login_sub:'Inicia sessão para continuar o teu progresso',
    tab_login:'Entrar', tab_register:'Registar',
    lbl_user:'Utilizador', lbl_pass:'Palavra-passe', ph_user:'nome de utilizador',
    btn_login:'Entrar', btn_register:'Criar conta',
    disclaimer:'Auxiliar de estudo para o livro <em style="color:#94a3b8">Essential Grammar in Use</em> de Raymond Murphy — a experiência é mais completa com o livro físico.',
    err_fill:'Preenche todos os campos.', err_creds:'Utilizador ou palavra-passe incorretos.',
    err_short_user:'O utilizador deve ter pelo menos 3 caracteres.',
    err_short_pass:'A palavra-passe deve ter pelo menos 4 caracteres.',
    err_exists:'Esse utilizador já existe.', err_conn:'Erro de ligação ao servidor.',
    back_map:'← Mapa',
    prog:function(d,tot){return d+' / '+tot+' unidades completas';},
    step1:'Teoria', step2:'Livro', step3:'Praticar', step4:'Concluir',
    btn_prev:'← Anterior', btn_next:'Seguinte →',
    s1_title:'📖 Passo 1 — Input Teórico',
    s1_desc:'Lê a explicação da unidade com atenção. Máximo 10–15 minutos.',
    img_prev:'◀ Anterior', img_next:'Seguinte ▶',
    btn_audio:'🔊 Ouvir', btn_stop:'⏹ Parar', no_img:'Sem imagem',
    s2_title:'✏️ Passo 2 — Produção Ativa (Active Recall)',
    s2_desc:'Resolve os exercícios do livro <strong>sem consultar a teoria</strong>. O esforço de recuperação consolida a memória.',
    s2_c1:'📚 Essential Grammar in Use',
    s2_c1d:'Abre o livro na teoria desta unidade e resolve os exercícios da página da direita.',
    page_badge:function(p){return 'Pág. '+p;},
    s2_c2:'💡 Como fazer',
    s2_c2d:'1. Lê o enunciado de cada exercício calmamente.<br>2. Escreve as tuas respostas sem olhar para a teoria da esquerda.<br>3. Só depois confirma as respostas e anota os erros.',
    s3_title:'🃏 Passo 3 — Exercícios',
    s3_desc:'Pratica com os exercícios do deck.',
    loading:'A carregar exercícios...',
    no_ex:'Nenhum exercício encontrado para esta unidade no deck de exercícios.',
    no_ex_hint:'Podes avançar para o Passo 4.',
    btn_knew:'✓ Sabia', btn_unknown:'✗ Não sabia', btn_check:'Verificar',
    ex_pct:function(p){return p+'% corretas';}, ex_done:function(n){return n+' exercícios completados';},
    ex_knew:'Sabia', ex_unknown:'Não sabia', btn_retry:'↺ Repetir exercícios',
    s4_title:'✅ Passo 4 — Concluir',
    s4_desc:'Revê o resultado dos exercícios e marca a unidade como concluída.',
    s4_pct:function(p){return p+'% corretas';},
    s4_right:function(n){return '✓ '+n+' certas';}, s4_wrong:function(n){return '✗ '+n+' erradas';},
    s4_of:function(n){return 'de '+n+' exercícios';},
    s4_no_stats:'Sem dados de exercícios — volta ao Passo 3 para praticar.',
    s4_already:'Unidade já marcada como concluída', btn_done:'✅ Marcar Unidade Completa',
    unit_complete:function(n){return 'Unidade '+n+' Completa!';},
    unit_unlocked:function(n){return 'Unidade '+n+' desbloqueada!';},
    keep_going:'Continua assim!', back_to_map:'← Voltar ao Mapa',
    err_copy:'⚠️  Nenhuma frase para copiar', err_save:'⚠️  Nenhuma frase para guardar',
    copied:'✓ Copiado para o clipboard!',
    saved:function(f){return '✓ Guardado: '+f;},
    err_step:function(n){return 'Erro no Passo '+n+':';},
  },
  en: {
    login_sub:'Sign in to continue your progress',
    tab_login:'Sign in', tab_register:'Register',
    lbl_user:'Username', lbl_pass:'Password', ph_user:'username',
    btn_login:'Sign in', btn_register:'Create account',
    disclaimer:'A study companion for <em style="color:#94a3b8">Essential Grammar in Use</em> by Raymond Murphy — the experience is more complete with the physical book.',
    err_fill:'Please fill in all fields.', err_creds:'Incorrect username or password.',
    err_short_user:'Username must be at least 3 characters.',
    err_short_pass:'Password must be at least 4 characters.',
    err_exists:'That username already exists.', err_conn:'Connection error.',
    back_map:'← Map',
    prog:function(d,tot){return d+' / '+tot+' units completed';},
    step1:'Theory', step2:'Book', step3:'Practice', step4:'Finish',
    btn_prev:'← Previous', btn_next:'Next →',
    s1_title:'📖 Step 1 — Theory Input',
    s1_desc:'Read the unit explanation carefully. Maximum 10–15 minutes.',
    img_prev:'◀ Previous', img_next:'Next ▶',
    btn_audio:'🔊 Listen', btn_stop:'⏹ Stop', no_img:'No image',
    s2_title:'✏️ Step 2 — Active Production (Active Recall)',
    s2_desc:'Complete the book exercises <strong>without consulting the theory</strong>. The retrieval effort consolidates memory.',
    s2_c1:'📚 Essential Grammar in Use',
    s2_c1d:"Open the book to this unit's theory and complete the exercises on the right-hand page.",
    page_badge:function(p){return 'p. '+p;},
    s2_c2:'💡 How to do it',
    s2_c2d:'1. Read each exercise instruction calmly.<br>2. Write your answers without looking at the theory on the left.<br>3. Then check the answers and note your mistakes.',
    s3_title:'🃏 Step 3 — Exercises',
    s3_desc:'Practice with the deck exercises.',
    loading:'Loading exercises...',
    no_ex:'No exercises found for this unit in the exercise deck.',
    no_ex_hint:'You can proceed to Step 4.',
    btn_knew:'✓ Knew it', btn_unknown:"✗ Didn't know", btn_check:'Check',
    ex_pct:function(p){return p+'% correct';}, ex_done:function(n){return n+' exercises completed';},
    ex_knew:'Knew', ex_unknown:"Didn't know", btn_retry:'↺ Retry exercises',
    s4_title:'✅ Step 4 — Finish',
    s4_desc:'Review the exercise results and mark the unit as complete.',
    s4_pct:function(p){return p+'% correct';},
    s4_right:function(n){return '✓ '+n+' correct';}, s4_wrong:function(n){return '✗ '+n+' wrong';},
    s4_of:function(n){return 'of '+n+' exercises';},
    s4_no_stats:'No exercise data — go back to Step 3 to practice.',
    s4_already:'Unit already marked as complete', btn_done:'✅ Mark Unit Complete',
    unit_complete:function(n){return 'Unit '+n+' Complete!';},
    unit_unlocked:function(n){return 'Unit '+n+' unlocked!';},
    keep_going:'Keep it up!', back_to_map:'← Back to Map',
    err_copy:'⚠️  No sentences to copy', err_save:'⚠️  No sentences to save',
    copied:'✓ Copied to clipboard!',
    saved:function(f){return '✓ Saved: '+f;},
    err_step:function(n){return 'Error in Step '+n+':';},
  },
  es: {
    login_sub:'Inicia sesión para continuar tu progreso',
    tab_login:'Entrar', tab_register:'Registrarse',
    lbl_user:'Usuario', lbl_pass:'Contraseña', ph_user:'nombre de usuario',
    btn_login:'Entrar', btn_register:'Crear cuenta',
    disclaimer:'Complemento de estudio para <em style="color:#94a3b8">Essential Grammar in Use</em> de Raymond Murphy — la experiencia es más completa con el libro físico.',
    err_fill:'Rellena todos los campos.', err_creds:'Usuario o contraseña incorrectos.',
    err_short_user:'El usuario debe tener al menos 3 caracteres.',
    err_short_pass:'La contraseña debe tener al menos 4 caracteres.',
    err_exists:'Ese usuario ya existe.', err_conn:'Error de conexión.',
    back_map:'← Mapa',
    prog:function(d,tot){return d+' / '+tot+' unidades completadas';},
    step1:'Teoría', step2:'Libro', step3:'Practicar', step4:'Concluir',
    btn_prev:'← Anterior', btn_next:'Siguiente →',
    s1_title:'📖 Paso 1 — Entrada Teórica',
    s1_desc:'Lee la explicación de la unidad con atención. Máximo 10–15 minutos.',
    img_prev:'◀ Anterior', img_next:'Siguiente ▶',
    btn_audio:'🔊 Escuchar', btn_stop:'⏹ Parar', no_img:'Sin imagen',
    s2_title:'✏️ Paso 2 — Producción Activa (Active Recall)',
    s2_desc:'Resuelve los ejercicios del libro <strong>sin consultar la teoría</strong>. El esfuerzo de recuperación consolida la memoria.',
    s2_c1:'📚 Essential Grammar in Use',
    s2_c1d:'Abre el libro en la teoría de esta unidad y resuelve los ejercicios de la página derecha.',
    page_badge:function(p){return 'Pág. '+p;},
    s2_c2:'💡 Cómo hacerlo',
    s2_c2d:'1. Lee el enunciado de cada ejercicio con calma.<br>2. Escribe tus respuestas sin mirar la teoría de la izquierda.<br>3. Solo después confirma las respuestas y anota los errores.',
    s3_title:'🃏 Paso 3 — Ejercicios',
    s3_desc:'Practica con los ejercicios del mazo.',
    loading:'Cargando ejercicios...',
    no_ex:'No se encontraron ejercicios para esta unidad en el mazo de ejercicios.',
    no_ex_hint:'Puedes avanzar al Paso 4.',
    btn_knew:'✓ Lo sabía', btn_unknown:'✗ No lo sabía', btn_check:'Verificar',
    ex_pct:function(p){return p+'% correctas';}, ex_done:function(n){return n+' ejercicios completados';},
    ex_knew:'Sabía', ex_unknown:'No sabía', btn_retry:'↺ Repetir ejercicios',
    s4_title:'✅ Paso 4 — Concluir',
    s4_desc:'Revisa el resultado de los ejercicios y marca la unidad como completada.',
    s4_pct:function(p){return p+'% correctas';},
    s4_right:function(n){return '✓ '+n+' correctas';}, s4_wrong:function(n){return '✗ '+n+' incorrectas';},
    s4_of:function(n){return 'de '+n+' ejercicios';},
    s4_no_stats:'Sin datos de ejercicios — vuelve al Paso 3 para practicar.',
    s4_already:'Unidad ya marcada como completada', btn_done:'✅ Marcar Unidad Completa',
    unit_complete:function(n){return '¡Unidad '+n+' Completada!';},
    unit_unlocked:function(n){return '¡Unidad '+n+' desbloqueada!';},
    keep_going:'¡Sigue así!', back_to_map:'← Volver al Mapa',
    err_copy:'⚠️  Sin frases para copiar', err_save:'⚠️  Sin frases para guardar',
    copied:'✓ Copiado al portapapeles',
    saved:function(f){return '✓ Guardado: '+f;},
    err_step:function(n){return 'Error en el Paso '+n+':';},
  },
  fr: {
    login_sub:'Connecte-toi pour continuer ta progression',
    tab_login:'Se connecter', tab_register:"S'inscrire",
    lbl_user:'Utilisateur', lbl_pass:'Mot de passe', ph_user:"nom d'utilisateur",
    btn_login:'Se connecter', btn_register:'Créer un compte',
    disclaimer:"Aide à l'étude du livre <em style=\"color:#94a3b8\">Essential Grammar in Use</em> de Raymond Murphy — l'expérience est plus complète avec le livre physique.",
    err_fill:'Remplis tous les champs.', err_creds:"Nom d'utilisateur ou mot de passe incorrect.",
    err_short_user:"Le nom d'utilisateur doit avoir au moins 3 caractères.",
    err_short_pass:'Le mot de passe doit avoir au moins 4 caractères.',
    err_exists:"Ce nom d'utilisateur existe déjà.", err_conn:'Erreur de connexion.',
    back_map:'← Carte',
    prog:function(d,tot){return d+' / '+tot+' unités complétées';},
    step1:'Théorie', step2:'Livre', step3:'Pratiquer', step4:'Terminer',
    btn_prev:'← Précédent', btn_next:'Suivant →',
    s1_title:'📖 Étape 1 — Apport Théorique',
    s1_desc:"Lis l'explication de l'unité attentivement. Maximum 10–15 minutes.",
    img_prev:'◀ Précédent', img_next:'Suivant ▶',
    btn_audio:'🔊 Écouter', btn_stop:'⏹ Arrêter', no_img:"Pas d'image",
    s2_title:'✏️ Étape 2 — Production Active (Active Recall)',
    s2_desc:"Résous les exercices du livre <strong>sans consulter la théorie</strong>. L'effort de récupération consolide la mémoire.",
    s2_c1:'📚 Essential Grammar in Use',
    s2_c1d:'Ouvre le livre à la théorie de cette unité et résous les exercices de la page de droite.',
    page_badge:function(p){return 'p. '+p;},
    s2_c2:'💡 Comment faire',
    s2_c2d:"1. Lis l'énoncé de chaque exercice calmement.<br>2. Écris tes réponses sans regarder la théorie à gauche.<br>3. Seulement ensuite, vérifie les réponses et note les erreurs.",
    s3_title:'🃏 Étape 3 — Exercices',
    s3_desc:"Pratique avec les exercices du paquet.",
    loading:'Chargement des exercices...',
    no_ex:"Aucun exercice trouvé pour cette unité dans le paquet d'exercices.",
    no_ex_hint:"Tu peux passer à l'Étape 4.",
    btn_knew:'✓ Je savais', btn_unknown:'✗ Je ne savais pas', btn_check:'Vérifier',
    ex_pct:function(p){return p+'% correctes';}, ex_done:function(n){return n+' exercices complétés';},
    ex_knew:'Savais', ex_unknown:'Ne savais pas', btn_retry:'↺ Refaire les exercices',
    s4_title:'✅ Étape 4 — Terminer',
    s4_desc:"Revoir les résultats des exercices et marquer l'unité comme terminée.",
    s4_pct:function(p){return p+'% correctes';},
    s4_right:function(n){return '✓ '+n+' correctes';}, s4_wrong:function(n){return '✗ '+n+' incorrectes';},
    s4_of:function(n){return 'sur '+n+' exercices';},
    s4_no_stats:"Pas de données d'exercices — retourne à l'Étape 3 pour pratiquer.",
    s4_already:'Unité déjà marquée comme terminée', btn_done:"✅ Marquer l'Unité Terminée",
    unit_complete:function(n){return 'Unité '+n+' Terminée !';},
    unit_unlocked:function(n){return 'Unité '+n+' débloquée !';},
    keep_going:'Continue comme ça !', back_to_map:'← Retour à la Carte',
    err_copy:'⚠️  Aucune phrase à copier', err_save:'⚠️  Aucune phrase à sauvegarder',
    copied:'✓ Copié dans le presse-papiers !',
    saved:function(f){return '✓ Sauvegardé : '+f;},
    err_step:function(n){return "Erreur à l'Étape "+n+' :';},
  },
};

function t(k) {
  var L = LANGS[S.lang] || LANGS.pt;
  var v = (L[k] !== undefined) ? L[k] : (LANGS.pt[k] || k);
  if (typeof v === 'function') return v.apply(null, Array.prototype.slice.call(arguments, 1));
  return v;
}

function setLang(code) {
  S.lang = code;
  localStorage.setItem('lang', code);
  applyLang();
  if (S.unit && document.getElementById('module-view').style.display !== 'none') renderStep();
}

function applyLang() {
  function set(id, prop, val) { var e=document.getElementById(id); if(e) e[prop]=val; }
  set('login-sub-text',  'textContent', t('login_sub'));
  set('tab-login',       'textContent', t('tab_login'));
  set('tab-register',    'textContent', t('tab_register'));
  set('lbl-username',    'textContent', t('lbl_user'));
  set('lbl-password',    'textContent', t('lbl_pass'));
  set('login-username',  'placeholder', t('ph_user'));
  set('login-submit-btn','textContent', _loginMode === 'login' ? t('btn_login') : t('btn_register'));
  set('login-disclaimer','innerHTML',   t('disclaimer'));
  set('btn-back-map',    'textContent', t('back_map'));
  set('btn-prev-step',   'textContent', t('btn_prev'));
  var nxt = document.getElementById('btn-next-step');
  if (nxt && nxt.textContent) nxt.textContent = t('btn_next');
  ['1','2','3','4'].forEach(function(n){
    var keys = ['step1','step2','step3','step4'];
    var e = document.getElementById('sb'+n);
    if (e) e.innerHTML = '<span class="n">'+n+'</span> '+t(keys[n-1])+'<span class="step-time" id="st'+n+'"></span>';
  });
  document.querySelectorAll('.lang-btn').forEach(function(b){
    b.className = 'lang-btn' + (b.getAttribute('data-lang') === S.lang ? ' active' : '');
  });
}

// ─── Auth ─────────────────────────────────────────────────────────────────────
var _loginMode = 'login';

function switchTab(mode) {
  _loginMode = mode;
  document.getElementById('tab-login').className    = 'login-tab' + (mode==='login'    ? ' active' : '');
  document.getElementById('tab-register').className = 'login-tab' + (mode==='register' ? ' active' : '');
  document.getElementById('login-submit-btn').textContent = mode === 'login' ? t('btn_login') : t('btn_register');
  document.getElementById('login-err').textContent = '';
}

function showLoginError(msg) {
  document.getElementById('login-err').textContent = msg;
}

async function loginSubmit() {
  var username = document.getElementById('login-username').value.trim();
  var password = document.getElementById('login-password').value;
  if (!username || !password) { showLoginError('Preenche todos os campos.'); return; }
  var endpoint = _loginMode === 'login' ? '/api/login' : '/api/register';
  try {
    var res = await fetch(endpoint, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({username, password})
    }).then(r => r.json());
    if (res.error) { showLoginError(t(res.error)); return; }
    S.user  = res.username;
    S.token = res.token;
    sessionStorage.setItem('token', res.token);
    await loadProgressAndEnter();
  } catch(e) {
    showLoginError(t('err_conn'));
  }
}

async function loadProgressAndEnter() {
  var res = await fetch('/api/progress', {
    headers: {'Authorization': 'Bearer ' + S.token}
  }).then(r => r.json());
  S.done = new Set(res.done || []);

  document.getElementById('login-view').style.display  = 'none';
  document.getElementById('map-view').style.display    = '';
  document.getElementById('hdr-user').style.display    = '';
  document.getElementById('hdr-user-name').textContent = S.user;
  applyLang();

  const data = await fetch('/api/units').then(r => r.json());
  S.units = data.units;
  renderMap();
}

function logout() {
  S.user  = null;
  S.token = null;
  S.done  = new Set();
  sessionStorage.removeItem('token');
  document.getElementById('map-view').style.display    = 'none';
  document.getElementById('module-view').style.display = 'none';
  document.getElementById('login-view').style.display  = '';
  document.getElementById('hdr-user').style.display    = 'none';
  document.getElementById('btn-back-map').style.display = 'none';
  document.getElementById('hdr-unit').textContent = '';
  document.getElementById('login-username').value = '';
  document.getElementById('login-password').value = '';
  document.getElementById('login-err').textContent = '';
  stopStepTimer();
}

async function saveProgress() {
  if (!S.token) return;
  await fetch('/api/progress', {
    method: 'POST',
    headers: {'Content-Type':'application/json', 'Authorization': 'Bearer ' + S.token},
    body: JSON.stringify({done: [...S.done]})
  });
}

// ─── Init ─────────────────────────────────────────────────────────────────────
async function init() {
  applyLang();
  var saved = sessionStorage.getItem('token');
  if (saved) {
    try {
      var me = await fetch('/api/me', {headers: {'Authorization': 'Bearer ' + saved}}).then(r => r.json());
      if (me.username) {
        S.user  = me.username;
        S.token = saved;
        await loadProgressAndEnter();
        window.addEventListener('resize', function() {
          if (document.getElementById('map-view').style.display !== 'none') renderMap();
        });
        return;
      }
    } catch(_) {}
    sessionStorage.removeItem('token');
  }
  // show login
  window.addEventListener('resize', function() {
    if (document.getElementById('map-view').style.display !== 'none') renderMap();
  });
}

// ─── Map ──────────────────────────────────────────────────────────────────────
var MAP_GROUPS = [
  {label:'Present',         emoji:'🌱', from:1,   to:9},
  {label:'Past',            emoji:'⏪', from:10,  to:14},
  {label:'Present Perfect', emoji:'⭐', from:15,  to:20},
  {label:'Passive',         emoji:'🔄', from:21,  to:22},
  {label:'Verb Forms',      emoji:'🔧', from:23,  to:24},
  {label:'Future',          emoji:'🔮', from:25,  to:28},
  {label:'Modals',          emoji:'🎭', from:29,  to:36},
  {label:'There & It',      emoji:'📍', from:37,  to:39},
  {label:'Auxiliary',       emoji:'⚙️', from:40,  to:43},
  {label:'Questions',       emoji:'❓', from:44,  to:49},
  {label:'Reported Speech', emoji:'💬', from:50,  to:50},
  {label:'-ing & to',       emoji:'➡️', from:51,  to:54},
  {label:'Go/Get/Do/Make',  emoji:'🚀', from:55,  to:58},
  {label:'Pronouns',        emoji:'👤', from:59,  to:64},
  {label:'Determiners',     emoji:'🗂️', from:65,  to:84},
  {label:'Adj & Adv',       emoji:'🎨', from:85,  to:92},
  {label:'Word Order',      emoji:'📐', from:93,  to:96},
  {label:'Conjunctions',    emoji:'🔗', from:97,  to:102},
  {label:'Prepositions',    emoji:'📌', from:103, to:113},
  {label:'Phrasal Verbs',   emoji:'💥', from:114, to:115},
];
var MAP_COLS = 3;

function unitNum(u) {
  var m = (u.label || '').match(/Unidade (\d+)/);
  return m ? parseInt(m[1]) : 0;
}

function isUnlocked(num) {
  if (num <= 1) return true;
  var prev = S.units.find(function(u) { return unitNum(u) === num - 1; });
  if (!prev) return true;
  return S.done.has(prev.label);
}

// ── Map constants ──────────────────────────────────────────────────────────────
var MAP_W    = 720;
var NODE_R   = 42;            // node radius px
var COL_X    = [80, 360, 640]; // left / center / right
var ROW_H    = 186;            // vertical distance between node rows
var ZHH      = 90;             // zone header height
var GRP_COLS = [              // zone banner accent colors per group index
  '#22c55e','#f97316','#3b82f6','#a855f7','#ec4899',
  '#06b6d4','#f59e0b','#84cc16','#8b5cf6','#ef4444',
  '#14b8a6','#fb923c','#22c55e','#60a5fa','#a78bfa',
  '#fb923c','#4ade80','#f472b6','#38bdf8','#e879f9'
];

function hexRgb(h) {
  return parseInt(h.slice(1,3),16)+','+parseInt(h.slice(3,5),16)+','+parseInt(h.slice(5,7),16);
}

// ── SVG decoration helpers ─────────────────────────────────────────────────────
function _star5(cx, cy, ro, ri, col) {
  var pts = [];
  for (var i = 0; i < 10; i++) {
    var a = Math.PI * i / 5 - Math.PI/2;
    var r = (i % 2 === 0) ? ro : ri;
    pts.push((cx + r*Math.cos(a)).toFixed(1) + ',' + (cy + r*Math.sin(a)).toFixed(1));
  }
  return '<polygon points="' + pts.join(' ') + '" fill="' + col + '"/>';
}

function _gear(cx, cy, ro, ri, n, col) {
  var pts = [];
  var step = Math.PI * 2 / n;
  var hw = step * 0.35;
  for (var i = 0; i < n; i++) {
    var a = step * i - Math.PI / 2;
    pts.push((cx+ri*Math.cos(a-hw)).toFixed(1)+','+(cy+ri*Math.sin(a-hw)).toFixed(1));
    pts.push((cx+ro*Math.cos(a-hw*.3)).toFixed(1)+','+(cy+ro*Math.sin(a-hw*.3)).toFixed(1));
    pts.push((cx+ro*Math.cos(a+hw*.3)).toFixed(1)+','+(cy+ro*Math.sin(a+hw*.3)).toFixed(1));
    pts.push((cx+ri*Math.cos(a+hw)).toFixed(1)+','+(cy+ri*Math.sin(a+hw)).toFixed(1));
  }
  return '<polygon points="' + pts.join(' ') + '" fill="' + col + '"/>'
       + '<circle cx="'+cx+'" cy="'+cy+'" r="'+(ri*.44).toFixed(1)+'" fill="none" stroke="'+col+'" stroke-width="2.5"/>';
}

function zoneDecor(gIdx, zy, zoneH, col) {
  var rgb   = hexRgb(col);
  var c     = 'rgba(' + rgb + ',.18)';
  var c2    = 'rgba(' + rgb + ',.10)';
  var mid   = zy + ZHH + (zoneH - ZHH) / 2;
  var x1 = 220, x2 = 500;
  var s = '<g>';

  switch (gIdx) {
    case 0: // Present — sun + sprout
      s += '<circle cx="'+x1+'" cy="'+mid+'" r="28" fill="'+c+'"/>';
      for (var i=0;i<8;i++){var ra=Math.PI*2*i/8;s+='<line x1="'+(x1+36*Math.cos(ra)).toFixed(1)+'" y1="'+(mid+36*Math.sin(ra)).toFixed(1)+'" x2="'+(x1+46*Math.cos(ra)).toFixed(1)+'" y2="'+(mid+46*Math.sin(ra)).toFixed(1)+'" stroke="'+c+'" stroke-width="4" stroke-linecap="round"/>';}
      s += '<circle cx="'+x2+'" cy="'+(mid+8)+'" r="16" fill="'+c+'"/>';
      s += '<line x1="'+x2+'" y1="'+(mid+24)+'" x2="'+x2+'" y2="'+(mid-6)+'" stroke="'+c+'" stroke-width="3.5" stroke-linecap="round"/>';
      s += '<path d="M'+x2+','+(mid+10)+' Q'+(x2-18)+','+(mid-8)+' '+(x2-26)+','+(mid)+'" fill="none" stroke="'+c+'" stroke-width="3" stroke-linecap="round"/>';
      s += '<path d="M'+x2+','+(mid+4)+' Q'+(x2+18)+','+(mid-10)+' '+(x2+24)+','+(mid-2)+'" fill="none" stroke="'+c+'" stroke-width="3" stroke-linecap="round"/>';
      break;

    case 1: // Past — clock + hourglass
      s += '<circle cx="'+x1+'" cy="'+mid+'" r="27" fill="none" stroke="'+c+'" stroke-width="5"/>';
      s += '<line x1="'+x1+'" y1="'+mid+'" x2="'+x1+'" y2="'+(mid-20)+'" stroke="'+c+'" stroke-width="4.5" stroke-linecap="round"/>';
      s += '<line x1="'+x1+'" y1="'+mid+'" x2="'+(x1+14)+'" y2="'+(mid+9)+'" stroke="'+c+'" stroke-width="3.5" stroke-linecap="round"/>';
      s += '<circle cx="'+x1+'" cy="'+mid+'" r="4" fill="'+c+'"/>';
      // hourglass
      s += '<polygon points="'+(x2-17)+','+(mid-25)+' '+(x2+17)+','+(mid-25)+' '+x2+','+mid+' '+(x2+17)+','+(mid+25)+' '+(x2-17)+','+(mid+25)+' '+x2+','+mid+'" fill="'+c+'"/>';
      s += '<line x1="'+(x2-18)+'" y1="'+(mid-25)+'" x2="'+(x2+18)+'" y2="'+(mid-25)+'" stroke="'+c+'" stroke-width="4" stroke-linecap="round"/>';
      s += '<line x1="'+(x2-18)+'" y1="'+(mid+25)+'" x2="'+(x2+18)+'" y2="'+(mid+25)+'" stroke="'+c+'" stroke-width="4" stroke-linecap="round"/>';
      break;

    case 2: // Present Perfect — star cluster
      s += _star5(x1, mid-8, 26, 11, c);
      s += _star5(x1-22, mid+16, 14, 6, c2);
      s += _star5(x1+20, mid+14, 11, 4, c2);
      s += _star5(x2, mid-6, 22, 9, c);
      s += _star5(x2+22, mid+14, 13, 5, c2);
      s += _star5(x2-20, mid+18, 10, 4, c2);
      break;

    case 3: // Passive — circular arrows
      s += '<circle cx="'+x1+'" cy="'+mid+'" r="24" fill="none" stroke="'+c+'" stroke-width="5" stroke-dasharray="46 20" stroke-linecap="round"/>';
      s += '<polygon points="'+x1+','+(mid-32)+' '+(x1-8)+','+(mid-20)+' '+(x1+8)+','+(mid-20)+'" fill="'+c+'"/>';
      s += '<circle cx="'+x2+'" cy="'+mid+'" r="20" fill="none" stroke="'+c+'" stroke-width="4" stroke-dasharray="36 18" stroke-linecap="round" transform="rotate(180,'+x2+','+mid+')"/>';
      s += '<polygon points="'+x2+','+(mid+28)+' '+(x2-7)+','+(mid+16)+' '+(x2+7)+','+(mid+16)+'" fill="'+c+'"/>';
      break;

    case 4: // Verb Forms — crossed wrenches
      s += '<rect x="'+(x1-5)+'" y="'+(mid-28)+'" width="10" height="36" rx="4" fill="'+c+'" transform="rotate(40,'+x1+','+mid+')"/>';
      s += '<circle cx="'+(x1-11)+'" cy="'+(mid-22)+'" r="9" fill="none" stroke="'+c+'" stroke-width="4.5"/>';
      s += '<circle cx="'+(x1+11)+'" cy="'+(mid+22)+'" r="9" fill="none" stroke="'+c+'" stroke-width="4.5"/>';
      s += '<rect x="'+(x2-5)+'" y="'+(mid-28)+'" width="10" height="36" rx="4" fill="'+c+'" transform="rotate(-40,'+x2+','+mid+')"/>';
      s += '<circle cx="'+(x2+11)+'" cy="'+(mid-22)+'" r="9" fill="none" stroke="'+c+'" stroke-width="4.5"/>';
      s += '<circle cx="'+(x2-11)+'" cy="'+(mid+22)+'" r="9" fill="none" stroke="'+c+'" stroke-width="4.5"/>';
      break;

    case 5: // Future — rocket + shooting star
      s += '<ellipse cx="'+x1+'" cy="'+(mid-4)+'" rx="11" ry="22" fill="'+c+'"/>';
      s += '<polygon points="'+x1+','+(mid-26)+' '+(x1-11)+','+(mid-4)+' '+(x1+11)+','+(mid-4)+'" fill="'+c+'"/>';
      s += '<polygon points="'+(x1-11)+','+(mid+8)+' '+(x1-22)+','+(mid+24)+' '+(x1-3)+','+(mid+10)+'" fill="'+c+'"/>';
      s += '<polygon points="'+(x1+11)+','+(mid+8)+' '+(x1+22)+','+(mid+24)+' '+(x1+3)+','+(mid+10)+'" fill="'+c+'"/>';
      s += '<ellipse cx="'+x1+'" cy="'+(mid+28)+'" rx="6" ry="10" fill="'+c2+'"/>';
      s += _star5(x2, mid-8, 18, 7, c);
      s += '<line x1="'+(x2+14)+'" y1="'+(mid-18)+'" x2="'+(x2+32)+'" y2="'+(mid-32)+'" stroke="'+c+'" stroke-width="2.5" stroke-linecap="round"/>';
      s += '<line x1="'+(x2+12)+'" y1="'+(mid-9)+'" x2="'+(x2+26)+'" y2="'+(mid-20)+'" stroke="'+c2+'" stroke-width="2" stroke-linecap="round"/>';
      break;

    case 6: // Modals — speech bubbles
      s += '<rect x="'+(x1-24)+'" y="'+(mid-20)+'" width="48" height="34" rx="9" fill="'+c+'"/>';
      s += '<polygon points="'+(x1-6)+','+(mid+14)+' '+(x1-18)+','+(mid+28)+' '+(x1+4)+','+(mid+14)+'" fill="'+c+'"/>';
      s += '<circle cx="'+(x1-9)+'" cy="'+mid+'" r="3.5" fill="rgba(0,0,0,.18)"/>';
      s += '<circle cx="'+x1+'" cy="'+mid+'" r="3.5" fill="rgba(0,0,0,.18)"/>';
      s += '<circle cx="'+(x1+9)+'" cy="'+mid+'" r="3.5" fill="rgba(0,0,0,.18)"/>';
      s += '<rect x="'+(x2-18)+'" y="'+(mid-28)+'" width="36" height="26" rx="7" fill="'+c2+'"/>';
      s += '<polygon points="'+(x2+4)+','+(mid-2)+' '+(x2+14)+','+(mid+12)+' '+(x2+16)+','+(mid-2)+'" fill="'+c2+'"/>';
      break;

    case 7: // There & It — map pin + house
      s += '<circle cx="'+x1+'" cy="'+(mid-14)+'" r="18" fill="'+c+'"/>';
      s += '<path d="M'+(x1-13)+','+(mid-8)+' Q'+x1+','+(mid+22)+' '+(x1+13)+','+(mid-8)+'" fill="'+c+'"/>';
      s += '<circle cx="'+x1+'" cy="'+(mid-14)+'" r="7" fill="rgba(0,0,0,.15)"/>';
      s += '<polygon points="'+(x2-22)+','+mid+' '+x2+','+(mid-26)+' '+(x2+22)+','+mid+'" fill="'+c+'"/>';
      s += '<rect x="'+(x2-17)+'" y="'+mid+'" width="34" height="22" rx="1" fill="'+c+'"/>';
      s += '<rect x="'+(x2-6)+'" y="'+(mid+10)+'" width="12" height="12" fill="rgba(0,0,0,.15)"/>';
      break;

    case 8: // Auxiliary — double gear
      s += _gear(x1-4, mid, 26, 17, 8, c);
      s += _gear(x1+20, mid+16, 16, 10, 6, c2);
      s += _gear(x2, mid, 24, 15, 7, c);
      s += _gear(x2+20, mid-14, 14, 9, 5, c2);
      break;

    case 9: // Questions — question marks
      s += '<text x="'+x1+'" y="'+(mid+16)+'" text-anchor="middle" font-size="56" font-weight="900" fill="'+c+'" font-family="Georgia,serif">?</text>';
      s += '<text x="'+x2+'" y="'+(mid+12)+'" text-anchor="middle" font-size="40" font-weight="900" fill="'+c2+'" font-family="Georgia,serif">?</text>';
      break;

    case 10: // Reported Speech — speech bubbles (outline style)
      s += '<rect x="'+(x1-26)+'" y="'+(mid-22)+'" width="52" height="36" rx="10" fill="none" stroke="'+c+'" stroke-width="4.5"/>';
      s += '<line x1="'+(x1-14)+'" y1="'+(mid-8)+'" x2="'+(x1+14)+'" y2="'+(mid-8)+'" stroke="'+c+'" stroke-width="3.5" stroke-linecap="round"/>';
      s += '<line x1="'+(x1-14)+'" y1="'+mid+'" x2="'+(x1+6)+'" y2="'+mid+'" stroke="'+c+'" stroke-width="3.5" stroke-linecap="round"/>';
      s += '<polygon points="'+(x1+8)+','+(mid+14)+' '+(x1+18)+','+(mid+28)+' '+(x1+20)+','+(mid+14)+'" fill="none" stroke="'+c+'" stroke-width="3"/>';
      s += '<text x="'+(x2-7)+'" y="'+(mid+8)+'" text-anchor="middle" font-size="40" font-weight="900" fill="'+c+'" font-family="Georgia,serif">"</text>';
      s += '<text x="'+(x2+10)+'" y="'+(mid+14)+'" text-anchor="middle" font-size="40" font-weight="900" fill="'+c2+'" font-family="Georgia,serif">"</text>';
      break;

    case 11: // -ing & to — arrows
      s += '<path d="M'+(x1-22)+','+(mid+14)+' Q'+(x1-22)+','+(mid-16)+' '+(x1+18)+','+(mid-16)+'" fill="none" stroke="'+c+'" stroke-width="6" stroke-linecap="round"/>';
      s += '<polygon points="'+(x1+12)+','+(mid-26)+' '+(x1+26)+','+(mid-16)+' '+(x1+12)+','+(mid-6)+'" fill="'+c+'"/>';
      s += '<line x1="'+(x2-22)+'" y1="'+mid+'" x2="'+(x2+14)+'" y2="'+mid+'" stroke="'+c+'" stroke-width="6" stroke-linecap="round"/>';
      s += '<polygon points="'+(x2+8)+','+(mid-11)+' '+(x2+24)+','+mid+' '+(x2+8)+','+(mid+11)+'" fill="'+c+'"/>';
      break;

    case 12: // Go/Get/Do/Make — rocket (bold) + running figure
      s += '<ellipse cx="'+x1+'" cy="'+(mid-4)+'" rx="13" ry="26" fill="'+c+'"/>';
      s += '<polygon points="'+x1+','+(mid-30)+' '+(x1-13)+','+(mid-4)+' '+(x1+13)+','+(mid-4)+'" fill="'+c+'"/>';
      s += '<polygon points="'+(x1-13)+','+(mid+8)+' '+(x1-26)+','+(mid+26)+' '+(x1-4)+','+(mid+10)+'" fill="'+c+'"/>';
      s += '<polygon points="'+(x1+13)+','+(mid+8)+' '+(x1+26)+','+(mid+26)+' '+(x1+4)+','+(mid+10)+'" fill="'+c+'"/>';
      s += '<ellipse cx="'+x1+'" cy="'+(mid+32)+'" rx="8" ry="13" fill="'+c2+'"/>';
      // running figure
      s += '<circle cx="'+x2+'" cy="'+(mid-20)+'" r="9" fill="'+c+'"/>';
      s += '<line x1="'+x2+'" y1="'+(mid-11)+'" x2="'+x2+'" y2="'+(mid+8)+'" stroke="'+c+'" stroke-width="5" stroke-linecap="round"/>';
      s += '<line x1="'+x2+'" y1="'+(mid-4)+'" x2="'+(x2-14)+'" y2="'+(mid+4)+'" stroke="'+c+'" stroke-width="3.5" stroke-linecap="round"/>';
      s += '<line x1="'+x2+'" y1="'+(mid-4)+'" x2="'+(x2+14)+'" y2="'+(mid-2)+'" stroke="'+c+'" stroke-width="3.5" stroke-linecap="round"/>';
      s += '<line x1="'+x2+'" y1="'+(mid+8)+'" x2="'+(x2-14)+'" y2="'+(mid+20)+'" stroke="'+c+'" stroke-width="3.5" stroke-linecap="round"/>';
      s += '<line x1="'+x2+'" y1="'+(mid+8)+'" x2="'+(x2+16)+'" y2="'+(mid+18)+'" stroke="'+c+'" stroke-width="3.5" stroke-linecap="round"/>';
      break;

    case 13: // Pronouns — two person silhouettes
      for (var pi=0;pi<2;pi++){var px=pi?x2:x1;s+='<circle cx="'+px+'" cy="'+(mid-18)+'" r="11" fill="'+c+'"/>';s+='<path d="M'+(px-16)+','+(mid+18)+' Q'+(px-16)+','+(mid-4)+' '+px+','+(mid-4)+' Q'+(px+16)+','+(mid-4)+' '+(px+16)+','+(mid+18)+' Z" fill="'+c+'"/>';}
      break;

    case 14: // Determiners — stacked books
      var books = [[-20,40,12],[-6,36,12],[8,44,12]];
      for (var bi=0;bi<books.length;bi++){s+='<rect x="'+(x1-books[bi][1]/2)+'" y="'+(mid+books[bi][0])+'" width="'+books[bi][1]+'" height="'+books[bi][2]+'" rx="2" fill="'+c+'"/>';}
      var books2 = [[-22,42,11],[-8,38,11],[6,46,11]];
      for (var bi2=0;bi2<books2.length;bi2++){s+='<rect x="'+(x2-books2[bi2][1]/2)+'" y="'+(mid+books2[bi2][0])+'" width="'+books2[bi2][1]+'" height="'+books2[bi2][2]+'" rx="2" fill="'+c+'"/>';}
      break;

    case 15: // Adj & Adv — palette + brush
      s += '<ellipse cx="'+x1+'" cy="'+mid+'" rx="26" ry="22" fill="'+c+'"/>';
      var dots=[[-12,-10],[0,-14],[12,-10],[16,2]];
      for(var di=0;di<dots.length;di++){s+='<circle cx="'+(x1+dots[di][0])+'" cy="'+(mid+dots[di][1])+'" r="5.5" fill="rgba(0,0,0,.2)"/>';}
      s += '<rect x="'+(x2-5)+'" y="'+(mid-28)+'" width="10" height="34" rx="4" fill="'+c+'"/>';
      s += '<path d="M'+(x2-7)+','+(mid+6)+' L'+(x2+7)+','+(mid+6)+' L'+(x2+5)+','+(mid+20)+' L'+x2+','+(mid+24)+' L'+(x2-5)+','+(mid+20)+' Z" fill="'+c+'"/>';
      break;

    case 16: // Word Order — ruler + grid
      s += '<rect x="'+(x1-28)+'" y="'+(mid-10)+'" width="56" height="20" rx="4" fill="'+c+'"/>';
      for(var ti=0;ti<=6;ti++){var tx=(x1-22)+ti*8;var th=ti%2===0?10:6;s+='<line x1="'+tx+'" y1="'+(mid+10)+'" x2="'+tx+'" y2="'+(mid+10-th)+'" stroke="rgba(0,0,0,.25)" stroke-width="1.5"/>';}
      for(var gx=0;gx<3;gx++){for(var gy=0;gy<3;gy++){s+='<rect x="'+(x2-15+gx*11)+'" y="'+(mid-15+gy*11)+'" width="9" height="9" rx="1.5" fill="'+c+'"/>';}}
      break;

    case 17: // Conjunctions — chain links
      for(var ci=0;ci<3;ci++){s+='<ellipse cx="'+(x1-20+ci*20)+'" cy="'+mid+'" rx="11" ry="8" fill="none" stroke="'+c+'" stroke-width="5"/>';}
      for(var ci2=0;ci2<3;ci2++){s+='<ellipse cx="'+x2+'" cy="'+(mid-18+ci2*18)+'" rx="8" ry="11" fill="none" stroke="'+c+'" stroke-width="4"/>';}
      break;

    case 18: // Prepositions — compass + pin
      s += '<circle cx="'+x1+'" cy="'+mid+'" r="26" fill="none" stroke="'+c+'" stroke-width="4.5"/>';
      s += '<polygon points="'+x1+','+(mid-28)+' '+(x1-8)+','+mid+' '+x1+','+(mid-10)+'" fill="'+c+'"/>';
      s += '<polygon points="'+x1+','+(mid+28)+' '+(x1+8)+','+mid+' '+x1+','+(mid+10)+'" fill="'+c2+'"/>';
      s += '<circle cx="'+x1+'" cy="'+mid+'" r="5" fill="'+c+'"/>';
      s += '<circle cx="'+x2+'" cy="'+(mid-14)+'" r="16" fill="none" stroke="'+c+'" stroke-width="4"/>';
      s += '<circle cx="'+x2+'" cy="'+(mid-14)+'" r="6" fill="'+c+'"/>';
      s += '<path d="M'+(x2-11)+','+(mid-4)+' Q'+x2+','+(mid+20)+' '+(x2+11)+','+(mid-4)+'" fill="'+c+'"/>';
      break;

    case 19: // Phrasal Verbs — lightning bolt + starburst
      s += '<polygon points="'+x1+','+(mid-30)+' '+(x1-16)+','+mid+' '+(x1+4)+','+mid+' '+(x1-8)+','+(mid+30)+' '+(x1+18)+','+(mid-2)+' '+(x1-2)+','+(mid-2)+'" fill="'+c+'"/>';
      var ep=[];for(var ei=0;ei<16;ei++){var ea=Math.PI*2*ei/16;var er=ei%2===0?24:14;ep.push((x2+er*Math.cos(ea)).toFixed(1)+','+(mid+er*Math.sin(ea)).toFixed(1));}
      s += '<polygon points="'+ep.join(' ')+'" fill="'+c+'"/>';
      break;

    default: break;
  }
  s += '</g>';
  return s;
}

function renderMap() {
  // Progress
  var totalDone = S.units.filter(function(u){ return S.done.has(u.label); }).length;
  var pct = (totalDone / 115 * 100).toFixed(1);
  var fill  = document.getElementById('map-prog-fill');
  var lbl   = document.getElementById('map-prog-label');
  var pctEl = document.getElementById('map-prog-pct');
  if (fill)  fill.style.width   = pct + '%';
  if (lbl)   lbl.textContent    = t('prog', totalDone, 115);
  if (pctEl) pctEl.textContent  = Math.round(pct) + '%';

  var el = document.getElementById('map-container');
  if (!el) return;

  // ── Responsive scale (layout-native, no CSS transform) ───────────────────
  var view_ = document.getElementById('map-view');
  var S_    = view_ ? Math.min(1, (view_.clientWidth - 32) / MAP_W) : 1;
  var mw    = Math.round(MAP_W * S_);
  var r_    = Math.round(ROW_H * S_);
  var z_    = Math.round(ZHH * S_);
  var n_    = Math.round(NODE_R * S_);
  var nodeSz = n_ * 2;

  // ── Build node list + zone headers ────────────────────────────────────────
  var nodes  = [];
  var zones  = [];
  var absRow = 0;
  var y      = Math.round(14 * S_);

  for (var g = 0; g < MAP_GROUPS.length; g++) {
    var grp   = MAP_GROUPS[g];
    var color = GRP_COLS[g] || '#6b7280';
    var gu    = S.units.filter(function(u){
      var n = unitNum(u);
      return n >= grp.from && n <= grp.to;
    });
    if (!gu.length) continue;

    var doneG = gu.filter(function(u){ return S.done.has(u.label); }).length;
    zones.push({ label:grp.label, emoji:grp.emoji, color:color, y:y, doneG:doneG, totalG:gu.length, gIdx:g });
    y += Math.round((ZHH + NODE_R + 18) * S_);

    var nRows = Math.ceil(gu.length / MAP_COLS);
    for (var r = 0; r < nRows; r++) {
      var rtl      = (absRow % 2 === 1);
      var rowUnits = gu.slice(r * MAP_COLS, (r + 1) * MAP_COLS);
      for (var j = 0; j < rowUnits.length; j++) {
        var colI = rtl ? (MAP_COLS - 1 - j) : j;
        if (colI >= COL_X.length) colI = COL_X.length - 1;
        nodes.push({ u: rowUnits[j], x: Math.round(COL_X[colI] * S_), y: y, color: color });
      }
      y += r_;
      absRow++;
    }
    y += Math.round(36 * S_);
  }

  var totalH = y + Math.round(40 * S_);

  // ── SVG path through nodes ────────────────────────────────────────────────
  function makePath(nodeList) {
    if (!nodeList.length) return '';
    var d = 'M' + nodeList[0].x + ' ' + nodeList[0].y;
    for (var i = 1; i < nodeList.length; i++) {
      var p = nodeList[i-1], c = nodeList[i];
      if (Math.abs(c.y - p.y) < 4) {
        d += ' L' + c.x + ' ' + c.y;
      } else {
        var bulge = Math.round(84 * S_);
        var dir   = p.x > mw / 2 ? 1 : -1;
        var cx    = p.x + dir * bulge;
        d += ' C' + cx + ' ' + p.y + ' ' + cx + ' ' + c.y + ' ' + c.x + ' ' + c.y;
      }
    }
    return d;
  }

  var lastDoneIdx = -1;
  for (var i = 0; i < nodes.length; i++) {
    if (S.done.has(nodes[i].u.label)) lastDoneIdx = i;
    else break;
  }

  var fullPath = makePath(nodes);
  var donePath = lastDoneIdx >= 0 ? makePath(nodes.slice(0, lastDoneIdx + 1)) : '';

  // ── Build HTML ────────────────────────────────────────────────────────────
  var svg = '<svg style="position:absolute;top:0;left:0;width:' + mw + 'px;height:' + totalH + 'px;'
          + 'pointer-events:none;overflow:visible;z-index:1">';

  // Zone decorations behind road — pass unscaled coords and wrap with SVG scale
  for (var zi = 0; zi < zones.length; zi++) {
    var zEnd  = zi + 1 < zones.length ? zones[zi + 1].y : totalH;
    var zh_   = zEnd - zones[zi].y;
    var decor = zoneDecor(zones[zi].gIdx, zones[zi].y / S_, zh_ / S_, zones[zi].color);
    svg += S_ < 1 ? '<g transform="scale(' + S_.toFixed(4) + ')">' + decor + '</g>' : decor;
  }

  svg += '<path d="' + fullPath + '" fill="none" stroke="#08090a" stroke-width="22" stroke-linecap="round" stroke-linejoin="round"/>';
  svg += '<path d="' + fullPath + '" fill="none" stroke="#1c1a10" stroke-width="14" stroke-linecap="round" stroke-linejoin="round"/>';
  svg += '<path d="' + fullPath + '" fill="none" stroke="rgba(255,255,255,.07)" stroke-width="2.5" stroke-dasharray="20 26" stroke-linecap="round"/>';

  if (donePath) {
    svg += '<path d="' + donePath + '" fill="none" stroke="#071c0e" stroke-width="22" stroke-linecap="round" stroke-linejoin="round"/>';
    svg += '<path d="' + donePath + '" fill="none" stroke="#14532d" stroke-width="14" stroke-linecap="round" stroke-linejoin="round"/>';
    svg += '<path d="' + donePath + '" fill="none" stroke="#16a34a" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" opacity=".6"/>';
    svg += '<path d="' + donePath + '" fill="none" stroke="rgba(134,239,172,.55)" stroke-width="3" stroke-dasharray="22 999" stroke-linecap="round">'
        +  '<animate attributeName="stroke-dashoffset" from="0" to="-' + (totalH * 2) + '" dur="2.5s" repeatCount="indefinite"/>'
        +  '</path>';
  }
  svg += '</svg>';

  // Zone banners
  var zoneSvg = '';
  for (var z = 0; z < zones.length; z++) {
    var zh = zones[z];
    var rgb = hexRgb(zh.color);
    var cntHtml = zh.doneG > 0
      ? '<span class="mz-banner-cnt" style="color:' + zh.color + '">' + zh.doneG + '/' + zh.totalG + '</span>'
      : '';
    zoneSvg += '<div class="mz-banner" style="'
      + 'top:' + zh.y + 'px;height:' + Math.round((ZHH - 10) * S_) + 'px;'
      + 'background:linear-gradient(90deg,rgba(' + rgb + ',.13) 0%,rgba(' + rgb + ',.04) 60%,transparent 100%);'
      + 'border-color:' + zh.color + ';'
      + 'box-shadow:0 0 0 1px rgba(' + rgb + ',.12) inset;'
      + '">'
      + '<span class="mz-banner-emoji">' + zh.emoji + '</span>'
      + '<span class="mz-banner-name" style="color:' + zh.color + '">' + escHtml(zh.label) + '</span>'
      + cntHtml
      + '</div>';
  }

  // Nodes
  var nodeHtml = '';
  for (var i = 0; i < nodes.length; i++) {
    var nd    = nodes[i];
    var u     = nd.u;
    var num   = unitNum(u);
    var done  = S.done.has(u.label);
    var unlkd = isUnlocked(num);
    var tip   = u.label.replace(/^Unidade \d+ — /, '');
    var delay = (i * 0.022).toFixed(3);
    var cls   = 'mn' + (done ? ' mn-done' : !unlkd ? ' mn-locked' : ' mn-active');
    var attrs = unlkd ? ' data-lbl="' + escHtml(u.label) + '" onclick="mapClick(this)"' : '';

    var inner = '<span class="mn-num">' + num + '</span>';
    if      (done)  inner += '<span class="mn-sub">✓</span>';
    else if (!unlkd) inner += '<span class="mn-sub">🔒</span>';

    nodeHtml += '<button class="' + cls + '" title="' + escHtml(tip) + '"'
      + attrs
      + ' style="left:' + nd.x + 'px;top:' + nd.y + 'px;animation-delay:' + delay + 's"'
      + '>' + inner + '</button>';
    nodeHtml += '<div class="mn-lbl' + (!unlkd ? ' mn-lbl-locked' : '') + '"'
      + ' style="left:' + nd.x + 'px;top:' + (nd.y + n_ + 5) + 'px"'
      + '>' + escHtml(tip) + '</div>';
  }

  // Set container dimensions, CSS vars for node size, and inject
  el.style.width  = mw + 'px';
  el.style.height = totalH + 'px';
  el.style.setProperty('--mn-sz', nodeSz + 'px');
  el.style.setProperty('--mn-fs', Math.max(10, Math.round(17 * S_)) + 'px');
  el.style.setProperty('--mn-ss', Math.max(8,  Math.round(12 * S_)) + 'px');
  el.style.setProperty('--mn-ls', Math.max(8,  Math.round(9.5 * S_)) + 'px');
  el.style.setProperty('--mn-lw', Math.round(108 * S_) + 'px');
  el.style.animation = 'none';
  void el.offsetHeight;
  el.style.animation = '';
  el.innerHTML = svg + zoneSvg + nodeHtml;
}


function mapClick(el) {
  var label = el.getAttribute('data-lbl');
  var i = S.units.findIndex(function(u) { return u.label === label; });
  if (i >= 0) selectUnit(i);
}

// ─── Unit selection ───────────────────────────────────────────────────────────
async function selectUnit(i) {
  var u = S.units[i];
  var data = await fetch('/api/unit/' + encodeURIComponent(u.label)).then(r => r.json());
  S.unit = { label: u.label, notes: data.notes, page: data.page, name: data.name };
  S.step = 1; S.noteIdx = 0;
  S.sentences = [
    {text:'',tokens:[],selIdx:-1,hide:'',hint:''},
    {text:'',tokens:[],selIdx:-1,hide:'',hint:''},
    {text:'',tokens:[],selIdx:-1,hide:'',hint:''},
  ];
  S.exCards = []; S.exIdx = 0; S.exRevealed = false;
  S.exKnown = 0; S.exUnknown = 0; S.exDone = false;
  S.stepTimes = [0, 0, 0, 0]; S.stepStart = 0;
  stopAudio();

  document.getElementById('map-view').style.display    = 'none';
  document.getElementById('module-view').style.display = 'flex';
  document.getElementById('hdr-unit').textContent       = u.label;
  document.getElementById('btn-back-map').style.display = '';

  startStepTimer();
  renderStep();
}

function returnToMap() {
  stopAudio();
  stopStepTimer();
  S.unit = null;
  document.getElementById('module-view').style.display = 'none';
  document.getElementById('map-view').style.display    = '';
  document.getElementById('hdr-unit').textContent       = '';
  document.getElementById('btn-back-map').style.display = 'none';
  renderMap();
  // Scroll to first unlocked-but-not-done node
  setTimeout(function() {
    var active = document.querySelector('.mn.mn-active');
    if (active) active.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, 80);
}

// ─── Timer ────────────────────────────────────────────────────────────────────
var STEP_LIMITS = [900, 900, 900, 0]; // segundos máx por passo; 0 = sem timer

function fmtTime(sec) {
  var m = Math.floor(sec / 60), s = sec % 60;
  return m + ':' + (s < 10 ? '0' : '') + s;
}

function startStepTimer() {
  clearInterval(S.timerInt);
  S.stepStart = Date.now();
  var limit = STEP_LIMITS[S.step - 1];
  var el = document.getElementById('step-timer');
  if (!limit) { if (el) el.style.display = 'none'; return; }
  if (el) { el.style.display = ''; el.className = ''; el.textContent = fmtTime(limit); }
  S.timerInt = setInterval(function() {
    var el = document.getElementById('step-timer');
    if (!el) return;
    var remaining = limit - Math.floor((Date.now() - S.stepStart) / 1000);
    if (remaining <= 0) {
      el.textContent = '0:00';
      el.className = 'urgent';
      clearInterval(S.timerInt); S.timerInt = null;
    } else {
      el.textContent = fmtTime(remaining);
      el.className = remaining <= 60 ? 'warning' : '';
    }
  }, 500);
}

function stopStepTimer() {
  clearInterval(S.timerInt);
  S.timerInt = null;
  var el = document.getElementById('step-timer');
  if (el) el.style.display = 'none';
}

function recordStepTime() {
  if (S.stepStart) {
    S.stepTimes[S.step - 1] += Math.floor((Date.now() - S.stepStart) / 1000);
  }
}

// ─── Steps ────────────────────────────────────────────────────────────────────
function goStep(n) {
  recordStepTime();
  S.step = n;
  startStepTimer();
  renderStep();
}
function prevStep() { if (S.step > 1) goStep(S.step - 1); }
function nextStep() { if (S.step < 4) goStep(S.step + 1); }

function renderStep() {
  [1,2,3,4].forEach(function(n) {
    var btn = document.getElementById('sb' + n);
    btn.className = 'step-btn' +
      (S.step === n ? ' active' : '') +
      (n < S.step   ? ' done'   : '');
    var tEl = document.getElementById('st' + n);
    if (tEl) tEl.textContent = (n < S.step && S.stepTimes[n-1] > 0) ? fmtTime(S.stepTimes[n-1]) : '';
  });
  document.getElementById('btn-prev-step').disabled      = S.step === 1;
  document.getElementById('btn-next-step').textContent   = t('btn_next');
  document.getElementById('btn-next-step').onclick       = nextStep;
  document.getElementById('btn-next-step').disabled      = (S.step === 4);
  document.getElementById('nav-footer').style.display    = 'flex';

  stopAudio();
  var c = document.getElementById('step-content');
  try {
    if      (S.step === 1) c.innerHTML = buildStep1();
    else if (S.step === 2) c.innerHTML = buildStep2();
    else if (S.step === 3) { c.innerHTML = buildStep3(); loadExCards(); }
    else if (S.step === 4) c.innerHTML = buildStep4();
  } catch(e) {
    c.innerHTML = '<div style="padding:20px;color:#f87171;font-family:monospace">'
      + '<b>' + t('err_step', S.step) + '</b><br>' + escHtml(String(e)) + '</div>';
  }
}

// ── Step 1: Theory ────────────────────────────────────────────────────────────
function buildStep1() {
  const notes = S.unit.notes;
  const n = notes.length;
  const note = notes[S.noteIdx] || {};
  return `
  <div class="step-header">
    <h2>${t('s1_title')}</h2>
    <p>${t('s1_desc')}</p>
  </div>
  <div id="img-nav">
    <button onclick="changeImg(-1)" ${S.noteIdx===0?'disabled':''}>${t('img_prev')}</button>
    <span id="img-counter">${S.noteIdx+1} / ${n}</span>
    <button onclick="changeImg(1)"  ${S.noteIdx===n-1?'disabled':''}>${t('img_next')}</button>
    <button id="btn-audio" ${note.aud?'':'disabled'} onclick="playAudio('${note.aud||''}')">
      ${t('btn_audio')}
    </button>
  </div>
  <div id="img-box">
    ${note.img
      ? `<img src="/media/${note.img}" alt="teoria">`
      : `<span style="color:var(--muted)">${t('no_img')}</span>`}
  </div>`;
}

function changeImg(dir) {
  const n = S.unit.notes.length;
  S.noteIdx = Math.max(0, Math.min(n-1, S.noteIdx + dir));
  document.getElementById('step-content').innerHTML = buildStep1();
  stopAudio();
}

// ── Step 2: Exercises ─────────────────────────────────────────────────────────
function buildStep2() {
  const page = S.unit.page || '—';
  return `
  <div class="step-header">
    <h2>${t('s2_title')}</h2>
    <p>${t('s2_desc')}</p>
  </div>
  <div class="exercise-card">
    <h3>${t('s2_c1')}</h3>
    <p>${t('s2_c1d')}</p>
    <div class="page-badge">${t('page_badge', page)}</div>
  </div>
  <div class="exercise-card">
    <h3>${t('s2_c2')}</h3>
    <p>${t('s2_c2d')}</p>
  </div>`;
}

// ── Step 3: Exercise Review ───────────────────────────────────────────────────
function buildStep3() {
  return '<div class="step-header"><h2>'+t('s3_title')+'</h2>'
       + '<p>'+t('s3_desc')+'</p></div>'
       + '<div id="ex-body"><div style="color:var(--muted);padding:20px;font-size:13px">'+t('loading')+'</div></div>';
}

async function loadExCards() {
  if (!S.unit) return;
  try {
    const data = await fetch('/api/exercises/' + encodeURIComponent(S.unit.label)).then(r => r.json());
    S.exCards = data.cards || [];
  } catch(e) {
    S.exCards = [];
  }
  renderExCard();
}

function renderExCard() {
  var el = document.getElementById('ex-body');
  if (!el) return;

  if (!S.exCards.length) {
    el.innerHTML = '<div class="ex-no-cards"><div class="ex-nc-icon">🔍</div>'
      + '<p>' + t('no_ex') + '</p>'
      + '<p style="font-size:12px;margin-top:4px">' + t('no_ex_hint') + '</p></div>';
    return;
  }

  if (S.exDone) { renderExSummary(); return; }

  var card  = S.exCards[S.exIdx];
  var total = S.exCards.length;
  var pct   = (S.exIdx / total * 100).toFixed(0);

  var html = '';
  html += '<div class="ex-topbar">';
  html += '<div class="ex-prog-wrap"><div class="ex-prog-fill" style="width:' + pct + '%"></div></div>';
  html += '<span class="ex-counter">' + (S.exIdx + 1) + '/' + total + '</span>';
  html += '<span class="ex-score" style="color:var(--green)">✓' + S.exKnown
        + '</span><span class="ex-score" style="color:#f87171;margin-left:8px">✗' + S.exUnknown + '</span>';
  html += '</div>';

  if (card.unit)   html += '<div class="ex-unit-badge">' + card.unit + '</div>';
  if (card.rubric) html += '<div class="ex-rubric">' + card.rubric + '</div>';

  var freeText = !S.exRevealed && !(card.choices && card.choices.length);

  html += '<div class="ex-card">';
  html += '<div class="ex-question" id="ex-q">'
        + renderClozeQ(card.question, S.exRevealed, freeText) + '</div>';

  if (S.exRevealed) {
    html += '<div class="ex-judge">';
    html += '<button class="ex-btn-knew" onclick="markExCard(true)">' + t('btn_knew') + '</button>';
    html += '<button class="ex-btn-unknown" onclick="markExCard(false)">' + t('btn_unknown') + '</button>';
    html += '</div>';
  } else if (card.choices && card.choices.length) {
    html += '<div class="ex-choices">';
    for (var i = 0; i < card.choices.length; i++) {
      html += '<button class="ex-choice" onclick="pickChoice(' + i + ', this)">'
            + escHtml(card.choices[i]) + '</button>';
    }
    html += '</div>';
  } else {
    html += '<button class="ex-btn-check" onclick="submitExAnswer()">' + t('btn_check') + '</button>';
  }

  html += '</div>';
  el.innerHTML = html;

  if (freeText) {
    var inp = document.getElementById('cloze-input');
    if (inp) {
      inp.focus();
      inp.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { e.preventDefault(); submitExAnswer(); }
      });
    }
  }
}

function renderClozeQ(q, reveal, inputMode) {
  return q.replace(/\{\{c1::([^:}]+)(?:::([^}]*))?\}\}/g, function(match, answer) {
    if (reveal) return '<span class="cloze-ans">' + escHtml(answer) + '</span>';
    if (inputMode) {
      var sz = Math.max(8, answer.length + 3);
      return '<input class="cloze-input" id="cloze-input" autocomplete="off" spellcheck="false" size="' + sz + '">';
    }
    return '<span class="cloze-blank">&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;</span>';
  });
}

function revealExCard() {
  S.exRevealed = true;
  renderExCard();
}

function submitExAnswer() {
  var card = S.exCards[S.exIdx];
  var m = card.question.match(/\{\{c1::([^:}]+)/);
  var correct = m ? m[1].trim().toLowerCase() : '';
  var inp = document.getElementById('cloze-input');
  var typed = inp ? inp.value.trim().toLowerCase() : '';

  if (typed === correct) {
    if (inp) { inp.classList.add('correct'); inp.disabled = true; }
    setTimeout(function() { markExCard(true); }, 700);
  } else {
    if (inp) { inp.classList.add('wrong'); inp.disabled = true; }
    var qEl = document.getElementById('ex-q');
    if (qEl) qEl.innerHTML = renderClozeQ(card.question, true, false);
    S.exRevealed = true;
    var cardEl = document.querySelector('.ex-card');
    var checkBtn = cardEl ? cardEl.querySelector('.ex-btn-check') : null;
    if (checkBtn) checkBtn.remove();
    if (cardEl) {
      var judgeDiv = document.createElement('div');
      judgeDiv.className = 'ex-judge';
      judgeDiv.innerHTML = '<button class="ex-btn-knew" onclick="markExCard(true)">'+t('btn_knew')+'</button>'
                         + '<button class="ex-btn-unknown" onclick="markExCard(false)">'+t('btn_unknown')+'</button>';
      cardEl.appendChild(judgeDiv);
    }
  }
}

function pickChoice(idx, btn) {
  var card    = S.exCards[S.exIdx];
  var m       = card.question.match(/\{\{c1::([^:}]+)/);
  var correct = m ? m[1].trim().toLowerCase() : '';
  var picked  = (card.choices[idx] || '').trim().toLowerCase();

  document.querySelectorAll('.ex-choice').forEach(function(b, i) {
    b.disabled = true;
    var w = (card.choices[i] || '').trim().toLowerCase();
    if (w === correct) b.classList.add('ex-choice-correct');
    else if (i === idx && picked !== correct) b.classList.add('ex-choice-wrong');
  });

  var qEl = document.getElementById('ex-q');
  if (qEl) qEl.innerHTML = renderClozeQ(card.question, true);

  S.exRevealed = true;
  var cardEl = document.querySelector('.ex-card');
  if (cardEl) {
    var judgeDiv = document.createElement('div');
    judgeDiv.className = 'ex-judge';
    judgeDiv.innerHTML = '<button class="ex-btn-knew" onclick="markExCard(true)">✓ Sabia</button>'
                       + '<button class="ex-btn-unknown" onclick="markExCard(false)">✗ Não sabia</button>';
    cardEl.appendChild(judgeDiv);
  }
}

function markExCard(knew) {
  if (knew) S.exKnown++; else S.exUnknown++;
  S.exIdx++;
  S.exRevealed = false;
  if (S.exIdx >= S.exCards.length) { S.exDone = true; renderExSummary(); }
  else renderExCard();
}

function renderExSummary() {
  var el = document.getElementById('ex-body');
  if (!el) return;
  var total = S.exKnown + S.exUnknown;
  var pct   = total ? Math.round(S.exKnown / total * 100) : 0;
  var medal = pct >= 80 ? '🏆' : pct >= 60 ? '⭐' : '💪';
  el.innerHTML = '<div class="ex-summary">'
    + '<div style="font-size:48px;margin-bottom:12px">' + medal + '</div>'
    + '<h3>' + t('ex_pct', pct) + '</h3>'
    + '<p class="ex-sub">' + t('ex_done', total) + '</p>'
    + '<div class="ex-stats">'
    + '<div class="ex-stat known"><span class="ex-stat-val">' + S.exKnown + '</span><span class="ex-stat-lbl">' + t('ex_knew') + '</span></div>'
    + '<div class="ex-stat unknown"><span class="ex-stat-val">' + S.exUnknown + '</span><span class="ex-stat-lbl">' + t('ex_unknown') + '</span></div>'
    + '</div>'
    + '<button class="ex-retry-btn" onclick="retryExCards()">' + t('btn_retry') + '</button>'
    + '</div>';
}

function retryExCards() {
  S.exIdx = 0; S.exRevealed = false;
  S.exKnown = 0; S.exUnknown = 0; S.exDone = false;
  renderExCard();
}

function buildSentenceCard(i) {
  var s       = S.sentences[i];
  var hasText = s.text.trim().length > 0;
  var hasSel  = s.selIdx >= 0;
  var done    = !!(s.text && s.hide && s.hint);
  var partial = !!(s.text && s.hide && !s.hint);

  var rA = hasText ? 'done'   : 'active';
  var rB = hasSel  ? 'done'   : (hasText ? 'active' : '');
  var rC = done    ? 'done'   : (hasSel  ? 'active' : '');
  var cardCls = 'sentence-card' + (done?' complete':partial?' partial':'');
  var statusTxt = done ? '✓ Completa' : partial ? '⌛ Falta pista' : '';
  var statusCls = done ? 'ok' : partial ? 'warn' : '';

  var h = '';
  h += '<div class="' + cardCls + '" id="sc-' + i + '">';

  // Header rail
  h += '<div class="sc-head">';
  h += '<div class="sc-rail">';
  h += '<span class="rail-s ' + rA + '" id="rail-a-' + i + '">① Escreve</span>';
  h += '<span class="rail-arr">›</span>';
  h += '<span class="rail-s ' + rB + '" id="rail-b-' + i + '">② Palavra</span>';
  h += '<span class="rail-arr">›</span>';
  h += '<span class="rail-s ' + rC + '" id="rail-c-' + i + '">③ Pista</span>';
  h += '</div>';
  h += '<span class="sc-status ' + statusCls + '" id="sc-st-' + i + '">' + statusTxt + '</span>';
  h += '</div>';

  // Textarea
  h += '<div class="sc-field">';
  h += '<label>✍️ Frase completa em inglês</label>';
  h += '<textarea class="sc-ta" id="ta-' + i + '"';
  h += ' placeholder="The deployment pipeline runs automated tests on every commit…"';
  h += ' oninput="onTextInput(' + i + ', this.value)"';
  h += ' onkeydown="taKeydown(event,' + i + ')">' + escHtml(s.text) + '</textarea>';
  h += '</div>';

  // Token section (B) — hidden until text typed
  h += '<div id="sc-b-' + i + '" class="tokens-wrap"' + (hasText ? '' : ' style="display:none"') + '>';
  h += '<div class="tokens-lbl">🖱 Clica na palavra que queres ocultar no Anki:</div>';
  h += '<div class="tokens" id="tokens-' + i + '">' + renderTokenButtons(i) + '</div>';
  h += '</div>';

  // Hint + preview section (C) — hidden until word selected
  h += '<div id="sc-c-' + i + '" class="hint-section"' + (hasSel ? '' : ' style="display:none"') + '>';
  h += '<div class="hint-bar">';
  h += '<span class="hint-lbl">💡</span>';
  h += '<input id="hint-' + i + '" placeholder="executa (Present Simple, 3ª pessoa)"';
  h += ' value="' + escHtml(s.hint) + '"';
  h += ' oninput="onHintInput(' + i + ', this.value)">';
  h += '<button class="sug-btn" onclick="useSuggestion(' + i + ')">✨ Sugestão</button>';
  h += '</div>';
  h += '<div class="sc-preview" id="prev-' + i + '">' + (hasSel ? buildPreview(i) : '') + '</div>';
  h += '</div>';

  // Reset
  h += '<button class="reset-btn" id="reset-' + i + '" onclick="resetSentence(' + i + ')"';
  h += (s.text || hasSel ? '' : ' style="display:none"') + '>↺ Limpar frase</button>';

  h += '</div>';
  return h;
}

function renderTokenButtons(i) {
  var s = S.sentences[i];
  var out = '';
  for (var j = 0; j < s.tokens.length; j++) {
    var t = s.tokens[j];
    var cls = 'token' + (j === s.selIdx ? ' sel' : '');
    var punc = t.p ? '<span class="tok-p">' + escHtml(t.p) + '</span>' : '';
    out += '<button class="' + cls + '" onclick="selectToken(' + i + ',' + j + ')">' + escHtml(t.w) + punc + '</button>';
  }
  return out;
}

function tokenize(text) {
  var re = /([A-Za-z''\-]+)([.,;:!?]?)/g;
  var tokens = [];
  var m;
  while ((m = re.exec(text)) !== null) {
    if (m[1]) tokens.push({ w: m[1], p: m[2] || '' });
  }
  return tokens;
}

// Auto-resize textarea height
function taResize(i) {
  var ta = document.getElementById('ta-' + i);
  if (!ta) return;
  ta.style.height = 'auto';
  ta.style.height = ta.scrollHeight + 'px';
}

// Tab key: jump from textarea → hint input
function taKeydown(e, i) {
  if (e.key === 'Tab' && S.sentences[i].selIdx >= 0) {
    e.preventDefault();
    document.getElementById('hint-' + i)?.focus();
  }
}

function onTextInput(i, text) {
  var s = S.sentences[i];
  var newToks = tokenize(text);

  // Preserve selection if selected word still exists
  if (s.selIdx >= 0) {
    var oldWord = s.hide;
    var newIdx = -1;
    for (var j = 0; j < newToks.length; j++) {
      if (newToks[j].w === oldWord) { newIdx = j; break; }
    }
    if (newIdx < 0) { s.selIdx = -1; s.hide = ''; }
    else { s.selIdx = newIdx; }
  }

  s.text   = text;
  s.tokens = newToks;
  var hasText = text.trim().length > 0;

  // Update tokens (no card rebuild — textarea keeps focus)
  var tokensEl = document.getElementById('tokens-' + i);
  var secB     = document.getElementById('sc-b-' + i);
  if (tokensEl) tokensEl.innerHTML = renderTokenButtons(i);
  if (secB)     secB.style.display = hasText ? '' : 'none';

  // Update preview if selection still valid
  if (s.selIdx >= 0) refreshPreview(i);

  // Show/hide reset button
  var rst = document.getElementById('reset-' + i);
  if (rst) rst.style.display = (s.text || s.selIdx >= 0) ? '' : 'none';

  taResize(i);
  updateCardStatus(i);
}

function selectToken(i, idx) {
  var s    = S.sentences[i];
  var same = s.selIdx === idx;

  // Toggle .sel class in place (no DOM rebuild)
  var tokensEl = document.getElementById('tokens-' + i);
  if (tokensEl) {
    var btns = tokensEl.querySelectorAll('.token');
    for (var j = 0; j < btns.length; j++) {
      if (!same && j === idx) btns[j].classList.add('sel');
      else                    btns[j].classList.remove('sel');
    }
  }

  if (same) { s.selIdx = -1; s.hide = ''; }
  else       { s.selIdx = idx; s.hide = s.tokens[idx] ? s.tokens[idx].w : ''; }

  var secC  = document.getElementById('sc-c-' + i);
  var shown = s.selIdx >= 0;
  if (secC) secC.style.display = shown ? '' : 'none';
  if (shown) {
    refreshPreview(i);
    setTimeout(function() { document.getElementById('hint-' + i)?.focus(); }, 60);
  }

  updateCardStatus(i);
}

function onHintInput(i, hint) {
  S.sentences[i].hint = hint;
  refreshPreview(i);
  updateCardStatus(i);
}

function updateCardStatus(i) {
  var s       = S.sentences[i];
  var done    = !!(s.text && s.hide && s.hint);
  var partial = !!(s.text && s.hide && !s.hint);
  var hasText = s.text.trim().length > 0;
  var hasSel  = s.selIdx >= 0;

  var card = document.getElementById('sc-' + i);
  if (card) card.className = 'sentence-card' + (done?' complete':partial?' partial':'');

  var st = document.getElementById('sc-st-' + i);
  if (st) {
    st.textContent = done ? '✓ Completa' : partial ? '⌛ Falta pista' : '';
    st.className   = 'sc-status ' + (done ? 'ok' : partial ? 'warn' : '');
  }

  // Update rail steps live
  var rA = document.getElementById('rail-a-' + i);
  var rB = document.getElementById('rail-b-' + i);
  var rC = document.getElementById('rail-c-' + i);
  if (rA) rA.className = 'rail-s ' + (hasText ? 'done'   : 'active');
  if (rB) rB.className = 'rail-s ' + (hasSel  ? 'done'   : hasText ? 'active' : '');
  if (rC) rC.className = 'rail-s ' + (done    ? 'done'   : hasSel  ? 'active' : '');

  var badge = document.getElementById('done-badge');
  if (badge) {
    var n = S.sentences.filter(function(s){ return s.text && s.hide && s.hint; }).length;
    badge.textContent = n + '/3 completas';
    badge.className   = 'done-badge' + (n === 3 ? ' all-done' : '');
  }
}

function useSuggestion(i) {
  var name = S.unit ? S.unit.name : '';
  var map = [
    [/present continuous|am doing|are you doing/i, 'está a fazer (Present Continuous)'],
    [/present simple|do\/work|don't|do you/i,      'faz (Present Simple, 3ª pessoa)'],
    [/past simple|worked|got|went|didn't/i,         'fez (Past Simple)'],
    [/past continuous|was doing/i,                  'estava a fazer (Past Continuous)'],
    [/present perfect/i,                            'tem feito (Present Perfect)'],
    [/going to/i,                                   'vai fazer (going to)'],
    [/will|shall/i,                                 'irá fazer (will)'],
    [/passive/i,                                    'é feito (Passive)'],
    [/might/i,                                      'pode/talvez (might)'],
    [/should/i,                                     'deveria (should)'],
    [/must/i,                                       'deve (must)'],
    [/could|can/i,                                  'consegue/pode (can/could)'],
    [/conditional|if i had/i,                       'teria feito (Conditional)'],
    [/used to/i,                                    'costumava (used to)'],
  ];
  var hint = '';
  for (var k = 0; k < map.length; k++) {
    if (map[k][0].test(name)) { hint = map[k][1]; break; }
  }
  if (!hint) hint = '… (' + name + ')';
  var el = document.getElementById('hint-' + i);
  if (el) { el.value = hint; S.sentences[i].hint = hint; }
  refreshPreview(i);
  updateCardStatus(i);
}

function resetSentence(i) {
  S.sentences[i] = { text:'', tokens:[], selIdx:-1, hide:'', hint:'' };
  var content = document.getElementById('step-content');
  if (!content) return;
  var cards = content.querySelectorAll('.sentence-card');
  if (cards[i]) {
    var tmp = document.createElement('div');
    tmp.innerHTML = buildSentenceCard(i);
    cards[i].replaceWith(tmp.firstElementChild);
  }
}

function refreshPreview(i) {
  var el = document.getElementById('prev-' + i);
  if (el) el.innerHTML = buildPreview(i);
}

function buildPreview(i) {
  var s    = S.sentences[i];
  var word = s.hide;
  var hint = s.hint || '…';
  var cloze = '{{c1::' + word + '::' + hint + '}}';
  var re  = new RegExp('\\b' + word.replace(/[.*+?^${}()|[\]\\]/g,'\\$&') + '\\b');
  var idx = s.text.search(re);
  if (idx < 0) {
    var lo = s.text.toLowerCase().indexOf(word.toLowerCase());
    if (lo < 0) return escHtml(s.text) + ' <span style="color:var(--amber)">(não localizada)</span>';
    return escHtml(s.text.slice(0,lo)) + '<span class="cloze-hi">' + escHtml(cloze) + '</span>' + escHtml(s.text.slice(lo+word.length));
  }
  return escHtml(s.text.slice(0,idx)) + '<span class="cloze-hi">' + escHtml(cloze) + '</span>' + escHtml(s.text.slice(idx+word.length));
}

// ── Step 4: Concluir ──────────────────────────────────────────────────────────
function buildStep4() {
  var total = S.exKnown + S.exUnknown;
  var pct   = total ? Math.round(S.exKnown / total * 100) : 0;
  var medal = pct >= 80 ? '🏆' : pct >= 60 ? '⭐' : '💪';
  var isDone = S.done.has(S.unit.label);

  var statsHtml = '';
  if (total) {
    statsHtml = '<div style="display:flex;align-items:center;gap:16px;padding:16px 20px;'
      + 'background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:24px">'
      + '<span style="font-size:32px">' + medal + '</span>'
      + '<div>'
      + '<div style="font-size:15px;font-weight:700;color:#fff">' + t('s4_pct', pct) + '</div>'
      + '<div style="font-size:12px;color:var(--muted);margin-top:3px">'
      + '<span style="color:var(--green)">' + t('s4_right', S.exKnown) + '</span>'
      + '&ensp;<span style="color:#f87171">' + t('s4_wrong', S.exUnknown) + '</span>'
      + '&ensp;' + t('s4_of', total) + '</div>'
      + '</div></div>';
  } else {
    statsHtml = '<div style="color:var(--muted);font-size:13px;margin-bottom:24px">'
      + t('s4_no_stats') + '</div>';
  }

  var doneBtn = isDone
    ? '<div style="display:flex;align-items:center;gap:10px;padding:14px 20px;background:#14532d;'
      + 'border:1px solid #16a34a;border-radius:var(--radius)">'
      + '<span style="font-size:20px">✅</span>'
      + '<span style="color:#4ade80;font-weight:600">' + t('s4_already') + '</span></div>'
    : '<button id="btn-done" onclick="markDone()"'
      + ' style="background:#14532d;color:#4ade80;border:1px solid #16a34a;'
      + 'border-radius:8px;padding:11px 28px;font-size:14px;font-weight:600;cursor:pointer">'
      + t('btn_done') + '</button>';

  return '<div class="step-header"><h2>'+t('s4_title')+'</h2>'
    + '<p>'+t('s4_desc')+'</p></div>'
    + statsHtml
    + doneBtn
    + '<span id="save-msg" style="display:block;margin-top:12px;font-size:13px;color:var(--green)"></span>';
}

function generateTSV() {
  const unit = S.unit?.name || '';
  const lines = S.sentences
    .filter(s => s.text && s.hide)
    .map(s => {
      const hint  = s.hint || s.hide;
      const cloze = s.text.replace(s.hide, `{{c1::${s.hide}::${hint}}}`);
      const extra = `<p><b>Regra:</b> ${escHtml(unit)}</p><p><b>Frase Completa:</b> ${escHtml(s.text)}</p><p><b>Contexto:</b> ${escHtml(S.context)}</p>`;
      return cloze + '\t' + extra;
    });
  return lines.join('\n');
}

function copyTSV() {
  const tsv = generateTSV();
  if (!tsv) return flash('save-msg', t('err_copy'));
  navigator.clipboard.writeText(tsv).then(() => flash('save-msg', t('copied')));
}

async function saveFile() {
  const tsv = generateTSV();
  if (!tsv) return flash('save-msg', t('err_save'));
  const res = await fetch('/api/save', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ unit: S.unit.label, tsv, complete: false })
  }).then(r => r.json());
  flash('save-msg', t('saved', res.file));
}

async function markDone() {
  await fetch('/api/save', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ unit: S.unit.label, tsv: '', complete: true })
  });
  S.done.add(S.unit.label);
  await saveProgress();

  var num   = unitNum(S.unit);
  var nextU = S.units.find(function(u) { return unitNum(u) === num + 1; });
  var nextHtml = nextU
    ? '<p style="font-size:13px;color:var(--muted);margin-bottom:28px">' + t('unit_unlocked', num + 1) + '</p>'
    : '<p style="font-size:13px;color:var(--muted);margin-bottom:28px">' + t('keep_going') + '</p>';

  var c = document.getElementById('step-content');
  c.innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;'
    + 'justify-content:center;height:100%;text-align:center;padding:40px">'
    + '<div style="font-size:72px;margin-bottom:20px">🏆</div>'
    + '<h2 style="font-size:24px;font-weight:800;color:#fff;margin-bottom:10px">' + t('unit_complete', num) + '</h2>'
    + nextHtml
    + '<button onclick="returnToMap()" style="background:var(--accent);color:#fff;border:none;'
    + 'border-radius:8px;padding:13px 36px;font-size:15px;font-weight:700;cursor:pointer;">'
    + t('back_to_map') + '</button>'
    + '</div>';

  document.getElementById('nav-footer').style.display = 'none';
}

function flash(id, msg) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = msg;
  setTimeout(() => el.textContent = '', 3500);
}

// ── Audio ─────────────────────────────────────────────────────────────────────
function playAudio(key) {
  stopAudio();
  if (!key) return;
  S.audioEl = new Audio('/media/' + key);
  S.audioEl.play();
  const btn = document.getElementById('btn-audio');
  if (btn) { btn.textContent = t('btn_stop'); btn.onclick = stopAudio; }
  S.audioEl.onended = () => {
    if (btn) { btn.textContent = t('btn_audio'); btn.onclick = () => playAudio(key); }
    S.audioEl = null;
  };
}
function stopAudio() {
  if (S.audioEl) { S.audioEl.pause(); S.audioEl = null; }
}

// ── Utils ─────────────────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s||'')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Keyboard nav
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (S.step === 1) {
    if (e.key === 'ArrowRight') changeImg(1);
    if (e.key === 'ArrowLeft')  changeImg(-1);
    if (e.key === ' ') { e.preventDefault(); const n=S.unit?.notes[S.noteIdx]; if(n?.aud) playAudio(n.aud); }
  }
});

init();
</script>
</body>
</html>
"""


# ─── HTTP Handler ─────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_GET(self):
        path = urllib.parse.unquote(self.path.split("?")[0])

        if path == "/":
            self._send(200, "text/html", HTML.encode())

        elif path == "/api/me":
            user = get_token_user(self)
            if user:
                self._send(200, "application/json", json.dumps({"username": user}).encode())
            else:
                self._send(401, "application/json", json.dumps({"error": "unauthorized"}).encode())

        elif path == "/api/progress":
            user = get_token_user(self)
            if not user:
                self._send(401, "application/json", json.dumps({"error": "unauthorized"}).encode())
                return
            with db() as conn:
                row = conn.execute(
                    "SELECT done FROM progress WHERE username = ?", (user,)
                ).fetchone()
            done = json.loads(row["done"]) if row else []
            self._send(200, "application/json", json.dumps({"done": done}).encode())

        elif path == "/api/units":
            payload = {
                "units": [{"label": lbl} for lbl in DECK["labels"]]
            }
            self._send(200, "application/json", json.dumps(payload).encode())

        elif path.startswith("/api/unit/"):
            label = path[len("/api/unit/"):]
            unit  = DECK["units"].get(label, {})
            self._send(200, "application/json", json.dumps({
                "notes": unit.get("notes", []),
                "page":  unit.get("page", "—"),
                "name":  unit.get("name", ""),
            }).encode())

        elif path.startswith("/media/"):
            key  = path[len("/media/"):]
            fpath = os.path.join(TMP, key)
            if os.path.exists(fpath):
                mime = DECK["mime"].get(key, "application/octet-stream")
                with open(fpath, "rb") as f:
                    self._send(200, mime, f.read())
            else:
                self._send(404, "text/plain", b"Not found")

        elif path.startswith("/api/exercises/"):
            label = path[len("/api/exercises/"):]
            cards = find_exercises_for_unit(label)
            self._send(200, "application/json",
                       json.dumps({"cards": cards, "total": len(cards)}).encode())

        elif path.startswith("/media-ex/"):
            key   = path[len("/media-ex/"):]
            fpath = os.path.join(TMP_EX, key) if TMP_EX else ""
            if fpath and os.path.exists(fpath):
                mime = DECK_EX["mime"].get(key, "application/octet-stream")
                with open(fpath, "rb") as f:
                    self._send(200, mime, f.read())
            else:
                self._send(404, "text/plain", b"Not found")

        else:
            self._send(404, "text/plain", b"Not found")

    def do_POST(self):
        path = urllib.parse.unquote(self.path)

        if path == "/api/login":
            length   = int(self.headers.get("Content-Length", 0))
            body     = json.loads(self.rfile.read(length))
            username = body.get("username", "").strip().lower()
            password = body.get("password", "")
            if not username or not password:
                self._send(200, "application/json", json.dumps({"error": "err_fill"}).encode())
                return
            with db() as conn:
                row = conn.execute(
                    "SELECT hash FROM users WHERE username = ?", (username,)
                ).fetchone()
            if not row or row["hash"] != hash_pwd(password):
                self._send(200, "application/json", json.dumps({"error": "err_creds"}).encode())
                return
            token = str(uuid.uuid4())
            SESSIONS[token] = username
            self._send(200, "application/json", json.dumps({"username": username, "token": token}).encode())

        elif path == "/api/register":
            length   = int(self.headers.get("Content-Length", 0))
            body     = json.loads(self.rfile.read(length))
            username = body.get("username", "").strip().lower()
            password = body.get("password", "")
            if not username or not password:
                self._send(200, "application/json", json.dumps({"error": "err_fill"}).encode())
                return
            if len(username) < 3:
                self._send(200, "application/json", json.dumps({"error": "err_short_user"}).encode())
                return
            if len(password) < 4:
                self._send(200, "application/json", json.dumps({"error": "err_short_pass"}).encode())
                return
            try:
                with db() as conn:
                    conn.execute(
                        "INSERT INTO users (username, hash) VALUES (?, ?)",
                        (username, hash_pwd(password))
                    )
                    conn.execute(
                        "INSERT INTO progress (username) VALUES (?)", (username,)
                    )
                    conn.commit()
            except sqlite3.IntegrityError:
                self._send(200, "application/json", json.dumps({"error": "err_exists"}).encode())
                return
            token = str(uuid.uuid4())
            SESSIONS[token] = username
            self._send(200, "application/json", json.dumps({"username": username, "token": token}).encode())

        elif path == "/api/progress":
            user = get_token_user(self)
            if not user:
                self._send(401, "application/json", json.dumps({"error": "unauthorized"}).encode())
                return
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            done   = json.dumps(body.get("done", []), ensure_ascii=False)
            with db() as conn:
                conn.execute(
                    "INSERT INTO progress (username, done) VALUES (?, ?) "
                    "ON CONFLICT(username) DO UPDATE SET done = excluded.done",
                    (user, done)
                )
                conn.commit()
            self._send(200, "application/json", json.dumps({"ok": True}).encode())

        elif path == "/api/save":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            unit   = body.get("unit", "unknown")
            tsv    = body.get("tsv", "")
            done   = body.get("complete", False)

            # Save file
            os.makedirs(OUT_DIR, exist_ok=True)
            safe = re.sub(r"[^\w\s\-]", "", unit)[:60].strip()
            fname = f"{safe} — {date.today()}.txt"
            fpath = os.path.join(OUT_DIR, fname)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(tsv)

            # Update tracker
            if done:
                mark_unit_complete(unit)

            self._send(200, "application/json",
                       json.dumps({"file": fpath}).encode())
        else:
            self._send(404, "text/plain", b"Not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


# ─── Entry point ─────────────────────────────────────────────────────────────

def cleanup(*_):
    if TMP:    shutil.rmtree(TMP,    ignore_errors=True)
    if TMP_EX: shutil.rmtree(TMP_EX, ignore_errors=True)
    sys.exit(0)

if __name__ == "__main__":
    if not os.path.exists(APKG):
        sys.exit(f"❌  Ficheiro não encontrado:\n   {APKG}")

    init_db()
    print("⏳  A carregar decks…")
    DECK.update(load_deck(APKG))
    print(f"✅  Teoria: {len(DECK['labels'])} unidades · "
          f"{sum(len(v['notes']) for v in DECK['units'].values())} notas")

    if os.path.exists(APKG_EX):
        DECK_EX.update(load_exercise_deck(APKG_EX))
        total_ex = sum(len(v) for v in DECK_EX["exercises"].values())
        print(f"✅  Exercícios: {len(DECK_EX['units'])} grupos · {total_ex} cartões")
    else:
        print(f"⚠️   Deck de exercícios não encontrado: {APKG_EX}")

    custom_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custom_exercises.json")
    if os.path.exists(custom_path):
        with open(custom_path, encoding="utf-8") as f:
            CUSTOM_EX.update({int(k): v for k, v in json.load(f).items()})
        total_custom = sum(len(v) for v in CUSTOM_EX.values())
        print(f"✅  Exercícios customizados: {len(CUSTOM_EX)} unidades · {total_custom} cartões")

    signal.signal(signal.SIGINT,  cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    socketserver.TCPServer.allow_reuse_address = True
    host = "127.0.0.1" if _LOCAL else "0.0.0.0"
    server = socketserver.TCPServer((host, PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"🌐  Servidor em {url}")
    if not _LOCAL:
        print(f"    Modo produção — porta {PORT}, bind 0.0.0.0")
    print("    Prime Ctrl+C para terminar.\n")

    if _LOCAL:
        threading.Timer(0.9, lambda: webbrowser.open(url)).start()
    server.serve_forever()
