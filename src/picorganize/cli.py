"""picOrganize CLI 入口：提供 `pic` 與 `picorganize` 指令。"""

from __future__ import annotations

import json
import sys
import webbrowser
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

# 確保 Windows PowerShell 預設 cp950 終端機也能正常輸出 UTF-8 與 Emoji
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from .captions import (
    ensure_style_profile,
    fetch_blogger_posts,
    generate_draft_captions,
    learn_style_profile,
)
from .config import load_config
from .exporter import export_trip
from .filter import run_filter
from .importer import import_sources
from .manifest import Manifest, get_trip, list_trips
from .selector import apply_agent_selection, auto_select
from .telegram import send_to_telegram, setup_telegram
from .watcher import wait_for_zips
from .web.server import create_app

app = typer.Typer(
    name="pic",
    help="旅行照片智慧挑選與個人風格社群發文工具（免社群 API、支援鎖定必選與拖曳排序）",
    no_args_is_help=True,
)


def _resolve_trip(trip_opt: str | None):
    cfg = load_config()
    ensure_style_profile(cfg)
    if trip_opt:
        return cfg, get_trip(cfg, trip_opt)
    trips = list_trips(cfg)
    if len(trips) == 1:
        return cfg, get_trip(cfg, trips[0])
    if not trips:
        raise typer.BadParameter("目前還沒有任何旅程，請先執行 `uv run pic new <旅程名稱>` 或在指令加上 `--trip`。")
    raise typer.BadParameter(f"請以 `--trip` 指定旅程名稱（現有：{', '.join(trips)}）。")


@app.command("new")
def cmd_new(name: Annotated[str, typer.Argument(help="旅程名稱，例如 2026-11-硬漢嶺")]) -> None:
    """建立新的旅程工作區與預設設定檔。"""
    cfg = load_config()
    ensure_style_profile(cfg)
    trip = get_trip(cfg, name)
    trip.ensure()
    if not trip.exists():
        trip.save(Manifest(trip=name))
    typer.echo(f"✅ 已建立旅程工作區：{trip.dir}")


@app.command("import")
def cmd_import(
    trip: Annotated[str, typer.Option("--trip", "-t", help="旅程名稱")],
    zip_files: Annotated[list[Path] | None, typer.Option("--zip", "-z", help="指定一或多個 Google 相簿下載的 ZIP 檔")] = None,
    zip_watch: Annotated[bool, typer.Option("--zip-watch", help="監看「下載」資料夾，等待新的相簿 ZIP 下載完成")] = False,
    count: Annotated[int | None, typer.Option("--count", "-c", help="預設挑選張數")] = None,
) -> None:
    """第一次匯入照片並執行本機快篩與初選（交由程式篩選，`origin=auto`）。"""
    cfg = load_config()
    ensure_style_profile(cfg)
    tr = get_trip(cfg, trip)
    tr.ensure()

    sources: list[Path] = []
    if zip_watch:
        typer.echo(f"👀 正在監看下載資料夾（{cfg.watch.downloads_dir}），請到 Google 相簿按 Shift+D 下載整本相簿...")
        sources = wait_for_zips(cfg)
        if not sources:
            raise typer.Exit(1)
        typer.echo(f"📦 偵測到下載完成：{[s.name for s in sources]}")
    elif zip_files:
        sources = list(zip_files)
    else:
        sources = [tr.inbox]

    typer.echo(f"📥 正在匯入照片至 `{tr.name}`（origin=auto）...")
    batch = import_sources(tr, sources, origin="auto")
    typer.echo(
        f"   新增 {batch.added} 張｜略過完全重複 {batch.skipped_dup} 張｜略過非照片／影片 {batch.skipped_other} 個"
    )

    typer.echo("🔍 正在執行本機快篩（HEIC 轉換、pHash 連拍去重、時間分群、模糊度與曝光檢測、產生 512px 縮圖）...")
    stats = run_filter(tr, cfg)
    typer.echo(
        f"   總計 {stats['total']} 張 → 候選 {stats['candidates']} 張｜淘汰 {stats['rejected']} 張"
        f"｜景點群組 {stats['clusters']} 個｜連拍組 {stats['burst_groups']} 組"
    )

    picked = auto_select(tr, cfg, count=count)
    typer.echo(f"✨ 已完成初步挑選 {len(picked)} 張（詳見 {tr.dir / 'candidates_summary.json'}）")


