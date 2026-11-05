"""surveillance-tui: follow each Surveillance item (TV and radio) through the rss-audio pipe. Read only.

One row per item: queued > window/download > opening > STT > upload > feed > notes, each stage done,
running, failed, skipped, not reached or unknown. The publish job owns the pipe up to the feed row; the
notes stage is the item's notes job (``inputKind: "notes"``, submitted by the publish job). Enter shows the files at each stage (work dir, B2,
feed row), the job, the STT engine and what the opening said. Sources, read with the settings the
``rss-audio-runtime-worker`` unit runs with: the controller's jobs and progress, the episode API, one
``rclone lsf``, the work dirs and the worker's journal. A source that fails reads ``unknown: <reason>``.
The episode API and B2 are read at most once a minute (``r`` forces them); everything else every 10 s.
Nothing is submitted, written or restarted; no URL host, token, bucket or env value is shown.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from config import WORKER_UNIT as UNIT
FEED = "surveillance"
STAGES = ("queued", "download", "opening", "stt", "upload", "feed", "notes")
DOCUMENTS = ("mp3", "script", "segments", "notes", "manifest", "page")   # B2 kinds, in upload order
VERSIONED = re.compile(r"^v(\d+)\.")
LABELS = {"queued": "queued", "download": "download", "opening": "opening", "stt": "STT", "notes": "notes",
          "upload": "upload", "feed": "feed"}
# Progress stage (tg_tools_common.progress) -> pipe stage. check_publication only asks "already published?".
GROUP = {"source_metadata": "download", "download_audio": "download", "convert_audio": "download",
         "source_retry": "download", "split_audio": "stt", "transcribe_chunks": "stt", "transcript_ready": "stt",
         "prepare_paragraphs": "notes", "topic_analysis": "notes", "reference_extraction": "notes",
         "upload_audio": "upload", "upload_document": "upload", "publication_retry": "feed",
         "publish_episode": "feed"}
GLYPHS = {True: dict(done="✓", running="▶", failed="✗", skipped="–", pending="·", unknown="?", sep=" › "),
          False: dict(done="+", running=">", failed="x", skipped="-", pending=".", unknown="?", sep=" > ")}
JOURNAL_LINE = re.compile(r"live window|Transcribing|chars from|Azure deployment|enrichment|publication|"
                          r"opening|WARNING|ERROR")


@dataclass(frozen=True)
class Unknown:
    reason: str

    def __str__(self) -> str:
        return f"unknown: {self.reason}"


def scrub(text, *hide: str) -> str:
    text = re.sub(r"https?://\S+", "<url>", str(text))
    for word in hide:
        if word:
            text = text.replace(word, "<hidden>")
    return " ".join(text.split())


def read(what: str, fn, *hide: str):
    try:
        return fn()
    except Exception as exc:  # every failed source is shown, never treated as empty
        return Unknown(f"{what}: {scrub(exc, *hide)[:120] or type(exc).__name__}")


def ts(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def span(seconds) -> str:
    seconds = max(0, int(seconds or 0))
    return (f"{seconds}s" if seconds < 60 else f"{seconds // 60}m{seconds % 60:02d}s" if seconds < 3600
            else f"{seconds // 3600}h{seconds // 60 % 60:02d}m")


def size(n) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def expected_prefix(input_json: dict) -> str:
    """The episode prefix (slug) the worker will name this job, computed by the pipeline's own naming."""
    import live_window
    import pipeline
    if input_json.get("slug"):   # a rebuild or notes job names its episode
        return str(input_json["slug"])
    options = pipeline.job_options(input_json)
    if "liveWindow" in input_json:
        window = live_window.parse_window(input_json["liveWindow"])
        identity, source = window.identity, window.source_url
    else:
        identity = source = input_json["sourceUrl"]
    metadata = pipeline.apply_options(pipeline.EpisodeMetadata(
        title="", channel="", description="", duration_seconds=0, published_human="", source_url=source), options)
    return pipeline._episode_names(metadata, hashlib.sha256(identity.encode("utf-8")).hexdigest()).prefix


