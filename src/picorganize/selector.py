"""智慧挑選：遵守「鎖定照片不可替換」規則，支援自動初選與 Agent 指定挑選。"""

from __future__ import annotations

from pathlib import Path

from .config import AppConfig
from .manifest import Manifest, Photo, Trip, dump_json


def auto_select(
    trip: Trip,
    cfg: AppConfig,
    count: int | None = None,
    dismiss_current_unlocked: bool = False,
    reset_dismissed: bool = False,
) -> list[Photo]:
    """依景點時間軸與品質分數挑出代表照片，鎖定照片固定占名額。

    - `dismiss_current_unlocked=True`：將目前未鎖定的已選照片標記為 `dismissed`，換一批新的候選照。
    - `reset_dismissed=True`：清除所有 `dismissed` 標記，從頭挑選。
    """
    m: Manifest = trip.load()
    target = count or m.select_count or cfg.select.count

    if reset_dismissed:
        for p in m.photos.values():
            p.dismissed = False
    elif dismiss_current_unlocked:
        for p in m.photos.values():
            if p.status == "selected" and not p.locked:
                p.dismissed = True
                p.ai_reason = None

    # 1. 鎖定照片一律保留
    locked = [p for p in m.sorted_photos() if p.locked]
    for p in locked:
        p.status = "selected"

    # 若鎖定張數超過預設張數，自動擴充（上限 max_count）
    effective_target = min(cfg.select.max_count, max(target, len(locked)))
    remaining = max(0, effective_target - len(locked))

    # 重設先前非鎖定的 selected 為 candidate
    for p in m.photos.values():
        if not p.locked and p.status == "selected":
            p.status = "candidate"

    candidates = [
        p
        for p in m.sorted_photos()
        if p.status == "candidate" and not p.locked and not p.dismissed
    ]
    # 若候選池因為多次換批不夠用，自動解除 dismissed 重新輪替
    if len(candidates) < remaining:
        for p in m.photos.values():
            if not p.locked:
                p.dismissed = False
        candidates = [p for p in m.sorted_photos() if p.status == "candidate" and not p.locked]

    picked: list[Photo] = []
    if remaining > 0 and candidates:
        # 依景點群組平均分配名額，同群組內挑品質最高且時間分散的照片
        by_cluster: dict[str, list[Photo]] = {}
        for p in candidates:
            by_cluster.setdefault(p.cluster or "spot-01", []).append(p)

        cluster_ids = sorted(by_cluster.keys())
        # 第一輪：每個還沒被鎖定照片涵蓋的景點先挑 1 張最高分
        locked_clusters = {p.cluster for p in locked}
        priority_clusters = [c for c in cluster_ids if c not in locked_clusters] + [
            c for c in cluster_ids if c in locked_clusters
        ]

        used = {p.id for p in locked}
        while len(picked) < remaining:
            added_in_round = False
            for cid in priority_clusters:
                if len(picked) >= remaining:
                    break
                pool = [p for p in by_cluster[cid] if p.id not in used]
                if not pool:
                    continue
                # 挑選品質分數最高、且與已選照片 pHash 差距較大的照片
                best = max(pool, key=lambda x: (x.scores.quality or 0.0, x.scores.blur or 0.0))
                best.status = "selected"
                if not best.ai_reason:
                    cname = m.clusters.get(cid).place if cid in m.clusters else None
                    loc = f"（{cname}）" if cname else ""
                    best.ai_reason = f"{cid}{loc} 清晰度最佳代表照（品質 {best.scores.quality:.2f}）"
                picked.append(best)
                used.add(best.id)
                added_in_round = True
            if not added_in_round:
                break

    final = sorted(locked + picked, key=lambda p: (p.taken_at, p.id))
    m.orders.ai_suggested = [p.id for p in final]
    if not m.orders.shared or dismiss_current_unlocked or reset_dismissed:
        m.orders.shared = list(m.orders.ai_suggested)
    m.sync_orders()

    # 輸出供 Antigravity Agent 檢視的候選清單摘要
    summary = {
        "trip": m.trip,
        "target_count": effective_target,
        "locked_ids": [p.id for p in locked],
        "selected": [
            {
                "id": p.id,
                "thumb": f"thumbs/{p.id}.jpg",
                "taken_at": p.taken_at.isoformat(),
                "cluster": p.cluster,
                "locked": p.locked,
                "quality": p.scores.quality,
                "ai_reason": p.ai_reason,
            }
            for p in final
        ],
        "candidates_by_cluster": {
            cid: [
                {
                    "id": p.id,
                    "thumb": f"thumbs/{p.id}.jpg",
                    "taken_at": p.taken_at.strftime("%H:%M:%S"),
                    "quality": p.scores.quality,
                    "blur": p.scores.blur,
                    "status": p.status,
                }
                for p in m.sorted_photos()
                if p.cluster == cid and p.status in ("candidate", "selected")
            ]
            for cid in sorted(m.clusters.keys())
        },
    }
    dump_json(trip.dir / "candidates_summary.json", summary)
    trip.save(m)
    return final


def apply_agent_selection(
    trip: Trip,
    picks: list[dict[str, str]],
) -> list[Photo]:
    """套用 Agent 指定的挑選結果（`[{"id": "...", "reason": "..."}]`），自動保留所有鎖定照片。"""
    m: Manifest = trip.load()
    locked_ids = {p.id for p in m.photos.values() if p.locked}

    # 先將非鎖定的照片重設為 candidate
    for p in m.photos.values():
        if not p.locked and p.status == "selected":
            p.status = "candidate"

    ordered_ids: list[str] = []
    for item in picks:
        pid = item["id"]
        if pid not in m.photos:
            continue
        p = m.photos[pid]
        p.status = "selected"
        if item.get("reason"):
            p.ai_reason = item["reason"]
        if pid not in ordered_ids:
            ordered_ids.append(pid)

    # 確保所有鎖定照片都在選取清單中（不可被 Agent 換掉）
    for pid in sorted(locked_ids):
        p = m.photos[pid]
        p.status = "selected"
        if pid not in ordered_ids:
            ordered_ids.append(pid)

    m.orders.ai_suggested = list(ordered_ids)
    m.orders.shared = list(ordered_ids)
    m.sync_orders()
    trip.save(m)
    return [m.photos[i] for i in m.orders.shared if i in m.photos]
