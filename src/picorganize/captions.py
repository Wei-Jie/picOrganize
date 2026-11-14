"""個人文風管理與多版本、分平台文案存取（支援保留／局部補寫／重寫與風格回饋迴圈）。"""

from __future__ import annotations

import base64
import io
import json
import re
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Literal

import requests
import yaml
from PIL import Image

from .config import AppConfig, load_secrets, project_root
from .manifest import PLATFORMS, Manifest, Trip

DEFAULT_STYLE_PROFILE = {
    "persona": {
        "role": "平實生活感創作者 / 美食與旅行紀錄者",
        "tone": "親切接地氣、帶微幽默與自嘲、重生活感與真實細節",
        "audience": "FB/IG 朋友與日常追蹤者",
    },
    "formatting_rules": {
        "paragraph_length": "每段至多 2 句話，維持充足留白",
        "emoji_usage": "每篇總共至多 2~3 個，點綴句首，不濫用",
        "hashtag_rules": "置於文末，4~6 個貼合情境標籤，含 1 個自嘲幽默標籤",
    },
    "vocabulary_guidelines": {
        "preferred_terms": ["老實說", "結果默默...", "直接斷電", "值得了"],
        "forbidden_phrases": [
            "靈魂的洗禮",
            "歲月靜好",
            "踏上一場未知的冒險",
            "每一刻都值得被珍惜",
            "讓我們一起...",
        ],
    },
    "few_shot_examples": [
        {
            "context": "散步與聚餐",
            "text": (
                "原本只是出門散步半小時，結果默默走進熱炒店點了一整桌。\n"
                "這家店招牌都褪色了，但牛肉的鍋氣完全騙不了人，白飯直接嗑了兩碗。\n"
                "走回家的路上晚風很涼，週末這樣過就夠充實了。\n\n"
                "#週末日常 #地方小店 #碳水炸彈 #體重計先收起來"
            ),
        }
    ],
}


