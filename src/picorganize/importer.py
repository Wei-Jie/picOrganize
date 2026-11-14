"""匯入照片：支援 Google 相簿下載的 ZIP 與本機資料夾。

- SHA-256 完全相同的檔案直接略過（重複下載整本相簿也不怕）。
- 第二道比對：原始檔名＋拍攝時間相同也視為同一張。
- 影片只記錄、不匯入（第一版只處理靜態照片）。
"""

from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .imaging import IMAGE_EXTS, VIDEO_EXTS, read_meta, time_from_filename
from .manifest import Batch, Manifest, Origin, Photo, Trip


@dataclass
class _Item:
    name: str  # 原始檔名（不含資料夾）
    data: bytes
    fallback_time: datetime | None  # ZIP 項目時間或檔案修改時間


def _iter_zip(path: Path, skipped: list[str]) -> Iterator[_Item]:
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name
            ext = Path(name).suffix.lower()
            if name.startswith(".") or ext not in IMAGE_EXTS:
                skipped.append(name)
                continue
            try:
                zt = datetime(*info.date_time)
            except ValueError:
                zt = None
            yield _Item(name, z.read(info), zt)


def _iter_folder(path: Path, skipped: list[str]) -> Iterator[_Item]:
    for f in sorted(path.rglob("*")):
        if not f.is_file() or "_skipped" in f.parts:
            continue
        ext = f.suffix.lower()
        if ext not in IMAGE_EXTS:
            skipped.append(f.name)
            continue
        yield _Item(f.name, f.read_bytes(), datetime.fromtimestamp(f.stat().st_mtime))


def _resolve_time(item: _Item, exif_time: datetime | None) -> tuple[datetime, str]:
    if exif_time:
        return exif_time, "exif"
    t = time_from_filename(item.name)
    if t:
        return t, "filename"
    if item.fallback_time:
        return item.fallback_time, "file"
    return datetime.now(), "now"


def import_sources(
    trip: Trip,
    sources: list[Path],
    origin: Origin = "auto",
    progress=None,
) -> Batch:
    """匯入一批來源（ZIP 或資料夾），回傳本批次的統計。

    origin="manual" 時，新照片會直接設為鎖定並選取（不經過淘汰）。
    """
    trip.ensure()
    m = trip.load()
    batch = Batch(
        n=m.next_batch(),
        source=",".join(s.name for s in sources),
        origin=origin,
        at=datetime.now(),
    )
    seen_sha = set(m.by_sha())
    seen_name_time = {(p.source_file, p.taken_at) for p in m.photos.values()}
    skipped: list[str] = []

    for src in sources:
        kind = "album_zip" if src.suffix.lower() == ".zip" else "folder"
        it = _iter_zip(src, skipped) if kind == "album_zip" else _iter_folder(src, skipped)
        for item in it:
            sha = hashlib.sha256(item.data).hexdigest()
            if sha in seen_sha:
                batch.skipped_dup += 1
                continue
            try:
                meta = read_meta(item.data)
            except Exception:
                skipped.append(item.name)
                continue
            taken, tsrc = _resolve_time(item, meta.taken)
            if (item.name, taken) in seen_name_time:
                batch.skipped_dup += 1
                continue
            ext = Path(item.name).suffix.lower()
            if ext == ".jpeg":
                ext = ".jpg"
            pid = f"{taken:%Y%m%d_%H%M%S}_{sha[:6]}"
            photo = Photo(
                id=pid,
                source=kind,
                source_file=item.name,
                batch=batch.n,
                taken_at=taken,
                time_source=tsrc,
                sha256=sha,
                ext=ext,
                width=meta.width,
                height=meta.height,
                gps=meta.gps,
                origin=origin,
                locked=origin == "manual",
            )
            (trip.raw / photo.raw_name).write_bytes(item.data)
            m.photos[pid] = photo
            seen_sha.add(sha)
            seen_name_time.add((item.name, taken))
            batch.added += 1
            batch.files.append(pid)
            if progress:
                progress(batch.added)
        batch.skipped_other = len(skipped)

    if skipped:
        (trip.inbox / "_skipped").mkdir(parents=True, exist_ok=True)
        log = trip.inbox / "_skipped" / f"batch{batch.n}.txt"
        log.write_text("\n".join(skipped), encoding="utf-8")

    m.batches.append(batch)
    trip.save(m)
    return batch
