#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fnOS OneDrive 3-2-1 Dual-Cluster Backup and Health Guard
- Phase 1: Local NAS -> od1_crypt (Incremental, Encrypted, Uncompressed)
- Phase 2: od1_union -> od2_union (Direct Ciphertext Mirror, Full Speed)
- Pre-flight Health and Capacity Checking with Telegram Alerts
"""

import os
import sys
import json
import time
import signal
import logging
import argparse
import subprocess
import configparser
import re
import shutil
import urllib.request
import urllib.parse
from datetime import datetime, date

# ================= Configuration =================
CONFIG_PATH = os.environ.get("RCLONE_CONFIG", "/config/rclone/rclone.conf")
LOG_DIR = os.environ.get("LOG_DIR", "/logs")
TG_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FREE_THRESHOLD_GB = float(os.environ.get("FREE_THRESHOLD_GB", "100.0"))
SYNC_SCHEDULE_TIME = os.environ.get("SYNC_SCHEDULE_TIME", "02:00")  # HH:MM format
DEFAULT_DATA_FOLDERS = ["1000", "1001", "1002", "@team"]
DOCKER_SRC = os.environ.get("DOCKER_SRC", "/docker_src")
STAGING_DIR = os.path.join(LOG_DIR, "staging")

def get_backup_targets() -> list:
    """自動探索 /data 下所有純數字使用者 UID (如 1000, 1001, 1002, 1003...) 與 @team 目錄"""
    targets = []
    data_dir = "/data"
    if os.path.exists(data_dir):
        for item in sorted(os.listdir(data_dir)):
            full_path = os.path.join(data_dir, item)
            if os.path.isdir(full_path):
                if item.isdigit() or item == "@team":
                    targets.append(item)
    return targets if targets else DEFAULT_DATA_FOLDERS

os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(LOG_DIR, "manager.log"), encoding="utf-8")
    ]
)

running = True
def sig_handler(signum, frame):
    global running
    logging.info(f"Received signal {signum}, shutting down gracefully...")
    running = False

signal.signal(signal.SIGTERM, sig_handler)
signal.signal(signal.SIGINT, sig_handler)

# ================= Telegram Notifications =================
def send_telegram(message: str) -> bool:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        logging.warning("Telegram Bot Token or Chat ID not configured. Skipping alert.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TG_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")
        return False

# ================= Rclone Helpers =================
def parse_union_upstreams(union_section: str) -> list:
    """Read upstreams defined in a union or alias remote from rclone.conf"""
    if not os.path.exists(CONFIG_PATH):
        logging.error(f"Config file not found: {CONFIG_PATH}")
        return []
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    if union_section not in cfg:
        logging.warning(f"Section [{union_section}] not found in {CONFIG_PATH}")
        return []
    sec = cfg[union_section]
    remotes = []
    sec_type = sec.get("type", "")
    if sec_type == "alias":
        target = sec.get("remote", "")
        remote = target.split(":")[0].strip()
        if remote:
            remotes.append(remote)
    elif sec_type == "union":
        upstreams_str = sec.get("upstreams", "")
        for item in upstreams_str.split():
            remote = item.split(":")[0].strip()
            if remote and remote not in remotes:
                remotes.append(remote)
    return remotes

def check_remote_quota(remote: str) -> dict:
    """Run `rclone about <remote>: --json` to get quota and health"""
    cmd = [
        "rclone", "about", f"{remote}:",
        "--json",
        f"--config={CONFIG_PATH}"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            return {
                "remote": remote,
                "status": "ERROR",
                "error": res.stderr.strip() or "Unknown error / Auth failed",
                "total_gb": 0,
                "used_gb": 0,
                "free_gb": 0
            }
        data = json.loads(res.stdout)
        total = data.get("total", 0) / (1024**3)
        used = data.get("used", 0) / (1024**3)
        free = data.get("free", 0) / (1024**3)
        status = "OK"
        if free < FREE_THRESHOLD_GB:
            status = "LOW_SPACE"
        return {
            "remote": remote,
            "status": status,
            "error": "",
            "total_gb": total,
            "used_gb": used,
            "free_gb": free
        }
    except Exception as e:
        return {
            "remote": remote,
            "status": "ERROR",
            "error": str(e),
            "total_gb": 0,
            "used_gb": 0,
            "free_gb": 0
        }

def get_local_storage_stats(path="/data") -> dict:
    """動態取得本地儲存池 (如 /vol1 掛載至 /data) 容量與使用量"""
    if not os.path.exists(path):
        return {}
    try:
        total, used, free = shutil.disk_usage(path)
        return {
            "total_tb": total / (1024**4),
            "used_gb": used / (1024**3),
            "used_tb": used / (1024**4),
            "free_tb": free / (1024**4),
            "use_percent": (used / total) * 100 if total > 0 else 0
        }
    except Exception as e:
        logging.error(f"Error reading disk usage for {path}: {e}")
        return {}

def get_all_union_clusters() -> list:
    """自動從 rclone.conf 中搜尋所有合流池/集群 (如 od1_union, od2_union, 或未來新增的 gdrive_union 等)"""
    if not os.path.exists(CONFIG_PATH):
        return ["od1_union", "od2_union"]
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    clusters = []
    for sec in cfg.sections():
        if sec.endswith("_union"):
            clusters.append(sec)
    return clusters if clusters else ["od1_union", "od2_union"]

def get_cluster_stats(cluster_name: str) -> dict:
    """動態計算單一雲端合流池內所有帳號之總量、已用、剩餘與健康狀態"""
    upstreams = parse_union_upstreams(cluster_name)
    accounts = []
    total_bytes = 0
    used_bytes = 0
    free_bytes = 0
    all_ok = True

    for r in upstreams:
        q = check_remote_quota(r)
        if q["status"] == "OK":
            t = q.get("total_gb", 0) * (1024**3)
            u = q.get("used_gb", 0) * (1024**3)
            f = q.get("free_gb", 0) * (1024**3)
            total_bytes += t
            used_bytes += u
            free_bytes += f
            accounts.append({
                "remote": r,
                "status": "🟢",
                "free_tb": q["free_gb"] / 1024.0,
                "free_gb": q["free_gb"],
                "total_tb": (t / (1024**4)) if t > 0 else 0
            })
        elif q["status"] == "LOW_SPACE":
            t = q.get("total_gb", 0) * (1024**3)
            u = q.get("used_gb", 0) * (1024**3)
            f = q.get("free_gb", 0) * (1024**3)
            total_bytes += t
            used_bytes += u
            free_bytes += f
            accounts.append({
                "remote": r,
                "status": "🟡",
                "free_tb": q["free_gb"] / 1024.0,
                "free_gb": q["free_gb"],
                "total_tb": (t / (1024**4)) if t > 0 else 0
            })
        else:
            all_ok = False
            accounts.append({
                "remote": r,
                "status": "🔴",
                "error": q.get("error", "連線異常")
            })

    total_tb = total_bytes / (1024**4)
    used_gb = used_bytes / (1024**3)
    free_tb = free_bytes / (1024**4)
    use_percent = (used_bytes / total_bytes * 100) if total_bytes > 0 else 0

    return {
        "name": cluster_name,
        "upstreams": upstreams,
        "accounts": accounts,
        "total_tb": total_tb,
        "used_gb": used_gb,
        "free_tb": free_tb,
        "use_percent": use_percent,
        "all_ok": all_ok
    }

def generate_daily_executive_report(duration_str: str, phase1_success: bool, phase2_success: bool, targets: list, docker_msg: str = "") -> str:
    """產出適合 Telegram 閱讀、高度自適應擴充的每日全維度日報"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    local_stat = get_local_storage_stats("/data")
    clusters = get_all_union_clusters()

    # 1. 本地儲存池狀態 (完全動態讀取)
    targets_str = ", ".join([f"<code>{t}</code>" for t in targets])
    if local_stat:
        local_sec = (
            f"🖥️ <b>本地資料池 (/vol1)</b>\n"
            f"• 陣列容量：<b>{local_stat['total_tb']:.1f} TB</b> ｜ 剩餘可用：<b>{local_stat['free_tb']:.1f} TB</b>\n"
            f"• 目前水位：<b>{local_stat['used_gb']:.1f} GB</b> ({local_stat['use_percent']:.1f}%)\n"
            f"• 自動納管：{targets_str}"
        )
    else:
        local_sec = f"🖥️ <b>本地資料池</b>\n• 自動納管：{targets_str}"

    # 2. 雲端集群狀態 (動態迴圈支援未來任意多個 union 集群與帳號擴充)
    cloud_sections = []
    all_clusters_ok = True
    for c_name in clusters:
        c_stat = get_cluster_stats(c_name)
        if not c_stat["all_ok"]:
            all_clusters_ok = False

        alias_title = "OD1 主儲存池" if "od1" in c_name else ("OD2 鏡像副本" if "od2" in c_name else f"雲端池 ({c_name})")
        lines = [f"☁️ <b>{alias_title} ({c_name})</b>"]
        lines.append(f"• 聚合總量：<b>{c_stat['total_tb']:.1f} TB</b> ｜ 剩餘可用：<b>{c_stat['free_tb']:.1f} TB</b>")
        lines.append(f"• 目前水位：<b>{c_stat['used_gb']:.1f} GB</b> ({c_stat['use_percent']:.1f}%)")
        lines.append("• 節點清單：")

        accs = c_stat.get("accounts", [])
        for idx, a in enumerate(accs):
            branch = "└" if idx == len(accs) - 1 else "├"
            if a["status"] in ["🟢", "🟡"]:
                lines.append(f"  {branch} <code>{a['remote']}</code> {a['status']} 剩餘 {a['free_tb']:.1f} TB")
            else:
                lines.append(f"  {branch} <code>{a['remote']}</code> 🔴 連線異常")
        cloud_sections.append("\n".join(lines))

    cloud_sec = "\n\n".join(cloud_sections)

    # 3. 本次備份傳輸指標
    phase1_status = "✅ 成功" if phase1_success else "❌ 失敗"
    phase2_status = "✅ 成功" if phase2_success else "❌ 失敗"
    transfer_lines = [
        "⚡ <b>本次備份傳輸總結</b>",
        f"• 階段一 (NAS ➜ OD1 加密)：{phase1_status}",
        f"• 階段二 (OD1 ➜ OD2 鏡像)：{phase2_status}"
    ]
    if docker_msg:
        transfer_lines.append(f"• 容器快照 (Docker GFS)：{docker_msg}")
    transfer_lines.append(f"• 執行總耗時：{duration_str}")
    transfer_sec = "\n".join(transfer_lines)

    # 4. 3-2-1 容災鏈路檢核
    is_fully_compliant = phase1_success and phase2_success and all_clusters_ok
    sla_badge = "🛡️ <b>完全合規 (3-2-1 Verified)</b>" if is_fully_compliant else "⚠️ <b>鏈路警示 (需檢視)</b>"

    sla_sec = (
        f"🛡️ <b>3-2-1 容災鏈路狀態</b>\n"
        f"• 鏈路檢核：{sla_badge}\n"
        f"• 拓撲節點：[本地陣列] 🟢 ➜ [雲端主本] {'🟢' if phase1_success else '🔴'} ➜ [異地鏡像] {'🟢' if phase2_success else '🔴'}"
    )

    full_report = (
        f"📊 <b>【fnOS 3-2-1 雙雲端每日維運日報】</b>\n"
        f"📅 <b>報告時間：</b> {now_str}\n\n"
        f"{local_sec}\n\n"
        f"{cloud_sec}\n\n"
        f"{transfer_sec}\n\n"
        f"{sla_sec}\n\n"
        f"⏰ <b>下次例行備份：</b> 每日 {SYNC_SCHEDULE_TIME}"
    )
    return full_report

