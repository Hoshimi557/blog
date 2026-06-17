#!/usr/bin/env python3
"""Cross-post new blog RSS items to Mastodon and Bluesky.

Repo path for this file: scripts/crosspost.py

State is tracked in crosspost_state.json.

On the FIRST run (no state file):
  - default: all existing feed items are recorded as already-posted, so the
    backlog is NOT posted -- only items published afterwards get sent.
  - if POST_BACKLOG=true: every existing item is posted instead. Use this
    once, via the "post_backlog" checkbox on the manual Run workflow button.

To redo a first run, delete crosspost_state.json from the repo.

Per-platform state means a failure on one platform retries independently
without re-posting to the other. Dedup is keyed on each item's URL, so
revising a post in place does NOT trigger a re-post.

Configuration is read from environment variables (set as GitHub secrets):
  FEED_URLS             optional, comma-separated; defaults to the site feed
  MASTODON_INSTANCE     e.g. https://mastodon.social   (omit to skip Mastodon)
  MASTODON_TOKEN        access token, scope write:statuses
  BLUESKY_HANDLE        e.g. yourname.bsky.social      (omit to skip Bluesky)
  BLUESKY_APP_PASSWORD  app password (NOT your main password)
  POST_BACKLOG          "true" to post the whole backlog on the first run
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

STATE_FILE = Path("crosspost_state.json")
DEFAULT_FEEDS = ["https://dual-dissent.pages.dev/index.xml"]

FEED_URLS = [u.strip() for u in os.environ.get("FEED_URLS", "").split(",") if u.strip()] \
    or DEFAULT_FEEDS
MASTODON_INSTANCE = os.environ.get("MASTODON_INSTANCE", "").rstrip("/")
MASTODON_TOKEN = os.environ.get("MASTODON_TOKEN", "").strip()
BLUESKY_HANDLE = os.environ.get("BLUESKY_HANDLE", "").strip()
BLUESKY_APP_PASSWORD = os.environ.get("BLUESKY_APP_PASSWORD", "").strip()
POST_BACKLOG = os.environ.get("POST_BACKLOG", "").strip().lower() == "true"

MASTODON_ENABLED = bool(MASTODON_INSTANCE and MASTODON_TOKEN)
BLUESKY_ENABLED = bool(BLUESKY_HANDLE and BLUESKY_APP_PASSWORD)


def strip_html(raw):
    """Remove HTML tags and unescape entities for a plain-text post body."""
    return html.unescape(re.sub(r"<[^>]+>", "", raw or "")).strip()


def truncate(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True)
    )


def collect_entries():
    entries = []
    for url in FEED_URLS:
        feed = feedparser.parse(url)
        for e in feed.entries:
            uid = e.get("id") or e.get("guid") or e.get("link")
            if not uid:
                continue
            entries.append({
                "uid": uid,
                "title": (e.get("title") or "").strip(),
                "summary": strip_html(e.get("summary") or e.get("description") or ""),
                "link": e.get("link") or "",
            })
    return entries


def compose_text(entry):
    """Essays have a title; titleless micro posts fall back to their content."""
    return entry["title"] or entry["summary"] or entry["link"]


def post_mastodon(entry):
    text = truncate(compose_text(entry), 460)
    body = (text + "\n\n" + entry["link"]).strip()
    resp = requests.post(
        MASTODON_INSTANCE + "/api/v1/statuses",
        headers={"Authorization": "Bearer " + MASTODON_TOKEN},
        data={"status": body, "visibility": "public"},
        timeout=30,
    )
    resp.raise_for_status()


def post_bluesky(entry):
    # 1. Authenticate with the app password.
    session = requests.post(
        "https://bsky.social/xrpc/com.atproto.server.createSession",
        json={"identifier": BLUESKY_HANDLE, "password": BLUESKY_APP_PASSWORD},
        timeout=30,
    )
    session.raise_for_status()
    s = session.json()

    text = truncate(compose_text(entry), 280)
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    # 2. Attach a link card so the blog URL renders nicely.
    if entry["link"]:
        record["embed"] = {
            "$type": "app.bsky.embed.external",
            "external": {
                "uri": entry["link"],
                "title": truncate(entry["title"] or text, 200),
                "description": truncate(entry["summary"], 280),
            },
        }
    resp = requests.post(
        "https://bsky.social/xrpc/com.atproto.repo.createRecord",
        headers={"Authorization": "Bearer " + s["accessJwt"]},
        json={
            "repo": s["did"],
            "collection": "app.bsky.feed.post",
            "record": record,
        },
        timeout=30,
    )
    resp.raise_for_status()


def main():
    state = load_state()
    entries = collect_entries()

    if not entries:
        print("No feed entries found -- check FEED_URLS. Nothing to do.")
        return 0

    # First run: empty state.
    if not state:
        if not POST_BACKLOG:
            # Seed: record the backlog as already-posted, send nothing.
            for e in entries:
                state[e["uid"]] = {"mastodon": True, "bluesky": True}
            save_state(state)
            print("First run: recorded %d existing items. No backlog posted." % len(entries))
            print("(Re-run with the 'post_backlog' option ticked to send the backlog.)")
            return 0
        # POST_BACKLOG: fall through and post every existing item.
        print("First run: POST_BACKLOG enabled -- posting %d existing item(s)." % len(entries))

    if not (MASTODON_ENABLED or BLUESKY_ENABLED):
        print("No platform configured -- set Mastodon and/or Bluesky secrets.")
        return 0

    posted = 0
    for e in entries:
        record = state.get(e["uid"], {})
        mast_done = record.get("mastodon", False)
        bsky_done = record.get("bluesky", False)
        did_post = False

        if MASTODON_ENABLED and not mast_done:
            try:
                post_mastodon(e)
                mast_done = did_post = True
                posted += 1
                print("Mastodon OK: " + e["uid"])
            except Exception as err:
                print("Mastodon FAILED: %s: %s" % (e["uid"], err))

        if BLUESKY_ENABLED and not bsky_done:
            try:
                post_bluesky(e)
                bsky_done = did_post = True
                posted += 1
                print("Bluesky OK: " + e["uid"])
            except Exception as err:
                print("Bluesky FAILED: %s: %s" % (e["uid"], err))

        # A disabled platform counts as done so the entry is not pending forever.
        state[e["uid"]] = {
            "mastodon": mast_done or not MASTODON_ENABLED,
            "bluesky": bsky_done or not BLUESKY_ENABLED,
        }
        if did_post:
            time.sleep(2)

    save_state(state)
    print("Done. %d post(s) sent." % posted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
