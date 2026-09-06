# fnOS OneDrive 3-2-1 雙集群異地加密備份系統

本專案提供基於 Docker 的全自動化 3-2-1 雲端備份方案，整合 Rclone 聚合池、客戶端內容加密、密文鏡像直傳與 Telegram 智慧告警守衛。

---

## 一、架構說明

1. **OD1 主儲存集群 (od1_union / od1_crypt)**：
   - 帳號：
as-main-01@3lym23.onmicrosoft.com (5TB)
   - 模式：增量備份、不壓縮、保留原檔名與目錄結構、檔案內容實體加密（XSalsa20）。
   - 備份來源：/vol1/1000 (sam), /vol1/1001 (Hiyoko), /vol1/1002 (miya), /vol1/@team。
   - 排除目錄：.@#local/**, 	humb/**, .recycle/**。

2. **OD2 鏡像副本集群 (od2_union / od2_crypt)**：
   - 帳號：
as-mirror-01@auvooo.cn (5TB)
   - 模式：密文直傳（直接鏡像 OD1 的加密後二進制檔，不佔用 CPU 解密運算）。

---

## 二、容器服務

專案目錄：/vol2/1000/docker/rclone-backup/

| 容器名稱 | 映像檔 | 用途 | 訪問位址 / 說明 |
| :--- | :--- | :--- | :--- |
| 
clone-web-dashboard | 
clone/rclone:latest | Web GUI 視覺化儀表板 | http://<NAS_IP>:5572 (內網免密碼直進) |
| 
clone-backup-guard | 
clone-backup-runner:latest | 自動化排程與守衛服務 | 每日 02:00 自動觸發同步，每 6 小時健康巡檢 |

---

## 三、常用維護指令

進入目錄：
`ash
cd /vol2/1000/docker/rclone-backup
`

1. **手動執行容量與健康檢查**：
   `ash
   docker exec rclone-backup-guard python3 /app/scripts/sync_manager.py --check-only
   `

2. **手動立即觸發全量雙集群備份**：
   `ash
   docker exec rclone-backup-guard python3 /app/scripts/sync_manager.py --sync-now
   `

3. **查看守衛即時日誌**：
   `ash
   docker logs -f rclone-backup-guard
   `

4. **查看詳細備份日誌**：
   `ash
   cat /vol2/1000/docker/rclone-backup/logs/manager.log
   cat /vol2/1000/docker/rclone-backup/logs/phase1_1001.log
   cat /vol2/1000/docker/rclone-backup/logs/phase2_mirror.log
   `

---

## 四、未來擴充帳號流程 (當 5TB 快滿時)

當 Telegram 收到容量低於 100GB 警報時：
1. 取得新帳號（如 od1_2）的 Token 與 Drive ID。
2. 編輯 /vol2/1000/docker/rclone-backup/config/rclone.conf：
   - 新增 [od1_2] 區塊。
   - 將 [od1_union] 修改為 	ype = union：
     `ini
     # 方案 A：循序填滿模式 (Sequential Fill / 溢流模式，首選推薦)
     # 滿了才換號，資料夾永不跨號拆分
     [od1_union]
     type = union
     upstreams = od1_1:fnOS_Backup od1_2:fnOS_Backup
     action_policy = epff
     create_policy = ff
     min_free_space = 50G
     `
3. 儲存即可，守衛下次排程時會自動將新檔案寫入新帳號，實現無痛擴容！