def run_health_guard() -> tuple[bool, str]:
    """Check both OD1 and OD2 pool health and space"""
    od1_remotes = parse_union_upstreams("od1_union")
    od2_remotes = parse_union_upstreams("od2_union")

    if not od1_remotes or not od2_remotes:
        msg = "⚠️ <b>【備份配置缺失】</b>\n未在 <code>rclone.conf</code> 中找到 <code>od1_union</code> 或 <code>od2_union</code> 的成員帳號，請先完成帳號授權配置！"
        send_telegram(msg)
        return False, "CONFIG_MISSING"

    logging.info(f"Checking OD1 upstreams: {od1_remotes}")
    logging.info(f"Checking OD2 upstreams: {od2_remotes}")

    od1_status = [check_remote_quota(r) for r in od1_remotes]
    od2_status = [check_remote_quota(r) for r in od2_remotes]

    critical_errors = []
    low_space_warnings = []

    # Evaluate OD1
    od1_has_healthy_space = False
    for s in od1_status:
        if s["status"] == "ERROR":
            critical_errors.append(f"• <b>OD1 主集群 [{s['remote']}]</b> 連線異常/失效：{s['error']}")
        elif s["status"] == "LOW_SPACE":
            low_space_warnings.append(f"• <b>OD1 主集群 [{s['remote']}]</b> 剩餘容量告急：{s['free_gb']:.1f} GB (&lt; {FREE_THRESHOLD_GB} GB)")
        else:
            od1_has_healthy_space = True

    # Evaluate OD2
    od2_has_healthy_space = False
    for s in od2_status:
        if s["status"] == "ERROR":
            critical_errors.append(f"• <b>OD2 鏡像集群 [{s['remote']}]</b> 連線異常/失效：{s['error']}")
        elif s["status"] == "LOW_SPACE":
            low_space_warnings.append(f"• <b>OD2 鏡像集群 [{s['remote']}]</b> 剩餘容量告急：{s['free_gb']:.1f} GB (&lt; {FREE_THRESHOLD_GB} GB)")
        else:
            od2_has_healthy_space = True

    if critical_errors:
        alert_msg = (
            "🚨 <b>【fnOS 備份系統 - 帳號異常告警】</b>\n"
            + "\n".join(critical_errors)
            + "\n\n⛔ <b>處置：</b>請登入檢查帳號授權或補齊新帳號，備份任務已暫停以策安全。"
        )
        send_telegram(alert_msg)
        return False, "ACCOUNT_ERROR"

    if low_space_warnings and not od1_has_healthy_space:
        alert_msg = (
            "⚠️ <b>【fnOS 備份系統 - 空間耗盡警報】</b>\n"
            + "\n".join(low_space_warnings)
            + f"\n\n📢 <b>請盡快新增 5TB 帳號擴充電腦池！</b>\n加入新帳號至 <code>od1_union</code> 後，系統將自動接續同步。"
        )
        send_telegram(alert_msg)
        return False, "SPACE_EXHAUSTED"

    if low_space_warnings:
        notice_msg = (
            "ℹ️ <b>【fnOS 備份系統 - 容量提示】</b>\n"
            + "\n".join(low_space_warnings)
            + "\n現有其他帳號仍有空間，備份將繼續執行，但建議盡快準備下一個 5TB 帳號。"
        )
        send_telegram(notice_msg)

    return True, "READY"

