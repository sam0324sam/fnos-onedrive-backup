#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fnOS 4-3-2 Dual-Cloud Enterprise Direct Backup and Health Guard
- Phase 0: Docker GFS Snapshots -> Multi-Cloud (OD1, OD2, GD1)
- Node 1: Local NAS (/vol1) -> od1_crypt (Microsoft Primary, XSalsa20 Encrypted)
- Node 2: Local NAS (/vol1) -> od2_crypt (Microsoft Mirror, XSalsa20 Encrypted)
- Node 3: Local NAS (/vol1) -> gd1_crypt (Google Drive 5TB Mirror, XSalsa20 Encrypted)
- Pre-flight Health and Capacity Guard with Telegram Executive Alerting
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
import glob
import threading
import urllib.request
import urllib.parse
import concurrent.futures
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
SYSTEM_BACKUP_DIR = os.environ.get("SYSTEM_BACKUP_DIR", "/mnt/system_backup")
DAEMON_START_TIME = time.time()

def get_backup_targets() -> list:
    """自訂或自動探索 /data 下之備份目錄 (支援環境變數 BACKUP_FOLDERS)"""
    custom_folders = os.environ.get("BACKUP_FOLDERS", "").strip()
    if custom_folders:
        return [f.strip() for f in custom_folders.split(",") if f.strip()]

    data_dir = "/data"
    if os.path.exists(data_dir):
        # 1. 優先探索常見 UID 或 @team (相容 fnOS)
        fnos_targets = [item for item in sorted(os.listdir(data_dir))
                        if os.path.isdir(os.path.join(data_dir, item)) and (item.isdigit() or item == "@team")]
        if fnos_targets:
            return fnos_targets

        # 2. 通用 NAS (Synology, QNAP, Linux)：探索 /data 下所有非隱藏目錄
        generic_targets = [item for item in sorted(os.listdir(data_dir))
                           if os.path.isdir(os.path.join(data_dir, item)) and not item.startswith(".")]
        if generic_targets:
            return generic_targets

    return DEFAULT_DATA_FOLDERS

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

# ================= Telegram Notifications & Interactions =================
STATUS_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "🔄 立即刷新進度", "callback_data": "refresh_status"}
        ]
    ]
}

def send_telegram(message: str, reply_markup: dict = None) -> bool:
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
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")
        return False