@app.command("add")
def cmd_add(
    trip: Annotated[str | None, typer.Option("--trip", "-t", help="旅程名稱")] = None,
    zip_files: Annotated[list[Path] | None, typer.Option("--zip", "-z", help="指定新下載的相簿 ZIP 或檔案路徑")] = None,
    zip_watch: Annotated[bool, typer.Option("--zip-watch", help="監看「下載」資料夾等待新 ZIP")] = False,
    auto: Annotated[bool, typer.Option("--auto", help="改交由程式快篩淘汰（預設為手動鎖定 🔒、絕不淘汰）")] = False,
) -> None:
    """追加照片（增量處理）：預設為 `origin=manual` + 🔒 鎖定，模糊或相似都不會被淘汰或被 AI 換掉。"""
    cfg, tr = _resolve_trip(trip)
    sources: list[Path] = []
    if zip_watch:
        typer.echo(f"👀 正在監看下載資料夾（{cfg.watch.downloads_dir}）...")
        sources = wait_for_zips(cfg)
    elif zip_files:
        sources = list(zip_files)
    else:
        sources = [tr.inbox]

    origin = "auto" if auto else "manual"
    typer.echo(f"➕ 正在追加照片至 `{tr.name}`（origin={origin}，{'交由程式篩選' if auto else '🔒 自動鎖定保留、不淘汰'}）...")
    batch = import_sources(tr, sources, origin=origin)
    typer.echo(f"   新增 {batch.added} 張｜略過既有重複 {batch.skipped_dup} 張")

    stats = run_filter(tr, cfg)
    if auto:
        auto_select(tr, cfg)
    m = tr.load()
    new_photos = [m.photos[i] for i in batch.files if i in m.photos]
    for p in new_photos:
        w_str = f"（⚠️ 提醒：{', '.join(p.warnings)}）" if p.warnings else ""
        typer.echo(f"   • {p.id} [{p.status}{' 🔒' if p.locked else ''}]{w_str}")
    typer.echo(f"✅ 追加完成！目前選取共 {len(m.selected())} 張（鎖定 {stats['locked']} 張）。")


@app.command("lock")
def cmd_lock(
    photo_ids: Annotated[list[str], typer.Argument(help="要鎖定的照片 ID（可傳入多個或後綴碼）")],
    trip: Annotated[str | None, typer.Option("--trip", "-t", help="旅程名稱")] = None,
) -> None:
    """手動將指定照片設為鎖定必選（🔒），救回已淘汰照片並排入選取清單。"""
    cfg, tr = _resolve_trip(trip)
    m = tr.load()
    matched = 0
    for q in photo_ids:
        for pid, p in m.photos.items():
            if pid == q or pid.endswith(q) or p.source_file == q:
                p.locked = True
                p.origin = "manual"
                p.status = "selected"
                p.dismissed = False
                p.reject_reason = None
                matched += 1
    m.sync_orders()
    tr.save(m)
    run_filter(tr, cfg)
    typer.echo(f"🔒 已鎖定 {matched} 張照片並加入選取清單。")


@app.command("unlock")
def cmd_unlock(
    photo_ids: Annotated[list[str], typer.Argument(help="要解除鎖定的照片 ID")],
    trip: Annotated[str | None, typer.Option("--trip", "-t", help="旅程名稱")] = None,
) -> None:
    """解除照片鎖定並取消選取。"""
    _, tr = _resolve_trip(trip)
    m = tr.load()
    matched = 0
    for q in photo_ids:
        for pid, p in m.photos.items():
            if pid == q or pid.endswith(q) or p.source_file == q:
                p.locked = False
                p.dismissed = True
                p.status = "candidate"
                matched += 1
    m.sync_orders()
    tr.save(m)
    typer.echo(f"🔓 已解除鎖定 {matched} 張照片。")