@dataclass
class Item:
    title: str
    jobs: list[dict]                      # oldest first; a redo (new key, same title) is the same item
    progress: dict | Unknown | None = None
    episode: dict | Unknown | None = None
    objects: list | Unknown = field(default_factory=list)
    prefix: str = ""
    notes_jobs: list[dict] = field(default_factory=list)   # oldest first; every notes job, cancelled ones too
    notes_progress: dict | Unknown | None = None           # the newest live notes job's
    page_jobs: list[dict] = field(default_factory=list)    # page-only jobs (notes_job.run_page)

    @property
    def job(self) -> dict:
        return self.jobs[-1]

    @property
    def notes_job(self) -> dict | None:
        """The newest notes job that is not cancelled; a cancelled one (a duplicate) shows in the detail only."""
        return next((j for j in reversed(self.notes_jobs) if j.get("status") != "cancelled"), None)

    @property
    def window(self) -> bool:
        return "liveWindow" in (self.job.get("inputJson") or {})

    @property
    def date(self) -> str:
        return str((self.job.get("inputJson") or {}).get("publishedAt") or self.job.get("createdAt") or "")[:10]

    @property
    def source(self) -> str:   # a rebuild of a radio day is still radio
        return "radio" if any("liveWindow" in (j.get("inputJson") or {}) for j in self.jobs) else "tv"


@dataclass
class Stage:
    name: str
    state: str | None = None   # done running failed skipped pending unknown
    text: str = ""


def _group_state(entries: list[dict], leased: bool, now: datetime) -> tuple[str, str]:
    begin = min(ts(e["startedAt"]) for e in entries)
    end = max(ts(e["updatedAt"]) for e in entries)
    live = [e for e in entries if e["kind"] in ("started", "advanced")]
    if live and leased:
        facts = live[-1].get("facts") or {}
        count = f" {facts['completed']}/{facts['total']}" if facts.get("total", 1) > 1 else ""
        return "running", span((now - begin).total_seconds()) + count
    last = max(entries, key=lambda e: (e["updatedAt"], e["kind"] == "completed"))
    state = "done" if last["kind"] == "completed" else "failed"
    return state, span((end - begin).total_seconds())


def _notes_job_state(item: Item, now: datetime) -> tuple[str, str]:
    """The notes stage from the newest notes job: waiting, retrying, running, done or failed."""
    nj = item.notes_job
    status, attempts, error = nj.get("status"), int(nj.get("attemptCount") or 0), nj.get("lastErrorClass")
    entries = [] if not isinstance(item.notes_progress, dict) else list(item.notes_progress.get("stages") or [])
    if isinstance(item.notes_progress, Unknown) and status not in TERMINAL:
        return "unknown", item.notes_progress.reason
    if status == "pending" and attempts == 0:
        created = ts(nj.get("createdAt"))
        return "running", "waiting " + span((now - created).total_seconds() if created else 0)
    if status in ("pending", "retryable"):
        return "running", f"retry {attempts}/{nj.get('maxAttempts')} {error or ''}".strip()
    if entries:
        state, text = _group_state(entries, status == "leased", now)
        if status == "leased" or status == "succeeded":
            return ("running" if status == "leased" else "done"), text
    if status == "leased":
        return "running", ""
    if status == "succeeded":
        return "done", ""
    return "failed", error or str(status)


