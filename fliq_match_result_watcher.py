#!/usr/bin/env python3
"""
Fliq Match Result watcher + Telegram bot (heartbeat-only automatic updates).
Adds Telegram command suggestions (setMyCommands) at startup so users see handlers when typing '/'.
"""

import os
import json
import re
import time
import threading
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

import requests
from dotenv import load_dotenv

# telegram
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

# -------------------------
# Config
# -------------------------
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID_RAW = os.getenv("TELEGRAM_CHAT_ID", "").strip()
if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN not set in environment or .env")
if not ADMIN_CHAT_ID_RAW:
    raise SystemExit("TELEGRAM_CHAT_ID not set in environment or .env")
try:
    ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_RAW)
except Exception:
    raise SystemExit("TELEGRAM_CHAT_ID must be numeric")

# core timing
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "900"))  # default 15 min
VALIDATION_INTERVAL_SECONDS = int(os.getenv("VALIDATION_INTERVAL_SECONDS", str(24 * 3600)))  # default 24h

# heartbeat config (default ON)
HEARTBEAT_ENABLED = os.getenv("HEARTBEAT_ENABLED", "true").lower() in ("1", "true", "yes")
HEARTBEAT_TEXT_NO_NEW = os.getenv("HEARTBEAT_TEXT_NO_NEW", "CHECK no new Match Result matches")

# files / limits
REMOVED_LOG_PATH = Path(os.getenv("REMOVED_LOG_PATH", "removed_markets.log"))
MATCHES_FILE = Path(os.getenv("MATCHES_FILE", "upcoming_match_results.json"))
REFERRAL_CODE = os.getenv("REFERRAL_CODE", "aD6VfTQkAW")
BASE_MARKET_URL = os.getenv("BASE_MARKET_URL", "https://www.fliq.one/#/multi-question")
FETCH_LIMIT = int(os.getenv("FETCH_LIMIT", "1000"))
REQUIRE_APPROVED = True
MAX_LINKS_IN_MESSAGE = int(os.getenv("MAX_LINKS_IN_MESSAGE", "30"))

# -------------------------
# Helpers
# -------------------------
def now_iso(ts: Optional[float] = None) -> str:
    d = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now()
    return d.strftime("%Y-%m-%d %H:%M:%S")

def utc_iso_from_unix(ts: Optional[int]) -> str:
    try:
        if not ts:
            return ""
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""

def slugify(text: str) -> str:
    s = (text or "").lower().strip()
    s = s.replace(":", " ")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")

def log_console(msg: str) -> None:
    print(f"[{now_iso()}] {msg}")

# -------------------------
# IO
# -------------------------
def load_snapshot() -> Dict[str, Any]:
    if not MATCHES_FILE.exists():
        return {}
    try:
        with MATCHES_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        log_console("[WARN] snapshot JSON invalid; ignoring")
        return {}
    except Exception as e:
        log_console(f"[WARN] failed to load snapshot: {e}")
        return {}