# ================= Docker GFS Snapshot Engine =================
def prune_gfs_snapshots(remote_dir: str):
    """
    實施 GFS (Grandfather-Father-Son) 階梯式生命週期淘汰：
    - Tier 1 (日備份): 最近 14 天內每天保留 1 份
    - Tier 2 (週備份): 最近 70 天 (約 10 週) 內，每週日保留 1 份 (weekday == 6)
    - Tier 3 (月備份): 最近 365 天 (1 年) 內，每月 1 號保留 1 份 (day == 1)
    其餘不符合上述梯度的過期快照自動刪除
    """
    logging.info(f"Running GFS retention pruning on {remote_dir}...")
    ls_cmd = [
        "rclone", "lsf", f"{remote_dir}/",
        f"--config={CONFIG_PATH}"
    ]
    res_ls = subprocess.run(ls_cmd, capture_output=True, text=True)
    if res_ls.returncode != 0:
        logging.warning(f"Unable to list files in {remote_dir} for pruning: {res_ls.stderr.strip()}")
        return

    today = date.today()
    files = [f.strip() for f in res_ls.stdout.splitlines() if f.strip()]
    pattern = re.compile(r"^docker_snapshot_(\d{8})\.tar\.gz$")

    for fname in files:
        match = pattern.match(fname)
        if not match:
            continue
        try:
            f_date = datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            continue

        age_days = (today - f_date).days
        keep = False

        # Tier 1: 最近 14 天
        if age_days <= 14:
            keep = True
        # Tier 2: 最近 70 天內的週日 (Sunday)
        elif age_days <= 70 and f_date.weekday() == 6:
            keep = True
        # Tier 3: 最近 365 天內的每月 1 號
        elif age_days <= 365 and f_date.day == 1:
            keep = True

        if not keep:
            logging.info(f"GFS Pruning: Deleting expired snapshot {fname} from {remote_dir} (Age: {age_days} days)...")
            del_cmd = [
                "rclone", "deletefile", f"{remote_dir}/{fname}",
                f"--config={CONFIG_PATH}"
            ]
            subprocess.run(del_cmd)