def stages(item: Item, now: datetime | None = None) -> list[Stage]:
    """The pipe of one item, from its newest job, that job's progress, the feed row and the B2 objects."""
    now = now or datetime.now(UTC)
    job, out = item.job, {name: Stage(name) for name in STAGES}
    status, error = job.get("status"), job.get("lastErrorClass")
    leased = status == "leased"
    entries = [] if isinstance(item.progress, Unknown) else list((item.progress or {}).get("stages") or [])
    downloaded = min((ts(e["updatedAt"]) for e in entries if e["stage"] == "download_audio"
                      and e["kind"] in ("completed", "interrupted")), default=None)
    grouped: dict[str, list[dict]] = {}
    for entry in sorted(entries, key=lambda e: e["startedAt"]):
        group = GROUP.get(entry["stage"])
        if group == "stt" and item.window and entry["stage"] == "transcribe_chunks" and (
                downloaded is None or ts(entry["startedAt"]) < downloaded):
            group = "opening"    # the 2-minute clip is transcribed inside the window fetch
        if group:
            grouped.setdefault(group, []).append(entry)
    for group, members in grouped.items():
        out[group].state, out[group].text = _group_state(members, leased, now)
    if "stt" in grouped and out["stt"].state == "done":
        out["stt"].text += " azure" if any(e["stage"] == "split_audio" for e in grouped["stt"]) else " m3"

    queued = out["queued"]
    created, attempts = ts(job.get("createdAt")), int(job.get("attemptCount") or 0)
    if status == "pending" and attempts == 0:
        queued.state, queued.text = "running", "waiting " + span((now - created).total_seconds() if created else 0)
    elif status == "pending":
        queued.state, queued.text = "running", f"retry {attempts}/{job.get('maxAttempts')} {error or ''}".strip()
    else:
        first = min((ts(e["startedAt"]) for e in entries), default=None)
        queued.state, queued.text = "done", span((first - created).total_seconds()) if first and created else ""
    if not item.window:
        out["opening"].state, out["opening"].text = "skipped", "n/a"
    if (job.get("inputJson") or {}).get("inputKind") == "rebuild" and out["stt"].state:
        out["download"].state, out["download"].text = "done", "stored mp3"   # read from the feed's store
    if isinstance(item.progress, Unknown):
        for name in ("download", "opening", "stt", "notes"):
            if item.window or name != "opening":
                out[name].state, out[name].text = "unknown", item.progress.reason

    upload, feed, notes = out["upload"], out["feed"], out["notes"]
    if isinstance(item.objects, Unknown):
        upload.state, upload.text = "unknown", item.objects.reason
    elif item.objects:
        kinds, versions = set(), set()
        for name, *_ in item.objects:
            rest = name[len(item.prefix) + 1:]
            if m := VERSIONED.match(rest):
                versions.add(int(m.group(1)))
                rest = rest[m.end():]
            kinds.add(rest.split(".")[0])
        upload.state = "running" if upload.state == "running" else "done"
        upload.text = "+".join(sorted(kinds, key=DOCUMENTS.index) if kinds <= set(DOCUMENTS) else sorted(kinds))
        if versions:
            upload.text += " v" + "/".join(map(str, sorted(versions)))
    elif upload.state == "done":
        upload.state, upload.text = "failed", "not in B2"
    if isinstance(item.episode, Unknown):
        feed.state, feed.text = "unknown", item.episode.reason
    elif item.episode:
        feed.state, feed.text = "done", f"#{item.episode.get('id')}"
        if item.episode.get("md_url"):
            notes.state, notes.text = "done", ""
        elif notes.state != "unknown" and (notes.state or item.episode.get("speech_text_url")):
            notes.state = "failed"
            notes.text = "script only" if item.episode.get("speech_text_url") else "audio only"
    elif status == "succeeded":
        feed.state, feed.text = "failed", "no feed row"
    if item.notes_job is not None:   # the newest live notes job, even over a row's notes (a rebuild's old ones)
        notes.state, notes.text = _notes_job_state(item, now)

    if status == "dead" and error == "window_not_show":
        heard = scrub(job.get("lastErrorText") or "")
        out["opening"].state, out["opening"].text = "skipped", heard.split(":", 1)[-1].strip(" '\"")[:60]
        for stage in out.values():
            if stage.state in (None, "failed", "running"):
                stage.state = "skipped"
    elif status in ("dead", "cancelled"):
        culprit = next((s for s in out.values() if s.state in (None, "failed", "running", "unknown")), None)
        if culprit and culprit.state != "unknown":   # where an unknown pipe failed is itself unknown
            culprit.state, culprit.text = "failed", error or status
    reached = False
    for stage in reversed(list(out.values())):
        if stage.state is None:
            stage.state = "skipped" if reached else "pending"
        reached = reached or stage.state in ("done", "running", "failed")
    return list(out.values())


def summary(job: dict) -> str:
    from scheduled_sources import describe_job
    return describe_job(job)[0].split(" (")[0]


# -- sources --------------------------------------------------------------------------------------

