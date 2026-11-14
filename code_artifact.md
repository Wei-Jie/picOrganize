# 旅行照片智慧挑選與個人風格社群發文系統規格指南

本文件旨在提供給 **Google Antigravity** 作為核心專案 Prompt 與執行藍圖。系統核心目標為：**以極低/零成本將大量出遊照片（300~500 張）自動化篩選、對齊創作者個人專屬文風撰寫社群貼文，並提供平滑的發布流程（支援一鍵發布或半自動複製）。**

---

## 系統整體架構圖

```
[出遊照片來源] (手機相簿 / Google 相簿 / 本機資料夾)
       │
       ▼
【階段 1：本機高效預處理與快速快篩】(Python + OpenCV / EXIF)
   ├── EXIF 時空分群 (依時間與 GPS 將照片分至各景點)
   ├── 模糊度檢測 (Laplacian Variance 剔除手震、失焦廢片)
   └── 本機生成輕量縮圖 (512px WebP/JPG，大幅節省頻寬與 Token)
       │ (500 張 ──► 精簡至 30~50 張候選照)
       ▼
【階段 2：Antigravity Agent / 多模態 AI 美學評選】
   ├── 多樣性構圖挑選 (全景、特寫、美食、合照平衡)
   └── 依故事動線挑出最終 6~9 張精華照片，複製至 `ready_to_post/`
       │
       ▼
【階段 3：個人專屬風格文案生成 (Style Guide 驅動)】
   ├── 讀取 `style_profile.yaml` (來自部落格/歷史發文的風格逆向工程)
   └── 提取照片具體細節 (食物、人物表情、天氣)，產出無 AI 機器味的專屬貼文
       │
       ▼
【階段 4：貼文發布與確認 (兩種落地模式)】
   ├── 模式 A (輕量靈活)：照片彈出資料夾 + 文案自動拷貝剪貼簿 ──► 手動拖曳發布
   └── 模式 B (全自動 API)：本機暫存圖床 ──► Meta Graph API 一鍵發布 FB/IG
```

---

## 第一階段：照片來源與本機預處理管線

### 1. 照片取得情境
* **Google 相簿情境**：手機出遊期間建立相簿，電腦端透過網頁版「全部下載（Shift + D）」解壓縮至 `./raw_photos`；或透過 Google Photos Picker API / 共享連結批次拉取。
* **本地相簿情境**：手機透過傳輸線或 AirDrop 直接將出遊照片放至專案 `./raw_photos` 目錄。

### 2. 本機快篩演算法 (完全不耗費外部 API 額度)
* **時間與空間聚類 (Clustering)**：利用 `exifread` 或 `Pillow` 讀取 `DateTimeOriginal`，相鄰時間若超過 30~45 分鐘則切分為下一個「景點區塊」。
* **模糊過濾 (Blur Detection)**：
  $$\text{Blur Score} = \operatorname{Var}(\nabla^2 I)$$
  使用 OpenCV 的拉普拉斯運算子計算方差，低於門檻者（通常小於 100）標記為模糊淘汰。
* **縮圖產生**：將通過初篩的候選照片縮放為長邊 512px，輸出至 `./temp_candidates` 供多模態模型判讀。

---

## 第二階段：風格逆向工程與文風模板設計

為了徹底洗除 AI 的機器味與假大空詞彙，系統使用外置的 `style_profile.yaml`。

### 1. 風格來源採集 (以 Google Blogger 為例)
可透過 Blogger 官方 JSON Feed 或一般網頁文章抓取工具（`trafilatura`）取得 3~6 篇歷史代表作：
* Blogger 官方端點：`{BLOG_URL}/feeds/posts/default?alt=json&max-results=6`
* 透過 LLM 對文章進行**「五大維度逆向工程」**：
  1. **語調與人設 (Tone & Persona)**：接地氣、平實、生活碎念感或幽默自嘲。
  2. **排版節奏 (Rhythm)**：每段 1~2 句、大量留白。
  3. **用詞偏好 (Preferred Terms)**：如「老實說」、「結果默默...」、「白飯直接嗑兩碗」。
  4. **嚴格禁用詞 (Forbidden Phrases)**：嚴禁「靈魂的洗禮」、「歲月靜好」、「踏上一場未知的冒險」等陳腔濫調。
  5. **Few-Shot 真實範本**：內嵌 2 篇作者本人的歷史貼文。

### 2. `style_profile.yaml` 範本規格
```yaml
persona:
  role: "平實生活感創作者 / 美食與旅行紀錄者"
  tone: "親切接地氣、帶微幽默與自嘲、重生活感與真實細節"
  audience: "FB/IG 朋友與日常追蹤者"

formatting_rules:
  paragraph_length: "每段至多 2 句話，維持充足留白"
  emoji_usage: "每篇總共至多 2~3 個，點綴句首，不濫用"
  hashtag_rules: "置於文末，4~6 個貼合情境標籤，含 1 個自嘲幽默標籤"

vocabulary_guidelines:
  preferred_terms:
    - "老實說"
    - "結果默默..."
    - "直接斷電"
    - "值得了"
  forbidden_phrases:
    - "靈魂的洗禮"
    - "歲月靜好"
    - "踏上一場未知的冒險"
    - "每一刻都值得被珍惜"
    - "讓我們一起..."

few_shot_examples:
  - context: "散步與聚餐"
    text: |
      原本只是出門散步半小時，結果默默走進熱炒店點了一整桌。
      這家店招牌都褪色了，但牛肉的鍋氣完全騙不了人，白飯直接嗑了兩碗。
      走回家的路上晚風很涼，週末這樣過就夠充實了。
      
      #週末日常 #地方小店 #碳水炸彈 #體重計先收起來
```

