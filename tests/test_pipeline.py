"""單元與整合測試：驗證重複略過、手動鎖定不淘汰、AI 不替換鎖定照、拖曳即時重新編號與移除 GPS。"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
import piexif
from fastapi.testclient import TestClient
from PIL import Image

from picorganize.config import AppConfig
from picorganize.exporter import export_trip
from picorganize.filter import run_filter
from picorganize.importer import import_sources
from picorganize.manifest import get_trip
from picorganize.selector import apply_agent_selection, auto_select
from picorganize.web.server import create_app


def _make_jpeg_bytes(seed: int, sharp: bool = True, with_gps: bool = True, dt_str: str = "2026:11:06 09:00:00") -> bytes:
    rng = np.random.default_rng(seed)
    if sharp:
        arr = rng.integers(30, 220, size=(300, 400, 3), dtype=np.uint8)
    else:
        arr = np.full((300, 400, 3), 128, dtype=np.uint8)
        arr[100:200, 100:200] = 135
    im = Image.fromarray(arr, mode="RGB")
    exif = {
        "0th": {},
        "Exif": {piexif.ExifIFD.DateTimeOriginal: dt_str.encode("ascii")},
        "GPS": {},
        "1st": {},
        "thumbnail": None,
    }
    if with_gps:
        exif["GPS"] = {
            piexif.GPSIFD.GPSLatitudeRef: b"N",
            piexif.GPSIFD.GPSLatitude: ((25, 1), (8, 1), (0, 1)),
            piexif.GPSIFD.GPSLongitudeRef: b"E",
            piexif.GPSIFD.GPSLongitude: ((121, 1), (25, 1), (0, 1)),
        }
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90, exif=piexif.dump(exif))
    return buf.getvalue()


def test_import_dedupe_lock_and_reorder(tmp_path: Path) -> None:
    cfg = AppConfig()
    trip = get_trip(cfg, "test-trip", root=tmp_path)

    # 1. 建立模擬相簿 ZIP（6 張清晰照片）
    zip1 = tmp_path / "album1.zip"
    with zipfile.ZipFile(zip1, "w") as z:
        for i in range(6):
            z.writestr(f"IMG_{i:02d}.jpg", _make_jpeg_bytes(100 + i, sharp=True, dt_str=f"2026:11:06 09:{i * 5:02d}:00"))

    b1 = import_sources(trip, [zip1], origin="auto")
    assert b1.added == 6
    run_filter(trip, cfg)
    picked = auto_select(trip, cfg, count=3)
    assert len(picked) == 3

    # 2. 模擬使用者在手機相簿加入 1 張「非常模糊」的照片後，重新下載整本相簿 ZIP
    zip2 = tmp_path / "album2.zip"
    blurry_bytes = _make_jpeg_bytes(999, sharp=False, dt_str="2026:11:06 09:06:00")
    with zipfile.ZipFile(zip2, "w") as z:
        for i in range(6):
            z.writestr(f"IMG_{i:02d}.jpg", _make_jpeg_bytes(100 + i, sharp=True, dt_str=f"2026:11:06 09:{i * 5:02d}:00"))
        z.writestr("BLURRY_MEMO.jpg", blurry_bytes)

    b2 = import_sources(trip, [zip2], origin="manual")
    # 舊的 6 張自動略過，只新增 1 張
    assert b2.skipped_dup == 6
    assert b2.added == 1

    run_filter(trip, cfg)
    m = trip.load()
    added_id = b2.files[0]
    added_photo = m.photos[added_id]

    # 驗證手動加入的照片：必定 locked=True、status="selected"，不會因為模糊被淘汰，但會有 blur warning
    assert added_photo.locked is True
    assert added_photo.status == "selected"
    assert "blur" in added_photo.warnings

    # 3. 驗證 AI 重挑或 Agent 指定挑選時，鎖定照片絕對不會被換掉
    apply_agent_selection(trip, [{"id": picked[0].id, "reason": "封面"}])
    m = trip.load()
    assert m.photos[added_id].status == "selected"
    assert added_id in m.orders.shared

    # 4. 驗證匯出後 GPS 完全移除，且拖曳排序 API 會即時重新編號 ready_to_post 內的檔案
    export_trip(trip, cfg)
    ig_files = sorted((trip.ready / "ig").glob("*.jpg"))
    assert len(ig_files) >= 2
    for f in ig_files:
        ex = piexif.load(str(f))
        assert not ex.get("GPS")

    client = TestClient(create_app("test-trip", cfg, root=tmp_path))
    cur_order = m.orders.shared
    reversed_order = list(reversed(cur_order))
    resp = client.post("/api/order", json={"platform": "shared", "independent": False, "ordered_ids": reversed_order})
    assert resp.status_code == 200
    state = resp.json()
    assert state["orders"]["shared"] == reversed_order
    # 第一張檔案後綴應對應新順序的第一張照片
    first_ready = state["ready_files"]["ig"][0]
    assert first_ready.startswith("01_")
    assert reversed_order[0][-6:] in first_ready