class LiveSources:
    """The worker unit's effective settings (as tg-tools telemetry reads them) and tg-tools' own clients."""

    def __init__(self) -> None:
        self.problem, self.settings, self.env = None, None, {}
        try:
            import config
            self.settings, self.env = config.worker_settings()
            from runtime_client import RuntimeClient
            self.client = RuntimeClient(self.settings)
        except Exception as exc:  # the env holds credentials: name the type only
            self.problem = f"{UNIT} settings unreadable ({type(exc).__name__})"

    def jobs(self) -> list[dict]:   # collect() reads every other source only after this one
        if self.problem:
            raise RuntimeError(self.problem)
        rows = self.client._request("GET", "/v1/jobs", query={
            "producerSystem": "rss-audio", "producerRefPrefix": "surveillance-", "limit": 500}, timeout=10)
        return rows["items"]

    def progress(self, job_id: str) -> dict:
        return self.client._request("GET", f"/v1/jobs/{job_id}/progress", query={"after": 0}, timeout=10)

    def episodes(self) -> list[dict]:
        import pipeline
        return pipeline._api_json(self.settings, "GET", f"/api/episodes?feed={FEED}")

    def objects(self) -> list[tuple[str, int, str]]:
        remote = f"{self.settings.b2_remote}:{self.settings.b2_bucket}/{FEED}/episodes/"
        done = subprocess.run([self.settings.rclone_bin, "lsf", "--format", "pst", "--separator", "\t", remote],
                              env=self.env, capture_output=True, text=True, timeout=60)
        if done.returncode:
            raise RuntimeError(f"rclone lsf exit {done.returncode}: {done.stderr[-200:]}")
        return [(name, int(n), when) for name, n, when in (line.split("\t") for line in done.stdout.splitlines())]

    def workdir(self, job_id: str) -> Path:
        return self.settings.work_dir / job_id   # job ids are UUIDs, so work_dirs.job_work_dir's name is the id

    def journal(self, since: datetime, until: datetime | None) -> list[str]:
        argv = ["journalctl", "--user", "-u", UNIT, "-o", "cat", "--no-pager", "-q",
                "--since", since.strftime("%Y-%m-%d %H:%M:%S UTC")]
        if until:
            argv += ["--until", until.strftime("%Y-%m-%d %H:%M:%S UTC")]
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        if done.returncode:
            raise RuntimeError(f"journalctl exit {done.returncode}")
        return done.stdout.splitlines()

    def hide(self) -> tuple[str, ...]:
        return (self.settings.b2_bucket,) if self.settings else ()


TERMINAL = {"succeeded", "dead", "cancelled"}
EDGE_SECONDS = 60   # the episode API (a public Worker) and B2 are read at most this often; `r` reads at once


def edge_read(cache: dict, name: str, what: str, fn, hide: tuple, force: bool, clock):
    held = cache.setdefault("edge", {})
    if force or name not in held or clock() - held[name][0] >= EDGE_SECONDS:
        held[name] = (clock(), read(what, fn, *hide))
    return held[name][1]


def feed_age(cache: dict, clock=time.monotonic) -> int | None:
    """Seconds since the episode API was last read, or None before the first read."""
    held = (cache.get("edge") or {}).get("episodes")
    return None if held is None else max(0, int(clock() - held[0]))


def collect(sources, cache: dict | None = None, force: bool = False, clock=time.monotonic) -> list[Item] | Unknown:
    hide = sources.hide()
    cache = {} if cache is None else cache
    jobs = read("controller", sources.jobs, *hide)
    if isinstance(jobs, Unknown):
        return jobs
    episodes = edge_read(cache, "episodes", "episode API", sources.episodes, hide, force, clock)
    objects = edge_read(cache, "objects", "B2", sources.objects, hide, force, clock)
    by_title: dict[str, list[dict]] = {}
    for job in sorted(jobs, key=lambda j: j.get("createdAt") or ""):
        by_title.setdefault((job.get("inputJson") or {}).get("title") or job["producerRef"], []).append(job)
    items = []
    for title, group in by_title.items():
        kind = lambda j: (j.get("inputJson") or {}).get("inputKind")
        notes = [j for j in group if kind(j) == "notes"]
        pages = [j for j in group if kind(j) == "page"]   # an add-on: in the detail, never the pipe
        item = Item(title, [j for j in group if j not in notes + pages] or notes or pages, notes_jobs=notes,
                    page_jobs=pages)
        for which, current in (("progress", item.job), ("notes_progress", item.notes_job)):
            if current is None:
                continue
            key = (current["jobId"], current.get("updatedAt"))
            if key not in cache or current.get("status") not in TERMINAL:
                cache[key] = read("progress", lambda: sources.progress(current["jobId"]), *hide)
            setattr(item, which, cache[key])
        item.prefix = read("name", lambda: expected_prefix(item.job.get("inputJson") or {}))
        if isinstance(item.prefix, Unknown):
            item.prefix = ""
        item.episode = episodes if isinstance(episodes, Unknown) else next(
            (row for row in episodes if (item.prefix and row.get("slug") == item.prefix) or row.get("title") == title),
            None)
        if not item.prefix and isinstance(item.episode, dict):
            item.prefix = item.episode.get("slug") or ""
        item.objects = objects if isinstance(objects, Unknown) else [
            o for o in objects if item.prefix and o[0].startswith(item.prefix + ".")]
        items.append(item)
    return sorted(items, key=lambda i: (i.date, i.source), reverse=True)


