"""旅程工作區與 manifest.json 狀態管理。"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .config import AppConfig

Status = Literal["imported", "rejected", "candidate", "selected"]
Origin = Literal["auto", "manual"]
PLATFORMS = ("ig", "fb", "threads")


class Scores(BaseModel):
    blur: float | None = None  # Laplacian 方差（越大越清晰）
    dark: float | None = None  # 過暗像素比例
    bright: float | None = None  # 過曝像素比例
    quality: float | None = None  # 綜合品質分數（0~1，群組內相對）


class Photo(BaseModel):
    id: str
    source: str  # album_zip / folder / picker
    source_file: str  # 原始檔名
    batch: int
    taken_at: datetime
    time_source: str  # exif / filename / zip / mtime
    sha256: str
    ext: str
    width: int = 0
    height: int = 0
    gps: tuple[float, float] | None = None
    phash: str | None = None
    burst_group: str | None = None
    cluster: str | None = None
    scores: Scores = Field(default_factory=Scores)
    status: Status = "imported"
    reject_reason: str | None = None
    origin: Origin = "auto"
    locked: bool = False
    dismissed: bool = False  # 使用者取消過的 AI 建議，AI 不再挑回
    warnings: list[str] = Field(default_factory=list)
    ai_reason: str | None = None
    focus: tuple[float, float] = (0.5, 0.5)  # 裁切焦點（0~1）

    @property
    def raw_name(self) -> str:
        return f"{self.id}{self.ext}"


class Cluster(BaseModel):
    id: str
    start: datetime
    end: datetime
    count: int = 0
    place: str | None = None  # GPS 反查的地名
    note: str = ""  # 使用者在確認頁輸入的地名／備註


class Batch(BaseModel):
    n: int
    source: str
    origin: Origin
    at: datetime
    files: list[str] = Field(default_factory=list)
    added: int = 0
    skipped_dup: int = 0
    skipped_other: int = 0


class Orders(BaseModel):
    shared: list[str] = Field(default_factory=list)
    ig: list[str] | None = None  # None 表示沿用 shared
    fb: list[str] | None = None
    threads: list[str] | None = None
    ai_suggested: list[str] = Field(default_factory=list)

    def for_platform(self, platform: str) -> list[str]:
        own = getattr(self, platform, None)
        return list(own) if own is not None else list(self.shared)


class Manifest(BaseModel):
    trip: str
    created_at: datetime = Field(default_factory=datetime.now)
    photos: dict[str, Photo] = Field(default_factory=dict)
    clusters: dict[str, Cluster] = Field(default_factory=dict)
    batches: list[Batch] = Field(default_factory=list)
    orders: Orders = Field(default_factory=Orders)
    select_count: int | None = None
    selection_confirmed: bool = False
    export_version: int = 0
    caption_version: int = 0

    # ---- 查詢 ----
    def by_sha(self) -> dict[str, Photo]:
        return {p.sha256: p for p in self.photos.values()}

    def selected(self) -> list[Photo]:
        return [p for p in self.photos.values() if p.status == "selected"]

    def next_batch(self) -> int:
        return max((b.n for b in self.batches), default=0) + 1

    def sorted_photos(self) -> list[Photo]:
        return sorted(self.photos.values(), key=lambda p: (p.taken_at, p.id))

    # ---- 順序維護 ----
    def sync_orders(self) -> None:
        """讓各平台順序與目前選取的照片一致：移除未選取者、把新選取者補到最後。"""
        sel = {p.id for p in self.selected()}
        by_time = [p.id for p in self.sorted_photos() if p.id in sel]

        def fix(lst: list[str]) -> list[str]:
            kept = [i for i in lst if i in sel]
            kept += [i for i in by_time if i not in kept]
            return kept

        self.orders.shared = fix(self.orders.shared)
        for plat in PLATFORMS:
            own = getattr(self.orders, plat)
            if own is not None:
                setattr(self.orders, plat, fix(own))


class Trip:
    """一趟旅程的工作區。"""

    def __init__(self, root: Path, name: str):
        self.name = name
        self.dir = root / name
        self.manifest_path = self.dir / "manifest.json"

    # 各子資料夾
    @property
    def inbox(self) -> Path:
        return self.dir / "inbox"

    @property
    def raw(self) -> Path:
        return self.dir / "raw"

    @property
    def thumbs(self) -> Path:
        return self.dir / "thumbs"

    @property
    def ready(self) -> Path:
        return self.dir / "ready_to_post"

    @property
    def captions(self) -> Path:
        return self.dir / "captions"

    def ensure(self) -> None:
        for d in (self.inbox, self.raw, self.thumbs, self.ready, self.captions):
            d.mkdir(parents=True, exist_ok=True)

    def exists(self) -> bool:
        return self.manifest_path.exists()

    def load(self) -> Manifest:
        if not self.manifest_path.exists():
            return Manifest(trip=self.name)
        return Manifest.model_validate_json(self.manifest_path.read_text(encoding="utf-8"))

    def save(self, m: Manifest) -> None:
        """以「寫入暫存檔再取代」的方式儲存，避免中途中斷造成檔案損毀。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        data = m.model_dump_json(indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".manifest.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, self.manifest_path)


def trips_root(cfg: AppConfig, root: Path | None = None) -> Path:
    base = root or Path.cwd()
    return base / cfg.trips_dir


def get_trip(cfg: AppConfig, name: str, root: Path | None = None) -> Trip:
    return Trip(trips_root(cfg, root), name)


def list_trips(cfg: AppConfig, root: Path | None = None) -> list[str]:
    r = trips_root(cfg, root)
    if not r.exists():
        return []
    return sorted(p.name for p in r.iterdir() if (p / "manifest.json").exists())


def dump_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
