"""將 picOrganize 的照片與文案推送至小灶私廚 (NEW_JWKL_CUISINE) 社群文章草稿匣。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import requests

from .captions import load_captions
from .config import AppConfig
from .manifest import Trip


def _load_cuisine_env(root: Path | None = None) -> tuple[str, str]:
    """依序從環境變數或專案 .env 載入小灶私廚後端網址與 API Key。"""
    base = os.getenv("CUISINE_API_BASE", "").strip()
    key = os.getenv("CUISINE_API_KEY", "").strip()

    env_path = (root or Path.cwd()) / ".env"
    if (not base or not key) and env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k == "CUISINE_API_BASE" and not base:
                base = v
            elif k == "CUISINE_API_KEY" and not key:
                key = v

    return (
        (base or "https://cuisine-backend-382473631041.asia-east1.run.app").rstrip("/"),
        key,
    )


def upload_image_to_cuisine(api_base: str, api_key: str, image_path: Path) -> str:
    """上傳單張圖片至小灶私廚後端 (/api/v1/upload)，回傳公開 CDN URL。"""
    url = f"{api_base}/api/v1/upload"
    with image_path.open("rb") as fh:
        resp = requests.post(
            url,
            headers={"X-API-KEY": api_key},
            files={"file": (image_path.name, fh, "image/jpeg")},
            timeout=60,
        )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("url"):
        raise RuntimeError(f"小灶圖片上傳回應缺少 url：{data}")
    return str(data["url"])


def _split_title_and_body(raw_text: str, fallback_title: str) -> tuple[str, str]:
    """從貼文內文抽出首行作為標題，其餘保留為 Markdown 內文。"""
    lines = [ln.rstrip() for ln in raw_text.strip().splitlines()]
    non_empty = [ln for ln in lines if ln.strip()]
    if not non_empty:
        return fallback_title, ""
    first = non_empty[0].strip().lstrip("#").strip()
    if len(first) <= 45 and len(non_empty) >= 2:
        idx = lines.index(non_empty[0])
        body = "\n".join(lines[idx + 1 :]).strip()
        return first, body or raw_text.strip()
    return fallback_title, raw_text.strip()


def publish_trip_to_cuisine(
    trip: Trip,
    cfg: AppConfig,
    platform: str = "fb",
    variant: str | None = None,
    title: str | None = None,
    category: str = "STORY",
    tags: str | None = None,
    publish_now: bool = False,
    max_extra_images: int = 4,
    dry_run: bool = False,
    root: Path | None = None,
) -> dict[str, Any]:
    """將指定旅程已排序的照片與選定文案推送至小灶私廚後台（預設 DRAFT 草稿供審閱）。"""
    resolved_root = root or trip.dir.parent.parent
    api_base, api_key = _load_cuisine_env(resolved_root)
    if not api_key:
        raise RuntimeError("尚未設定 CUISINE_API_KEY（請於 picOrganize/.env 或環境變數補上）。")

    ready_dir = trip.ready / platform
    photos = sorted(
        [p for p in ready_dir.iterdir() if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
    ) if ready_dir.exists() else []

    caps = load_captions(trip)
    ver_id = variant or caps.get("active_variant") or "v1"
    variants = caps.get("variants") or {}
    v_data = variants.get(ver_id) or (next(iter(variants.values())) if variants else {})
    raw_caption = str(v_data.get(platform) or v_data.get("fb") or v_data.get("ig") or "").strip()

    if not raw_caption and not photos:
        raise RuntimeError("找不到可用的文案或匯出照片，請先執行 `pic review` 或 `pic publish`。")

    resolved_title, body_md = _split_title_and_body(raw_caption, title or trip.name)
    if title:
        resolved_title = title.strip()

    if dry_run:
        return {
            "dry_run": True,
            "api_base": api_base,
            "title": resolved_title,
            "category": category,
            "status": "PUBLISHED" if publish_now else "DRAFT",
            "photo_count": len(photos),
            "preview": body_md[:120],
        }

    cover_url = ""
    extra_urls: list[str] = []
    if photos:
        cover_url = upload_image_to_cuisine(api_base, api_key, photos[0])
        for extra_photo in photos[1 : 1 + max_extra_images]:
            extra_urls.append(upload_image_to_cuisine(api_base, api_key, extra_photo))

    if extra_urls:
        gallery_md = "\n\n".join(f"![{trip.name}]({u})" for u in extra_urls)
        body_md = f"{body_md}\n\n{gallery_md}".strip()

    payload = {
        "title": resolved_title,
        "content": body_md or resolved_title,
        "category": category.upper(),
        "coverImageUrl": cover_url or None,
        "tags": tags or f"小灶日常,{trip.name}",
        "status": "PUBLISHED" if publish_now else "DRAFT",
    }

    resp = requests.post(
        f"{api_base}/api/v1/posts",
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    created = resp.json()
    return {
        "post_id": created.get("id"),
        "title": created.get("title"),
        "status": created.get("status"),
        "cover_url": cover_url,
        "uploaded_photos": (1 if cover_url else 0) + len(extra_urls),
    }