---

## 第三階段：發布模式規劃

### 模式 A：半自動輔助（零門檻、零 API 審查、推薦自用）
1. 腳本完成照片挑選後，自動建立 `ready_to_post/` 資料夾，將選中的 6~9 張原圖複製過去。
2. 整合 `pyperclip`，腳本完成時自動將產出的文案**直接複製進系統剪貼簿**。
3. 自動彈出本機檔案總管視窗（Windows Explorer / Mac Finder）。
4. **人工發文動作**：打開網頁版 FB/IG ──► 將資料夾照片全選拖入發文框 ──► `Ctrl+V` 貼上文字 ──► 點擊發布（耗時約 10 秒）。

---

### 模式 B：全自動 API 發布（Meta Graph API 閉環）

若需要 Antigravity 內建 Web 預覽並一鍵同時發送到 Facebook 與 Instagram，需滿足以下技術要素：

#### 1. 前置權限與帳號綁定
* **Instagram**：必須切換為「商業帳號（Business）」或「創作者帳號（Creator）」。
* **Facebook 粉專**：建立粉專並於後台「已連結帳號」中綁定該 IG 帳號。
* **Meta for Developers App**：建立 Business 類型應用程式，取得 `App ID` 與 `App Secret`。

#### 2. 獲取永久有效 Page Access Token
1. 在 Graph API Explorer 取得帶有權限的短效 User Token：
   * 權限：`pages_manage_posts`, `pages_read_engagement`, `instagram_basic`, `instagram_content_publish`
2. 交換 60 天長效 User Token：
   ```bash
   GET https://graph.facebook.com/v26.0/oauth/access_token?grant_type=fb_exchange_token&client_id={APP_ID}&client_secret={APP_SECRET}&fb_exchange_token={SHORT_LIVED_TOKEN}
   ```
3. 取得永久 Page Token：
   ```bash
   GET https://graph.facebook.com/v26.0/me/accounts?access_token={LONG_LIVED_USER_TOKEN}
   ```
   *回傳之 `data[0].access_token` 即為永不到期之 Page Token。*
4. 查詢 IG 商業帳號 ID：
   ```bash
   GET https://graph.facebook.com/v26.0/{PAGE_ID}?fields=instagram_business_account&access_token={PAGE_TOKEN}
   ```

#### 3. 圖片公開網址中繼 (Image Hosting)
Meta API 要求圖片必須為公開 HTTPS URL，無法直接上傳本機實體檔案。
* 做法：發文前先透過腳本將 `ready_to_post/` 的圖片上傳至臨時雲端儲存空間（如 Cloudinary、Supabase Storage、AWS S3），取得公開 URL。

#### 4. API 發布呼叫序列
* **Facebook 多圖貼文**：
  1. 對每張圖片呼叫 `POST /{page-id}/photos`，帶入 `url` 與 `published=false`，取得 `media_fbid`。
  2. 呼叫 `POST /{page-id}/feed`，將所有 `attached_media` 與文案 `message` 一次發出。
* **Instagram 輪播貼文 (Carousel)**：
  1. 對每張圖片呼叫 `POST /{ig-user-id}/media`，設定 `is_carousel_item=true`，取得子容器 ID。
  2. 呼叫 `POST /{ig-user-id}/media`，設定 `media_type=CAROUSEL`、`children={IDs}` 與文案 `caption`，取得主容器 ID。
  3. 呼叫 `POST /{ig-user-id}/media_publish`，帶入主容器 ID 正式發布。

---

## 第四階段：給 Antigravity 的標準啟動指令 (Prompt Template)

當照片放入專案目錄後，可直接在 Antigravity 貼上以下指示開始作業：

```markdown
我已經將本次旅遊的 300~500 張照片放置於 `./raw_photos`。
同時專案根目錄中已包含由我歷史文章逆向生成的 `style_profile.yaml`。

請為我執行完整處理流程：
1. 執行本地 Python 腳本：
   - 讀取照片 EXIF 進行時間分群。
   - 計算 Laplacian 方差過濾手震與模糊廢片。
   - 產生 512px 縮圖至 `./temp_candidates`。
2. 扮演社群影像編輯，檢視縮圖：
   - 依據旅程時間軸，挑選出最具故事代表性、構圖多元的 8 張照片。
   - 將這 8 張照片的原圖複製到 `./ready_to_post`。
3. 讀取 `style_profile.yaml`，根據挑選照片的具體細節（食物、表情、光線），撰寫符合我個人語氣的社群貼文。
4. 將生成之文案自動複製至剪貼簿，並開啟 `./ready_to_post` 檔案視窗方便我立即發布。
```