"""Podcast links the owner can open (cloudflare P5b, 2026-11-03).

The rss-feeds Worker serves a podcast only under its keyed root, ``https://<host>/<name>-<key>/``, which maps to
the podcast's B2 prefix. D1 and the producers keep storage-form URLs (``https://<host>/<prefix>/...``); this
turns one into the keyed link for a message. The root (``*_PUBLIC_ROOT``) holds the key, so it lives only in the
0600 env file. Without a root, or for any other URL, the value is returned unchanged.

This is tg-tools' half of the mapping; the Worker's is rss-feeds ``src/podcasts.js``. Each repo's suite checks the
other's (here ``shared/tests/test_podcast_urls.py``), so a host or podcast changed on one side only fails a test
instead of giving Telegram links that 404.
"""
from __future__ import annotations

import os
import re

# Every host the Worker has served from (rss-feeds STORED_HOSTS).
STORAGE_HOSTS = frozenset({
    "rss-feeds.stations.work", "rss-edge.05802.workers.dev", "rss.stations.work",
    "rss-b2-proxy.05802.workers.dev", "rss-b2-proxy.aaronkuna.workers.dev",
})
# The podcasts tg-tools publishes: B2 prefix -> public name (rss-feeds PODCASTS prefix/name).
PODCASTS = {"audio": "episodes", "tts": "speech", "surveillance": "surveillance"}


def display_url(url: str | None, prefix: str, root: str | None) -> str | None:
    if not url or not root or prefix not in PODCASTS:
        return url
    match = re.match(r"https?://([^/]+)/" + re.escape(prefix) + r"/(.+)$", url, re.S)
    if not match or match.group(1) not in STORAGE_HOSTS:
        return url
    return root.rstrip("/") + "/" + match.group(2)


def display_url_from_env(url: str | None, prefix: str, root_variable: str, environ=None) -> str | None:
    source = os.environ if environ is None else environ
    return display_url(url, prefix, source.get(root_variable, "").strip())
