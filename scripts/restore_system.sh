#!/bin/bash
# ==============================================================================
# fnOS 系統一鍵極速災難復原腳本 (Disaster Recovery Restore Script)
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "================================================================="
echo "   🛡️  fnOS 4-3-2 系統一鍵極速災難復原精靈 (Disaster Recovery)   "
echo "================================================================="

TARGET_DISK="$1"
if [ -z "$TARGET_DISK" ] || [ ! -b "$TARGET_DISK" ]; then
    echo "❌ 錯誤：未指定有效的目標磁碟設備！"
    echo ""
    echo "用法："
    echo "  sudo ./restore_system.sh /dev/sdX  (SATA SSD)"
    echo "  sudo ./restore_system.sh /dev/nvmeXn1 (M.2 NVMe SSD)"
    echo ""
    echo "當前主機可用硬碟清單："
    lsblk -d -o NAME,SIZE,MODEL,TRAN,SERIAL
    echo ""
    exit 1
fi

echo "⚠️  【極度危險警告】"
echo "即將對目標磁碟：[$TARGET_DISK] 進行重新分割、格式化並還原 fnOS 全系統！"
echo "目標磁碟上的所有舊資料將被 100% 永久清除覆蓋！"
echo ""
echo "請務必再三確認 [$TARGET_DISK] 是您新裝上的「空系統 SSD」，"
echo "絕不能是您的 4 顆 12TB 資料碟或 NVMe 應用池！"
echo "-----------------------------------------------------------------"
read -p "請輸入大寫 'YES' 確認開始復原: " CONFIRM
if [ "$CONFIRM" != "YES" ]; then
    echo "操作已安全取消。"
    exit 0
fi

# 1. 識別分割區名稱
if [[ "$TARGET_DISK" =~ [0-9]$ ]]; then
    PART_BOOT="${TARGET_DISK}p1"
    PART_ROOT="${TARGET_DISK}p2"
else
    PART_BOOT="${TARGET_DISK}1"
    PART_ROOT="${TARGET_DISK}2"
fi

echo ""
echo "[1/5] 正在清除舊分區並建立 GPT 分割表..."
umount "${TARGET_DISK}"* 2>/dev/null || true
parted -s "$TARGET_DISK" mklabel gpt
parted -s "$TARGET_DISK" mkpart "BOOT" fat32 1MiB 95MiB
parted -s "$TARGET_DISK" set 1 esp on
parted -s "$TARGET_DISK" mkpart "SYSTEM" ext4 95MiB 100%

# 確保內核重讀分區表
partprobe "$TARGET_DISK" 2>/dev/null || sleep 2

echo "[2/5] 正在格式化並還原系統原始 UUID..."
ORIGINAL_BOOT_UUID="69F9-D2E7"
ORIGINAL_ROOT_UUID="fd95eab2-de97-4308-926a-852750bfcb60"

# 格式化 EFI
mkfs.vfat -F 32 -i "${ORIGINAL_BOOT_UUID//-/}" "$PART_BOOT"

# 格式化 根分區 (強制指定原 UUID，保證開機引導 fstab 零修改秒開機)
mkfs.ext4 -F -U "$ORIGINAL_ROOT_UUID" -L "SYSTEM" "$PART_ROOT"

echo "[3/5] 還原 EFI 開機導引鏡像..."
if [ -f "${SCRIPT_DIR}/efi_boot.img" ]; then
    dd if="${SCRIPT_DIR}/efi_boot.img" of="$PART_BOOT" bs=1M status=none
    echo "✅ EFI 引導鏡像已直接位元寫入 $PART_BOOT"
else
    echo "⚠️  未找到 efi_boot.img，跳過 EFI 鏡像寫入"
fi

echo "[4/5] 解壓還原 fnOS 根目錄全量系統..."
BACKUP_ARCHIVE="${SCRIPT_DIR}/fnos_system_backup_latest.tar.zst"
if [ ! -f "$BACKUP_ARCHIVE" ]; then
    BACKUP_ARCHIVE=$(ls -t "${SCRIPT_DIR}"/fnos_system_backup_*.tar.zst 2>/dev/null | head -n 1)
fi

if [ -z "$BACKUP_ARCHIVE" ] || [ ! -f "$BACKUP_ARCHIVE" ]; then
    echo "❌ 錯誤：未在 ${SCRIPT_DIR} 找到任何系統備份壓縮檔 (fnos_system_backup_*.tar.zst)！"
    exit 1
fi

TMP_ROOT="/tmp/fnos_restore_mount"
mkdir -p "$TMP_ROOT"
mount "$PART_ROOT" "$TMP_ROOT"

echo "正在由 $(basename "$BACKUP_ARCHIVE") 還原檔案至 $PART_ROOT..."
tar --numeric-owner --xattrs --acls -xp -I zstd -f "$BACKUP_ARCHIVE" -C "$TMP_ROOT"

echo "[5/5] 重建虛擬核心掛載目錄與權限修復..."
mkdir -p "$TMP_ROOT"/{proc,sys,dev,run,tmp,mnt,vol1,vol2,vol00}
chmod 1777 "$TMP_ROOT/tmp"

# 安全卸載
sync
umount "$TMP_ROOT"

echo ""
echo "================================================================="
echo "🎉 恭喜！fnOS 全系統已成功復原至 $TARGET_DISK！"
echo "================================================================="
echo "後續操作："
echo "1. 拔掉 Live USB 開機隨身碟"
echo "2. 重新啟動主機 (sudo reboot)"
echo "3. 系統將自動由新 SSD 開機，完全無縫回到原有的所有配置！"
echo "================================================================="