@app.command("select")
def cmd_select(
    trip: Annotated[str | None, typer.Option("--trip", "-t", help="旅程名稱")] = None,
    count: Annotated[int | None, typer.Option("--count", "-c", help="目標挑選張數")] = None,
    reselect: Annotated[bool, typer.Option("--reselect", "-r", help="換一批：排除目前未鎖定的照片，挑下一批高分候選照")] = False,
    reset: Annotated[bool, typer.Option("--reset", help="清除排除紀錄，從頭挑選")] = False,
    apply_json: Annotated[Path | None, typer.Option("--apply-json", help="套用 Agent 輸出的挑選 JSON 檔")] = None,
) -> None:
    """執行或重新挑選照片（所有 `locked=true` 的照片固定保留、不會被換掉）。"""
    cfg, tr = _resolve_trip(trip)
    if apply_json:
        picks = json.loads(apply_json.read_text(encoding="utf-8"))
        final = apply_agent_selection(tr, picks)
        typer.echo(f"✅ 已套用 Agent 挑選結果，共 {len(final)} 張。")
    else:
        final = auto_select(
            tr,
            cfg,
            count=count,
            dismiss_current_unlocked=reselect,
            reset_dismissed=reset,
        )
        typer.echo(f"✨ 已挑選 {len(final)} 張代表照片（鎖定照片已全數保留）：")
    for idx, p in enumerate(final, start=1):
        lock_mark = "🔒" if p.locked else "  "
        typer.echo(f"  {idx:02d}. {lock_mark} {p.id} ({p.cluster}) - {p.ai_reason or '手動鎖定'}")


@app.command("write")
def cmd_write(
    trip: Annotated[str | None, typer.Option("--trip", "-t", help="旅程名稱")] = None,
    mode: Annotated[str, typer.Option("--mode", "-m", help="keep（保留）／patch（局部補寫）／rewrite（重寫）")] = "rewrite",
) -> None:
    """依 `style_profile.yaml` 為 FB／IG／Threads 生成或更新多版本貼文文案。"""
    cfg, tr = _resolve_trip(trip)
    if mode not in ("keep", "patch", "rewrite"):
        raise typer.BadParameter("--mode 必須是 keep、patch 或 rewrite")
    caps = generate_draft_captions(tr, cfg, mode=mode)  # type: ignore[arg-type]
    typer.echo(f"✍️ 已產出文案 v{caps['version']}（模式：{mode}），檔案位於：{tr.captions / f'v{caps['version']}.md'}")


@app.command("style-learn")
def cmd_style_learn(
    blog: Annotated[str | None, typer.Option("--blog", "-b", help="Google Blogger 網址（自動抓取最新 6 篇文章分析）")] = None,
    file: Annotated[Path | None, typer.Option("--file", "-f", help="包含歷史貼文內容的文字檔路徑（多篇可用空行或 --- 分隔）")] = None,
    max_posts: Annotated[int, typer.Option("--max-posts", help="從 Blogger 抓取的最大文章數")] = 6,
) -> None:
    """從歷史貼文或 Google Blogger 逆向分析你的寫作風格，自動產生 `style_profile.yaml`。"""
    cfg = load_config()
    samples: list[str] = []
    if blog:
        typer.echo(f"🌐 正在從 Blogger 抓取最新 {max_posts} 篇文章：{blog} ...")
        posts = fetch_blogger_posts(blog, max_results=max_posts)
        for p in posts:
            samples.append(f"標題：{p['title']}\n{p['text']}")
        typer.echo(f"   成功抓取 {len(posts)} 篇文章。")
    if file:
        if not file.exists():
            raise typer.BadParameter(f"找不到檔案：{file}")
        raw = file.read_text(encoding="utf-8")
        parts = [s.strip() for s in raw.split("---") if s.strip()] or [raw.strip()]
        samples.extend(parts)

    if not samples:
        raise typer.BadParameter("請至少提供 `--blog <Blogger網址>` 或 `--file <歷史貼文檔案>` 其中之一。")

    typer.echo("🧬 正在請 Gemini 進行五大維度文風逆向工程...")
    profile = learn_style_profile(cfg, samples)
    typer.echo(f"✅ 已成功產生並更新 `{cfg.style_profile}`！")
    typer.echo(f"   • 人設語調：{profile.get('persona', {}).get('tone', '')}")
    typer.echo(f"   • 偏好用詞：{', '.join(profile.get('vocabulary_guidelines', {}).get('preferred_terms', []))}")


