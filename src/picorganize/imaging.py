"""影像讀取與 EXIF 解析的共用工具。

注意：OpenCV 的 `imread` 不支援 Windows 中文路徑，因此一律以 Pillow 讀檔再轉成 numpy。
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps

try:  # 支援 iPhone HEIC
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:  # pragma: no cover - 缺少套件時仍可處理 JPG
    pass

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv"}

TAG_ORIENTATION = 274
TAG_DATETIME = 306
TAG_EXIF_IFD = 0x8769
TAG_GPS_IFD = 0x8825
TAG_DT_ORIGINAL = 36867
TAG_OFFSET_ORIGINAL = 36881


def open_image(src: Path | bytes, max_side: int | None = None) -> Image.Image:
    """開啟影像並依 EXIF 方向轉正。指定 max_side 時會利用 JPEG draft 模式加速解碼。"""
    im = Image.open(io.BytesIO(src) if isinstance(src, bytes) else src)
    if max_side and im.format == "JPEG":
        im.draft("RGB", (max_side, max_side))
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    if max_side:
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return im


def _parse_exif_dt(value: str | None, offset: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.strptime(value.strip()[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    if offset:
        try:
            aware = datetime.strptime(f"{dt:%Y-%m-%d %H:%M:%S}{offset.strip()}", "%Y-%m-%d %H:%M:%S%z")
            return aware.astimezone().replace(tzinfo=None)  # 統一換算成本機時間
        except ValueError:
            pass
    return dt


def _ratio(v) -> float:
    try:
        return float(v)
    except TypeError:
        num, den = v
        return num / den if den else 0.0


def _gps(gps_ifd) -> tuple[float, float] | None:
    try:
        lat = gps_ifd[2]
        lon = gps_ifd[4]
        la = _ratio(lat[0]) + _ratio(lat[1]) / 60 + _ratio(lat[2]) / 3600
        lo = _ratio(lon[0]) + _ratio(lon[1]) / 60 + _ratio(lon[2]) / 3600
        if gps_ifd.get(1) == "S":
            la = -la
        if gps_ifd.get(3) == "W":
            lo = -lo
        if la == 0 and lo == 0:
            return None
        return (round(la, 6), round(lo, 6))
    except (KeyError, IndexError, TypeError, ZeroDivisionError):
        return None


class ImageMeta:
    def __init__(self, width: int, height: int, taken: datetime | None, gps: tuple[float, float] | None):
        self.width = width
        self.height = height
        self.taken = taken
        self.gps = gps


def read_meta(src: Path | bytes) -> ImageMeta:
    """只讀取檔頭資訊（尺寸、拍攝時間、GPS），不解碼整張影像。"""
    im = Image.open(io.BytesIO(src) if isinstance(src, bytes) else src)
    w, h = im.size
    taken = None
    gps = None
    try:
        ex = im.getexif()
    except Exception:
        ex = None
    if ex:
        if ex.get(TAG_ORIENTATION) in (5, 6, 7, 8):
            w, h = h, w
        sub = ex.get_ifd(TAG_EXIF_IFD)
        taken = _parse_exif_dt(sub.get(TAG_DT_ORIGINAL), sub.get(TAG_OFFSET_ORIGINAL)) or _parse_exif_dt(
            ex.get(TAG_DATETIME), None
        )
        gps_ifd = ex.get_ifd(TAG_GPS_IFD)
        if gps_ifd:
            gps = _gps(gps_ifd)
    return ImageMeta(w, h, taken, gps)


_RE_EPOCH_MS = re.compile(r"(?<!\d)(1[5-9]\d{11})(?!\d)")
_RE_PXL = re.compile(r"PXL_(\d{8})_(\d{6})")  # Pixel 檔名為 UTC 時間
_RE_GENERIC = re.compile(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})[-_ T]?(\d{2})[-_.:]?(\d{2})[-_.:]?(\d{2})")


def time_from_filename(name: str) -> datetime | None:
    """從檔名推測時間：毫秒時間戳、Pixel 的 PXL_ 格式、一般 YYYYMMDD_HHMMSS。"""
    stem = Path(name).stem
    m = _RE_PXL.search(stem)
    if m:
        utc = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return utc.astimezone().replace(tzinfo=None)
    m = _RE_EPOCH_MS.search(stem)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000)
    m = _RE_GENERIC.search(stem)
    if m:
        try:
            return datetime(*map(int, m.groups()))
        except ValueError:
            return None
    return None