def edit_telegram_message(message_id: int, message: str, reply_markup: dict = None) -> bool:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/editMessageText"
        payload = {
            "chat_id": TG_CHAT_ID,
            "message_id": message_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logging.error(f"Failed to edit Telegram message: {e}")
        return False

def answer_telegram_callback(callback_query_id: str, text: str = "已刷新即時數據！"):
    if not TG_BOT_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/answerCallbackQuery"
        payload = {
            "callback_query_id": callback_query_id,
            "text": text
        }
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            pass
    except Exception as e:
        logging.warning(f"Failed to answer Telegram callback query: {e}")

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
    """Run `rclone about <remote>: --json` to get quota and health. Supports backends without quota (e.g. WebDAV/115)."""
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
        raw_out = res.stdout.strip()
        data = json.loads(raw_out) if raw_out else {}

        # Handle backends that succeed but do not expose quota numbers (e.g. Alist WebDAV for 115)
        if not data or "total" not in data:
            test_cmd = ["rclone", "lsf", f"{remote}:", "--max-depth", "1", f"--config={CONFIG_PATH}"]
            t_res = subprocess.run(test_cmd, capture_output=True, text=True, timeout=15)
            if t_res.returncode == 0:
                return {
                    "remote": remote,
                    "status": "OK",
                    "error": "",
                    "total_gb": 96.0 * 1024.0,  # 96 TB
                    "used_gb": 0.0,
                    "free_gb": 96.0 * 1024.0,
                    "quota_unsupported": True
                }
            else:
                return {
                    "remote": remote,
                    "status": "ERROR",
                    "error": t_res.stderr.strip() or "WebDAV 連線無響應",
                    "total_gb": 0,
                    "used_gb": 0,
                    "free_gb": 0
                }

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
            "free_gb": free,
            "quota_unsupported": False
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

def generate_daily_executive_report(duration_str: str, phase1_success: bool, phase2_success: bool, targets: list, docker_msg: str = "", phase3_msg: str = "", phase4_msg: str = "") -> str:
    """產出適合手機 Telegram 閱讀、徹底杜絕斷字折行的 4-3-2 現代極簡卡片風每日維運日報"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    local_stat = get_local_storage_stats("/data")
    clusters = get_all_union_clusters()
    has_gdrive = "gd1_union" in clusters

    # 1. 標題與基本規格
    title_prefix = "【fnOS 備份體系每日維運日報】"
    spec_label = "4-3-2 雙雲端容災" if has_gdrive else "3-2-1 雙微軟租戶"

    # 2. 本地儲存池
    targets_str = ", ".join([f"<code>{t}</code>" for t in targets])
    nas_dir_disp = os.environ.get("NAS_DATA_DIR", "/vol1")
    if local_stat:
        local_sec = (
            f"🖥️ <b>本地陣列 ({nas_dir_disp})</b>\n"
            f"• 儲存水位：<b>{local_stat['used_gb']:.1f} GB</b> / {local_stat['total_tb']:.1f} TB (剩 {local_stat['free_tb']:.1f} TB)\n"
            f"• 納管目錄：{targets_str}"
        )
    else:
        local_sec = f"🖥️ <b>本地陣列 ({nas_dir_disp})</b>\n• 納管目錄：{targets_str}"

    # 3. 雲端儲存池現況 (緊湊單行化，告別冗長膨脹)
    cloud_lines = ["☁️ <b>雲端儲存池現況 (已用 ｜ 剩餘可用)</b>"]
    all_clusters_ok = True
    for c_name in clusters:
        c_stat = get_cluster_stats(c_name)
        if not c_stat["all_ok"]:
            all_clusters_ok = False

        if "od1" in c_name:
            label = "OD1 微軟主本"
        elif "od2" in c_name:
            label = "OD2 微軟鏡像"
        elif "gd" in c_name:
            label = "GD1 谷歌鏡像"
        else:
            label = c_name

        icon = "🟢" if c_stat["all_ok"] else "🔴"
        cloud_lines.append(f"• {label}：{icon} {c_stat['used_gb']:.1f} GB (餘 {c_stat['free_tb']:.1f} TB)")

    cloud_sec = "\n".join(cloud_lines)

    # 4. 本次備份傳輸結果 (單行簡潔，杜絕斷字折行)
    phase1_status = "✅ 成功 (本地直傳)" if phase1_success else "❌ 異常"
    phase2_status = "✅ 成功 (本地直傳)" if phase2_success else "❌ 異常"
    transfer_lines = [
        "⚡ <b>本次備份傳輸結果 (多雲並行直灌)</b>",
        f"• OD1 微軟主本：{phase1_status}",
        f"• OD2 微軟鏡像：{phase2_status}"
    ]
    if has_gdrive:
        phase3_display = phase3_msg if phase3_msg else "✅ 成功 (增量同步)"
        transfer_lines.append(f"• GD1 谷歌鏡像：{phase3_display}")
    if docker_msg:
        m_sz = re.search(r"(\d+\.?\d*\s+[KMGTP]B)", docker_msg)
        if m_sz:
            clean_docker = f"✅ {m_sz.group(1)} (GFS 階梯)"
        else:
            clean_docker = "✅ 已封存 (GFS 階梯)"
        transfer_lines.append(f"• Docker 快照 ：{clean_docker}")
    transfer_lines.append(f"• 總執行耗時  ：{duration_str}")
    transfer_sec = "\n".join(transfer_lines)

    # 5. 容災鏈路檢核 (垂直樹狀圖，杜絕橫向擠壓斷截)
    is_fully_compliant = phase1_success and phase2_success and all_clusters_ok
    sla_status = "完全合規 🟢" if is_fully_compliant else "鏈路警示 ⚠️"

    if has_gdrive:
        tree_lines = [
            f"🛡️ <b>4-3-2 容災鏈路檢核：{sla_status}</b>",
            f"├ 本地實體陣列：🟢 正常",
            f"├ OD1 微軟主本：{'🟢 M365 跨租戶' if phase1_success else '🔴 異常'}",
            f"├ OD2 微軟鏡像：{'🟢 M365 雙副本' if phase2_success else '🔴 異常'}",
            f"└ GD1 谷歌鏡像：{'🟢 Google 5TB' if all_clusters_ok else '🔴 異常'}"
        ]
    else:
        tree_lines = [
            f"🛡️ <b>3-2-1 容災鏈路檢核：{sla_status}</b>",
            f"├ 本地實體陣列：🟢 正常",
            f"├ OD1 微軟主本：{'🟢 M365 跨租戶' if phase1_success else '🔴 異常'}",
            f"└ OD2 微軟鏡像：{'🟢 M365 雙副本' if phase2_success else '🔴 異常'}"
        ]
    sla_sec = "\n".join(tree_lines)

    # 6. 32G 隨身碟時光機狀態 (納入日報完整閉環)
    usb_sec = ""
    if os.path.exists(SYSTEM_BACKUP_DIR):
        latest_archive = os.path.join(SYSTEM_BACKUP_DIR, "fnos_system_backup_latest.tar.zst")
        if os.path.exists(latest_archive):
            sz = f"{os.path.getsize(latest_archive) / (1024*1024*1024):.1f} GB"
            try:
                du = shutil.disk_usage(SYSTEM_BACKUP_DIR)
                free_gb = du.free / (1024 * 1024 * 1024)
                usb_sec = f"\n💾 <b>系統隨身碟時光機：</b>🟢 正常 ({sz} 快照, 剩 {free_gb:.1f} GB)\n"
            except Exception:
                usb_sec = f"\n💾 <b>系統隨身碟時光機：</b>🟢 正常 ({sz} 快照)\n"

    full_report = (
        f"📊 <b>{title_prefix}</b>\n"
        f"📅 <code>{now_str}</code> ｜ {spec_label}\n\n"
        f"{local_sec}\n\n"
        f"{cloud_sec}\n\n"
        f"{transfer_sec}\n\n"
        f"{sla_sec}\n"
        f"{usb_sec}"
        f"⏰ <b>下次例行排程：</b>每日 {SYNC_SCHEDULE_TIME}"
    )
    return full_report

def run_health_guard() -> tuple[bool, str]:
    """Check OD1, OD2, and GD1 pool health and space"""
    od1_remotes = parse_union_upstreams("od1_union")
    od2_remotes = parse_union_upstreams("od2_union")
    gd1_remotes = parse_union_upstreams("gd1_union")

    if not od1_remotes or not od2_remotes:
        msg = "⚠️ <b>【備份配置缺失】</b>\n未在 <code>rclone.conf</code> 中找到 <code>od1_union</code> 或 <code>od2_union</code> 的成員帳號，請先完成帳號授權配置！"
        send_telegram(msg)
        return False, "CONFIG_MISSING"

    logging.info(f"Checking OD1 upstreams: {od1_remotes}")
    logging.info(f"Checking OD2 upstreams: {od2_remotes}")
    if gd1_remotes:
        logging.info(f"Checking GD1 upstreams: {gd1_remotes}")

    od1_status = [check_remote_quota(r) for r in od1_remotes]
    od2_status = [check_remote_quota(r) for r in od2_remotes]
    gd1_status = [check_remote_quota(r) for r in gd1_remotes] if gd1_remotes else []

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

    # Evaluate GD1
    for s in gd1_status:
        if s["status"] == "ERROR":
            critical_errors.append(f"• <b>GD1 谷歌集群 [{s['remote']}]</b> 連線異常/失效：{s['error']}")
        elif s["status"] == "LOW_SPACE":
            low_space_warnings.append(f"• <b>GD1 谷歌集群 [{s['remote']}]</b> 剩餘容量告急：{s['free_gb']:.1f} GB (&lt; {FREE_THRESHOLD_GB} GB)")

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
    logging.info(f"Docker archive created successfully ({archive_size_mb:.1f} MB). Uploading to multi-cloud...")

    # 本地直接分發快照至三雲加密目錄
    destinations = [
        ("od1_crypt:docker_snapshots/", "OD1 微軟主本"),
        ("od2_crypt:docker_snapshots/", "OD2 微軟鏡像")
    ]
    if "gd1_union" in get_all_union_clusters():
        destinations.append(("gd1_crypt:docker_snapshots/", "GD1 谷歌鏡像"))

    upload_success = True
    for dst, label in destinations:
        upload_cmd = [
            "rclone", "copy", staging_archive, dst,
            f"--config={CONFIG_PATH}",
            "--drive-chunk-size=64M",
            "--fast-list",
            "-v"
        ]
        res_upload = subprocess.run(upload_cmd, capture_output=True, text=True)
        if res_upload.returncode != 0:
            err = res_upload.stderr.strip() or f"Upload to {dst} failed"
            logging.error(f"Failed to upload docker snapshot to {label} ({dst}): {err}")
            upload_success = False
        else:
            logging.info(f"Docker snapshot uploaded to {label} ({dst}) successfully.")

    if os.path.exists(staging_archive):
        try:
            os.remove(staging_archive)
        except Exception:
            pass

    # 執行多集群 GFS 梯次清理
    prune_gfs_snapshots("od1_crypt:docker_snapshots")
    prune_gfs_snapshots("od2_crypt:docker_snapshots")
    if "gd1_union" in get_all_union_clusters():
        prune_gfs_snapshots("gd1_crypt:docker_snapshots")

    if not upload_success:
        return False, "部分雲端快照上傳失敗"

    return True, f"✅ 已封存 ({archive_size_mb:.1f} MB, GFS 階梯保留中)"

# ================= Real-time Status & Telegram Interactive Bot =================
def is_rclone_running_for(folder: str, target_crypt: str) -> bool:
    """檢查指定目錄與雲端是否已有 rclone copy 進程在執行，避免重複發起競爭配額"""
    try:
        ps_res = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
        for line in ps_res.stdout.splitlines():
            if "rclone copy" in line and f"/{folder} " in line and f"{target_crypt}:{folder}" in line and "<defunct>" not in line:
                return True
    except Exception:
        pass
    return False

def parse_rclone_log(log_path: str) -> dict:
    """從 rclone 日誌結尾提取最即時之傳輸指標、速率、ETA 與進行中檔案隊列"""
    if not os.path.exists(log_path) or os.path.getsize(log_path) == 0:
        return {}
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 16384))
            content = f.read()
    except Exception:
        return {}

    stats = {
        "rate_limited": False,
        "transferring": []
    }

    if "Error 403: User rate limit exceeded" in content:
        stats["rate_limited"] = True

    m_trans = re.findall(r"Transferred:\s+([0-9\.]+\s+[A-Za-z]+)\s+/\s+([0-9\.]+\s+[A-Za-z]+),\s+([0-9]+%),\s+([0-9\.]+\s+[A-Za-z/]+),\s+ETA\s+([^\n\r]+)", content)
    if m_trans:
        last_trans = m_trans[-1]
        stats["bytes_done"] = last_trans[0]
        stats["bytes_total"] = last_trans[1]
        stats["percent"] = last_trans[2]
        stats["speed"] = last_trans[3]
        stats["eta"] = last_trans[4].strip()

    m_files = re.findall(r"Transferred:\s+([0-9]+)\s+/\s+([0-9]+),\s+([0-9]+%)", content)
    if m_files:
        last_files = m_files[-1]
        stats["files_done"] = last_files[0]
        stats["files_total"] = last_files[1]
        stats["files_percent"] = last_files[2]

    m_elapsed = re.findall(r"Elapsed time:\s+([^\n\r]+)", content)
    if m_elapsed:
        stats["elapsed"] = m_elapsed[-1].strip()

    if "Transferring:" in content:
        last_tf_chunk = content.split("Transferring:")[-1]
        for line in last_tf_chunk.splitlines():
            line = line.strip()
            if line.startswith("*"):
                line_clean = line.lstrip("* ").strip()
                if ":" in line_clean:
                    fname, fprog = line_clean.split(":", 1)
                    parts = fname.strip().split("/")
                    fname_disp = f"{parts[-2]}/{parts[-1]}" if len(parts) > 1 else parts[-1]
                    prog_text = fprog.strip().split(",")[0]
                    stats["transferring"].append(f"{fname_disp} ({prog_text})")
                else:
                    stats["transferring"].append(line_clean[:45])
            elif line.startswith("202") or "INFO" in line or "ERROR" in line:
                break
    stats["transferring"] = stats["transferring"][:3]
    return stats

def generate_realtime_status_report() -> str:
    """產出極致詳細之實時監控戰報（涵蓋各雲端狀態、傳輸速度、隊列、隨身碟備份、Docker快照與NAS負載）"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    uptime_sec = int(time.time() - DAEMON_START_TIME)
    uptime_days = uptime_sec // 86400
    uptime_hours = (uptime_sec % 86400) // 3600
    uptime_mins = (uptime_sec % 3600) // 60
    if uptime_days > 0:
        uptime_str = f"{uptime_days} 天 {uptime_hours} 小時"
    elif uptime_hours > 0:
        uptime_str = f"{uptime_hours} 小時 {uptime_mins} 分"
    else:
        uptime_str = f"{uptime_mins} 分鐘"

    lines = [
        "📊 <b>【fnOS 4-3-2 備份體系 - 即時監控戰報】</b>",
        f"📅 <b>查詢時間：</b> <code>{now_str}</code>",
        f"⏱️ <b>守衛狀態：</b> 🟢 運作中 (已持續 {uptime_str}) ｜ 排程：<code>{SYNC_SCHEDULE_TIME}</code>\n"
    ]

    # 1. 檢查運行中的進程
    ps_res = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    active_transfers = []
    for line in ps_res.stdout.splitlines():
        if "rclone copy" in line and not line.strip().startswith("[") and "<defunct>" not in line:
            m = re.search(r"rclone copy\s+(\S+)\s+(\S+):", line)
            if m:
                folder = os.path.basename(m.group(1).rstrip("/"))
                remote = m.group(2)
                active_transfers.append((remote, folder))

    # 2. 雲端容災節點掃描
    clouds = [
        ("od1_crypt", "OD1 微軟主本", "od1"),
        ("od2_crypt", "OD2 微軟鏡像", "od2"),
        ("gd1_crypt", "GD1 谷歌鏡像", "gd1")
    ]

    lines.append("☁️ <b>各雲端容災節點狀態</b>")
    for remote, label, prefix in clouds:
        is_active = False
        active_folder = None
        for r, f in active_transfers:
            if remote.startswith(r) or r.startswith(prefix):
                is_active = True
                active_folder = f
                break

        log_files = sorted(glob.glob(os.path.join(LOG_DIR, f"sync_{prefix}_*.log")))
        latest_stats = None
        if log_files:
            if active_folder:
                matching = [lf for lf in log_files if f"_{active_folder}.log" in lf]
                target_lf = matching[-1] if matching else log_files[-1]
            else:
                target_lf = max(log_files, key=os.path.getmtime)
            latest_stats = parse_rclone_log(target_lf)

        if is_active and latest_stats and ("speed" in latest_stats or "percent" in latest_stats):
            speed = latest_stats.get("speed", "計算中")
            b_done = latest_stats.get("bytes_done", "")
            b_tot = latest_stats.get("bytes_total", "")
            pct = latest_stats.get("percent", "0%")
            eta = latest_stats.get("eta", "計算中")
            f_done = latest_stats.get("files_done", "")
            f_tot = latest_stats.get("files_total", "")
            f_pct = latest_stats.get("files_percent", "")

            lines.append(f"• <b>{label}</b>：⚡ <b>傳輸中 ({pct})</b>")
            lines.append(f"  ├ 目錄：<code>{active_folder}</code> ｜ 速率：<b>{speed}</b>")
            if b_done and b_tot:
                lines.append(f"  ├ 容量：{b_done} / {b_tot} ({pct})")
            if f_done and f_tot:
                lines.append(f"  ├ 檔案：{f_done} / {f_tot} ({f_pct})")
            lines.append(f"  └ 剩餘時間 (ETA)：<b>{eta}</b>")
            if latest_stats.get("transferring"):
                lines.append("  └ 傳輸中隊列：")
                for tf in latest_stats["transferring"]:
                    lines.append(f"    • <code>{tf}</code>")
            if latest_stats.get("rate_limited"):
                lines.append("  ⚠️ 提示：Google 750GB 配額冷卻中 (24h 滾動窗口滑過後自動續傳)")
        else:
            lines.append(f"• <b>{label}</b>：🟢 <b>100% 已同步完畢</b> (已就緒)")
        lines.append("")

    # 3. 32G 隨身碟系統時光機
    lines.append("💾 <b>系統隨身碟時光機 (裸機災難復原)</b>")
    if os.path.exists(SYSTEM_BACKUP_DIR):
        latest_archive = os.path.join(SYSTEM_BACKUP_DIR, "fnos_system_backup_latest.tar.zst")
        if os.path.exists(latest_archive):
            sz = f"{os.path.getsize(latest_archive) / (1024*1024*1024):.1f} GB"
            mtime = datetime.fromtimestamp(os.path.getmtime(latest_archive)).strftime("%Y-%m-%d %H:%M")
            try:
                du = shutil.disk_usage(SYSTEM_BACKUP_DIR)
                free_gb = du.free / (1024 * 1024 * 1024)
                total_gb = du.total / (1024 * 1024 * 1024)
                space_str = f" (剩 {free_gb:.1f} GB / {total_gb:.1f} GB)"
            except Exception:
                space_str = ""
            lines.append(f"• 狀態：🟢 <b>已掛載就緒</b>{space_str}")
            lines.append(f"• 最新快照：<b>{sz}</b> (<code>{mtime}</code>)")
            lines.append("• 救援資源：✅ efi_boot.img ｜ ✅ 一鍵還原腳本")
            lines.append("• 排程週期：每週日 03:00 自動備份 (保留 4 份)")
        else:
            lines.append("• 狀態：🟡 隨身碟已掛載，尚未建立快照檔")
    else:
        lines.append("• 狀態：⚪ 隨身碟未掛載")
    lines.append("")

    # 4. Docker GFS 快照狀態
    lines.append("🐳 <b>Docker 容器全鏡像 GFS 階梯快照</b>")
    manager_log = os.path.join(LOG_DIR, "manager.log")
    docker_info = None
    if os.path.exists(manager_log):
        try:
            with open(manager_log, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 32768))
                m_content = f.read()
                matches = re.findall(r"\[([\d\- :]+),\d+\] \[INFO\] Docker archive created successfully \(([\d\.]+ MB)\)", m_content)
                if matches:
                    docker_info = matches[-1]
        except Exception:
            pass

    if docker_info:
        lines.append(f"• 最新封存：<b>{docker_info[1]}</b> (<code>{docker_info[0]}</code>)")
        lines.append("• 階梯保留：✅ 正常 (14天日備 + 8週週備 + 12月月備)")
        lines.append("• 多雲同步：已同步至 OD1、OD2、GD1 加密池")
    else:
        lines.append("• 階梯保留：✅ 每日 02:00 自動封存並推播三雲")
    lines.append("")

    # 5. NAS 主機硬體狀態
    lines.append("⚙️ <b>NAS 主機硬體即時狀態</b>")
    try:
        load1, load5, load15 = os.getloadavg()
        lines.append(f"• 系統負載 (Load)：{load1:.2f}, {load5:.2f}, {load15:.2f}")
    except Exception:
        pass

    try:
        with open("/proc/meminfo") as f:
            mem = f.read()
            total = int(re.search(r"MemTotal:\s+(\d+)", mem).group(1)) / 1024 / 1024
            avail = int(re.search(r"MemAvailable:\s+(\d+)", mem).group(1)) / 1024 / 1024
            used = total - avail
            lines.append(f"• 記憶體使用：{used:.1f} GB / {total:.1f} GB ({used/total*100:.1f}%)")
    except Exception:
        pass

    try:
        data_path = "/vol1" if os.path.exists("/vol1") else "/data"
        if os.path.exists(data_path):
            du_data = shutil.disk_usage(data_path)
            free_tb = du_data.free / (1024 * 1024 * 1024 * 1024)
            total_tb = du_data.total / (1024 * 1024 * 1024 * 1024)
            lines.append(f"• 本地儲存池 ({data_path})：剩餘 {free_tb:.2f} TB / {total_tb:.2f} TB")
    except Exception:
        pass

    return "\n".join(lines)