@app.command("export")
def cmd_export(
    trip: Annotated[str | None, typer.Option("--trip", "-t", help="旅程名稱")] = None,
) -> None:
    """移除 GPS 與機密 EXIF、依平台比例裁切，並依輪播順序輸出 `01_...jpg` 至 `ready_to_post/`。"""
    cfg, tr = _resolve_trip(trip)
    outputs = export_trip(tr, cfg)
    m = tr.load()
    typer.echo(f"📦 已匯出 v{m.export_version} 至 `{tr.ready}`：")
    for plat, files in outputs.items():
        typer.echo(f"   • {plat.upper()}: {len(files)} 張（{files[0].name if files else '無'} ...）")


def _serve(trip_opt: str | None, start_page: str, port: int, open_browser: bool) -> None:
    import socket

    cfg, tr = _resolve_trip(trip_opt)
    url = f"http://127.0.0.1:{port}/{start_page}"
    lan_ip = "127.0.0.1"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
    except Exception:
        pass
    typer.echo(f"🌐 電腦版工作台：{url}")
    typer.echo(f"📱 手機隨身版 App（同 Wi-Fi 手機瀏覽器輸入）：http://{lan_ip}:{port}/app/")
    if open_browser:
        webbrowser.open(url)
    web_app = create_app(tr.name, cfg)
    uvicorn.run(web_app, host="0.0.0.0", port=port, log_level="warning")


@app.command("review")
def cmd_review(
    trip: Annotated[str | None, typer.Option("--trip", "-t", help="旅程名稱")] = None,
    port: Annotated[int, typer.Option("--port", "-p", help="本機連接埠")] = 8765,
    no_open: Annotated[bool, typer.Option("--no-open", help="不自動開啟瀏覽器")] = False,
) -> None:
    """開啟「人工確認選圖頁」本機網頁：可換圖、救回淘汰照片、鎖定必選、拖曳順序。"""
    _serve(trip, "review", port, not no_open)


@app.command("publish")
def cmd_publish(
    trip: Annotated[str | None, typer.Option("--trip", "-t", help="旅程名稱")] = None,
    port: Annotated[int, typer.Option("--port", "-p", help="本機連接埠")] = 8765,
    no_open: Annotated[bool, typer.Option("--no-open", help="不自動開啟瀏覽器")] = False,
) -> None:
    """開啟「A+ 本機發布預覽頁」：支援 SortableJS 拖曳排序（即時改檔名）、一鍵複製文案、開啟資料夾與傳送 Telegram。"""
    _serve(trip, "publish", port, not no_open)


@app.command("telegram-setup")
def cmd_telegram_setup(
    token: Annotated[str | None, typer.Option("--token", help="從 @BotFather 取得的 Bot Token")] = None,
) -> None:
    """自動向 Telegram Bot API 查詢你的 `chat_id` 並寫入 `.env`。"""
    _, chat_id = setup_telegram(token=token)
    typer.echo(f"✅ Telegram 設定完成！已將 TELEGRAM_BOT_TOKEN 與 TELEGRAM_CHAT_ID={chat_id} 寫入 `.env`。")


@app.command("send")
def cmd_send(
    trip: Annotated[str | None, typer.Option("--trip", "-t", help="旅程名稱")] = None,
    platform: Annotated[str, typer.Option("--platform", "-p", help="ig / fb / threads")] = "ig",
    variant: Annotated[str | None, typer.Option("--variant", "-v", help="文案版本（narrative / short / humor）")] = None,
    mode: Annotated[str | None, typer.Option("--mode", help="document（原畫質檔案）或 photo（一般壓縮照片）")] = None,
) -> None:
    """將排好順序的照片與文案傳送到你的 Telegram。"""
    cfg, tr = _resolve_trip(trip)
    res = send_to_telegram(tr, cfg, platform=platform, variant=variant, mode=mode)
    typer.echo(
        f"📤 已傳送 {res['platform'].upper()}（v{res['version']}）共 {res['photos']} 張照片至 Telegram！"
    )
