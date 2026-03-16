# pd-asset-pipeline

Production-ready FastAPI service for a Custom GPT Action that finds public-domain archive content and turns extracted pages into sale-ready print bundles.

## What this service does

- `POST /health` – basic status check.
- `POST /search_sources` – searches Biodiversity Heritage Library (BHL) and Internet Archive metadata.
- `POST /extract_pages` – downloads a PDF or image URLs and extracts page images + thumbnails.
- `POST /build_bundle` – applies cleanup pipeline and generates print-ready folders + `Print_Guide.pdf` + final ZIP.
- `GET /bundle/{bundle_id}` – downloads generated ZIP.

### Bundle folder structure

```text
Bundle_Name/
  Masters/
  Print_2x3/
  Print_3x4/
  Print_4x5/
  Print_11x14/
  Preview/
  Print_Guide.pdf
```

## Tech stack

- FastAPI
- Pillow
- PyMuPDF
- requests
- reportlab

## Quick start (Chromebook + Linux)

> Works in Linux terminal on Chromebook (Crostini) and standard Linux distributions.

### 1) Clone and enter project

```bash
git clone <your-repo-url> pd-asset-pipeline
cd pd-asset-pipeline
```

### 2) Create virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3) Install dependencies

```bash
pip install -r requirements.txt
```

### 4) Configure environment

```bash
cp .env.example .env
# Optional: add BHL_API_KEY in .env
```

### 5) Run server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open docs at: `http://localhost:8000/docs`

## Docker run

```bash
docker build -t pd-asset-pipeline .
docker run --rm -p 8000:8000 --env-file .env pd-asset-pipeline
```

## API examples

### Health check

```bash
curl -X POST http://localhost:8000/health
```

### Search sources

```bash
curl -X POST http://localhost:8000/search_sources \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "diatom atlas",
    "archive": ["bhl", "internet_archive"],
    "year_start": 1800,
    "year_end": 1930,
    "limit": 8
  }'
```

### Extract pages from PDF

```bash
curl -X POST http://localhost:8000/extract_pages \
  -H 'Content-Type: application/json' \
  -d '{
    "pdf_url": "https://example.org/archive-book.pdf",
    "page_numbers": [12, 15, 18]
  }'
```

### Auto-detect likely plates (when page_numbers omitted)

The service scores pages with heuristics tuned for scientific plate detection:

- radial symmetry
- edge density
- contrast
- low text density

Highest scoring pages are returned first.

### Build bundle from extracted pages

```bash
curl -X POST http://localhost:8000/build_bundle \
  -H 'Content-Type: application/json' \
  -d '{
    "bundle_name": "Diatom_Plate_Set_01",
    "extracted_page_paths": [
      "data/extracted/<run_id>/page_0012.jpg",
      "data/extracted/<run_id>/page_0015.jpg"
    ],
    "print_ratios": ["2x3", "3x4", "4x5", "11x14"],
    "cleanup_flags": {
      "crop_margins": true,
      "remove_border_artifacts": true,
      "autocontrast": true,
      "sharpen_line_art": true
    }
  }'
```

Response includes:

- `bundle_id`
- `local_path`
- `download_url`

### Download bundle zip

```bash
curl -L http://localhost:8000/bundle/<bundle_id> --output bundle.zip
```

## Sample foraminifera/diatom command

Sample payload: `samples/foraminifera_bundle.json`

```bash
curl -X POST http://localhost:8000/build_bundle \
  -H 'Content-Type: application/json' \
  --data @samples/foraminifera_bundle.json
```

## Custom GPT Action schema

Use: `schemas/custom_gpt_action_openapi.json`

- Import this file in your Custom GPT Action config.
- Set server URL to your deployed API base URL.

## Notes for beginners

- All generated files are under `data/`.
- If you only have image URLs, use `/extract_pages` with `image_urls`.
- If you have a PDF, `/extract_pages` can extract explicit pages or auto-choose plate-like pages.
- `/build_bundle` can accept local extracted paths directly.
