# 🛡️ fnOS 系統磁碟災難復原手冊 (Disaster Recovery SOP)

> **當現役的 120G 系統 SSD 損壞、無法開機時，請依照本手冊步驟於 3 分鐘內完整還原系統。**

---

## 📑 核心復原原理
本備份隨身碟內存放了：
1. `efi_boot.img`：包含 UEFI 開機導引記錄的位元映像。
2. `fnos_system_backup_latest.tar.zst`：採用 `zstd` 高壓縮打包的全系統根分區（已排除相片陣列與虛擬目錄，實佔約 12GB 檔案）。
3. `sde_partition_table.sfdisk`：原始 GPT 分割表結構。
4. `restore_system.sh`：全自動一鍵還原指令碼。

透過自動還原**原始磁區 UUID（`fd95eab2-de97-4308-926a-852750bfcb60`）**，還原後新 SSD 開機時系統與引導將 **100% 視為同一顆原始硬碟，零修改設定秒開機**！

---

## 🛠️ 災難復原三步驟 (Disaster Recovery Steps)

### 【步驟一】硬體替換
1. 將損壞的舊 SSD 拔下。
2. 安裝任何一顆容量 $\ge 120GB$ 的新 SSD（SATA 或 M.2 NVMe 皆可，例如 128G、256G、512G 等）。
3. **保留此 32GB 備份隨身碟插在 NAS 上**。

---

### 【步驟二】使用開機隨身碟進入救援環境
由於 NAS 目前沒有可用系統，請任選以下一種方式進入 Linux 救援環境：
* **方式 A（最推薦 - 任何 Linux Live USB）**：
  - 手邊若有任何 Ubuntu Live / Debian Live / Ventoy 開機隨身碟，插上 NAS 開機進入「Try Ubuntu」或 Live 終端機。
* **方式 B（微型救援碟 SystemRescue）**：
  - 下載免費的 [SystemRescue](https://www.system-rescue.org/) 燒錄至隨身碟開機。

---

### 【步驟三】執行一鍵自動還原

1. 開啟 Live 終端機（Terminal），切換為 root 權限：
   ```bash
   sudo su
   ```

2. 找到此 32GB 備份隨身碟並掛載：
   ```bash
   # 查看磁碟標籤，找到標籤為 FNOS_SYS_BACKUP 的分區
   mkdir -p /mnt/backup
   mount -L FNOS_SYS_BACKUP /mnt/backup
   cd /mnt/backup
   ```

3. 確認新 SSD 的設備名稱：
   ```bash
   lsblk -d -o NAME,SIZE,MODEL,TRAN
   ```
   > ⚠️ **請務必仔細核對新 SSD 的代號（例如 `/dev/sda` 或 `/dev/nvme0n1`），絕對不可指定到 4 顆 12TB 硬碟！**

4. 執行一鍵還原腳本：
   ```bash
   # 若新 SSD 是 SATA 介面 (例如 /dev/sda)
   ./restore_system.sh /dev/sda

   # 若新 SSD 是 M.2 NVMe 介面 (例如 /dev/nvme0n1)
   ./restore_system.sh /dev/nvme0n1
   ```

5. 依提示輸入大寫 `YES`，腳本將在 **2 ~ 3 分鐘內自動完成**：
   - 重建 GPT 分割表
   - 還原 EFI 引導分區
   - 格式化並分配原始系統 UUID
   - 解壓縮還原 12GB 系統檔案
   - 修復系統掛載目錄與權限

---

## 🏁 完工開機
1. 移除開機救援隨身碟（32GB 備份隨身碟可繼續插在 NAS 上）。
2. 輸入 `reboot` 重新啟動主機。
3. **主機將自動由新 SSD 開機進入 fnOS**：
   - 所有使用者帳號、密碼、IP 設定 100% 保持原樣。
   - 4 顆 12TB 硬碟（`/vol1`）與 NVMe Docker（`/vol2`）自動掛載，完全無縫銜接！

---
*文件維護：fnOS SRE 自動化運維中心 ｜ 生成時間：2026-09-08*
