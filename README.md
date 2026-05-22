# Essential Grammar in Use — Study App

A self-hosted web app for studying **Essential Grammar in Use** (Raymond Murphy, Cambridge University Press) using a structured 4-step method with spaced repetition via Anki.

**Live demo:** [grammar-study.onrender.com](https://grammar-study.onrender.com)

> **Note:** This app is a study companion. The full experience requires the physical book.
> Free tier on Render — first load after inactivity may take ~30 s to wake up.

---

## Features

- **Snake map** with 115 grammar units, sequential unlock, and per-zone visual themes
- **4-step study flow** per unit: Theory → Book → Practice → Conclude
- **Countdown timers** per step (15 min each), with visual warnings
- **Exercise deck** — interactive cloze exercises from the Anki exercise pack
- **Multi-user login** with per-user progress persistence
- **Multilingual UI** — PT / EN / ES / FR
- **Responsive** — works on desktop and mobile

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Server | Python 3 · `http.server` · `socketserver` (zero dependencies locally) |
| Database | SQLite (local dev) · PostgreSQL via [Neon](https://neon.tech) (production) |
| Frontend | Vanilla HTML / CSS / JS — no frameworks, no build step |
| Hosting | [Render](https://render.com) free tier · Docker |
| Auth | SHA-256 password hashing · UUID session tokens |

---

## Local Development

### Prerequisites
- Python 3.9+
- The two `.apkg` Anki deck files placed in `Anki/`

### Run
```bash
python anki_reader.py
```

The server starts on `http://127.0.0.1:7654` and opens the browser automatically.

---

## Deployment (Render + Neon)

### 1. Database — Neon (free PostgreSQL)
1. Create a free account at [neon.tech](https://neon.tech)
2. Create a project and copy the **Connection string**

### 2. Hosting — Render (free web service)
1. Push this repo to GitHub (keep it **private** — the `.apkg` files are proprietary)
2. Create a new **Web Service** on [render.com](https://render.com)
3. Connect the GitHub repo · set **Runtime** to `Docker` · **Instance Type** to `Free`
4. Add the environment variable:

| Variable | Value |
|----------|-------|
| `DATABASE_URL` | `postgresql://...` (from Neon) |

5. Deploy — the app will be live at `https://<your-app>.onrender.com`

> **Free tier note:** Render free services sleep after 15 min of inactivity. The first request after sleep takes ~30 s to wake up.

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PORT` | No | `7654` | HTTP port (set automatically by Render) |
| `DATABASE_URL` | No | — | PostgreSQL connection string. If unset, falls back to local SQLite |
| `DATA_DIR` | No | project root | Directory for SQLite file and saved output |

---

## Project Structure

```
.
├── anki_reader.py          # Single-file Python server + full HTML/CSS/JS app
├── Anki/
│   ├── English_Essential_Grammar_In_Use_Theory.apkg
│   └── English_Grammar_In_Use_Exercises.apkg
├── custom_exercises.json   # Additional exercises not covered by the deck
├── Dockerfile
├── requirements.txt
├── .dockerignore
└── .gitignore
```

---

## License

MIT — see [LICENSE](LICENSE)
