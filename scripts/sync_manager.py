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
import urllib.request
import urllib.parse
from datetime import datetime

# ================= Configuration =================
CONFIG_PATH = os.environ.get("RCLONE_CONFIG", "/config/rclone/rclone.conf")
LOG_DIR = os.environ.get("LOG_DIR", "/logs")
TG_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FREE_THRESHOLD_GB = float(os.environ.get("FREE_THRESHOLD_GB", "100.0"))
SYNC_SCHEDULE_TIME = os.environ.get("SYNC_SCHEDULE_TIME", "02:00")  # HH:MM format
DEFAULT_DATA_FOLDERS = ["1000", "1001", "1002", "@team"]

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

# ================= Sync Logic =================
def execute_backup():
    """Perform Phase 1 and Phase 2 backup"""
    logging.info("Starting Backup Workflow...")
    start_time = time.time()
    
    # 1. Health Guard Pre-check
    can_proceed, status = run_health_guard()
    if not can_proceed:
        logging.warning(f"Health guard blocked backup with status: {status}")
        return

    # 2. Phase 1: NAS -> od1_crypt
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

    od1_remotes = parse_union_upstreams("od1_union")
    od2_remotes = parse_union_upstreams("od2_union")
    od1_free_total = sum(check_remote_quota(r)["free_gb"] for r in od1_remotes)
    od2_free_total = sum(check_remote_quota(r)["free_gb"] for r in od2_remotes)

    if phase2_success:
        summary_msg = (
            "🎉 <b>【fnOS 備份系統 - 每日雙集群同步完成】</b>\n\n"
            + "<b>階段一（NAS ➜ OD1 加密備份）：</b> 成功\n"
            + "\n".join(phase1_details) + "\n\n"
            + "<b>階段二（OD1 ➜ OD2 密文鏡像）：</b> 成功\n\n"
            + f"⏱️ <b>總耗時：</b> {duration_str}\n"
            + f"📊 <b>OD1 池剩餘：</b> {od1_free_total:.1f} GB\n"
            + f"📊 <b>OD2 池剩餘：</b> {od2_free_total:.1f} GB\n"
            + "🛡️ 3-2-1 雙雲端異地副本狀態完整！"
        )
    else:
        summary_msg = (
            "⚠️ <b>【fnOS 備份系統 - 部分完成提醒】</b>\n\n"
            + "<b>階段一（NAS ➜ OD1）：</b> ✅ 成功\n"
            + "<b>階段二（OD1 ➜ OD2 鏡像）：</b> ❌ 失敗 (請檢查 phase2 日誌)\n"
            + f"⏱️ <b>總耗時：</b> {duration_str}\n"
        )
    send_telegram(summary_msg)

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
    parser.add_argument("--sync-now", action="store_true", help="Run backup immediately")
    parser.add_argument("--daemon", action="store_true", help="Run as background daemon scheduler")
    args = parser.parse_args()

    if args.check_only:
        can, stat = run_health_guard()
        print(f"Health Check Result: {stat} (Can Proceed: {can})")
    elif args.sync_now:
        execute_backup()
    elif args.daemon:
        run_daemon()
    else:
        parser.print_help()