def execute_docker_gfs_backup() -> tuple[bool, str]:
    """
    1. 打包 /docker_src 排除無效暫存與日誌
    2. 上傳至 od1_crypt:docker_snapshots/
    3. 執行 GFS (14天日備份 + 8週週備份 + 12個月月備份) 生命週期修剪
    4. 同步修剪 od2_crypt:docker_snapshots/ 確保副本一致性
    """
    if not os.path.exists(DOCKER_SRC):
        logging.info(f"Docker source path {DOCKER_SRC} not found, skipping docker backup.")
        return True, "未掛載略過"

    os.makedirs(STAGING_DIR, exist_ok=True)
    today_str = datetime.now().strftime("%Y%m%d")
    archive_name = f"docker_snapshot_{today_str}.tar.gz"
    staging_archive = os.path.join(STAGING_DIR, archive_name)

    logging.info(f"Creating Docker GFS archive: {archive_name}...")
    tar_cmd = [
        "tar", "-czf", staging_archive,
        "-C", DOCKER_SRC,
        "--exclude=rclone-backup/logs/*",
        "--exclude=rclone-backup/.git/*",
        "--exclude=rclone-backup/staging/*",
        "--exclude=*/cache/*",
        "--exclude=*/.cache/*",
        "--exclude=*/Crashpad/*",
        "."
    ]
    res_tar = subprocess.run(tar_cmd, capture_output=True, text=True)
    if res_tar.returncode != 0:
        err = res_tar.stderr.strip() or "Tar command failed"
        logging.error(f"Failed to create docker archive: {err}")
        return False, f"打包失敗: {err}"

    archive_size_mb = os.path.getsize(staging_archive) / (1024 * 1024)
    logging.info(f"Docker archive created successfully ({archive_size_mb:.1f} MB). Uploading to od1_crypt:docker_snapshots/...")

    upload_cmd = [
        "rclone", "copy", staging_archive, "od1_crypt:docker_snapshots/",
        f"--config={CONFIG_PATH}",
        "--drive-chunk-size=64M",
        "--fast-list",
        "-v"
    ]
    res_upload = subprocess.run(upload_cmd, capture_output=True, text=True)

    if os.path.exists(staging_archive):
        try:
            os.remove(staging_archive)
        except Exception:
            pass

    if res_upload.returncode != 0:
        err = res_upload.stderr.strip() or "Rclone upload failed"
        logging.error(f"Failed to upload docker snapshot: {err}")
        return False, f"上傳失敗: {err}"

    logging.info(f"Docker snapshot {archive_name} uploaded successfully ({archive_size_mb:.1f} MB).")

    # 執行雙集群 GFS 梯次清理
    prune_gfs_snapshots("od1_crypt:docker_snapshots")
    prune_gfs_snapshots("od2_crypt:docker_snapshots")

    return True, f"✅ 已封存 ({archive_size_mb:.1f} MB, GFS 階梯保留中)"