# -- rendering ------------------------------------------------------------------------------------

def strip(item: Item, utf: bool = True, now: datetime | None = None) -> list[tuple[str, str]]:
    """(text, state) cells of the stage strip."""
    glyphs, cells = GLYPHS[utf], []
    for i, stage in enumerate(stages(item, now)):
        label = "window" if stage.name == "download" and item.window else LABELS[stage.name]
        text = "" if stage.state == "unknown" or len(stage.text) > 14 else stage.text
        cells.append(((glyphs["sep"] if i else "") + glyphs[stage.state] + label + (f" {text}" if text else ""),
                      stage.state))
    return cells


def row(item: Item, utf: bool = True, now: datetime | None = None) -> list[tuple[str, str]]:
    return [(f"{item.date}  {item.source:<5}  {summary(item.job)[:18]:<18}  ", "plain"), *strip(item, utf, now)]


def stage_of(name: str) -> str:
    if "opening" in name:
        return "opening"
    if name.endswith((".mp3", ".m4a")) or name.startswith(("live", "concat-", "window", "segments")):
        return "window/download"
    if name in ("stt-engine.txt", "script.md", "script.sha256", "paragraphs.json"):
        return "STT"
    return "notes" if name.endswith((".json", ".md")) or name.startswith("tts-claude") else ""


def workdir_lines(path: Path) -> list[str]:
    if not path.is_dir():
        return [f"  (no work dir: {path.name})"]
    lines = []
    for entry in sorted(path.iterdir(), key=lambda p: p.stat().st_mtime):
        stat = entry.stat()
        if entry.is_dir():
            files = [f for f in entry.rglob("*") if f.is_file()]
            newest = max((f.stat().st_mtime for f in files), default=stat.st_mtime)
            lines.append(f"  {stage_of(entry.name):<16}{entry.name}/  {len(files)} files  "
                         f"{size(sum(f.stat().st_size for f in files))}  {time.strftime('%H:%M:%S', time.gmtime(newest))}")
            lines += [f"  {stage_of(f.name):<16}  {f.name}  {size(f.stat().st_size)}" for f in files
                      if not f.name.startswith("seg-")][:8]
        else:
            lines.append(f"  {stage_of(entry.name):<16}{entry.name}  {size(stat.st_size)}  "
                         f"{time.strftime('%H:%M:%S', time.gmtime(stat.st_mtime))}")
    return lines


