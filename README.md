# picOrganize：旅行照片智慧挑選與個人風格社群發文系統

以 `uv` 與 `.venv` 建構的本機照片挑選、個人文風貼文生成與無 API 風險發布工具。完整規格請見 [SPEC.md](file:///c:/AI/picOrganize/SPEC.md)。

## 核心特色

1. **手機建 Google 相簿 → 電腦下載 ZIP 直接匯入**（保留原始畫質與 GPS，免設定 Google Cloud OAuth）。
2. **本機高速快篩（零外部 API 費用）**：支援 HEIC、SHA-256 完全去重、pHash 連拍分組、時間景點分群、離線 GPS 地名反查、群組內相對模糊度排名與過曝／過暗過濾。
3. **手動加入＝自動鎖定（🔒 絕不淘汰）**：
   - 使用 `pic add` 追加照片，或在確認頁手動勾選／救回淘汰照片時，一律標記為 `origin: manual` + `locked: true`。
   - 鎖定照片即使略模糊或與既有照片相似，也**只顯示 `⚠️` 提醒、絕不淘汰，AI 也不會將它換掉**。
4. **A+ 本機發布預覽頁（支援滑鼠／觸控拖曳排序）**：
   - 以 `SortableJS` 直接拖曳調整貼文照片順序，放開後**即時重新編號 `ready_to_post/` 內的檔案（`01_...jpg`、`02_...jpg`）**，不必重新轉檔。
   - 支援各平台共用順序，或勾選「此平台獨立排序」（例如 IG 單獨換封面）。
   - 匯出時自動清除所有 GPS 與裝置序號 EXIF，保護個人隱私。
5. **Telegram Bot 傳送到手機**：文案獨立一則訊息（方便長按複製），照片依你排好的順序以原畫質檔案（`document`）分批傳送。

## 常用指令（一律透過 `uv run` 執行）

```powershell
# 1. 第一次匯入相簿 ZIP 並執行本機快篩與初選
uv run pic import --trip 115-11-06_爬爬硬漢嶺 --zip 115-11-06_爬爬硬漢嶺-1-001.zip
# 或是監看「下載」資料夾，在 Google 相簿網頁按 Shift+D 下載整本相簿：
uv run pic import --trip 115-11-06_爬爬硬漢嶺 --zip-watch

# 2. 之後想追加照片（預設自動鎖定 🔒，模糊或相似都不會被刷掉）
uv run pic add --trip 115-11-06_爬爬硬漢嶺 --zip 新下載的相簿.zip

# 3. 開啟本機「人工確認選圖頁」與「A+ 發布預覽＆拖曳排序頁」
uv run pic review --trip 115-11-06_爬爬硬漢嶺
uv run pic publish --trip 115-11-06_爬爬硬漢嶺

# 4. Telegram Bot 設定與傳送到手機
uv run pic telegram-setup --token <你的_BOT_TOKEN>
uv run pic send --trip 115-11-06_爬爬硬漢嶺 --platform ig
```