# ================= Sync Logic =================
def execute_backup():
    """Perform Phase 0 (Docker GFS), Phase 1, and Phase 2 backup"""
    logging.info("Starting Backup Workflow...")
    start_time = time.time()

    # 1. Health Guard Pre-check
    can_proceed, status = run_health_guard()
    if not can_proceed:
        logging.warning(f"Health guard blocked backup with status: {status}")
        return

    # 2. Phase 0: Docker GFS Snapshot
    logging.info("=== Phase 0: Starting Docker GFS Snapshot ===")
    docker_success, docker_msg = execute_docker_gfs_backup()

    # 3. Phase 1: NAS -> od1_crypt
    logging.info("=== Phase 1: Starting NAS -> od1_crypt ===")
    phase1_success = True
    phase1_details = []

    targets = get_backup_targets()
    logging.info(f"Discovered backup target folders: {targets}")

    for folder in targets:
        src_path = f"/data/{folder}"
        if not os.path.exists(src_path):
            logging.info(f"Source folder {src_path} does not exist, skipping.")
            continue
        
        dst_remote = f"od1_crypt:{folder}"
        log_file = os.path.join(LOG_DIR, f"phase1_{folder}.log")
        
        cmd = [
            "rclone", "copy", src_path, dst_remote,
            f"--config={CONFIG_PATH}",
            "--transfers=4",
            "--checkers=8",
            "--tpslimit=10",
            "--fast-list",
            "--drive-chunk-size=64M",
            "--exclude=.@#local/**",
            "--exclude=thumb/**",
            "--exclude=.recycle/**",
            "-v",
            f"--log-file={log_file}"
        ]
        logging.info(f"Syncing {src_path} -> {dst_remote}...")
        res = subprocess.run(cmd)
        if res.returncode != 0:
            logging.error(f"Phase 1 failed for {folder}")
            phase1_success = False
            phase1_details.append(f"❌ <code>{folder}</code> 同步失敗 (Code {res.returncode})")
        else:
            logging.info(f"Phase 1 finished for {folder}")
            phase1_details.append(f"✅ <code>{folder}</code> 增量同步完成")

    if not phase1_success:
        msg = "❌ <b>【fnOS 備份告警 - 階段一同步失敗】</b>\n" + "\n".join(phase1_details) + "\n請查看日誌排除問題。"
        send_telegram(msg)
        return

    # 3. Phase 2: od1_union -> od2_union (Raw Ciphertext Mirror, Full Speed)
    logging.info("=== Phase 2: Starting od1_union -> od2_union (Raw Mirror) ===")
    phase2_log = os.path.join(LOG_DIR, "phase2_mirror.log")
    cmd_phase2 = [
        "rclone", "copy", "od1_union:", "od2_union:",
        f"--config={CONFIG_PATH}",
        "--transfers=4",
        "--checkers=8",
        "--tpslimit=10",
        "--fast-list",
        "--drive-chunk-size=64M",
        "-v",
        f"--log-file={phase2_log}"
    ]
    res_phase2 = subprocess.run(cmd_phase2)
    phase2_success = (res_phase2.returncode == 0)
    
    duration = int(time.time() - start_time)
    duration_str = f"{duration // 60} 分 {duration % 60} 秒"

    report_msg = generate_daily_executive_report(duration_str, phase1_success, phase2_success, targets, docker_msg)
    send_telegram(report_msg)

