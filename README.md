# 🛡️ fnOS 4-3-2 企業級跨雲端雙巨頭極速加密備份系統規劃書
> **Architecture & Operation Blueprint for Enterprise-Grade 4-3-2 Dual-Cloud Backup Solution**  
> *維護者：sam0324sam ｜ 核心引擎：Docker + Rclone + Python SRE 守衛 ｜ 版本：v4.1 (4-3-2 Dual-Cloud Edition)*

---

## 📑 目錄
1. [系統背景與設計原則](#一-系統背景與設計原則)
2. [系統拓撲與架構設計](#二-系統拓撲與架構設計)
3. [快速上手指南 (Quick Start)](#三-快速上手指南-quick-start)
4. [多階段備份流程與資料流向](#四-多階段備份流程與資料流向)
5. [儲存擴充與負載策略 (方案 A 循序填滿)](#五-儲存擴充與負載策略-方案-a-循序填滿)
6. [Docker 堆疊 GFS 階梯式生命週期規劃](#六-docker-堆疊-gfs-階梯式生命週期規劃)
7. [零信任安全與資安防禦體系](#七-零信任安全與資安防禦體系)
8. [SRE 監控、熔斷與 Telegram 維運戰報](#八-sre-監控熔斷與-telegram-維運戰報)
9. [災難復原手冊 (Disaster Recovery SOP)](#九-災難復原手冊-disaster-recovery-sop)
10. [日常維護與 CLI 指令手冊](#十-日常維護與-cli-指令手冊)

---

## 一、 系統背景與設計原則

本專案旨在為 fnOS NAS 打造一套**高可靠、防勒索病毒、跨租戶容災、跨雲異質、極速扇出、零運維**的 4-3-2 雙雲端自動化備份體系。

### 🌟 核心目標 (4-3-2 跨雲端雙巨頭備份鐵律)
* **4 份資料副本**：本地原始資料 + OD1 雲端主儲存 + OD2 雲端異地鏡像 + GD1 谷歌異雲鏡像。
* **3 種不同介質/體系**：本地實體 NVMe/HDD 陣列 + 微軟 M365 國際公有雲 + 谷歌 Google Cloud 國際公有雲。
* **2 處實體異地/供應商隔離**：
  1. **微軟跨租戶隔離 (Cross-Tenant)**（不同組織租戶隔離：`Tenant_A` ➜ `Tenant_B`）
  2. **谷歌跨雲端隔離 (Cross-Cloud Provider)**（Google One / Google Drive 全球高可用節點），徹底規避單一雲端供應商斷供、帳號風控封鎖風險。

### 💎 設計原則
1. **零信任客戶端加密 (Client-side Zero-Trust)**：檔案離開 NAS 前，於內存完成 XSalsa20 強度加密，雲端僅儲存 `.bin` 密文，微軟與谷歌均無法分析檔案內容，100% 免疫特徵審查與屏蔽。
2. **只增不減 (Append-Only Copy)**：本地誤刪或遭受勒索病毒加密修改時，雲端歷史檔案**永不自動刪除**。
3. **冷熱資料分離 (Storage Tiering)**：
   * 守衛程式與高頻日誌置於高速池（如 NVMe SSD），避免日常巡檢喚醒硬碟。
   * 資料來源讀取大容量機械陣列（如 RAID 10/5），兼顧極速讀寫與延長硬碟壽命。
4. **本地串流加密直灌 (Direct Local Multi-Cloud Streaming)**：
   * 本地讀取 NAS 陣列，經內存管道即時以 XSalsa20 加密後直接上傳各雲端（OD1、OD2、GD1）。
   * 徹底規避雲端中轉下載 API 延遲與限流，家用寬頻上傳跑滿（15 ~ 20+ MB/s），記憶體佔用極低（~60-150MB）。
5. **多雲並行容災矩陣**：
   * 一套金鑰架構，在微軟跨租戶（OD1/OD2）與谷歌（GD1）三方自動同步，杜絕單點故障與供應商綁定。
6. **零寫死自適應 (Zero-Hardcoding)**：自動探索本地 UID 使用者目錄、自動掃描多雲合流池、動態生成行動端最適排版戰報。

---

## 二、 系統拓撲與架構設計

```mermaid
graph TD
    subgraph Local_NAS ["🖥️ 本地 NAS (fnOS / Linux / Synology)"]
        SSD["高速應用池 (NVMe/SSD)<br>• rclone-backup 守衛容器<br>• Docker 配置檔"]
        HDD["主資料儲存池 (RAID 陣列)<br>• 使用者目錄 (1000, 1001...)<br>• 共享目錄 (@team / share)"]
        Guard["rclone-backup-guard<br>(Python 3 智慧守衛進程)"]
        WebGUI["rclone-web-dashboard<br>(Port 5572 Web 儀表板)"]
    end

    subgraph Direct_Engine ["⚡ 本地串流加密分發引擎"]
        Crypt["XSalsa20 內存加密管道 (64KB~16MB Buffer)<br>檔案內容 .bin 密文 ｜ 目錄結構明文/加密"]
    end

    subgraph Dual_Cloud ["☁️ 雙巨頭跨雲異地容災 (OneDrive 5TB x 2 + Google Drive 5TB)"]
        OD1["☁️ OD1 微軟主儲存 (od1_union)<br>租戶 A: 獨立微軟組織<br>(5.0 TB 原生空間，可循序擴充)"]
        OD2["☁️ OD2 微軟鏡像 (od2_union)<br>租戶 B: 獨立微軟組織<br>(5.0 TB 獨立租戶，跨域容災)"]
        GD1["☁️ GD1 谷歌鏡像 (gd1_union)<br>Google Drive / Google One<br>(5.0 TB 獨立跨雲異質鏡像)"]
    end

    HDD -->|唯讀增量掃描| Crypt
    SSD -.->|Docker 快照打包| Crypt
    Crypt ==>|本地直推 (15~20 MB/s)| OD1
    Crypt ==>|本地直推 (15~20 MB/s)| OD2
    Crypt ==>|本地直推 (15~20 MB/s)| GD1

    Guard -->|每日 02:00 / 巡檢告警| TG["📱 Telegram 機器人通知"]
```

---

## 三、 快速上手指南 (Quick Start)

只需 3 步驟即可在任何 Linux / NAS 環境（fnOS、群暉 Synology、威聯通 QNAP、TrueNAS、Debian/Ubuntu）完成部署：

### 1. Clone 儲存庫
```bash
git clone https://github.com/sam0324sam/fnos-onedrive-backup.git
cd fnos-onedrive-backup
```

### 2. 初始化設定檔
```bash
# 複製環境變數範本
cp .env.example .env

# 複製 Rclone 配置範本
cp config/rclone.conf.example config/rclone.conf
```
* **編輯 `.env`**：填入您的 `TELEGRAM_BOT_TOKEN` 與 `TELEGRAM_CHAT_ID`，並根據主機路徑調整 `NAS_DATA_DIR`（預設 `/vol1`，群暉可設 `/volume1`）。
* **編輯 `config/rclone.conf`**：填入您的 OneDrive / Google Drive Token 與自訂加密金鑰密碼。

### 3. 一鍵啟動容器
```bash
docker compose up -d
```

### 4. 驗證連線與發送首份戰報
```bash
# 檢查健康狀態與雲端容量
docker compose exec rclone-backup-guard python3 /app/scripts/sync_manager.py --check-only

# 發送測試維運戰報至 Telegram
docker compose exec rclone-backup-guard python3 /app/scripts/sync_manager.py --test-report
```

---

## 四、 多階段備份流程與資料流向

系統於每日凌晨 **02:00** 自動啟動流水線（Pipeline）：

### 1. 階段 0：Docker 容器堆疊快照多雲分發 (GFS Snapshot)
* **來源**：`/docker_src` (唯讀掛載主機 Docker 配置目錄 `${DOCKER_SRC_DIR}`)。
* **處理**：排除日誌、快取與 `.git`，使用 `tar -czf` 完整封裝 Linux 權限、UID/GID 與軟連結。
* **分發**：本地打包後直接推送至 `od1_crypt:docker_snapshots/`、`od2_crypt:docker_snapshots/` 與 `gd1_crypt:docker_snapshots/`。
* **淘汰**：執行 GFS 生命週期演算法，同時修剪三雲端過期快照。

### 2. 階段 1：NAS 主資料直灌三雲端 (Direct Incremental Multi-Cloud)
* **來源**：`/data` (唯讀掛載主機儲存池 `${NAS_DATA_DIR}`)。
* **探索**：自動掃描所有使用者目錄（支援環境變數 `BACKUP_FOLDERS` 自訂），過濾相簿快取與回收站。
* **比對與傳輸**：依檔案大小（Size）與修改時間（ModTime）進行高速增量比對，未變更檔案秒跳過。
* **鏈路排程**：
  1. **節點一 (OD1)**：本地 NAS ➜ `od1_crypt:` (微軟 OD1 主本)
  2. **節點二 (OD2)**：本地 NAS ➜ `od2_crypt:` (微軟 OD2 鏡像)
  3. **節點三 (GD1)**：本地 NAS ➜ `gd1_crypt:` (谷歌 GD1 5TB 鏡像)

> [!TIP]
> **本地加密串流優勢**：Go 語言 Rclone 核心以 `io.Reader` 串流管線執行 XSalsa20 加密，僅在內存中維持數十 MB 緩衝區，不耗硬碟且加密速度高達 1.5+ GB/s。直接上傳省去微軟下載 API 延遲，速度可達寬頻極限！

---

## 五、 儲存擴充與負載策略 (方案 A 循序填滿)

當單一 5TB 帳號即將寫滿時，本系統支援**無縫橫向擴充（Scale-Out）**，上層加密與掛載完全無需變動。

### 寫入策略：方案 A（循序填滿 / 溢流模式，首選推薦）
* **核心配置**：
  * `create_policy = ff` (First Found 依序填滿)
  * `action_policy = epff` (優先於既有路徑帳號操作)
  * `min_free_space = 50G` (可用空間低於 50GB 時自動溢流至下一帳號)
* **優勢**：
  * **零碎片化**：帳號 1（5TB）沒滿前，所有檔案與目錄 100% 留在帳號 1。
  * **資料夾絕不拆分**：避免 100 張相片被拆成 50/50 散落兩帳號，登入微軟/谷歌網頁版直觀清爽。

### 擴充 SOP（當收到容量低於 100GB 告警時）：
1. 取得新帳號（如 `od1_2` 或 `gd1_2`）授權。
2. 編輯 `config/rclone.conf`，新增對應區塊。
3. 將對應 union 由 alias 切換為 union：
   ```ini
   [od1_union]
   type = union
   upstreams = od1_1:fnOS_Backup od1_2:fnOS_Backup
   action_policy = epff
   create_policy = ff
   min_free_space = 50G
   ```
4. 儲存即可，守衛程式將自動將新資料填入新帳號！

---

## 六、 Docker 堆疊 GFS 階梯式生命週期規劃

為防止某個容器設定改壞但太晚發現，系統實作企業級 **Grandfather-Father-Son (GFS)** 時光機：

| 層級 (Tier) | 保存週期 | 快照頻率 | 份數 | 目的 |
| :--- | :--- | :--- | :--- | :--- |
| **Son (日快照)** | 最近 **14 天** | 每天 1 份 | 14 份 | 應對近期的配置改壞、密碼庫失誤立即復原 |
| **Father (週快照)** | 最近 **10 週 (70 天)** | 每週日 1 份 | 8 份 | 應對中期未察覺的資料損毀或異常 |
| **Grandfather (月快照)**| 最近 **1 年 (365 天)** | 每月 1 號 1 份 | 12 份 | 年度存檔時光機，可隨時復原歷史特定月份狀態 |

* **總快照上限**：約 **34 份**。
* **空間耗費**：總計僅約 **14 ~ 16 GB**（不到 5TB 的 0.3%）。
* **雙雲端同步修剪**：演算法同時向 `od1_crypt`、`od2_crypt` 與 `gd1_crypt` 執行過期淘汰，確保存放空間整齊一致。

---

## 七、 零信任安全與資安防禦體系

### 1. 檔案內容實體加密
* 採用 **XSalsa20 演算法** 進行串流加密。
* 雲端原生檢視僅能看見目錄架構與 `.bin` 密文檔案，無法預覽或分析任何檔案真實內容。

### 2. 憑證分離與安全範本
* 敏感金鑰與 Telegram Token 集中於本機 `.env`（Linux 權限 `600`）。
* Git 版本庫透過 `.gitignore` 嚴格隔離真實 `rclone.conf`、`.env` 與 `logs/`。
* 程式庫提供脫敏範本：`config/rclone.conf.example` 與 `.env.example`。

### 3. 微軟 OAuth 權限避坑規範
* 避開預設的 `.All` 全域權限（避免觸發 Entra ID「需要管理員核准」死鎖）。
* 強制使用限縮作用域：`access_scopes = Files.Read Files.ReadWrite offline_access`，確保一般子帳號直通授權。

---

## 八、 SRE 監控、熔斷與 Telegram 維運戰報

### 1. 告警與熔斷機制
* **每 6 小時靜默巡檢**：檢查微軟 Token 有效性與 API 連通性（正常狀態靜音，不疲勞洗版）。
* **容量告警門檻 (< 100 GB)**：
  * 單帳號剩餘 < 100GB 且有其他帳號時 ➜ 發送「ℹ️ 容量提示（建議擴容）」。
  * 所有帳號剩餘 < 100GB ➜ **主動熔斷暫停備份**，發送「⚠️ 空間耗盡警報」，防止寫爆拋錯。
* **帳號失效告警**：Token 失效時立即暫停備份並發送緊急告警。

### 2. 每日 02:00 全維度自適應日報格式
Telegram 戰報採用手機最適化垂直卡片風結構，杜絕孤字斷行：

```html
📊 【fnOS 備份體系每日維運日報】
📅 2026-09-12 02:00:00 ｜ 4-3-2 雙雲端容災

🖥️ 本地陣列 (/vol1)
• 儲存水位：714.2 GB / 21.8 TB (剩 21.1 TB)
• 納管目錄：1000, 1001, 1002, @team

☁️ 雲端儲存池現況 (已用 ｜ 剩餘可用)
• OD1 微軟主本：🟢 714.2 GB (餘 4.3 TB)
• OD2 微軟鏡像：🟢 714.2 GB (餘 4.3 TB)
• GD1 谷歌鏡像：🟢 714.2 GB (餘 4.3 TB)

⚡ 本次備份傳輸結果 (多雲並行直灌)
• OD1 微軟主本：✅ 成功 (本地直傳)
• OD2 微軟鏡像：✅ 成功 (本地直傳)
• GD1 谷歌鏡像：✅ 成功 (增量同步)
• Docker 快照 ：✅ 496 MB (GFS 階梯)
• 總執行耗時  ：3 分 45 秒

🛡️ 4-3-2 容災鏈路檢核：完全合規 🟢
├ 本地實體陣列：🟢 正常
├ OD1 微軟主本：🟢 M365 跨租戶
├ OD2 微軟鏡像：🟢 M365 雙副本
└ GD1 谷歌鏡像：🟢 Google 5TB

💾 系統隨身碟時光機：🟢 正常 (3.2 GB 快照, 剩 24.5 GB)
⏰ 下次例行排程：每日 02:00
```

---

## 九、 災難復原手冊 (Disaster Recovery SOP)

### 情境 1：使用者誤刪本地相片或檔案，需單獨還原
從 `od1_crypt:` 或 `od2_crypt:` 提取解密檔案至指定路徑：
```bash
# 還原特定目錄 (例如相簿目錄 Photos)
docker compose exec rclone-backup-guard rclone copy "od1_crypt:1001/Photos" "/data/1001/Photos_Restored" --config=/config/rclone/rclone.conf -P
```

### 情境 2：Docker 容器配置損壞或回滾歷史快照
從微軟或谷歌雲端拉取歷史 GFS 壓縮包並在本地還原：
```bash
# 1. 檢視雲端所有可用的 GFS 快照
docker compose exec rclone-backup-guard rclone lsf "od1_crypt:docker_snapshots/" --config=/config/rclone/rclone.conf

# 2. 下載特定日期的快照 (例如 20260906)
docker compose exec rclone-backup-guard rclone copy "od1_crypt:docker_snapshots/docker_snapshot_20260906.tar.gz" "/tmp/" --config=/config/rclone/rclone.conf -P

# 3. 解壓還原至主機 Docker 目錄 (保留原始權限)
tar -zxvf /tmp/docker_snapshot_20260906.tar.gz -C "${DOCKER_SRC_DIR:-/vol2/1000/docker}"
```

### 情境 3：微軟帳號遭遇風控或服務中斷時，從 Google Drive 解密還原
微軟服務不可用時，直接切換由 Google Drive 5TB 原生鏡像還原：
```bash
# 從 Google Drive 解密還原指定使用者資料夾
docker compose exec rclone-backup-guard rclone copy "gd1_crypt:1000/MyDocuments" "/data/1000/MyDocuments_Restored" --config=/config/rclone/rclone.conf -P
```

### 情境 4：NAS 整機損毀或新機冷啟動復原 (Bare-metal Recovery)
1. 在新機器安裝 Docker 與 Git。
2. Clone 本倉庫：
   ```bash
   git clone https://github.com/sam0324sam/fnos-onedrive-backup.git
   cd fnos-onedrive-backup
   ```
3. 依 `config/rclone.conf.example` 與 `.env.example` 填回金鑰。
4. 全量下載解密資料庫與使用者目錄（可選任一健康雲端節點）：
   ```bash
   docker compose exec rclone-backup-guard rclone copy od1_crypt: /data/ --config=/config/rclone/rclone.conf -P
   ```

---

## 十、 日常維護與 CLI 指令手冊

進入專案根目錄後執行：

| 維護情境 | 執行指令 |
| :--- | :--- |
| **手動健康與容量檢查** | `docker compose exec rclone-backup-guard python3 /app/scripts/sync_manager.py --check-only` |
| **手動測試 Telegram 戰報** | `docker compose exec rclone-backup-guard python3 /app/scripts/sync_manager.py --test-report` |
| **手動立即執行 Docker GFS 備份**| `docker compose exec rclone-backup-guard python3 /app/scripts/sync_manager.py --docker-backup-now` |
| **手動立即直推微軟 OD1 (主本)** | `docker compose exec rclone-backup-guard python3 /app/scripts/sync_manager.py --sync-od1-now` |
| **手動立即直推微軟 OD2 (鏡像)** | `docker compose exec rclone-backup-guard python3 /app/scripts/sync_manager.py --sync-od2-now` |
| **手動立即直推谷歌 GD1 (5TB 鏡像)** | `docker compose exec rclone-backup-guard python3 /app/scripts/sync_manager.py --sync-gd1-now` |
| **手動立即直推兩大鏡像 (OD2 + GD1)** | `docker compose exec rclone-backup-guard python3 /app/scripts/sync_manager.py --sync-mirrors-now` |
| **手動立即觸發全量多雲直推備份** | `docker compose exec rclone-backup-guard python3 /app/scripts/sync_manager.py --sync-now` |
| **即時查看守衛即時日誌** | `docker compose logs -f rclone-backup-guard` |
| **檢視詳細歷史日誌** | `cat logs/manager.log` ｜ `cat logs/sync_od1_1000.log` |
| **存取 Web GUI 儀表板** | 瀏覽器開啟 `http://<NAS_IP>:5572/` (內網免密碼直連) |

---
*本文件由 AI 研發中心架構團隊（Orchestra Edition）維護，遵循 SOTA 儲存架構與零信任安全規範。*
