#!/bin/bash
# ==============================================================================
# fnOS 4-3-2 系統盤自動化全盤快照與版本輪替腳本 (System OS Backup Script)
# ==============================================================================
set -e

BACKUP_DIR="/mnt/system_backup"
DATE=$(date +%Y%m%d_%H%M%S)
ARCHIVE_NAME="fnos_system_backup_${DATE}.tar.zst"
TARGET_ARCHIVE="${BACKUP_DIR}/${ARCHIVE_NAME}"
LOG_FILE="/vol2/1000/docker/rclone-backup/logs/system_backup.log"

mkdir -p "$(dirname "$LOG_FILE")"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "================================================================="
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🚀 開始執行 fnOS 系統盤全自動備份"
echo "================================================================="

# 1. 檢查掛載點
if ! mountpoint -q "$BACKUP_DIR"; then
    echo "⚠️  $BACKUP_DIR 未掛載，嘗試重新掛載..."
    mount "$BACKUP_DIR" 2>/dev/null || true
    if ! mountpoint -q "$BACKUP_DIR"; then
        echo "❌ 錯誤：隨身碟備份路徑 $BACKUP_DIR 未正確掛載！請檢查隨身碟連線。"
        exit 1
    fi
fi

# 2. 備份分區表與原始硬體資訊
echo "[1/4] 備份硬碟分區表與 UUID 元數據..."
sfdisk -d /dev/sde > "${BACKUP_DIR}/sde_partition_table.sfdisk"
blkid /dev/sde1 /dev/sde2 > "${BACKUP_DIR}/disk_uuids.txt"
cat /etc/fstab > "${BACKUP_DIR}/fstab.bak"

# 3. 備份 EFI 開機導引分區 (94MB)
echo "[2/4] 備份 EFI 開機引導分區 (/dev/sde1)..."
dd if=/dev/sde1 of="${BACKUP_DIR}/efi_boot.img" bs=1M status=none

# 4. 高速多核心壓縮備份根目錄 (使用 --one-file-system 隔離非系統目錄)
echo "[3/4] 使用 zstd 多核壓縮備份根系統 (/dev/sde2)..."
START_TIME=$(date +%s)

tar --numeric-owner --xattrs --acls --one-file-system -cp \
    --exclude='./tmp/*' \
    --exclude='./var/tmp/*' \
    --exclude='./var/cache/*' \
    --exclude='./swapfile' \
    -I 'zstd -T0 -3' \
    -f "$TARGET_ARCHIVE" \
    -C / .

ln -sf "$ARCHIVE_NAME" "${BACKUP_DIR}/fnos_system_backup_latest.tar.zst"

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
ARCHIVE_SIZE=$(du -h "$TARGET_ARCHIVE" | cut -f1)

echo "✅ 系統打包完成！耗時: ${DURATION} 秒，備份檔大小: ${ARCHIVE_SIZE}"

# 5. 歷史版本輪替 (保留最新 4 份)
echo "[4/4] 執行歷史版本輪替 (保留最新 4 份)..."
cd "$BACKUP_DIR"
ls -t fnos_system_backup_*.tar.zst 2>/dev/null | tail -n +5 | while read -r old_backup; do
    if [ -n "$old_backup" ] && [ -f "$old_backup" ]; then
        echo "🗑️  刪除過期快照: $old_backup"
        rm -f "$old_backup"
    fi
done

# 確保權限
chown -R 1000:1001 "$BACKUP_DIR" 2>/dev/null || true

echo "================================================================="
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🎉 fnOS 系統盤備份成功完成！"
echo "隨身碟剩餘容量："
df -h "$BACKUP_DIR"
echo "================================================================="