# ================= Daemon Loop =================
def run_daemon():
    logging.info(f"fnOS Backup Daemon started. Daily scheduled sync time: {SYNC_SCHEDULE_TIME}")
    send_telegram(f"🚀 <b>【fnOS 備份守衛已啟動】</b>\n守衛服務已就緒，每日預設於 <b>{SYNC_SCHEDULE_TIME}</b> 執行雙雲端同步。")

    last_sync_date = ""
    last_health_check_hour = -1

    while running:
        now = datetime.now()
        current_time_str = now.strftime("%H:%M")
        current_date_str = now.strftime("%Y-%m-%d")

        # Daily scheduled execution
        if current_time_str == SYNC_SCHEDULE_TIME and last_sync_date != current_date_str:
            last_sync_date = current_date_str
            execute_backup()

        # Regular health guard check every 6 hours (at 00:00, 06:00, 12:00, 18:00)
        if now.hour % 6 == 0 and now.hour != last_health_check_hour and now.minute == 0:
            last_health_check_hour = now.hour
            logging.info("Running scheduled 6-hour health check...")
            run_health_guard()

        time.sleep(30)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="fnOS OneDrive Dual-Cluster Backup Manager")
    parser.add_argument("--check-only", action="store_true", help="Only run health & capacity check")
    parser.add_argument("--docker-backup-now", action="store_true", help="Run Docker GFS snapshot and upload now")
    parser.add_argument("--test-report", action="store_true", help="Generate and send daily executive report for testing")
    parser.add_argument("--sync-now", action="store_true", help="Run backup immediately")
    parser.add_argument("--daemon", action="store_true", help="Run as background daemon scheduler")
    args = parser.parse_args()

    if args.check_only:
        can, stat = run_health_guard()
        print(f"Health Check Result: {stat} (Can Proceed: {can})")
    elif args.docker_backup_now:
        success, msg = execute_docker_gfs_backup()
        print(f"Docker Backup Result: {success} -> {msg}")
    elif args.test_report:
        targets = get_backup_targets()
        report = generate_daily_executive_report("測試 (0 分 0 秒)", True, True, targets, "✅ 已封存 (496 MB，GFS 階梯保留中)")
        print(report)
        success = send_telegram(report)
        print(f"Telegram Send Result: {success}")
    elif args.sync_now:
        execute_backup()
    elif args.daemon:
        run_daemon()
    else:
        parser.print_help()
