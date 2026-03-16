import io
import json
import math
import os
import re
import shutil
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import fitz
import numpy as np
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

APP_NAME = "pd-asset-pipeline"
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DOWNLOAD_DIR = DATA_DIR / "downloads"
EXTRACTED_DIR = DATA_DIR / "extracted"
BUNDLES_DIR = DATA_DIR / "bundles"
BUNDLE_INDEX = DATA_DIR / "bundle_index.json"

for directory in [DATA_DIR, DOWNLOAD_DIR, EXTRACTED_DIR, BUNDLES_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

if not BUNDLE_INDEX.exists():
    BUNDLE_INDEX.write_text("{}", encoding="utf-8")

app = FastAPI(title=APP_NAME, version="1.0.0")


class SearchSourcesRequest(BaseModel):
    query: str
    archive: Optional[List[str]] = Field(default=["bhl", "internet_archive"])
    year_start: Optional[int] = None
    year_end: Optional[int] = None
    limit: int = 10


class SearchResultItem(BaseModel):
    title: str
    author: Optional[str]
    year: Optional[int]
    source: str
    source_url: str
    item_id: str


class ExtractPagesRequest(BaseModel):
    pdf_url: Optional[str] = None
    image_urls: Optional[List[str]] = None
    page_numbers: Optional[List[int]] = None
    max_auto_pages: int = 8


class ExtractedPage(BaseModel):
    page_number: int
    image_path: str
    thumbnail_path: str
    score: Optional[float] = None


class BuildBundleRequest(BaseModel):
    bundle_name: str
    source_urls: Optional[List[str]] = None
    extracted_page_paths: Optional[List[str]] = None
    print_ratios: List[str] = Field(default=["2x3", "3x4", "4x5", "11x14"])
    cleanup_flags: Dict[str, bool] = Field(
        default={
            "crop_margins": True,
            "remove_border_artifacts": True,
            "autocontrast": True,
            "sharpen_line_art": True,
        }
    )


def _load_bundle_index() -> dict:
    return json.loads(BUNDLE_INDEX.read_text(encoding="utf-8"))


def _save_bundle_index(index_data: dict) -> None:
    BUNDLE_INDEX.write_text(json.dumps(index_data, indent=2), encoding="utf-8")


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_")


def _download_file(url: str, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    fname = url.split("?")[0].split("/")[-1] or f"download-{uuid.uuid4().hex}"
    destination = target_dir / fname

    with requests.get(url, timeout=45, stream=True) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 128):
                if chunk:
                    handle.write(chunk)

    return destination


def _bhl_search(query: str, year_start: Optional[int], year_end: Optional[int], limit: int) -> List[dict]:
    params = {
        "op": "PublicationSearch",
        "searchterm": query,
        "page": 1,
        "pagesize": min(limit, 25),
        "format": "json",
    }
    api_key = os.getenv("BHL_API_KEY", "")
    if api_key:
        params["apikey"] = api_key

    try:
        response = requests.get("https://www.biodiversitylibrary.org/api3", params=params, timeout=25)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return []

    results = []
    for item in payload.get("Result", []):
        year = None
        year_str = str(item.get("Year", "")).strip()
        if year_str.isdigit():
            year = int(year_str)
        if year_start and year and year < year_start:
            continue
        if year_end and year and year > year_end:
            continue

        item_id = str(item.get("TitleID", ""))
        results.append(
            {
                "title": item.get("ShortTitle") or item.get("FullTitle") or "Unknown title",
                "author": item.get("Authors") or None,
                "year": year,
                "source": "Biodiversity Heritage Library",
                "source_url": f"https://www.biodiversitylibrary.org/title/{item_id}" if item_id else "https://www.biodiversitylibrary.org/",
                "item_id": item_id,
            }
        )

    return results[:limit]


def _internet_archive_search(query: str, year_start: Optional[int], year_end: Optional[int], limit: int) -> List[dict]:
    filters = ["mediatype:texts"]
    if year_start:
        filters.append(f"year:[{year_start} TO 9999]")
    if year_end:
        filters.append(f"year:[0 TO {year_end}]")

    params = {
        "q": query,
        "fl[]": ["identifier", "title", "creator", "year"],
        "rows": limit,
        "page": 1,
        "output": "json",
        "fq": filters,
    }

    try:
        response = requests.get("https://archive.org/advancedsearch.php", params=params, timeout=25)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return []

    docs = payload.get("response", {}).get("docs", [])
    results = []
    for doc in docs:
        year = None
        raw_year = str(doc.get("year", "")).strip()
        if raw_year.isdigit():
            year = int(raw_year)

        identifier = doc.get("identifier", "")
        results.append(
            {
                "title": doc.get("title") or "Unknown title",
                "author": doc.get("creator") or None,
                "year": year,
                "source": "Internet Archive",
                "source_url": f"https://archive.org/details/{identifier}" if identifier else "https://archive.org/",
                "item_id": identifier,
            }
        )

    return results


def _ensure_rgb(image: Image.Image) -> Image.Image:
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def _thumbnail(image: Image.Image, max_size: Tuple[int, int] = (360, 360)) -> Image.Image:
    thumb = image.copy()
    thumb.thumbnail(max_size)
    return thumb


def _estimate_text_density(gray: Image.Image) -> float:
    arr = np.array(gray)
    edges = np.abs(np.diff(arr.astype(np.int16), axis=1))
    tiny_transitions = (edges > 25).mean()
    return float(tiny_transitions)


def _plate_score(image: Image.Image) -> Dict[str, float]:
    gray = ImageOps.grayscale(image)
    arr = np.array(gray).astype(np.float32)

    contrast = float(arr.std() / 64.0)

    edges_x = np.abs(np.diff(arr, axis=1))
    edges_y = np.abs(np.diff(arr, axis=0))
    edge_density = float(((edges_x > 18).mean() + (edges_y > 18).mean()) / 2.0)

    flipped = np.fliplr(arr)
    h, w = arr.shape
    overlap_w = min(w, flipped.shape[1])
    radial_symmetry = 1.0 - float(np.mean(np.abs(arr[:, :overlap_w] - flipped[:, :overlap_w])) / 255.0)

    text_density = _estimate_text_density(gray)
    low_text_density = max(0.0, 1.0 - (text_density * 2.2))

    score = (0.20 * radial_symmetry) + (0.30 * edge_density) + (0.30 * min(1.0, contrast)) + (0.20 * low_text_density)
    return {
        "score": round(score, 4),
        "radial_symmetry": round(radial_symmetry, 4),
        "edge_density": round(edge_density, 4),
        "contrast": round(contrast, 4),
        "low_text_density": round(low_text_density, 4),
    }


def _crop_margins(image: Image.Image) -> Image.Image:
    bg = Image.new(image.mode, image.size, image.getpixel((0, 0)))
    diff = ImageChops.difference(image, bg)
    bbox = diff.getbbox()
    if bbox:
        return image.crop(bbox)
    return image


def _remove_border_artifacts(image: Image.Image, border: int = 8) -> Image.Image:
    return ImageOps.crop(image, border=border)


def _autocontrast(image: Image.Image) -> Image.Image:
    return ImageOps.autocontrast(image)


def _sharpen(image: Image.Image) -> Image.Image:
    enhanced = ImageEnhance.Sharpness(image).enhance(1.6)
    return enhanced.filter(ImageFilter.SHARPEN)


def _resize_and_crop(image: Image.Image, ratio_w: int, ratio_h: int, target_long: int = 4800) -> Image.Image:
    target_ratio = ratio_w / ratio_h
    img_ratio = image.width / image.height

    if img_ratio > target_ratio:
        new_width = int(image.height * target_ratio)
        left = (image.width - new_width) // 2
        image = image.crop((left, 0, left + new_width, image.height))
    elif img_ratio < target_ratio:
        new_height = int(image.width / target_ratio)
        top = (image.height - new_height) // 2
        image = image.crop((0, top, image.width, top + new_height))

    if ratio_w >= ratio_h:
        width = target_long
        height = int(target_long * ratio_h / ratio_w)
    else:
        height = target_long
        width = int(target_long * ratio_w / ratio_h)

    return image.resize((width, height), Image.Resampling.LANCZOS)


def _write_print_guide(bundle_dir: Path, bundle_name: str, file_count: int, print_ratios: List[str]) -> Path:
    pdf_path = bundle_dir / "Print_Guide.pdf"
    c = canvas.Canvas(str(pdf_path), pagesize=letter)
    c.setTitle(f"{bundle_name} Print Guide")
    c.setFont("Helvetica-Bold", 16)
    c.drawString(70, 740, f"{bundle_name} - Print Guide")
    c.setFont("Helvetica", 11)
    c.drawString(70, 715, f"Created: {datetime.utcnow().isoformat()} UTC")
    c.drawString(70, 700, f"Included master images: {file_count}")
    c.drawString(70, 685, f"Ratios generated: {', '.join(print_ratios)}")
    y = 650
    for line in [
        "Tips:",
        "1) Use JPG quality 95+ when printing.",
        "2) For best results, use matte archival paper.",
        "3) Keep original Masters untouched for future edits.",
        "4) Preview folder is low-res and web-friendly.",
    ]:
        c.drawString(70, y, line)
        y -= 20
    c.save()
    return pdf_path


def _zip_folder(folder: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in folder.rglob("*"):
            if file.is_file():
                zf.write(file, arcname=file.relative_to(folder.parent))


def _extract_pdf_pages(pdf_path: Path, page_numbers: Optional[List[int]], run_dir: Path, max_auto_pages: int) -> List[ExtractedPage]:
    doc = fitz.open(pdf_path)
    pages = page_numbers if page_numbers else list(range(1, doc.page_count + 1))

    scored = []
    for pno in pages:
        if pno < 1 or pno > doc.page_count:
            continue
        page = doc.load_page(pno - 1)
        pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        info = _plate_score(img)
        scored.append((pno, img, info["score"]))

    if not page_numbers:
        scored = sorted(scored, key=lambda x: x[2], reverse=True)[:max_auto_pages]

    output: List[ExtractedPage] = []
    for pno, img, score in scored:
        img_path = run_dir / f"page_{pno:04d}.jpg"
        thumb_path = run_dir / f"page_{pno:04d}_thumb.jpg"
        _ensure_rgb(img).save(img_path, quality=95)
        _thumbnail(img).save(thumb_path, quality=90)
        output.append(
            ExtractedPage(
                page_number=pno,
                image_path=str(img_path),
                thumbnail_path=str(thumb_path),
                score=score,
            )
        )

    return output


def _extract_image_urls(image_urls: List[str], run_dir: Path) -> List[ExtractedPage]:
    output = []
    for idx, url in enumerate(image_urls, start=1):
        local = _download_file(url, run_dir)
        img = Image.open(local).convert("RGB")
        score = _plate_score(img)["score"]
        img_path = run_dir / f"page_{idx:04d}.jpg"
        thumb_path = run_dir / f"page_{idx:04d}_thumb.jpg"
        img.save(img_path, quality=95)
        _thumbnail(img).save(thumb_path, quality=90)
        output.append(ExtractedPage(page_number=idx, image_path=str(img_path), thumbnail_path=str(thumb_path), score=score))
    return output


@app.post("/health")
def health() -> dict:
    return {"status": "ok", "service": APP_NAME}


@app.post("/search_sources", response_model=List[SearchResultItem])
def search_sources(payload: SearchSourcesRequest) -> List[SearchResultItem]:
    archives = set(payload.archive or [])
    results: List[dict] = []
    if "bhl" in archives:
        results.extend(_bhl_search(payload.query, payload.year_start, payload.year_end, payload.limit))
    if "internet_archive" in archives:
        results.extend(_internet_archive_search(payload.query, payload.year_start, payload.year_end, payload.limit))

    if not results:
        return []

    unique = {}
    for item in results:
        key = f"{item['source']}::{item['item_id']}"
        unique[key] = item

    deduped = list(unique.values())[: payload.limit]
    return [SearchResultItem(**item) for item in deduped]


@app.post("/extract_pages", response_model=List[ExtractedPage])
def extract_pages(payload: ExtractPagesRequest) -> List[ExtractedPage]:
    if not payload.pdf_url and not payload.image_urls:
        raise HTTPException(status_code=400, detail="Provide either pdf_url or image_urls")

    run_dir = EXTRACTED_DIR / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=True)

    if payload.pdf_url:
        pdf_path = _download_file(payload.pdf_url, run_dir)
        return _extract_pdf_pages(pdf_path, payload.page_numbers, run_dir, payload.max_auto_pages)

    return _extract_image_urls(payload.image_urls or [], run_dir)


@app.post("/build_bundle")
def build_bundle(payload: BuildBundleRequest) -> dict:
    if not payload.extracted_page_paths and not payload.source_urls:
        raise HTTPException(status_code=400, detail="Provide extracted_page_paths or source_urls")

    bundle_id = uuid.uuid4().hex
    bundle_base_name = _safe_name(payload.bundle_name) or f"bundle_{bundle_id[:8]}"
    bundle_root = BUNDLES_DIR / bundle_id
    bundle_dir = bundle_root / bundle_base_name
    masters_dir = bundle_dir / "Masters"
    preview_dir = bundle_dir / "Preview"

    ratio_map = {
        "2x3": (2, 3),
        "3x4": (3, 4),
        "4x5": (4, 5),
        "11x14": (11, 14),
    }

    for folder in [masters_dir, preview_dir, *(bundle_dir / f"Print_{r}" for r in payload.print_ratios)]:
        folder.mkdir(parents=True, exist_ok=True)

    source_paths: List[Path] = []
    if payload.extracted_page_paths:
        source_paths.extend(Path(p) for p in payload.extracted_page_paths)

    if payload.source_urls:
        dl_dir = bundle_root / "source_downloads"
        for url in payload.source_urls:
            source_paths.append(_download_file(url, dl_dir))

    master_images: List[Path] = []
    for idx, src in enumerate(source_paths, start=1):
        img = Image.open(src).convert("RGB")

        if payload.cleanup_flags.get("crop_margins", True):
            img = _crop_margins(img)
        if payload.cleanup_flags.get("remove_border_artifacts", True):
            img = _remove_border_artifacts(img)
        if payload.cleanup_flags.get("autocontrast", True):
            img = _autocontrast(img)
        if payload.cleanup_flags.get("sharpen_line_art", True):
            img = _sharpen(img)

        master_path = masters_dir / f"master_{idx:03d}.jpg"
        img.save(master_path, quality=96)
        master_images.append(master_path)

        prev = _thumbnail(img, (1200, 1200))
        prev.save(preview_dir / f"preview_{idx:03d}.jpg", quality=88)

        for ratio in payload.print_ratios:
            if ratio not in ratio_map:
                continue
            rw, rh = ratio_map[ratio]
            out = _resize_and_crop(img, rw, rh)
            out.save(bundle_dir / f"Print_{ratio}" / f"print_{ratio}_{idx:03d}.jpg", quality=96)

    _write_print_guide(bundle_dir, payload.bundle_name, len(master_images), payload.print_ratios)

    zip_path = bundle_root / f"{bundle_base_name}.zip"
    _zip_folder(bundle_dir, zip_path)

    index = _load_bundle_index()
    index[bundle_id] = {
        "bundle_name": payload.bundle_name,
        "zip_path": str(zip_path),
        "created_at": datetime.utcnow().isoformat(),
    }
    _save_bundle_index(index)

    return {
        "bundle_id": bundle_id,
        "local_path": str(zip_path),
        "download_url": f"/bundle/{bundle_id}",
    }


@app.get("/bundle/{bundle_id}")
def get_bundle(bundle_id: str):
    index = _load_bundle_index()
    if bundle_id not in index:
        raise HTTPException(status_code=404, detail="Bundle not found")

    zip_path = Path(index[bundle_id]["zip_path"])
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Bundle file missing")

    return FileResponse(zip_path, filename=zip_path.name, media_type="application/zip")
