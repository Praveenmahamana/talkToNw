# Airline Schedule Intelligence Dashboard — Setup Guide

This guide lets anyone replicate the dashboard on a new machine in minutes.

---

## Prerequisites

| Tool | Minimum version | Check |
|------|----------------|-------|
| Python | 3.11+ | `python --version` |
| pip | bundled with Python | `pip --version` |
| Git (optional) | any | for cloning the repo |

---

## 1 — Get the code

**Option A – clone from Git**
```bash
git clone <your-repo-url>
cd airline_schedule_app
```

**Option B – copy the folder**  
Copy the entire `airline_schedule_app\` folder to the target machine.

---

## 2 — Install Python dependencies

```powershell
pip install -r requirements.txt
```

> Takes ~2 minutes on first run (DuckDB, NetworkX, kuzu, etc.).

---

## 3 — Configure `.env`

Copy the example and fill in your values:

```powershell
Copy-Item .env.example .env
```

Then edit `.env`:

```env
# ── AI (pick ONE block) ───────────────────────────────────────────────────────

# Option A: Vertex AI / Gemini via GCP service account
VERTEX_PROJECT_ID=<your-gcp-project>
VERTEX_LOCATION=us-central1
VERTEX_MODEL=gemini-2.5-flash
VERTEX_SERVICE_ACCOUNT_JSON=C:\path\to\your-service-account.json

# Option B: GitHub Copilot API
GITHUB_COPILOT_TOKEN=<your-token>

# ── Database ──────────────────────────────────────────────────────────────────
SCHEDAI_DB_PATH=data/output/schedules.duckdb

# ── Schedule data folder (SSIM / workset output) ──────────────────────────────
SCHEDAI_DATA_FOLDER=C:\path\to\your\workset\out
```

> If neither AI option is configured the app runs in **deterministic-only mode** (all rule-engine features still work).

---

## 4 — Run the dashboard

### Quickest way — use the startup script

```powershell
.\run.ps1
```

Optional parameters:
```powershell
.\run.ps1 -Port 8080 -DataFolder "C:\other\workset\out"
```

The script:
1. Validates Python and `.env`
2. Installs / verifies dependencies
3. Kills any process already using the port
4. Starts `uvicorn` in the background
5. Tails the log until "Startup complete"

### Manual way

```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 5 — Open the dashboard

| Page | URL |
|------|-----|
| Dashboard | http://localhost:8000 |
| Interactive API docs | http://localhost:8000/docs |
| Health check | http://localhost:8000/health |

---

## 6 — Stop the server

```powershell
# Find the PID
netstat -ano | findstr :8000

# Stop it
Stop-Process -Id <PID>
```

Or just close the terminal if you ran it interactively.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ModuleNotFoundError` | Run `pip install -r requirements.txt` again |
| Port already in use | `run.ps1` handles this automatically, or kill the PID manually |
| `Dashboard CSV missing` warnings | These are optional pre-computed reports; the app works without them |
| Vertex AI not available | App falls back to deterministic mode — set credentials in `.env` |
| DB empty on startup | Set `SCHEDAI_DATA_FOLDER` to a folder containing SSIM schedule files |

---

## Logs

| File | Contents |
|------|----------|
| `server_live.log` | stdout (startup, requests, info) |
| `server_live.err` | stderr (warnings, errors) |
| `logs/app.log` | structured loguru application log |