def save_snapshot(snapshot: Dict[str, Any]) -> None:
    try:
        with MATCHES_FILE.open("w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, ensure_ascii=False, sort_keys=True)
    except Exception as e:
        log_console(f"[ERROR] saving snapshot failed: {e}")

def append_removed_log(entries: List[Tuple[str, str]]) -> None:
    try:
        with REMOVED_LOG_PATH.open("a", encoding="utf-8") as f:
            for header, reason in entries:
                f.write(f"{now_iso()} | REMOVED | {header} | reason={reason}\n")
    except Exception as e:
        log_console(f"[WARN] failed writing removed_log: {e}")

def build_market_url(match: Dict[str, Any]) -> str:
    slug = match.get("slug")
    mid = match.get("multi_question_id")
    if not slug or not mid:
        return "(URL unavailable)"
    return f"{BASE_MARKET_URL}/{slug}-{mid}?referral={REFERRAL_CODE}"

# -------------------------
# Fliq API
# -------------------------
def fetch_questions(limit: int = FETCH_LIMIT) -> List[Dict[str, Any]]:
    params = [
        ("select", "questionId"),
        ("select", "lotSize"),
        ("select", "tickSize"),
        ("select", "decimal"),
        ("select", "isSettled"),
        ("select", "settlementPrice"),
        ("select", "contractAddress"),
        ("select", "yesTokenMarketId"),
        ("select", "noTokenMarketId"),
        ("select", "blockchainMetadata"),
        ("metadataSelect", "parentQuestionId"),
        ("metadataSelect", "questionHeader"),
        ("metadataSelect", "parentQuestionHeader"),
        ("metadataSelect", "questionHeaderExpanded"),
        ("metadataSelect", "category"),
        ("metadataSelect", "tags"),
        ("metadataSelect", "questionEndTime"),
        ("metadataSelect", "imgUrl"),
        ("metadataSelect", "tweetId"),
        ("metadataSelect", "partnerTag"),
        ("metadataSelect", "isMadeByTemplate"),
        ("limit", str(limit)),
    ]
    resp = requests.get("https://auto-question.fliq.one/question", params=params, timeout=20)
    resp.raise_for_status()
    j = resp.json()
    return j.get("questions", [])

def is_upcoming_match_option(q: Dict[str, Any]) -> bool:
    bm = q.get("blockchainMetadata") or {}
    category = (bm.get("category") or "").lower()
    headers = " ".join([
        str(bm.get("questionHeader") or ""),
        str(bm.get("parentQuestionHeader") or ""),
        str(bm.get("questionHeaderExpanded") or "")
    ]).lower()
    if category != "football":
        return False
    if "match result" not in headers:
        return False
    if q.get("isSettled") is True:
        return False
    try:
        end_ts = int(bm.get("questionEndTime") or 0)
    except Exception:
        return False
    return end_ts > int(datetime.now(timezone.utc).timestamp())

def build_live_snapshot(require_approved: bool = REQUIRE_APPROVED) -> Dict[str, Any]:
    questions = fetch_questions(limit=FETCH_LIMIT)
    candidates = [q for q in questions if is_upcoming_match_option(q)]
    groups: Dict[str, Dict[str, Any]] = {}
    for q in candidates:
        bm = q.get("blockchainMetadata") or {}
        parent_header = bm.get("parentQuestionHeader") or ""
        parent_id = bm.get("parentQuestionId")
        if not parent_header or not parent_id:
            continue
        key = parent_header.strip()
        try:
            end_ts = int(bm.get("questionEndTime") or 0)
        except Exception:
            end_ts = None
        if key not in groups:
            groups[key] = {
                "match_header": parent_header,
                "slug": slugify(parent_header),
                "multi_question_id": str(parent_id),
                "options": [],
                "questionEndTime": end_ts,
                "questionEndTime_iso": utc_iso_from_unix(end_ts) if end_ts else "",
                "is_approved": False,
            }
        yes = q.get("yesTokenMarketId")
        no = q.get("noTokenMarketId")
        tradable = bool(yes) and bool(no) and str(yes) != "0" and str(no) != "0"
        groups[key]["options"].append({
            "questionId": str(q.get("questionId")),
            "title": (bm.get("questionHeaderExpanded") or bm.get("questionHeader") or "").strip(),
            "yesTokenMarketId": str(yes) if yes else "",
            "noTokenMarketId": str(no) if no else "",
            "option_is_tradable": tradable,
        })
        if tradable:
            groups[key]["is_approved"] = True
    if require_approved:
        groups = {k: v for k, v in groups.items() if v.get("is_approved")}
    return groups

# -------------------------
# Validation
# -------------------------
def validate_snapshot(saved: Dict[str, Any]) -> Dict[str, Any]:
    if not saved:
        return {}
    try:
        live = build_live_snapshot(REQUIRE_APPROVED)
    except Exception as e:
        log_console(f"[ERROR] validation fetch failed: {e}")
        return saved
    removed: List[Tuple[str, str]] = []
    updated: List[str] = []
    for key in list(saved.keys()):
        if key not in live:
            removed.append((key, "no longer approved/present"))
            saved.pop(key, None)
            continue
        live_info = live[key]
        saved_info = saved.get(key, {})
        changed = False
        if saved_info.get("questionEndTime") != live_info.get("questionEndTime"):
            saved_info["questionEndTime"] = live_info.get("questionEndTime")
            saved_info["questionEndTime_iso"] = live_info.get("questionEndTime_iso")
            changed = True
        if json.dumps(saved_info.get("options", []), sort_keys=True) != json.dumps(live_info.get("options", []), sort_keys=True):
            saved_info["options"] = live_info.get("options", [])
            changed = True
        if saved_info.get("is_approved") != live_info.get("is_approved"):
            saved_info["is_approved"] = live_info.get("is_approved")
            changed = True
        if changed:
            saved[key] = saved_info
            updated.append(key)
    if removed:
        append_removed_log(removed)
    if removed or updated:
        save_snapshot(saved)
        lines = []
        if removed:
            lines.append(f"Removed {len(removed)} markets no longer approved:")
            for r, _ in removed[:20]:
                lines.append(f"- {r}")
        if updated:
            lines.append(f"Updated {len(updated)} markets with fresh data:")
            for u in updated[:20]:
                lines.append(f"- {u}")
        summary = "*Validation summary*\n" + "\n".join(lines)
        log_console(summary.replace("\n", " | "))
        try:
            send_telegram_message(summary)
        except Exception as e:
            log_console(f"[WARN] validation: failed sending summary: {e}")
    return saved

# -------------------------
# Notifier (HTTP Telegram)
# -------------------------
def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not ADMIN_CHAT_ID:
        log_console("[TELEGRAM DISABLED] Would send:")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": ADMIN_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        log_console("[TELEGRAM] sent (HTTP)")
    except Exception as e:
        log_console(f"[ERROR] telegram send failed: {e}")

# -------------------------
# Register bot commands (slash suggestions)
# -------------------------
def set_bot_commands() -> None:
    """
    Call setMyCommands via Bot API so Telegram clients show suggestions on '/'.
    Uses HTTP API (synchronous) for simplicity.
    """
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands"
    commands = [
        {"command": "start", "description": "Start the bot"},
        {"command": "status", "description": "Number of current upcoming markets and latest"},
        {"command": "links", "description": "Links to current markets"},
        {"command": "validate", "description": "Force validation now (admin only)"},
        {"command": "help", "description": "Show help text"},
    ]
    try:
        r = requests.post(url, json={"commands": commands}, timeout=10)
        if r.status_code == 200:
            log_console("setMyCommands: commands registered")
        else:
            log_console(f"setMyCommands: unexpected status {r.status_code} - {r.text}")
    except Exception as e:
        log_console(f"setMyCommands failed: {e}")

# -------------------------
# Utilities for /status
# -------------------------
def build_status_message(snapshot: Dict[str, Any], max_items: int = 10) -> str:
    if not snapshot:
        return "No upcoming approved Match Result markets in snapshot."
    items = list(snapshot.items())
    items.sort(key=lambda kv: kv[1].get("first_detected_at") or "", reverse=True)
    n = len(items)
    latest_header = items[0][0]
    latest_time = items[0][1].get("first_detected_at", "unknown")
    short = "\n".join(f"- {h}" for h, _ in items[:max_items])
    return (
        f"Known upcoming approved Match Result markets: {n}\n"
        f"Latest detected: {latest_header}  (at {latest_time})\n\n"
        f"Recent (up to {max_items}):\n{short}"
    )

# -------------------------
# Watcher (thread) - heartbeat-only automatic updates
# -------------------------
def watcher_loop():
    log_console(f"watcher started, poll={POLL_INTERVAL_SECONDS}s validation_interval={VALIDATION_INTERVAL_SECONDS}s heartbeat={'on' if HEARTBEAT_ENABLED else 'off'}")
    saved = load_snapshot()
    # initial validation
    saved = validate_snapshot(saved)
    last_validation = time.time()
    while True:
        try:
            current = build_live_snapshot(REQUIRE_APPROVED)
            current_keys = set(current.keys())
            saved_keys = set(saved.keys())
            # carry over first_detected_at
            for k in current_keys:
                if k in saved and "first_detected_at" in saved[k]:
                    current[k]["first_detected_at"] = saved[k]["first_detected_at"]
            new_keys = sorted(list(current_keys - saved_keys))
            now_str = now_iso()
            if new_keys:
                log_console(f"[NEW] {len(new_keys)} new approved markets")
                for k in new_keys:
                    m = current[k]
                    m["first_detected_at"] = now_str
                    url = build_market_url(m)
                    options_block = "\n".join(f"- [{o['questionId']}] {o['title']}" for o in m.get("options", []))
                    msg = (
                        "*New Match Result match detected*\n"
                        f"Match: {m.get('match_header')}\n"
                        f"End time (UTC): {m.get('questionEndTime_iso')}\n"
                        f"First detected at: {m.get('first_detected_at')}\n\n"
                        f"Options:\n{options_block}\n\n"
                        f"Link: {url}"
                    )
                    try:
                        send_telegram_message(msg)
                        log_console(f"alert sent for {k}")
                    except Exception as e:
                        log_console(f"[ERROR] sending alert: {e}")
                    saved[k] = m
            else:
                log_console("[CHECK] no new matches")
                if HEARTBEAT_ENABLED:
                    try:
                        send_telegram_message(HEARTBEAT_TEXT_NO_NEW)
                    except Exception as e:
                        log_console(f"[WARN] failed sending heartbeat: {e}")
            # persist snapshot
            save_snapshot(current)
            saved = current
            # periodic validation
            if VALIDATION_INTERVAL_SECONDS > 0:
                now_ts = time.time()
                if now_ts - last_validation >= VALIDATION_INTERVAL_SECONDS:
                    log_console("running periodic validation")
                    saved = validate_snapshot(saved)
                    last_validation = now_ts
        except Exception as e:
            log_console(f"[ERROR] watcher loop: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)

# -------------------------
# Telegram handlers (async)
# -------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fliq Match Watcher active. Commands: /status /links /help /validate")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = (
        "/start - start message\n"
        "/status - count and latest matches\n"
        "/links - links to current markets (up to first 30)\n"
        "/validate - force validation of snapshot now (admin only)\n"
        "/help - this message\n"
    )
    await update.message.reply_text(txt)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    snapshot = await asyncio.to_thread(load_snapshot)
    text = build_status_message(snapshot, max_items=10)
    await update.message.reply_text(text)

async def cmd_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    snapshot = await asyncio.to_thread(load_snapshot)
    if not snapshot:
        await update.message.reply_text("No upcoming approved Match Result markets in snapshot.")
        return
    items = list(snapshot.items())
    items.sort(key=lambda kv: kv[1].get("first_detected_at") or "", reverse=True)
    lines = []
    for header, info in items[:MAX_LINKS_IN_MESSAGE]:
        url = build_market_url(info)
        end = info.get("questionEndTime_iso") or "unknown"
        lines.append(f"- {header}  (ends {end})\n  {url}")
    if len(items) > MAX_LINKS_IN_MESSAGE:
        lines.append(f"...and {len(items)-MAX_LINKS_IN_MESSAGE} more. See snapshot file.")
    text = "Upcoming markets:\n\n" + "\n\n".join(lines)
    await update.message.reply_text(text)

async def cmd_validate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sender = update.effective_user.id
    if sender != ADMIN_CHAT_ID:
        await update.message.reply_text("You are not authorized to run validation.")
        return
    await update.message.reply_text("Running validation now...")
    cleaned = await asyncio.to_thread(lambda: validate_snapshot(load_snapshot()))
    await update.message.reply_text("Validation complete. Current saved markets: " + str(len(cleaned)))

# -------------------------
# Main
# -------------------------
def main():
    # start watcher thread
    t = threading.Thread(target=watcher_loop, daemon=True, name="fliq-watcher")
    t.start()

    # register bot command suggestions (slash menu)
    set_bot_commands()

    # start telegram bot (polling)
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("links", cmd_links))
    app.add_handler(CommandHandler("validate", cmd_validate))

    log_console("bot starting (run_polling)...")
    try:
        app.run_polling()
    except KeyboardInterrupt:
        log_console("bot stopped by user")
    finally:
        try:
            save_snapshot(load_snapshot())
        except Exception:
            pass

if __name__ == "__main__":
    main()
