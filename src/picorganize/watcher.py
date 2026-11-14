"""監看「下載」資料夾：偵測從 Google 相簿下載的 ZIP 壓縮檔（含大型相簿拆成的多個分卷 ZIP）。"""

from __future__ import annotations

import time
from pathlib import Path

from .config import AppConfig


def wait_for_zips(cfg: AppConfig, watch_dir: Path | None = None, timeout_seconds: int = 600) -> list[Path]:
    """監看指定資料夾，等待新的 `.zip` 下載完成並穩定後回傳檔案清單。"""
    folder = watch_dir or Path(cfg.watch.downloads_dir)
    if not folder.exists():
        raise FileNotFoundError(f"找不到監看資料夾：{folder}")

    existing = {p.resolve() for p in folder.glob("*.zip")}
    found: dict[Path, tuple[int, float]] = {}
    ready: list[Path] = []
    first_ready_at: float | None = None
    start = time.time()

    while time.time() - start < timeout_seconds:
        now = time.time()
        # 忽略還在下載中的 .crdownload / .part
        downloading = any(folder.glob("*.crdownload")) or any(folder.glob("*.part"))
        for z in folder.glob("*.zip"):
            rp = z.resolve()
            if rp in existing or rp in ready:
                continue
            try:
                sz = rp.stat().st_size
            except OSError:
                continue
            if sz <= 0:
                continue
            prev = found.get(rp)
            if prev is None or prev[0] != sz:
                found[rp] = (sz, now)
            elif now - prev[1] >= cfg.watch.settle_seconds and not downloading:
                ready.append(rp)
                if first_ready_at is None:
                    first_ready_at = now

        # 若已經收到至少一個穩定的 ZIP，再多等一小段時間確認有沒有多卷分拆的下一包
        if ready and first_ready_at and not downloading and (now - first_ready_at) >= min(10, cfg.watch.batch_window_seconds):
            if len(ready) == len(found):
                return sorted(ready)

        time.sleep(1.0)

    return sorted(ready)