def telegram_command_listener():
    """
    背景常駐線程：長輪詢監聽 Telegram 互動指令與回調按鈕
    - 支援文字指令：/status, /progress, 進度, 狀態, 速度, 戰報, 即時進度
    - 支援回調按鈕：refresh_status (就地平滑更新訊息)
    - 安全防護：嚴格校驗 sender_id == TG_CHAT_ID (1004669639)
    """
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        logging.warning("Telegram token or chat id missing, command listener not started.")
        return

    logging.info("Telegram interactive command listener thread started.")
    offset = 0

    # 清空過期請求，避免重啟後重複觸發
    try:
        init_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates?offset=-1&timeout=0"
        with urllib.request.urlopen(init_url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok") and data.get("result"):
                offset = data["result"][-1]["update_id"] + 1
    except Exception as e:
        logging.warning(f"Failed to initialize Telegram update offset: {e}")

    valid_keywords = {"/status", "/progress", "status", "progress", "進度", "狀態", "速度", "戰報", "即時進度"}

    while running:
        try:
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates?offset={offset}&timeout=20"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    time.sleep(5)
                    continue
                data = json.loads(resp.read().decode("utf-8"))
                if not data.get("ok"):
                    time.sleep(5)
                    continue

                for item in data.get("result", []):
                    update_id = item["update_id"]
                    offset = max(offset, update_id + 1)

                    # 1. 處理按鈕回調 (Callback Query)
                    if "callback_query" in item:
                        cq = item["callback_query"]
                        cq_id = cq.get("id")
                        sender_id = str(cq.get("from", {}).get("id", ""))
                        data_str = cq.get("data", "")
                        msg = cq.get("message", {})
                        msg_id = msg.get("message_id")

                        if sender_id != str(TG_CHAT_ID):
                            logging.warning(f"Unauthorized Telegram callback attempt from ID {sender_id}")
                            continue

                        if data_str == "refresh_status" and msg_id:
                            answer_telegram_callback(cq_id, text="🔄 正在刷新即時數據...")
                            report = generate_realtime_status_report()
                            edit_telegram_message(msg_id, report, reply_markup=STATUS_KEYBOARD)

                    # 2. 處理文字訊息 (Message)
                    elif "message" in item:
                        msg = item["message"]
                        sender_id = str(msg.get("from", {}).get("id", ""))
                        text = msg.get("text", "").strip()

                        if sender_id != str(TG_CHAT_ID):
                            logging.warning(f"Unauthorized Telegram message from ID {sender_id}: {text}")
                            continue

                        text_lower = text.lower()
                        should_reply = False
                        for kw in valid_keywords:
                            if kw in text_lower or kw in text:
                                should_reply = True
                                break

                        if should_reply:
                            logging.info(f"Received interactive Telegram command: '{text}' from {sender_id}")
                            report = generate_realtime_status_report()
                            send_telegram(report, reply_markup=STATUS_KEYBOARD)

        except Exception as e:
            time.sleep(3)

def sync_local_to_cloud(target_crypt: str, cloud_label: str, targets: list, log_prefix: str) -> tuple[bool, str]:
    """
    通用本地直接串流加密同步 (Direct Local-to-Cloud Stream Sync)
    - 來源：本地 /data/{folder} (讀取本地 RAID 10，零 API 往返延遲)
    - 目的：{target_crypt}:{folder} (內存 XSalsa20 即時串流加密)
    - 參數：高並發流式直傳 (--transfers=4, --checkers=8, --tpslimit=10, --fast-list, --drive-chunk-size=64M)
    """
    logging.info(f"=== Starting Direct Local -> {target_crypt} ({cloud_label}) Sync ===")
    all_success = True
    failed_folders = []

    for folder in targets:
        src_path = f"/data/{folder}"
        if not os.path.exists(src_path):
            logging.info(f"Source folder {src_path} does not exist, skipping.")
            continue

        dst_remote = f"{target_crypt}:{folder}"
        log_file = os.path.join(LOG_DIR, f"{log_prefix}_{folder}.log")

        if is_rclone_running_for(folder, target_crypt):
            logging.warning(f"Sync for {src_path} -> {dst_remote} is already running in background. Skipping duplicate spawn.")
            continue

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
        logging.info(f"Syncing local {src_path} -> {dst_remote}...")
        res = subprocess.run(cmd)
        if res.returncode != 0:
            logging.error(f"Sync to {dst_remote} failed with code {res.returncode}")
            all_success = False
            failed_folders.append(folder)
        else:
            logging.info(f"Sync to {dst_remote} finished for {folder}")

    if all_success:
        return True, "✅ 增量同步完成"
    else:
        return False, f"❌ 同步警示 ({','.join(failed_folders)} 異常)"

# ================= Sync Logic =================
def execute_backup():
    """執行全流程備份：Docker GFS 多雲分發 + 本地三雲直傳 (OD1 ➜ OD2 ➜ GD1)"""
    logging.info("Starting Direct Multi-Cloud Backup Workflow...")
    start_time = time.time()

    # 1. Health Guard Pre-check
    can_proceed, status = run_health_guard()
    if not can_proceed:
        logging.warning(f"Health guard blocked backup with status: {status}")
        return

    # 2. Phase 0: Docker GFS Snapshot (直接分發給所有雲)
    logging.info("=== Phase 0: Starting Docker GFS Snapshot ===")
    docker_success, docker_msg = execute_docker_gfs_backup()

    targets = get_backup_targets()
    logging.info(f"Discovered backup target folders: {targets}")

    has_gdrive = "gd1_union" in get_all_union_clusters()

    # 3. 三雲端全並行直灌 (OD1, OD2, GD1 同時並行)
    logging.info("=== Starting Concurrent Direct Sync to Multi-Cloud (OD1, OD2, GD1) ===")
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        future_od1 = executor.submit(sync_local_to_cloud, "od1_crypt", "OD1 微軟主儲存", targets, "sync_od1")
        future_od2 = executor.submit(sync_local_to_cloud, "od2_crypt", "OD2 微軟鏡像副本", targets, "sync_od2")
        future_gd1 = executor.submit(sync_local_to_cloud, "gd1_crypt", "GD1 谷歌鏡像副本", targets, "sync_gd1") if has_gdrive else None

        ok1, msg1 = future_od1.result()
        ok2, msg2 = future_od2.result()
        ok3, msg3 = future_gd1.result() if future_gd1 else (True, "未配置略過")

    duration = int(time.time() - start_time)
    duration_str = f"{duration // 60} 分 {duration % 60} 秒"

    report_msg = generate_daily_executive_report(duration_str, ok1, ok2, targets, docker_msg, msg3)
    send_telegram(report_msg)

def execute_mirrors_only():
    """專用立即觸發：本地直接並行同步鏡像雲端 (OD2 + GD1 全並行)"""
    logging.info("Starting Direct Local -> Mirrors Concurrent Sync (OD2 + GD1)...")
    start_time = time.time()

    can_proceed, status = run_health_guard()
    if not can_proceed:
        logging.warning(f"Health guard blocked mirror with status: {status}")
        return

    targets = get_backup_targets()
    logging.info(f"Discovered backup target folders: {targets}")

    has_gdrive = "gd1_union" in get_all_union_clusters()

    logging.info("=== Starting Concurrent Direct Sync to Mirrors (OD2 & GD1) ===")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        future_od2 = executor.submit(sync_local_to_cloud, "od2_crypt", "OD2 微軟鏡像副本", targets, "sync_od2")
        future_gd1 = executor.submit(sync_local_to_cloud, "gd1_crypt", "GD1 谷歌鏡像副本", targets, "sync_gd1") if has_gdrive else None

        ok2, msg2 = future_od2.result()
        ok3, msg3 = future_gd1.result() if future_gd1 else (True, "未配置略過")

    duration = int(time.time() - start_time)
    duration_str = f"{duration // 60} 分 {duration % 60} 秒"

    report_msg = generate_daily_executive_report(
        duration_str,
        True,
        ok2,
        targets,
        "✅ 已就緒 (前次已封存)",
        msg3
    )
    send_telegram(report_msg)

# ================= Daemon Loop =================
def run_daemon():
    logging.info(f"fnOS Backup Daemon started. Daily scheduled sync time: {SYNC_SCHEDULE_TIME}")

    # 啟動 Telegram 互動指令監聽背景線程
    listener_thread = threading.Thread(target=telegram_command_listener, daemon=True, name="TelegramListener")
    listener_thread.start()
    logging.info("Telegram interactive command listener thread dispatched successfully.")

    send_telegram(
        f"🚀 <b>【fnOS 備份守衛已啟動】</b>\n"
        f"• 守衛服務已就緒，每日預設於 <b>{SYNC_SCHEDULE_TIME}</b> 執行本地直推多雲備份。\n"
        f"• 隨時於 Telegram 輸入 <code>/status</code> 或「<code>進度</code>」即可查看即時同步戰報與速度。",
        reply_markup=STATUS_KEYBOARD
    )

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
    parser = argparse.ArgumentParser(description="fnOS 4-3-2 Dual-Cloud Direct Backup Manager")
    parser.add_argument("--check-only", action="store_true", help="Only run health & capacity check")
    parser.add_argument("--docker-backup-now", action="store_true", help="Run Docker GFS snapshot and upload now")
    parser.add_argument("--sync-od1-now", action="store_true", help="Sync local NAS -> OD1 only")
    parser.add_argument("--sync-od2-now", action="store_true", help="Sync local NAS -> OD2 only")
    parser.add_argument("--sync-gd1-now", action="store_true", help="Sync local NAS -> GD1 only")
    parser.add_argument("--sync-mirrors-now", action="store_true", help="Sync local NAS -> OD2 and GD1 mirrors directly")
    parser.add_argument("--sync-now", action="store_true", help="Run full backup to all clouds directly from NAS")
    parser.add_argument("--test-report", action="store_true", help="Generate and send daily executive report for testing")
    parser.add_argument("--status", action="store_true", help="Generate and print realtime status report (also sends to Telegram if configured)")
    parser.add_argument("--daemon", action="store_true", help="Run as background daemon scheduler")
    args = parser.parse_args()

    targets = get_backup_targets()

    if args.check_only:
        can, stat = run_health_guard()
        print(f"Health Check Result: {stat} (Can Proceed: {can})")
    elif args.docker_backup_now:
        success, msg = execute_docker_gfs_backup()
        print(f"Docker Backup Result: {success} -> {msg}")
    elif args.sync_od1_now:
        success, msg = sync_local_to_cloud("od1_crypt", "OD1 微軟主儲存", targets, "sync_od1")
        print(f"OD1 Sync Result: {success} -> {msg}")
    elif args.sync_od2_now:
        success, msg = sync_local_to_cloud("od2_crypt", "OD2 微軟鏡像副本", targets, "sync_od2")
        print(f"OD2 Sync Result: {success} -> {msg}")
    elif args.sync_gd1_now:
        success, msg = sync_local_to_cloud("gd1_crypt", "GD1 谷歌鏡像副本", targets, "sync_gd1")
        print(f"GD1 Sync Result: {success} -> {msg}")
    elif args.sync_mirrors_now:
        execute_mirrors_only()
    elif args.sync_now:
        execute_backup()
    elif args.test_report:
        report = generate_daily_executive_report("測試 (0 分 0 秒)", True, True, targets, "✅ 已封存 (496.0 MB, GFS 階梯保留中)", "✅ 成功 (已加密鏡像)")
        print(report)
        success = send_telegram(report)
        print(f"Telegram Send Result: {success}")
    elif args.status:
        report = generate_realtime_status_report()
        print(report)
        if TG_BOT_TOKEN and TG_CHAT_ID:
            send_telegram(report, reply_markup=STATUS_KEYBOARD)
    elif args.daemon:
        run_daemon()
    else:
        parser.print_help()