def detail(item: Item, sources, utf: bool = True, now: datetime | None = None) -> list[tuple[str, str]]:
    """(line, state) pairs for the detail view."""
    hide, job, glyphs = sources.hide(), item.job, GLYPHS[utf]
    out: list[tuple[str, str]] = [(f"{item.title}   [{item.source}, {item.prefix or 'name unknown'}]", "plain")]
    for other in item.jobs + [j for j in item.notes_jobs + item.page_jobs if j not in item.jobs]:
        out.append((f"job {other['jobId']}  {other['producerRef']}  {other.get('status')}  attempt "
                    f"{other.get('attemptCount')}/{other.get('maxAttempts')}  updated {str(other.get('updatedAt'))[:19]}"
                    + (f"  lease {other.get('leaseOwner')} to {str(other.get('leaseExpiresAt'))[11:19]}"
                       if other.get("leaseExpiresAt") else ""), "plain"))
    for which in (job, item.notes_job) if item.notes_job is not job else (job,):
        if which and which.get("lastErrorClass"):
            out.append((f"  last error ({'notes' if which is item.notes_job else 'publish'}) {which['lastErrorClass']}: "
                        f"{scrub(which.get('lastErrorText') or '', *hide)[:300]}", "failed"))
    out.append(("", "plain"))
    for stage in stages(item, now):
        out.append((f"  {glyphs[stage.state]} {LABELS[stage.name]:<9}{stage.state:<9}{stage.text}", stage.state))
    progress = item.progress
    if isinstance(progress, Unknown):
        out.append((f"progress {progress}", "unknown"))
    if isinstance(item.notes_progress, Unknown):
        out.append((f"notes progress {item.notes_progress}", "unknown"))
    kind = "notes" if item.notes_job is job else (
        "rebuild" if (job.get("inputJson") or {}).get("inputKind") == "rebuild" else "publish")
    for heading, source in ((kind, progress),
                            ("notes", item.notes_progress if item.notes_job is not job else None)):
        entries = (source or {}).get("stages", []) if isinstance(source, dict) else []
        if entries:
            out.append((f"  {heading} job progress", "plain"))
        for entry in entries:
            facts = {k: v for k, v in (entry.get("facts") or {}).items() if k != "title"}
            out.append((f"    {entry['startedAt'][11:19]}-{entry['updatedAt'][11:19]}  {entry['stage']:<21}"
                        f"{entry['kind']:<12}{facts or ''}", "plain"))

    attempt = progress.get("attempt") if isinstance(progress, dict) else None
    engine, opening = None, None
    out.append(("", "plain"))
    workdir = read("work dir", lambda: sources.workdir(job["jobId"]), *hide)
    out.append((f"work dir  {workdir}" if isinstance(workdir, Unknown) else "work dir", "plain"))
    if isinstance(workdir, Path):
        listing = read("work dir", lambda: workdir_lines(workdir), *hide)
        out += [(f"  {listing}", "unknown")] if isinstance(listing, Unknown) else [(line, "plain") for line in listing]
        engine = read("engine", lambda: (workdir / "stt-engine.txt").read_text().strip() or None)
        engine = None if isinstance(engine, Unknown) else engine
    out.append((f"B2  {FEED}/episodes/", "plain"))
    if isinstance(item.objects, Unknown):
        out.append((f"  {item.objects}", "unknown"))
    for name, n, when in item.objects if isinstance(item.objects, list) else []:
        out.append((f"  upload          {name}  {size(n)}  {when}", "plain"))
    if isinstance(item.objects, list) and not item.objects:
        out.append(("  (no objects under this name)", "plain"))
    if isinstance(item.episode, Unknown):
        out.append((f"feed  {item.episode}", "unknown"))
    elif item.episode:
        ep = item.episode
        out.append((f"feed  #{ep.get('id')} {ep.get('slug')}  pub_date {ep.get('pub_date')}  duration "
                    f"{ep.get('duration')}  mp3 {size(ep.get('mp3_size') or 0)}", "plain"))
        for key in ("mp3_url", "speech_text_url", "md_url", "manifest_url", "page_url"):
            out.append((f"  {key:<16}{urlsplit(ep[key]).path if ep.get(key) else '-'}", "plain"))
    else:
        out.append(("feed  no row", "plain"))

    lines: list[str] | Unknown = []
    if attempt and ts(attempt.get("startedAt")):
        lines = read("journal", lambda: sources.journal(ts(attempt["startedAt"]), ts(attempt.get("completedAt"))))
    for line in lines if isinstance(lines, list) else []:
        if m := re.search(r"chars from (\S+)", line):
            engine = engine or m.group(1)
        if m := re.search(r"with Azure deployment (\S+)", line):
            engine = f"azure {m.group(1)}"
        if "opening.m4a" in line:
            opening = "matched (the heard text is logged only on a skip)"
    if job.get("lastErrorClass") == "window_not_show":
        opening = scrub(job.get("lastErrorText") or "", *hide)
    where = "" if not engine else " (Azure)" if engine.startswith("azure") else " (m3 self-hosted)"
    out.append((f"STT engine  {engine or 'not known yet'}{where}", "plain"))
    if item.window:
        out.append((f"opening heard  {opening or 'not checked yet'}", "skipped" if job.get("lastErrorClass") ==
                    "window_not_show" else "plain"))
    out.append(("journal (current attempt)" if attempt else "journal  no attempt yet", "plain"))
    if isinstance(lines, Unknown):
        out.append((f"  {lines}", "unknown"))
    out += [(f"  {scrub(line, *hide)[:200]}", "plain") for line in lines if JOURNAL_LINE.search(line)][-25:] \
        if isinstance(lines, list) else []
    return out


# -- terminal -------------------------------------------------------------------------------------

def run_once(sources, out, key: str | None = None, utf: bool = True, now: datetime | None = None) -> int:
    items = collect(sources)
    if isinstance(items, Unknown):
        print(f"Surveillance: {items}", file=out)
        return 1
    print(f"Surveillance, {len(items)} items (times UTC)", file=out)
    for item in items:
        print("".join(text for text, _ in row(item, utf, now)), file=out)
    chosen = next((i for i in items if key and (key in i.job["producerRef"] or key in i.title)), items[0] if items else None)
    if chosen:
        print("", file=out)
        print("\n".join(text for text, _ in detail(chosen, sources, utf, now)), file=out)
    return 0


