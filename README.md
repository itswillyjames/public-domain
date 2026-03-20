# pd-asset-pipeline

A FastAPI service + simple web UI that turns public-domain archive pages into sale-ready digital print bundles.

## Features

- `POST /health`
- `POST /search_sources` (BHL + Internet Archive)
- `POST /extract_pages` (PDF URL or image URLs)
- `POST /build_bundle` (cleanup + print exports + ZIP)
- `GET /bundle/{bundle_id}` (download ZIP)
- Beginner web UI at `/` for end-to-end workflow

## Exact beginner Chromebook Linux run steps

1. **Enable Linux on Chromebook** (Settings → Developers → Linux development environment).
2. Open Linux Terminal and run:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl jq
```

3. Clone and enter project:

```bash
git clone <YOUR_REPO_URL> pd-asset-pipeline
cd pd-asset-pipeline
```

4. Create/activate virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

5. Install Python packages:

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

6. Optional env setup:

```bash
cp .env.example .env
# Add BHL_API_KEY if you have one
```

7. Start server:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

8. Open:
- Web UI: `http://localhost:8000/`
- Swagger docs: `http://localhost:8000/docs`

---

## Real working sample using archive.org foraminifera PDF

Source used:
`https://archive.org/download/foraminiferathei00cush/foraminiferathei00cush.pdf`

### 1) Extract pages

```bash
curl -sS -X POST http://localhost:8000/extract_pages \
  -H 'Content-Type: application/json' \
  --data @samples/foraminifera_extract.json \
  | tee /tmp/extract.json
```

### 2) Build bundle from extracted page paths

```bash
jq -n --arg name "Foraminifera_Real_Bundle" \
  --argjson paths "$(jq '[.[].image_path]' /tmp/extract.json)" \
  '{bundle_name:$name, extracted_page_paths:$paths, print_ratios:["2x3","3x4","4x5","11x14"], cleanup_flags:{crop_margins:true,remove_border_artifacts:true,autocontrast:true,sharpen_line_art:true}}' \
  | curl -sS -X POST http://localhost:8000/build_bundle \
      -H 'Content-Type: application/json' \
      -d @- | tee /tmp/build.json
```

### 3) Download generated ZIP

```bash
BUNDLE_ID=$(jq -r '.bundle_id' /tmp/build.json)
curl -L "http://localhost:8000/bundle/${BUNDLE_ID}" --output Foraminifera_Real_Bundle.zip
```

---

## Simple frontend web page

The app ships a basic UI at `/` with:
- Source URL input
- Optional page selection
- Bundle name input
- **Build Bundle** button
- Download link for generated ZIP

File: `app/static/index.html`

---

## Deployment (Render)

1. Push this repo to GitHub.
2. In Render: **New +** → **Web Service**.
3. Connect repo.
4. Set:
   - Runtime: Python 3
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Add env vars (optional): `BHL_API_KEY`.
6. Deploy.
7. Confirm health:

```bash
curl -X POST https://YOUR-RENDER-URL/health
```

## Deployment (Railway)

1. Push repo to GitHub.
2. In Railway: **New Project** → **Deploy from GitHub repo**.
3. Add service variables if needed (`BHL_API_KEY`).
4. Railway auto-detects Python.
5. Start command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

6. Deploy and test:

```bash
curl -X POST https://YOUR-RAILWAY-URL/health
```

---

## Exact ngrok instructions (local testing + GPT Action)

1. Install ngrok and authenticate once:

```bash
ngrok config add-authtoken <YOUR_NGROK_TOKEN>
```

2. Run API locally:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

3. In a second terminal, expose it:

```bash
ngrok http 8000
```

4. Copy HTTPS forwarding URL (example):
`https://abc123.ngrok-free.app`

5. Verify:

```bash
curl -X POST https://abc123.ngrok-free.app/health
```

---

## Custom GPT Action schema and import steps

Schema file: `schemas/custom_gpt_action_openapi.json`

### Import steps

1. Deploy API (Render/Railway/ngrok).
2. Open schema file.
3. Replace server URLs with your deployed HTTPS URL.
4. In ChatGPT GPT Builder:
   - Go to **Configure** → **Actions** → **Import from OpenAPI**.
   - Paste schema JSON.
5. Save action.
6. Test actions in this order:
   - `health`
   - `extractPages` (foraminifera sample)
   - `buildBundle`
   - `downloadBundle`

---

## Project files

- `app/main.py` – API + processing pipeline
- `app/static/index.html` – simple frontend
- `schemas/custom_gpt_action_openapi.json` – action schema
- `samples/foraminifera_extract.json` – real extraction sample
- `samples/foraminifera_bundle.json` – build options sample
- `requirements.txt` / `Dockerfile` / `.env.example`
