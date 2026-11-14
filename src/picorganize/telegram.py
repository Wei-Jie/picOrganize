"""Telegram Bot 傳送模組：
- `setup_telegram`：讀取 `@BotFather` 產生的 Token，透過 `getUpdates` 自動抓取 `chat_id` 並寫入 `.env`。
- `send_to_telegram`：先傳送獨立一則純文字文案（方便手機長按複製），再依預覽頁排好的順序以 `sendMediaGroup` 分批（每批最多 10 張）傳送照片。
"""

from __future__ import annotations

import json
from pathlib import Path

import requests

from .captions import load_captions
from .config import AppConfig, load_secrets, project_root
from .manifest import Manifest, Trip

API_BASE = "https://api.telegram.org/bot{token}/{method}"


def _write_env_var(env_path: Path, key: str, value: str) -> None:
    lines: list[str] = []
    found = False
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def setup_telegram(token: str | None = None, root: Path | None = None) -> tuple[str, str]:
    """向 Telegram API 查詢最新傳訊給 Bot 的使用者 `chat_id`，並寫入 `.env`。"""
    base = root or project_root()
    secrets = load_secrets(base)
    bot_token = (token or secrets.telegram_bot_token).strip()
    if not bot_token:
        raise ValueError("尚未設定 TELEGRAM_BOT_TOKEN，請先透過 --token 傳入或寫入 .env。")

    env_path = base / ".env"
    _write_env_var(env_path, "TELEGRAM_BOT_TOKEN", bot_token)

    resp = requests.get(API_BASE.format(token=bot_token, method="getUpdates"), timeout=15)
    resp.raise_for_status()
    data = resp.json()
    results = data.get("result") or []
    chat_id = ""
    for item in reversed(results):
        msg = item.get("message") or item.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            chat_id = str(chat["id"])
            break

    if not chat_id:
        raise RuntimeError("已將 Token 寫入 .env，但還找不到對話紀錄；請在 Telegram 點開你的新 Bot 按下「開始（/start）」後再試一次。")

    _write_env_var(env_path, "TELEGRAM_CHAT_ID", chat_id)
    return bot_token, chat_id


def send_to_telegram(
    trip: Trip,
    cfg: AppConfig,
    platform: str = "ig",
    variant: str | None = None,
    mode: str | None = None,
    root: Path | None = None,
) -> dict:
    """將指定平台的文案與排好順序的照片傳送到使用者的 Telegram。"""
    base = root or project_root()
    secrets = load_secrets(base)
    if not secrets.telegram_bot_token:
        raise ValueError("尚未設定 TELEGRAM_BOT_TOKEN，請先執行 `uv run pic telegram-setup`。")
    if not secrets.telegram_chat_id:
        # 若已設定 Token 但尚未抓過 chat_id，自動嘗試從 getUpdates 取得並寫入 .env
        _, cid = setup_telegram(token=secrets.telegram_bot_token, root=base)
        secrets.telegram_chat_id = cid

    m: Manifest = trip.load()
    caps = load_captions(trip)
    v_key = variant or caps.get("active_variant", "narrative")
    v_data = (caps.get("variants") or {}).get(v_key) or {}
    caption_text = v_data.get(platform) or ""

    ver = max(1, m.export_version)
    header = f"【{platform.upper()}｜v{ver}｜{m.trip}】"

    # 1. 先傳送標題與純文字文案（獨立成則，方便手機長按整段複製）
    for text in (header, caption_text) if caption_text else (header,):
        r = requests.post(
            API_BASE.format(token=secrets.telegram_bot_token, method="sendMessage"),
            json={"chat_id": secrets.telegram_chat_id, "text": text},
            timeout=20,
        )
        r.raise_for_status()

    # 2. 依 ready_to_post/<platform> 內的編號順序分批傳送照片（每組 2~10 張）
    plat_dir = trip.ready / platform
    files = sorted(plat_dir.glob("*.jpg"))
    send_mode = (mode or cfg.telegram.mode).lower()
    media_type = "photo" if send_mode == "photo" else "document"

    batches_sent = 0
    for start in range(0, len(files), 10):
        chunk = files[start : start + 10]
        if len(chunk) == 1:
            method = "sendPhoto" if media_type == "photo" else "sendDocument"
            with chunk[0].open("rb") as fh:
                r = requests.post(
                    API_BASE.format(token=secrets.telegram_bot_token, method=method),
                    data={"chat_id": secrets.telegram_chat_id},
                    files={media_type: (chunk[0].name, fh, "image/jpeg")},
                    timeout=120,
                )
                r.raise_for_status()
        else:
            media = []
            open_files = {}
            try:
                for idx, fpath in enumerate(chunk):
                    key = f"file{idx}"
                    media.append({"type": media_type, "media": f"attach://{key}"})
                    open_files[key] = (fpath.name, fpath.open("rb"), "image/jpeg")
                r = requests.post(
                    API_BASE.format(token=secrets.telegram_bot_token, method="sendMediaGroup"),
                    data={"chat_id": secrets.telegram_chat_id, "media": json.dumps(media)},
                    files=open_files,
                    timeout=180,
                )
                r.raise_for_status()
            finally:
                for _, fh, _ in open_files.values():
                    fh.close()
        batches_sent += 1

    return {"platform": platform, "version": ver, "photos": len(files), "batches": batches_sent}