def run_curses(sources) -> None:
    import curses
    import locale
    locale.setlocale(locale.LC_ALL, "")
    utf = "utf" in (locale.getpreferredencoding() or "").lower()

    def main(screen):
        try:
            curses.curs_set(0)
        except curses.error:   # a terminal without cursor control
            pass
        screen.timeout(1000)
        colors = {"plain": 0}
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            for n, (state, color) in enumerate((("done", curses.COLOR_GREEN), ("running", curses.COLOR_CYAN),
                                                ("failed", curses.COLOR_RED), ("skipped", curses.COLOR_YELLOW),
                                                ("unknown", curses.COLOR_MAGENTA)), start=1):
                curses.init_pair(n, color, -1)
                colors[state] = curses.color_pair(n)
        colors["pending"] = curses.A_DIM
        cache, items, loaded, selected, opened, scroll, lines = {}, [], float("-inf"), 0, None, 0, None
        force = False
        while True:
            if time.monotonic() - loaded > 10:   # controller, progress, journal and work dir; the edge reads keep 60 s
                screen.addstr(0, 0, "refreshing…" if utf else "refreshing...")
                screen.refresh()
                items, loaded, force = collect(sources, cache, force), time.monotonic(), False
                match = [i for i in items if i.title == opened] if isinstance(items, list) else []
                lines = detail(match[0], sources, utf) if match else None
            height, width = screen.getmaxyx()
            screen.erase()
            if isinstance(items, Unknown):
                screen.addnstr(0, 0, f"Surveillance: {items}", width - 1, colors["unknown"])
            elif lines is not None:
                for y, (text, state) in enumerate(lines[scroll:scroll + height - 1]):
                    screen.addnstr(y, 0, text, width - 1, colors.get(state, 0))
            else:
                selected = min(selected, max(0, len(items) - 1))
                top = max(0, selected - (height - 2))
                for y, item in enumerate(items[top:top + height - 1]):
                    x = 0
                    for text, state in row(item, utf):
                        if x < width - 1:
                            screen.addnstr(y, x, text, width - 1 - x, colors.get(state, 0) |
                                           (curses.A_REVERSE if top + y == selected else 0))
                        x += len(text)
            help_text = ("r refresh  arrows/j k scroll  esc back  q quit" if lines is not None
                         else "r refresh  arrows/j k move  enter open  q quit")
            age = feed_age(cache)
            feed = "" if age is None else f"   feed read {span(age)} ago"
            screen.addnstr(height - 1, 0, f"{help_text}{feed}   read {time.strftime('%H:%M:%S', time.gmtime())}Z",
                           width - 1, curses.A_DIM)
            key = screen.getch()
            if key in (ord("q"), ord("Q")):
                return
            if key == ord("r"):
                loaded, force = float("-inf"), True
            elif key in (curses.KEY_DOWN, ord("j")):
                scroll, selected = (scroll + 1, selected) if lines is not None else (scroll, selected + 1)
            elif key in (curses.KEY_UP, ord("k")):
                scroll, selected = (max(0, scroll - 1), selected) if lines is not None else (scroll, max(0, selected - 1))
            elif key in (curses.KEY_ENTER, 10, 13) and lines is None and isinstance(items, list) and items:
                opened, scroll, lines = items[selected].title, 0, detail(items[selected], sources, utf)
            elif key in (27, curses.KEY_BACKSPACE, curses.KEY_LEFT, 127, ord("h")) and lines is not None:
                opened, lines = None, None

    os.environ.setdefault("ESCDELAY", "25")   # Esc goes back at once
    curses.wrapper(main)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Follow Surveillance episodes through the rss-audio pipe (read only).")
    parser.add_argument("--once", action="store_true", help="print the list and one item's detail, then exit")
    parser.add_argument("--item", help="with --once: the item to detail (part of its key or title); default newest")
    args = parser.parse_args(argv)
    sources = LiveSources()
    if args.once:
        return run_once(sources, sys.stdout, args.item, utf="utf" in (sys.stdout.encoding or "").lower())
    run_curses(sources)
    return 0


if __name__ == "__main__":
    sys.exit(main())
