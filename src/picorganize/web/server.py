"""FastAPI 後端：同時提供人工確認頁（`/review`）與 A+ 發布預覽頁（`/publish`）。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..captions import (
    fetch_blogger_posts,
    generate_draft_captions,
    learn_style_profile,
    load_captions,
    load_style_profile,
    save_captions,
    save_style_example,
)
from ..config import AppConfig, load_config
from ..cuisine_publisher import publish_trip_to_cuisine
from ..exporter import export_trip, renumber_platform
from ..manifest import PLATFORMS, Manifest, Trip, get_trip
from ..selector import auto_select
from ..telegram import send_to_telegram

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"
MOBILE_APP_DIR = WEB_DIR.parent.parent.parent / "mobile_app"


class ToggleReq(BaseModel):
    photo_id: str
    select: bool


class ReselectReq(BaseModel):
    count: int | None = None
    dismiss_current: bool = True
    reset_dismissed: bool = False


class NoteReq(BaseModel):
    cluster_id: str
    note: str


class OrderReq(BaseModel):
    platform: str = "shared"  # shared / ig / fb / threads
    independent: bool = False
    ordered_ids: list[str]


class CaptionUpdateReq(BaseModel):
    active_variant: str
    platform: str
    text: str


class RegenerateCaptionReq(BaseModel):
    mode: Literal["keep", "patch", "rewrite"] = "rewrite"


class SaveStyleReq(BaseModel):
    context: str
    text: str


class StyleLearnReq(BaseModel):
    blog_url: str | None = None
    raw_posts: str | None = None


class TelegramSendReq(BaseModel):
    platform: str = "ig"
    variant: str | None = None
    mode: str | None = None


class CuisinePublishReq(BaseModel):
    platform: str = "fb"
    variant: str | None = None
    title: str | None = None
    category: str = "STORY"
    publish_now: bool = False


def create_app(trip_name: str, cfg: AppConfig | None = None, root: Path | None = None) -> FastAPI:
    cfg = cfg or load_config(root)
    trip: Trip = get_trip(cfg, trip_name, root)
    if not trip.exists():
        raise FileNotFoundError(f"找不到旅程：{trip_name}")

    app = FastAPI(title=f"picOrganize - {trip_name}")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    if MOBILE_APP_DIR.exists():
        app.mount("/app", StaticFiles(directory=MOBILE_APP_DIR, html=True), name="mobile_app")
    app.mount("/thumbs", StaticFiles(directory=trip.thumbs), name="thumbs")
    app.mount("/raw", StaticFiles(directory=trip.raw), name="raw")

    @app.get("/", response_class=HTMLResponse)
    @app.get("/review", response_class=HTMLResponse)
    @app.get("/publish", response_class=HTMLResponse)
    def index_page() -> str:
        return (WEB_DIR / "ui.html").read_text(encoding="utf-8")

    @app.get("/ready/{platform}/{filename}")
    def ready_file(platform: str, filename: str):
        p = trip.ready / platform / filename
        if not p.exists():
            raise HTTPException(404, "找不到匯出照片")
        return FileResponse(p, headers={"Cache-Control": "no-store"})

    @app.get("/api/state")
    def get_state() -> dict:
        m: Manifest = trip.load()
        m.sync_orders()
        max_batch = max((b.n for b in m.batches), default=1)
        caps = load_captions(trip)
        if not caps.get("variants"):
            caps = generate_draft_captions(trip, cfg, mode="rewrite")

        ready_files: dict[str, list[str]] = {}
        for plat in PLATFORMS:
            d = trip.ready / plat
            ready_files[plat] = sorted(f.name for f in d.glob("*.jpg")) if d.exists() else []

        return {
            "trip": m.trip,
            "max_batch": max_batch,
            "export_version": m.export_version,
            "selection_confirmed": m.selection_confirmed,
            "orders": m.orders.model_dump(),
            "clusters": [c.model_dump(mode="json") for c in sorted(m.clusters.values(), key=lambda x: x.id)],
            "photos": [p.model_dump(mode="json") for p in m.sorted_photos()],
            "captions": caps,
            "ready_files": ready_files,
        }

    @app.post("/api/toggle")
    def toggle_photo(req: ToggleReq) -> dict:
        """使用者在確認頁點擊照片：勾選時自動設為 manual + locked；取消勾選時解除鎖定並標記 dismissed。"""
        m: Manifest = trip.load()
        p = m.photos.get(req.photo_id)
        if not p:
            raise HTTPException(404, "找不到照片")
        if req.select:
            p.status = "selected"
            p.origin = "manual"
            p.locked = True
            p.dismissed = False
            p.reject_reason = None
        else:
            p.locked = False
            p.dismissed = True
            p.status = "candidate"
        m.sync_orders()
        trip.save(m)
        return get_state()

    @app.post("/api/cluster-note")
    def update_cluster_note(req: NoteReq) -> dict:
        m: Manifest = trip.load()
        c = m.clusters.get(req.cluster_id)
        if not c:
            raise HTTPException(404, "找不到景點群組")
        c.note = req.note.strip()
        trip.save(m)
        return {"ok": True}

    @app.post("/api/order")
    def update_order(req: OrderReq) -> dict:
        """拖曳調整順序：存回 manifest，並即時重新編號 `ready_to_post/` 內的檔案。"""
        m: Manifest = trip.load()
        valid = [i for i in req.ordered_ids if i in m.photos and m.photos[i].status == "selected"]
        if req.platform == "shared" or not req.independent:
            m.orders.shared = valid
            if req.platform in PLATFORMS and not req.independent:
                setattr(m.orders, req.platform, None)
            m.sync_orders()
            trip.save(m)
            for plat in PLATFORMS:
                if getattr(m.orders, plat) is None:
                    renumber_platform(trip, plat, m.orders.shared)
        else:
            setattr(m.orders, req.platform, valid)
            m.sync_orders()
            trip.save(m)
            renumber_platform(trip, req.platform, m.orders.for_platform(req.platform))
        return get_state()

    @app.post("/api/reset-order")
    def reset_ai_order() -> dict:
        m: Manifest = trip.load()
        m.orders.shared = list(m.orders.ai_suggested)
        m.orders.ig = None
        m.orders.fb = None
        m.orders.threads = None
        m.sync_orders()
        trip.save(m)
        for plat in PLATFORMS:
            renumber_platform(trip, plat, m.orders.shared)
        return get_state()

    @app.post("/api/reselect")
    def reselect_photos(req: ReselectReq) -> dict:
        """重新挑選：保留所有 🔒 鎖定照片，將未鎖定的照片換成下一批高分候選照。"""
        auto_select(
            trip,
            cfg,
            count=req.count,
            dismiss_current_unlocked=req.dismiss_current,
            reset_dismissed=req.reset_dismissed,
        )
        return get_state()

    @app.post("/api/confirm-and-export")
    def confirm_and_export(req: RegenerateCaptionReq) -> dict:
        """確認選圖 → 更新文案（保留／局部補寫／重寫）→ 匯出各平台無 GPS 成品。"""
        m: Manifest = trip.load()
        m.selection_confirmed = True
        trip.save(m)
        generate_draft_captions(trip, cfg, mode=req.mode)
        export_trip(trip, cfg)
        return get_state()

    @app.post("/api/caption")
    def update_caption(req: CaptionUpdateReq) -> dict:
        caps = load_captions(trip)
        variants = caps.get("variants") or {}
        if req.active_variant not in variants:
            variants[req.active_variant] = {"label": req.active_variant}
        variants[req.active_variant][req.platform] = req.text
        saved = save_captions(trip, variants, active_variant=req.active_variant, bump=False)
        return {"captions": saved}

    @app.post("/api/regenerate-captions")
    def regenerate_captions(req: RegenerateCaptionReq) -> dict:
        saved = generate_draft_captions(trip, cfg, mode=req.mode)
        return {"captions": saved}

    @app.post("/api/save-style")
    def save_style(req: SaveStyleReq) -> dict:
        total = save_style_example(cfg, req.context, req.text, root)
        return {"ok": True, "examples_count": total}

    @app.get("/api/style-profile")
    def get_style_profile() -> dict:
        return load_style_profile(cfg, root)

    @app.get("/api/client-config")
    def get_client_config() -> dict:
        """僅供本機／區網連線時自動帶入 .env 中的金鑰，免去手動重複貼上（GitHub Pages 靜態版不會有此端點）。"""
        from ..config import load_secrets

        sec = load_secrets(root)
        return {
            "geminiKey": sec.gemini_api_key,
            "tgToken": sec.telegram_bot_token,
            "tgChatId": sec.telegram_chat_id,
        }

    @app.post("/api/style-learn")
    def api_style_learn(req: StyleLearnReq) -> dict:
        samples: list[str] = []
        if req.blog_url and req.blog_url.strip():
            posts = fetch_blogger_posts(req.blog_url.strip())
            samples.extend(f"標題：{p['title']}\n{p['text']}" for p in posts)
        if req.raw_posts and req.raw_posts.strip():
            parts = [s.strip() for s in req.raw_posts.split("---") if s.strip()]
            samples.extend(parts)
        if not samples:
            raise HTTPException(400, "請輸入 Blogger 網址或貼上至少 1 篇歷史貼文")
        try:
            profile = learn_style_profile(cfg, samples, root)
            return {"ok": True, "profile": profile}
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.post("/api/open-folder")
    def open_folder(platform: str = "ig") -> dict:
        target = (trip.ready / platform).resolve()
        target.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return {"ok": True, "path": str(target)}

    @app.post("/api/send-telegram")
    def api_send_telegram(req: TelegramSendReq) -> dict:
        try:
            res = send_to_telegram(trip, cfg, platform=req.platform, variant=req.variant, mode=req.mode, root=root)
            return {"ok": True, **res}
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.post("/api/publish-cuisine")
    def api_publish_cuisine(req: CuisinePublishReq) -> dict:
        try:
            res = publish_trip_to_cuisine(
                trip,
                cfg,
                platform=req.platform,
                variant=req.variant,
                title=req.title,
                category=req.category,
                publish_now=req.publish_now,
            )
            return {"ok": True, **res}
        except Exception as e:
            raise HTTPException(400, str(e))

    return app
