"""本機快篩與品質評分管線（不耗費任何外部 API 額度）。

步驟：
1. 對尚未計算分數的照片計算 pHash、Laplacian 模糊度、曝光分布並產生 512px 縮圖。
2. 依拍攝時間切分景點群組（`spot-01`、`spot-02`...），若有 GPS 則離線反查地名。
3. 連拍／相似照分組（`burst_group`），同組只留最清晰的一張。
4. 在同一景點群組內依模糊度排名，淘汰最模糊的比例（預設 25%），以及嚴重過曝／過暗的照片。
5. **鎖定的照片（`locked: true`）一律不淘汰**：改寫入 `warnings`（如 `blur`、`exposure`、`similar:<id>`），並維持 `selected` 狀態。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import cv2
import imagehash
import numpy as np
from PIL import Image

from .config import AppConfig
from .imaging import open_image
from .manifest import Cluster, Manifest, Photo, Scores, Trip


def _analyze_one(raw_path: Path, thumb_path: Path, analyze_size: int, thumb_size: int) -> tuple[str, Scores]:
    """計算單張照片的 pHash、模糊度、曝光比例，並輸出縮圖。"""
    im = open_image(raw_path, max_side=analyze_size)
    ph = str(imagehash.phash(im))

    rgb = np.asarray(im.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    total = float(gray.size) or 1.0
    dark = float(np.count_nonzero(gray < 15)) / total
    bright = float(np.count_nonzero(gray > 245)) / total

    if not thumb_path.exists():
        thumb = im.copy()
        thumb.thumbnail((thumb_size, thumb_size), Image.Resampling.LANCZOS)
        thumb.convert("RGB").save(thumb_path, format="JPEG", quality=85, optimize=True)

    return ph, Scores(blur=round(blur, 2), dark=round(dark, 4), bright=round(bright, 4))


def _assign_clusters(photos: list[Photo], cfg: AppConfig, old_clusters: dict[str, Cluster]) -> dict[str, Cluster]:
    """依拍攝時間間隔切分景點群組，並保留既有群組的使用者備註。"""
    if not photos:
        return {}
    gap = timedelta(minutes=cfg.cluster.gap_minutes)
    groups: list[list[Photo]] = [[photos[0]]]
    for p in photos[1:]:
        if p.taken_at - groups[-1][-1].taken_at > gap:
            groups.append([p])
        else:
            groups[-1].append(p)

    # 收集每個群組的第一個有效 GPS 座標以批次離線反查地名
    coords: list[tuple[int, tuple[float, float]]] = []
    for idx, g in enumerate(groups):
        gps = next((p.gps for p in g if p.gps), None)
        if gps:
            coords.append((idx, gps))

    places: dict[int, str] = {}
    if coords:
        try:
            import reverse_geocoder as rg

            results = rg.search([c[1] for c in coords], verbose=False)
            for (idx, _), r in zip(coords, results):
                admin = r.get("admin1") or ""
                name = r.get("name") or ""
                places[idx] = f"{admin} {name}".strip() if admin != name else name
        except Exception:
            pass

    new_clusters: dict[str, Cluster] = {}
    for idx, g in enumerate(groups, start=1):
        cid = f"spot-{idx:02d}"
        for p in g:
            p.cluster = cid
        prev = old_clusters.get(cid)
        new_clusters[cid] = Cluster(
            id=cid,
            start=g[0].taken_at,
            end=g[-1].taken_at,
            count=len(g),
            place=places.get(idx - 1) or (prev.place if prev else None),
            note=prev.note if prev else "",
        )
    return new_clusters


def _phash_dist(a: str | None, b: str | None) -> int:
    if not a or not b:
        return 999
    return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)


def run_filter(trip: Trip, cfg: AppConfig, progress=None) -> dict[str, int]:
    """對旅程內的照片執行快篩與評分，支援增量處理（只計算新照片的特徵）。"""
    trip.ensure()
    m: Manifest = trip.load()

    # 1. 對尚未計算過特徵的照片平行計算
    todo = [p for p in m.photos.values() if p.phash is None or p.scores.blur is None or not (trip.thumbs / f"{p.id}.jpg").exists()]
    if todo:
        def work(p: Photo):
            ph, sc = _analyze_one(
                trip.raw / p.raw_name,
                trip.thumbs / f"{p.id}.jpg",
                cfg.blur.analyze_size,
                cfg.thumb_size,
            )
            p.phash = ph
            p.scores = sc

        with ThreadPoolExecutor(max_workers=min(8, max(2, len(todo)))) as ex:
            for idx, _ in enumerate(ex.map(work, todo), start=1):
                if progress:
                    progress(idx, len(todo))

    photos = m.sorted_photos()
    if not photos:
        trip.save(m)
        return {"total": 0, "candidates": 0, "rejected": 0, "locked": 0}

    # 2. 時間分群與離線地名反查
    m.clusters = _assign_clusters(photos, cfg, m.clusters)

    # 3. 每個景點群組內計算相對模糊度排名與綜合品質分數
    blur_cutoffs: dict[str, float] = {}
    by_cluster: dict[str, list[Photo]] = {}
    for p in photos:
        by_cluster.setdefault(p.cluster or "spot-01", []).append(p)

    for cid, group in by_cluster.items():
        blurs = sorted(p.scores.blur or 0.0 for p in group)
        if len(blurs) >= 4 and cfg.blur.drop_ratio > 0:
            k = max(0, int(len(blurs) * cfg.blur.drop_ratio) - 1)
            blur_cutoffs[cid] = blurs[k]
        else:
            blur_cutoffs[cid] = -1.0
        max_b = max(blurs) or 1.0
        min_b = min(blurs)
        span = (max_b - min_b) or 1.0
        for p in group:
            b_norm = ((p.scores.blur or 0.0) - min_b) / span
            exp_pen = max(0.0, (p.scores.dark or 0.0) - 0.15) + max(0.0, (p.scores.bright or 0.0) - 0.15)
            p.scores.quality = round(max(0.0, min(1.0, b_norm - exp_pen * 0.5)), 3)

    # 4. 連拍分組（只針對非鎖定照片分組淘汰；鎖定照片另外做相似提醒）
    auto_photos = [p for p in photos if not p.locked]
    for p in photos:
        p.burst_group = None
    burst_idx = 0
    visited: set[str] = set()
    burst_groups: dict[str, list[Photo]] = {}
    for i, p in enumerate(auto_photos):
        if p.id in visited:
            continue
        group = [p]
        visited.add(p.id)
        for q in auto_photos[i + 1 :]:
            if (q.taken_at - group[-1].taken_at).total_seconds() > cfg.dedupe.burst_seconds:
                break
            if any(_phash_dist(q.phash, g.phash) <= cfg.dedupe.phash_distance for g in group):
                group.append(q)
                visited.add(q.id)
        if len(group) > 1:
            burst_idx += 1
            gid = f"g{burst_idx:03d}"
            for g in group:
                g.burst_group = gid
            burst_groups[gid] = group

    burst_winners = {
        gid: max(g, key=lambda x: (x.scores.blur or 0.0)).id
        for gid, g in burst_groups.items()
    }

    # 5. 判定每張照片的狀態與鎖定照片的 warnings
    clip = cfg.exposure.clip_ratio
    sim_window = timedelta(minutes=cfg.dedupe.similar_minutes)

    for p in photos:
        p.warnings = []
        cutoff = blur_cutoffs.get(p.cluster or "spot-01", -1.0)
        is_blur = (p.scores.blur or 0.0) <= cutoff
        is_exp = (p.scores.dark or 0.0) > clip or (p.scores.bright or 0.0) > clip
        is_dup = bool(p.burst_group and burst_winners.get(p.burst_group) != p.id)

        if p.locked:
            # 鎖定照片一律保留並設為 selected，但附上提醒
            p.status = "selected"
            p.reject_reason = None
            if is_blur:
                p.warnings.append("blur")
            if is_exp:
                p.warnings.append("exposure")
            for other in photos:
                if other.id == p.id:
                    continue
                if abs(other.taken_at - p.taken_at) <= sim_window and _phash_dist(p.phash, other.phash) <= cfg.dedupe.phash_distance:
                    p.warnings.append(f"similar:{other.id}")
                    break
            continue

        # 已由 AI 挑選且尚未被淘汰的，維持 selected
        if is_dup:
            p.status = "rejected"
            p.reject_reason = "duplicate"
        elif is_exp:
            p.status = "rejected"
            p.reject_reason = "exposure"
        elif is_blur:
            p.status = "rejected"
            p.reject_reason = "blur"
        else:
            p.reject_reason = None
            if p.status != "selected":
                p.status = "candidate"

    m.sync_orders()
    trip.save(m)
    return {
        "total": len(photos),
        "candidates": sum(1 for p in photos if p.status in ("candidate", "selected")),
        "rejected": sum(1 for p in photos if p.status == "rejected"),
        "locked": sum(1 for p in photos if p.locked),
        "clusters": len(m.clusters),
        "burst_groups": len(burst_groups),
    }
