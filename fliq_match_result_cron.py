#!/usr/bin/env python3
"""
Single-run Fliq Match Result watcher suitable for cron (GitHub Actions).

Behavior:
- Fetches current approved "Match Result" groups from Fliq
- Compares with saved snapshot (upcoming_match_results.json)
- Sends Telegram alerts (HTTP API) for newly detected approved markets
- Appends removed markets to removed_markets.log
- Writes updated snapshot and exits
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# Config (can be pulled from environment)
API_BASE = os.getenv("API_BASE", "https://auto-question.fliq.one/question")
REFERRAL_CODE = os.getenv("REFERRAL_CODE", "aD6VfTQkAW")
BASE_MARKET_URL = os.getenv("BASE_MARKET_URL", "https://www.fliq.one/#/multi-question")

MATCHES_FILE = Path(os.getenv("MATCHES_FILE", "upcoming_match_results.json"))
REMOVED_LOG_PATH = Path(os.getenv("REMOVED_LOG_PATH", "removed_markets.log"))

FETCH_LIMIT = int(os.getenv("FETCH_LIMIT", "1000"))
REQUIRE_APPROVED = os.getenv("REQUIRE_APPROVED", "true").lower() in ("1", "true", "yes")

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))

# Telegram (use secrets in github actions)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID_RAW = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_CHAT_ID: Optional[int] = int(TELEGRAM_CHAT_ID_RAW) if TELEGRAM_CHAT_ID_RAW.isdigit() else None

MAX_LINKS_IN_MESSAGE = int(os.getenv("MAX_LINKS_IN_MESSAGE", "30"))

# Utils
def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def unix_to_iso(ts: Optional[int]) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""

def slugify_header(header: str) -> str:
    s = (header or "").lower().strip()
    s = s.replace(":", " ")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")

def log(msg: str) -> None:
    print(f"[{now_iso()}] {msg}")

# Storage helpers
def load_matches() -> Dict[str, Any]:
    if not MATCHES_FILE.exists():
        return {}
    try:
        with MATCHES_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        log("snapshot file invalid JSON, starting fresh")
        return {}
    except Exception as e:
        log(f"failed loading snapshot: {e}")
        return {}

def save_matches(data: Dict[str, Any]) -> None:
    try:
        with MATCHES_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
    except Exception as e:
        log(f"failed to save snapshot: {e}")

def append_removed_log(entry: str) -> None:
    try:
        REMOVED_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with REMOVED_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{now_iso()}] {entry}\n")
    except Exception as e:
        log(f"failed to append removed log: {e}")

# Fliq API
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
    r = requests.get(API_BASE, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    return j.get("questions", []) if isinstance(j, dict) else []

def looks_like_upcoming_match(q: Dict[str, Any]) -> bool:
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
    now_ts = int(datetime.now(timezone.utc).timestamp())
    return end_ts > now_ts

def build_market_url(match: Dict[str, Any]) -> str:
    slug = match.get("slug")
    mid = match.get("multi_question_id")
    if not slug or not mid:
        return "(URL unavailable)"
    return f"{BASE_MARKET_URL}/{slug}-{mid}?referral={REFERRAL_CODE}"

def build_current_matches_snapshot(require_approved: bool = REQUIRE_APPROVED) -> Dict[str, Any]:
    questions = fetch_questions(limit=FETCH_LIMIT)
    candidates = [q for q in questions if looks_like_upcoming_match(q)]
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
                "slug": slugify_header(parent_header),
                "multi_question_id": str(parent_id),
                "options": [],
                "questionEndTime": end_ts,
                "questionEndTime_iso": unix_to_iso(end_ts) if end_ts else "",
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
        groups = {k: v for k, v in groups.items() if v.get("is_approved", False)}
    return groups

# Notifier (HTTP)
def send_telegram_via_http(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram disabled; no token/chat id")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()

# Main single-run procedure
def run_once():
    log("run start")
    saved = load_matches()
    try:
        current = build_current_matches_snapshot(REQUIRE_APPROVED)
    except Exception as e:
        log(f"fetch failed: {e}")
        return

    saved_keys = set(saved.keys())
    current_keys = set(current.keys())

    new_keys = sorted(list(current_keys - saved_keys))
    removed_keys = sorted(list(saved_keys - current_keys))

    # carry over first_detected_at
    for k in current_keys:
        if k in saved and "first_detected_at" in saved[k]:
            current[k]["first_detected_at"] = saved[k]["first_detected_at"]

    now_str = now_iso()
    if new_keys:
        log(f"new {len(new_keys)} markets")
        for k in new_keys:
            m = current[k]
            m["first_detected_at"] = now_str
            options_block = "\n".join(f"- [{o['questionId']}] {o['title']}" for o in m.get("options", []))
            url = build_market_url(m)
            msg = (
                "*New Match Result match detected*\n"
                f"Match: {m.get('match_header')}\n"
                f"End time (UTC): {m.get('questionEndTime_iso')}\n"
                f"First detected at (local): {m.get('first_detected_at')}\n\n"
                f"Options:\n{options_block}\n\n"
                f"Link: {url}"
            )
            try:
                send_telegram_via_http(msg)
                log(f"alert sent for {k}")
            except Exception as e:
                log(f"failed to send alert for {k}: {e}")
    else:
        log("no new markets")

    if removed_keys:
        log(f"removed {len(removed_keys)} markets")
        for k in removed_keys:
            try:
                append_removed_log(f"Removed: {k} data={json.dumps(saved.get(k, {}), ensure_ascii=False)}")
            except Exception as e:
                log(f"failed writing removed log for {k}: {e}")

    # persist current snapshot
    save_matches(current)
    log("run done")

if __name__ == "__main__":
    run_once()
