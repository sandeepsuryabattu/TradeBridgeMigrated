## Running the app (Production — Ubuntu Server via PM2)

### Prerequisites

- PM2 installed globally: `npm install -g pm2`
- Python venv at `./venv/` with all dependencies installed
- `.env` file in project root with all Kotak/Telegram credentials

### Start / Restart the backend

```bash
cd /home/ubuntu/telegram-kotak-trader
pm2 start 3   # start by PM2 id (kotak-trader)
# or
pm2 restart kotak-trader
```

### Stop the backend

```bash
pm2 stop kotak-trader
```

### View live logs

```bash
pm2 logs kotak-trader              # stream live
pm2 logs kotak-trader --lines 200  # last 200 lines (non-streaming)
pm2 logs kotak-trader --lines 500 --nostream  # dump and exit
```

### PM2 process list

```bash
pm2 list
```

Relevant entry: **id=3, name=kotak-trader**

### How PM2 starts uvicorn

PM2 runs the backend via the command stored in the PM2 ecosystem config (or auto-detected from the saved process list). The effective command is:

```bash
/home/ubuntu/telegram-kotak-trader/venv/bin/python3 \
  venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8001
```

Stdout → `/home/ubuntu/.pm2/logs/kotak-trader-out.log`  
Stderr → `/home/ubuntu/.pm2/logs/kotak-trader-error.log`

### Save PM2 process list (survive reboot)

```bash
pm2 save
pm2 startup   # follow the printed command to enable systemd auto-start
```

---

## Local Development (macOS / manual)

From the project root:

```bash
source venv/bin/activate
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8001
```

Then open `http://127.0.0.1:8001/` in your browser.

---

## Frontend (static files only)

The frontend is served directly by FastAPI as static files — no separate server needed.  
If you ever want standalone static serving:

```bash
cd frontend
python -m http.server 3000
```

Note: API calls will still target `http://localhost:8001` by default.
