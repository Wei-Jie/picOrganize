"""全域設定：讀取專案根目錄的 `config.yaml` 與 `.env`。"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DedupeCfg(BaseModel):
    phash_distance: int = 6  # pHash 漢明距離門檻，越小越嚴格
    burst_seconds: int = 10  # 連拍判定的最大時間差（秒）
    similar_minutes: int = 10  # 鎖定照片「相似提醒」的比對時間範圍（分鐘）


class ClusterCfg(BaseModel):
    gap_minutes: int = 40  # 相鄰照片時間差超過此值即切分景點


class BlurCfg(BaseModel):
    drop_ratio: float = 0.25  # 每個景點群組內淘汰模糊度最差的比例
    analyze_size: int = 1024  # 計算模糊度前先縮放的長邊尺寸


class ExposureCfg(BaseModel):
    clip_ratio: float = 0.35  # 過暗或過曝像素比例超過此值即淘汰


class SelectCfg(BaseModel):
    count: int = 8  # 預設挑選張數
    max_count: int = 20  # IG 輪播上限


class ExportCfg(BaseModel):
    ig_size: tuple[int, int] = (1080, 1350)  # IG 4:5
    fb_long_side: int = 2048
    jpeg_quality: int = 92
    sequential_time: bool = False  # 依順序改寫拍攝時間（預設關閉）


class TelegramCfg(BaseModel):
    mode: str = "document"  # document（保留原畫質）或 photo（會被壓縮）


class WatchCfg(BaseModel):
    downloads_dir: str = str(Path.home() / "Downloads")
    settle_seconds: int = 8  # ZIP 檔案大小穩定多久後才視為下載完成
    batch_window_seconds: int = 120  # 這段時間內下載的多個 ZIP 視為同一批


class AppConfig(BaseModel):
    trips_dir: str = "trips"
    thumb_size: int = 512
    dedupe: DedupeCfg = Field(default_factory=DedupeCfg)
    cluster: ClusterCfg = Field(default_factory=ClusterCfg)
    blur: BlurCfg = Field(default_factory=BlurCfg)
    exposure: ExposureCfg = Field(default_factory=ExposureCfg)
    select: SelectCfg = Field(default_factory=SelectCfg)
    export: ExportCfg = Field(default_factory=ExportCfg)
    telegram: TelegramCfg = Field(default_factory=TelegramCfg)
    watch: WatchCfg = Field(default_factory=WatchCfg)
    style_profile: str = "style_profile.yaml"
    style_max_examples: int = 6  # 風格回饋迴圈保留的範例篇數


class Secrets(BaseSettings):
    """機密設定，從 `.env` 讀取，不可提交至版控。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    gemini_api_key: str = ""


def project_root() -> Path:
    """以目前工作目錄作為專案根目錄。"""
    return Path.cwd()


def load_config(root: Path | None = None) -> AppConfig:
    root = root or project_root()
    path = root / "config.yaml"
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return AppConfig.model_validate(data)
    return AppConfig()


def load_secrets(root: Path | None = None) -> Secrets:
    root = root or project_root()
    return Secrets(_env_file=root / ".env")  # type: ignore[call-arg]
