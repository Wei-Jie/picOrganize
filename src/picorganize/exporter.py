"""匯出成品與即時重新編號：
- 移除所有 GPS 與裝置序號 EXIF（僅選擇性寫入拍攝時間）。
- IG：裁切為 4:5（1080×1350），以 `photo.focus` 為中心。
- FB／Threads：保留原比例，長邊縮放至 2048px。
- 檔名格式：`01_20261106_1430.jpg`、`02_...jpg`，拖曳調整順序時可即時重新編號而不必重新轉檔。
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import piexif
from PIL import Image

from .config import AppConfig
from .imaging import open_image
from .manifest import PLATFORMS, Manifest, Photo, Trip


def _crop_aspect(im: Image.Image, target_w: int, target_h: int, focus: tuple[float, float]) -> Image.Image:
    """依指定焦點 (fx, fy) 裁切為目標比例後縮放至 (target_w, target_h)。"""
    w, h = im.size
    target_ratio = target_w / target_h
    cur_ratio = w / h
    fx, fy = focus

    if cur_ratio > target_ratio:
        new_w = int(round(h * target_ratio))
        cx = int(round(w * fx))
        left = max(0, min(w - new_w, cx - new_w // 2))
        box = (left, 0, left + new_w, h)
    else:
        new_h = int(round(w / target_ratio))
        cy = int(round(h * fy))
        top = max(0, min(h - new_h, cy - new_h // 2))
        box = (0, top, w, top + new_h)

    cropped = im.crop(box)
    return cropped.resize((target_w, target_h), Image.Resampling.LANCZOS)


def _clean_exif_bytes(taken_at: datetime) -> bytes:
    """只寫入乾淨的拍攝時間 EXIF，完全不包含 GPS 與相機序號。"""
    dt_str = taken_at.strftime("%Y:%m:%d %H:%M:%S").encode("ascii")
    exif_dict = {
        "0th": {piexif.ImageIFD.DateTime: dt_str},
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: dt_str,
            piexif.ExifIFD.DateTimeDigitized: dt_str,
        },
        "GPS": {},
        "1st": {},
        "thumbnail": None,
    }
    return piexif.dump(exif_dict)


def _out_name(index: int, photo: Photo) -> str:
    return f"{index:02d}_{photo.taken_at:%Y%m%d_%H%M}_{photo.id[-6:]}.jpg"


def export_trip(trip: Trip, cfg: AppConfig) -> dict[str, list[Path]]:
    """將目前選取的照片匯出至 `ready_to_post/{ig,fb,threads}`，並保留歷史版本備份。"""
    m: Manifest = trip.load()
    m.sync_orders()
    m.export_version += 1
    ver = f"v{m.export_version}"

    history_root = trip.ready / "_history" / ver
    outputs: dict[str, list[Path]] = {}
    base_time = datetime.now().replace(microsecond=0)

    for plat in PLATFORMS:
        plat_dir = trip.ready / plat
        if plat_dir.exists():
            shutil.rmtree(plat_dir)
        plat_dir.mkdir(parents=True, exist_ok=True)
        hist_dir = history_root / plat
        hist_dir.mkdir(parents=True, exist_ok=True)

        ids = m.orders.for_platform(plat)
        files: list[Path] = []
        for idx, pid in enumerate(ids, start=1):
            p = m.photos.get(pid)
            if not p:
                continue
            src = trip.raw / p.raw_name
            im = open_image(src).convert("RGB")
            if plat == "ig":
                tw, th = cfg.export.ig_size
                # 橫幅照片若比例過寬（> 1.91:1）裁為 1.91:1，否則統一裁為 4:5 縱向輪播
                out_im = _crop_aspect(im, tw, th, p.focus)
            else:
                out_im = im.copy()
                out_im.thumbnail((cfg.export.fb_long_side, cfg.export.fb_long_side), Image.Resampling.LANCZOS)

            stamp = base_time + timedelta(seconds=idx) if cfg.export.sequential_time else p.taken_at
            exif_bytes = _clean_exif_bytes(stamp)
            out_path = plat_dir / _out_name(idx, p)
            out_im.save(
                out_path,
                format="JPEG",
                quality=cfg.export.jpeg_quality,
                optimize=True,
                exif=exif_bytes,
            )
            shutil.copy2(out_path, hist_dir / out_path.name)
            files.append(out_path)
        outputs[plat] = files

    trip.save(m)
    return outputs


_RE_SUFFIX = re.compile(r"^\d+_(\d{8}_\d{4}_[0-9a-f]{6})\.jpg$")


def renumber_platform(trip: Trip, platform: str, ordered_ids: list[str]) -> list[str]:
    """在預覽頁拖曳改變順序時，直接將 `ready_to_post/<platform>` 內的檔案重新編號，不重新轉檔。"""
    m: Manifest = trip.load()
    plat_dir = trip.ready / platform
    if not plat_dir.exists():
        return []

    by_id: dict[str, Path] = {}
    for f in plat_dir.glob("*.jpg"):
        s = _RE_SUFFIX.match(f.name)
        if s:
            tail = s.group(1)[-6:]
            for pid in ordered_ids:
                if pid.endswith(tail):
                    by_id[pid] = f
                    break

    # 第一階段：先改為暫存檔名避免撞名
    temp_map: dict[str, Path] = {}
    for pid, f in by_id.items():
        tmp = plat_dir / f".tmp_{pid}.jpg"
        os.replace(f, tmp)
        temp_map[pid] = tmp

    # 第二階段：依新順序寫回正式編號檔名
    result: list[str] = []
    for idx, pid in enumerate(ordered_ids, start=1):
        p = m.photos.get(pid)
        tmp = temp_map.get(pid)
        if not p or not tmp:
            continue
        final = plat_dir / _out_name(idx, p)
        os.replace(tmp, final)
        result.append(final.name)
    return result