def ensure_style_profile(cfg: AppConfig, root: Path | None = None) -> Path:
    base = root or project_root()
    path = base / cfg.style_profile
    if not path.exists():
        path.write_text(
            yaml.safe_dump(DEFAULT_STYLE_PROFILE, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    return path


def load_style_profile(cfg: AppConfig, root: Path | None = None) -> dict:
    path = ensure_style_profile(cfg, root)
    return yaml.safe_load(path.read_text(encoding="utf-8")) or DEFAULT_STYLE_PROFILE


def save_style_example(cfg: AppConfig, context: str, text: str, root: Path | None = None) -> int:
    """將使用者修改後的最終版貼文存回 `style_profile.yaml` 的 `few_shot_examples`。"""
    path = ensure_style_profile(cfg, root)
    data = load_style_profile(cfg, root)
    examples: list[dict] = list(data.get("few_shot_examples") or [])
    examples.append({"context": context, "text": text.strip(), "saved_at": datetime.now().strftime("%Y-%m-%d")})
    if len(examples) > cfg.style_max_examples:
        examples = examples[-cfg.style_max_examples :]
    data["few_shot_examples"] = examples
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return len(examples)


def fetch_blogger_posts(blog_url: str, max_results: int = 6) -> list[dict[str, str]]:
    """從 Google Blogger 官方 JSON Feed 抓取歷史文章（免 API Key）。"""
    base = blog_url.strip().rstrip("/")
    if not base.startswith("http"):
        base = f"https://{base}"
    feed_url = f"{base}/feeds/posts/default?alt=json&max-results={max_results}"
    r = requests.get(feed_url, timeout=15)
    r.raise_for_status()
    entries = (r.json().get("feed") or {}).get("entry") or []
    posts: list[dict[str, str]] = []
    for e in entries:
        title = (e.get("title") or {}).get("$t") or ""
        raw_html = ((e.get("content") or e.get("summary") or {}).get("$t")) or ""
        # 將 <br> 與 </p> 轉為換行後去除 HTML 標籤
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", raw_html)
        text = re.sub(r"<[^>]+>", "", text)
        text = unescape(text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text:
            posts.append({"title": title.strip(), "text": text[:1500]})
    return posts


def learn_style_profile(
    cfg: AppConfig,
    sample_texts: list[str],
    root: Path | None = None,
) -> dict:
    """透過 Gemini 對歷史貼文進行「五大維度逆向工程」，自動產生並儲存 `style_profile.yaml`。"""
    base = root or project_root()
    secrets = load_secrets(base)
    if not secrets.gemini_api_key:
        raise ValueError("尚未在 .env 設定 GEMINI_API_KEY。")

    joined = "\n\n=== 歷史文章分隔線 ===\n\n".join(t.strip() for t in sample_texts if t.strip())
    prompt = f"""你是專業的個人文風逆向工程分析師。請分析以下作者本人過去撰寫的真實貼文／部落格文章，進行「五大維度逆向工程」，提取出作者專屬的寫作風格參照檔。

【作者歷史文章樣本】：
{joined}

請嚴格只回傳 JSON，格式完全對齊以下結構（全部使用繁體中文台灣用語）：
{{
  "persona": {{
    "role": "作者人設與定位（例如：平實生活感創作者 / 美食與旅行紀錄者）",
    "tone": "語調特色（例如：親切接地氣、帶微幽默與自嘲、重生活細節）",
    "audience": "目標受眾（例如：FB/IG 朋友與日常追蹤者）"
  }},
  "formatting_rules": {{
    "paragraph_length": "段落長度與留白習慣（例如：每段 1~2 句話，維持充足留白）",
    "emoji_usage": "Emoji 使用頻率與位置習慣",
    "hashtag_rules": "Hashtag 數量、位置與風格習慣"
  }},
  "vocabulary_guidelines": {{
    "preferred_terms": ["從樣本中歸納出的作者高頻口頭禪或連接詞 4~8 個"],
    "forbidden_phrases": ["靈魂的洗禮", "歲月靜好", "踏上一場未知的冒險", "每一刻都值得被珍惜", "讓我們一起...", "以及與作者平實風格衝突的矯情 AI 詞彙"]
  }},
  "few_shot_examples": [
    {{
      "context": "從樣本中挑選最具代表性的段落主題 1",
      "text": "從樣本中擷取或濃縮成社群貼文長度的作者真實原文範例 1"
    }},
    {{
      "context": "從樣本中挑選最具代表性的段落主題 2",
      "text": "從樣本中擷取或濃縮成社群貼文長度的作者真實原文範例 2"
    }}
  ]
}}"""

    profile_data = None
    last_err = ""
    for model_name in ("gemini-flash-lite-latest", "gemini-3-flash-preview", "gemini-3.5-flash"):
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={secrets.gemini_api_key}",
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "responseMimeType": "application/json",
                        "thinkingConfig": {"thinkingLevel": "LOW"},
                    },
                },
                timeout=30,
            )
            if r.ok:
                raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                profile_data = json.loads(raw)
                break
            last_err = f"{model_name}: HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)

    if not profile_data:
        raise RuntimeError(f"Gemini 風格分析失敗：{last_err}")

    path = base / cfg.style_profile
    path.write_text(
        yaml.safe_dump(profile_data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return profile_data


def captions_file(trip: Trip, version: int | None = None) -> Path:
    m = trip.load()
    v = version or max(1, m.caption_version)
    return trip.captions / f"v{v}.json"


def load_captions(trip: Trip, version: int | None = None) -> dict:
    """讀取指定版本的文案結構：`{"version": 1, "active_variant": "v1", "variants": {"v1": {"label": "...", "ig": "...", "fb": "...", "threads": "..."}}}`。"""
    p = captions_file(trip, version)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"version": 0, "active_variant": "narrative", "variants": {}}


def save_captions(trip: Trip, variants: dict[str, dict[str, str]], active_variant: str = "narrative", bump: bool = True) -> dict:
    """儲存多版本、分平台文案，同時輸出方便閱讀的 `.md` 檔。"""
    trip.ensure()
    m: Manifest = trip.load()
    if bump or m.caption_version == 0:
        m.caption_version += 1
    ver = m.caption_version
    payload = {
        "version": ver,
        "updated_at": datetime.now().isoformat(),
        "active_variant": active_variant if active_variant in variants else next(iter(variants), "narrative"),
        "variants": variants,
    }
    (trip.captions / f"v{ver}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # 同步輸出 Markdown 方便檢視
    md_lines = [f"# {m.trip} 文案（v{ver}）\n"]
    for key, item in variants.items():
        md_lines.append(f"## 版本：{item.get('label', key)} (`{key}`)\n")
        for plat in PLATFORMS:
            if item.get(plat):
                md_lines.append(f"### {plat.upper()}\n\n{item[plat]}\n")
    (trip.captions / f"v{ver}.md").write_text("\n".join(md_lines), encoding="utf-8")
    trip.save(m)
    return payload


def generate_draft_captions(
    trip: Trip,
    cfg: AppConfig,
    mode: Literal["keep", "patch", "rewrite"] = "rewrite",
) -> dict:
    """根據行程與景點資訊產出符合 `style_profile.yaml` 結構的預設草稿（Agent 可再覆寫為更細膩的看圖文案）。"""
    m: Manifest = trip.load()
    m.sync_orders()
    prev = load_captions(trip) if m.caption_version > 0 else None

    if mode == "keep" and prev and prev.get("variants"):
        return prev

    sel_ids = m.orders.shared
    sel_photos = [m.photos[i] for i in sel_ids if i in m.photos]
    places: list[str] = []
    notes: list[str] = []
    for p in sel_photos:
        c = m.clusters.get(p.cluster or "")
        if c:
            if c.note and c.note not in notes:
                notes.append(c.note)
            elif c.place and c.place not in places:
                places.append(c.place)

    date_str = sel_photos[0].taken_at.strftime("%m/%d") if sel_photos else ""
    span_hours = (
        round((sel_photos[-1].taken_at - sel_photos[0].taken_at).total_seconds() / 3600, 1)
        if len(sel_photos) >= 2
        else 0
    )
    spot_text = "、".join(notes or places) or m.trip
    tag_clean = m.trip.replace("-", "").replace("_", "").replace(" ", "")

    if mode == "patch" and prev and prev.get("variants"):
        # 局部補寫：保留使用者已改過的文字，只在段落末尾補上新加入照片的描述
        new_locked = [p for p in sel_photos if p.origin == "manual"]
        patch_line = (
            f"\n補記：後來又翻出這幾張（{', '.join(p.taken_at.strftime('%H:%M') for p in new_locked[:3])}），還是覺得一定要放進來。"
            if new_locked
            else "\n（已依最新挑選的照片微調順序）"
        )
        patched: dict[str, dict[str, str]] = {}
        for k, v in prev["variants"].items():
            patched[k] = dict(v)
            for plat in PLATFORMS:
                body = v.get(plat, "")
                if "\n#" in body:
                    head, tags = body.rsplit("\n#", 1)
                    patched[k][plat] = f"{head.rstrip()}{patch_line}\n\n#{tags}"
                else:
                    patched[k][plat] = f"{body.rstrip()}{patch_line}"
        return save_captions(trip, patched, active_variant=prev.get("active_variant", "narrative"), bump=True)

    # 若有設定 GEMINI_API_KEY，直接將選取的照片縮圖送給 Gemini 進行快速看圖寫作
    secrets = load_secrets()
    if secrets.gemini_api_key and sel_photos:
        style = load_style_profile(cfg)
        parts: list[dict] = [
            {
                "text": (
                    f"你是個人風格社群寫手。\n"
                    f"旅程／主題名稱：{m.trip}\n"
                    f"景點與備註：{spot_text}\n"
                    f"個人文風設定：{json.dumps(style, ensure_ascii=False)}\n\n"
                    f"【重要規則】：\n"
                    f"1. 請仔細觀察以下 {len(sel_photos)} 張已選照片的真實畫面細節（照片拍什麼就寫什麼，切勿套用與畫面無關的詞彙）。\n"
                    f"2. 每篇貼文（fb、ig、threads）都【必須自然穿插 3～5 個 Emoji 表情符號】（如 😛、😂、✨、☀️、🙌、🥾、🍽️、🤣），絕不可完全沒有 Emoji！\n"
                    f"3. 請勿在貼文中捏造 https://example.com 等假網址。\n"
                    f"請撰寫 3 個版本（narrative 敘事生活版 / short 精簡短版 / humor 幽默自嘲版）× 3 個平台（fb / ig / threads）的繁體中文台灣用語貼文。\n"
                    f"請嚴格只回傳 JSON：\n"
                    f'{{"narrative":{{"fb":"...","ig":"...","threads":"..."}},'
                    f'"short":{{"fb":"...","ig":"...","threads":"..."}},'
                    f'"humor":{{"fb":"...","ig":"...","threads":"..."}}}}'
                )
            }
        ]
        for idx, p in enumerate(sel_photos[:8], start=1):
            tpath = trip.thumbs / f"{p.id}.jpg"
            if tpath.exists():
                im = Image.open(tpath).convert("RGB")
                im.thumbnail((320, 320))
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=72)
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                parts.append({"text": f"第 {idx} 張照片 ({p.taken_at:%H:%M})："})
                parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
        for model_name in ("gemini-flash-lite-latest", "gemini-3-flash-preview"):
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={secrets.gemini_api_key}",
                    json={
                        "contents": [{"parts": parts}],
                        "generationConfig": {
                            "responseMimeType": "application/json",
                            "thinkingConfig": {"thinkingLevel": "LOW"},
                        },
                    },
                    timeout=45,
                )
                if r.ok:
                    cands_list = r.json().get("candidates") or []
                    part_list = (cands_list[0].get("content") or {}).get("parts") or []
                    raw = next((pt["text"] for pt in part_list if "text" in pt), "{}")
                    ai_caps = json.loads(raw)
                    for k, lbl in (("narrative", "敘事生活版"), ("short", "精簡短版"), ("humor", "幽默自嘲版")):
                        if isinstance(ai_caps.get(k), dict):
                            ai_caps[k]["label"] = lbl
                    return save_captions(trip, ai_caps, active_variant="narrative", bump=True)
            except Exception:
                continue

    # 離線備援模板（不含任何爬山專屬字眼）
    variants = {
        "narrative": {
            "label": "敘事生活版",
            "fb": (
                f"📍 {m.trip}（{date_str}）\n\n"
                f"原本只是想說出門晃晃，結果默默在 {spot_text} 待了 {span_hours} 小時。\n"
                f"挑了 {len(sel_photos)} 張最喜歡的畫面留個紀錄，老實說整理照片的時候覺得這趟真的很值得。\n\n"
                f"#{tag_clean} #週末日常 #生活紀錄"
            ),
            "ig": (
                f"{m.trip} 的 {len(sel_photos)} 個喜歡瞬間 ✨\n\n"
                f"一路邊走邊拍，把當下的光線跟氣氛都收進相簿裡了，往左滑看更多畫面 👉\n\n"
                f"#{tag_clean} #週末去哪裡 #日常碎片 #隨手拍 #生活感"
            ),
            "threads": (
                f"整理完 {m.trip} 的照片，精選 {len(sel_photos)} 張最喜歡的瞬間，老實說這趟真的很值得 ✨"
            ),
        },
        "short": {
            "label": "精簡短版",
            "fb": (
                f"{date_str}｜{m.trip}\n"
                f"把天氣、風景跟好心情都收進這 {len(sel_photos)} 張照片裡了。\n\n"
                f"#{tag_clean} #日常紀錄"
            ),
            "ig": (
                f"把喜歡的光線跟畫面都裝進相簿裡 ✨\n"
                f"（{m.trip}，{len(sel_photos)} pics）\n\n"
                f"#{tag_clean} #生活碎片 #隨手拍 #週末充電"
            ),
            "threads": f"{m.trip} 紀錄 ✔️（{len(sel_photos)} pics）",
        },
        "humor": {
            "label": "幽默自嘲版",
            "fb": (
                f"每次出門前都說「今天隨便逛逛不特別拍照」，結果回家一看相簿又是一整排。\n\n"
                f"在 {spot_text} 晃了一圈，默默還是按了一堆快門，挑出這 {len(sel_photos)} 張精華跟大家分享！\n\n"
                f"#{tag_clean} #週末日常 #手機記憶體殺手"
            ),
            "ig": (
                f"說好的隨便拍拍，結果手機相簿又差點爆掉 😂\n\n"
                f"從幾百張裡面好不容易挑出 {len(sel_photos)} 張 {m.trip} 現場實況 👉\n\n"
                f"#{tag_clean} #選擇障礙發作 #日常碎念 #週末日常"
            ),
            "threads": f"有人跟我一樣每次出門都說不特別拍照，結果回家相簿直接多兩百張嗎 😂",
        },
    }
    return save_captions(trip, variants, active_variant="narrative", bump=True)
