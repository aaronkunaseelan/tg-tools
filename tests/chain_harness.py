"""Disposable stack for the native lane chains.

Each chain drives one declared route through the real implementation: gateway
admission and handoff, the deployed runtime-controller over HTTP on a private
PostgreSQL cluster, the lane worker, result projection, and gateway delivery.
Only true external edges are replaced: Telegram MTProto (a fake gateway that
hands in normalized messages and serves file bytes), and remote HTTP services
(Telegram Bot API, rss-edge, Azure/OpenAI) served by one local recording
server. rclone runs for real against a local backend, so "exact get" reads the
published object back by its key.

A chain fails when the stack cannot start: a skipped chain is not evidence.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]
PG_BIN = Path("/usr/lib/postgresql/16/bin")
CONTROLLER = Path(os.environ.get("TG_CHAIN_CONTROLLER", Path.home() / ".local/lib/runtime-controller/current"))
POLICY = Path(os.environ.get("TG_CHAIN_POLICY", Path.home() / ".config/runtime-controller/runtime-policy.yaml"))
TOKEN = "chain-token"
CHAT_ID = -1000000000001
OWNER_ID = 12025550101


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------
# Local HTTP edge


@dataclass
class EdgeRequest:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body or b"null")


Handler = Callable[[EdgeRequest], tuple]


def _multipart_fields(req: EdgeRequest) -> dict[str, Any]:
    """Form fields by name; file parts keep their bytes as {"filename", "content"}."""
    from email.parser import BytesParser
    from email.policy import HTTP
    message = BytesParser(policy=HTTP).parsebytes(
        f"Content-Type: {req.headers['content-type']}\r\n\r\n".encode() + req.body)
    fields: dict[str, Any] = {}
    for part in message.iter_parts():
        name, filename = part.get_param("name", header="content-disposition"), part.get_filename()
        content = part.get_payload(decode=True)
        fields[name] = {"filename": filename, "content": content} if filename else content.decode()
    return fields


class Edge:
    """One local server for every remote HTTP dependency.

    Routes are (method, regex) -> handler(request) -> (status, body[, content_type]). Bodies
    that are dict/list are sent as JSON, bytes as-is. Built-in state models
    the two services chains read back: the Telegram Bot API (sent messages and
    documents) and the rss-edge episode API.
    """

    def __init__(self) -> None:
        self.requests: list[EdgeRequest] = []
        self.routes: list[tuple[str, re.Pattern[str], Handler]] = []
        self.telegram: list[dict[str, Any]] = []
        self.episodes: dict[str, list[dict[str, Any]]] = {}
        self._next_message_id = 9000
        self._lock = threading.Lock()
        self.route("POST", r"/bot[^/]+/(\w+)$", self._telegram)
        self.route("GET", r"/api/episodes$", self._episodes_get)
        self.route("POST", r"/api/episodes$", self._episodes_post)
        edge = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _serve(self) -> None:
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                req = EdgeRequest(self.command, parts.path, parse_qs(parts.query),
                                  {k.lower(): v for k, v in self.headers.items()}, self.rfile.read(length))
                status, body, *content_type = edge.dispatch(req)
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Type", content_type[0] if content_type else
                                 "application/octet-stream" if isinstance(body, bytes) else "application/json")
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def route(self, method: str, pattern: str, handler: Handler) -> None:
        self.routes.insert(0, (method, re.compile(pattern), handler))

    def dispatch(self, req: EdgeRequest) -> tuple[int, Any]:
        with self._lock:
            self.requests.append(req)
        for method, pattern, handler in self.routes:
            if method == req.method and pattern.search(req.path):
                return handler(req)
        return 404, {"error": f"no edge route for {req.method} {req.path}"}

    def close(self) -> None:
        self.server.shutdown()

    # Telegram Bot API: every send returns a fresh message id and is recorded.
    def _telegram(self, req: EdgeRequest) -> tuple[int, Any]:
        method = req.path.rsplit("/", 1)[1]
        if req.headers.get("content-type", "").startswith("multipart/"):
            payload = _multipart_fields(req)
        else:
            payload = req.json() or {}
        with self._lock:
            self._next_message_id += 1
            message_id = self._next_message_id
            self.telegram.append({"method": method, "payload": payload, "message_id": message_id})
        return 200, {"ok": True, "result": {"message_id": message_id}}

    def sent(self, method: str | None = None) -> list[dict[str, Any]]:
        return [m for m in self.telegram if method is None or m["method"] == method]

    # rss-edge episode API: POST creates, GET filters by feed/slug.
    def _episodes_post(self, req: EdgeRequest) -> tuple[int, Any]:
        body = req.json()
        row = {**body, "id": sum(len(v) for v in self.episodes.values()) + 1}
        self.episodes.setdefault(body["feed"], []).append(row)
        return 201, row

    def _episodes_get(self, req: EdgeRequest) -> tuple[int, Any]:
        feed = req.query.get("feed", [""])[0]
        slug = req.query.get("slug", [None])[0]
        rows = [r for r in self.episodes.get(feed, []) if slug is None or r["slug"] == slug]
        return 200, rows


# --------------------------------------------------------------------------
# PostgreSQL + runtime-controller


@dataclass
class Stack:
    root: Path
    pg_dir: Path
    pg_port: int
    controller_url: str
    edge: Edge
    bucket: Path
    procs: list[subprocess.Popen] = field(default_factory=list)

    def dsn(self, db: str = "rc") -> str:
        return f"dbname={db} user={os.environ.get('USER', 'ubuntu')} host={self.pg_dir} port={self.pg_port}"

    def psql(self, sql: str, db: str = "rc") -> str:
        out = subprocess.run([PG_BIN / "psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", self.dsn(db), "-c", sql],
                             check=True, capture_output=True, text=True)
        return out.stdout.strip()

    def api(self, method: str, path: str, payload: Any = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode()
        req = Request(self.controller_url + path, data=data, method=method,
                      headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        with urlopen(req, timeout=10) as response:
            return json.loads(response.read() or b"null")

    def job(self, job_id: str) -> dict[str, Any]:
        return self.api("GET", f"/v1/jobs/{job_id}")

    def rclone_env(self) -> dict[str, str]:
        return {"RCLONE_CONFIG_CHAINFS_TYPE": "local", "RCLONE_CONFIG": str(self.root / "rclone.conf")}

    def stop(self) -> None:
        for proc in self.procs:
            proc.terminate()
            proc.wait(timeout=10)
        subprocess.run([PG_BIN / "pg_ctl", "-D", self.pg_dir / "data", "-m", "immediate", "stop"],
                       capture_output=True)
        self.edge.close()
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.pg_dir, ignore_errors=True)


def _wait_http(url: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            urlopen(Request(url, headers={"Authorization": f"Bearer {TOKEN}"}), timeout=1).read()
            return
        except OSError as exc:
            if getattr(exc, "code", None):  # any HTTP answer means the server is up
                return
            if time.monotonic() > deadline:
                raise RuntimeError(f"{url} did not come up") from exc
            time.sleep(0.1)


def start_stack(*, meeting_schema: bool = False, controller: bool = True) -> Stack:
    """controller=False stops after migrations: a real PG for edge tests, no HTTP API."""
    root = Path(tempfile.mkdtemp(prefix="tg-chain-"))
    # The private-PG fence used by every tg-tools PG test: sockets live under
    # /tmp/phase-one-pg-*, never the production socket directory.
    pg_dir = Path(tempfile.mkdtemp(prefix="phase-one-pg-", dir="/tmp"))
    user = os.environ.get("USER", "ubuntu")
    pg_port = free_port()
    subprocess.run([PG_BIN / "initdb", "-D", pg_dir / "data", "-U", user, "-A", "trust"], check=True, capture_output=True)
    subprocess.run([PG_BIN / "pg_ctl", "-D", pg_dir / "data", "-l", pg_dir / "log", "-w", "-o",
                    f"-k {pg_dir} -c listen_addresses='' -p {pg_port} -c fsync=off", "start"], check=True, capture_output=True)
    edge = Edge()
    (root / "rclone.conf").write_text("")
    stack = Stack(root=root, pg_dir=pg_dir, pg_port=pg_port, controller_url="", edge=edge, bucket=root / "bucket")
    stack.bucket.mkdir()
    try:
        subprocess.run([PG_BIN / "createdb", "-h", pg_dir, "-p", str(pg_port), "-U", user, "rc"], check=True, capture_output=True)
        env = {**os.environ, "PGHOST": str(pg_dir), "PGPORT": str(pg_port), "PGUSER": user, "PGDATABASE": "rc",
               "RUNTIME_CONTROLLER_DATABASE_NAME": "rc"}
        subprocess.run(["node", "dist/src/cli.js", "migrate"], cwd=CONTROLLER, env=env, check=True, capture_output=True)
        for sql in sorted((REPO / "tg-gateway" / "migrations").glob("*.sql")):
            subprocess.run([PG_BIN / "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", stack.dsn(), "-f", sql],
                           check=True, capture_output=True)
        if meeting_schema:
            subprocess.run([PG_BIN / "createdb", "-h", pg_dir, "-p", str(pg_port), "-U", user, "meeting"], check=True, capture_output=True)
            subprocess.run([PG_BIN / "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", stack.dsn("meeting"),
                            "-f", REPO / "meeting-summary" / "schema.sql"], check=True, capture_output=True)
        if not controller:
            return stack
        port = free_port()
        log = open(root / "controller.log", "wb")
        stack.procs.append(subprocess.Popen(
            ["node", "dist/src/apps/http-api.js"], cwd=CONTROLLER, stdout=log, stderr=subprocess.STDOUT,
            env={**env, "RUNTIME_CONTROLLER_PORT": str(port), "RUNTIME_CONTROLLER_BIND_HOST": "127.0.0.1",
                 "RUNTIME_CONTROLLER_POLICY_FILE": str(POLICY), "RUNTIME_CONTROLLER_BEARER_TOKEN": TOKEN}))
        stack.controller_url = f"http://127.0.0.1:{port}"
        _wait_http(stack.controller_url + "/v1/jobs")
    except BaseException:
        stack.stop()
        raise
    return stack


# --------------------------------------------------------------------------
# Model CLIs

FAKE_MODEL = r'''#!/usr/bin/env python3
"""Stand-in for the `claude` and `pi` CLIs: answers the prompt shapes the lanes
send (reader write/review/revise, enrichment topics/mentions) through the same
argv/stdin/stdout contract, and logs every call."""
import json, os, re, sys
argv = sys.argv[1:]
if argv[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
    sys.exit(0)
raw = sys.stdin.read()
prompt = raw
if "--input-format" in argv:
    blocks = json.loads(raw.splitlines()[0])["message"]["content"]
    prompt = "\n\n".join(b.get("text", "") for b in blocks)
ids = re.findall(r"^\[(p\d{5})\] ", prompt, re.M)
if "FINDINGS: N" in prompt:
    step, text = "review", "1. The script drops the source's hedge.\n=== FINDINGS: 1 ==="
elif "=== RULINGS ===" in prompt:
    step, text = "revise", SCRIPT + "=== RULINGS ===\n1 accepted: hedge restored.\n"
elif "=== TITLE ===" in prompt:
    step, text = "write", SCRIPT
elif '"mentions"' in prompt:
    heads = re.findall(r'^- "(.*)" covers ', prompt, re.M)
    step, text = "mentions", json.dumps({"topics": [{"heading": h, "mentions": []} for h in heads]})
elif "startParagraphId" in prompt:
    step, text = "topics", json.dumps({"summary": "A chain-test source about one measured finding.",
        "topics": [{"heading": "The finding", "startParagraphId": ids[0], "endParagraphId": ids[-1]}]})
else:
    step, text = "unknown", "unrecognised prompt"
with open(LOG, "a") as log:
    log.write(json.dumps({"cli": os.path.basename(sys.argv[0]), "step": step, "argv": argv}) + "\n")
usage = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
if os.path.basename(sys.argv[0]) == "claude":
    result = {"type": "result", "result": text, "is_error": False, "total_cost_usd": 0, "usage": usage}
    print(json.dumps(result))
elif "--mode" in argv:
    print(json.dumps({"type": "agent_end", "messages": [{"role": "assistant", "stopReason": "stop",
        "content": [{"type": "text", "text": text}],
        "usage": {"input": 10, "output": 5, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 15, "cost": {"total": 0}}}]}))
else:
    print(text)
'''

FAKE_SCRIPT = ("=== TITLE ===\nPlastic bottles shed particles\n=== SYNOPSIS ===\nA study measured particles.\n"
               "=== SCRIPT ===\n## The finding\nHeating water in a polypropylene bottle released millions of "
               "particles per litre, the authors report, though exposure estimates remain uncertain.\n")


def install_fake_models(bin_dir: Path, log: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    body = FAKE_MODEL.replace("argv = sys.argv[1:]", f"SCRIPT = {FAKE_SCRIPT!r}\nLOG = {str(log)!r}\nargv = sys.argv[1:]")
    for name in ("claude", "pi"):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    log.touch()
    # Offline OAuth store for the codex preflight (pi_harness.preflight.CODEX_AUTH_PATH).
    store = bin_dir / "codex-auth.json"
    store.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"refresh_token": "chain"}}))
    return store


def model_calls(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


# --------------------------------------------------------------------------
# Telegram MTProto edge


class FakeTelegram:
    """Replaces TelethonGateway. Tests hand in normalized InboundMessages;
    `files` serves attachment bytes by file id; direct replies are recorded."""

    files: dict[str, bytes] = {}
    replies: list[Any] = []

    def __init__(self, **_: Any) -> None:
        pass

    def normalize_message(self, raw: Any, *, received_at: str = "") -> Any:
        return raw

    async def send_text(self, message: Any) -> int:
        FakeTelegram.replies.append(message)
        return 500 + len(FakeTelegram.replies)

    async def normalize_callback(self, event: Any) -> Any:
        return event.callback

    async def answer_callback(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def edit_text(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def download_file(self, file_id: str, *, target_path: str | Path) -> Any:
        from tg_gateway.models import DownloadedAttachment
        Path(target_path).write_bytes(FakeTelegram.files[file_id])
        return DownloadedAttachment(file_id=file_id, path=str(target_path), file_name=Path(target_path).name)


def gateway_env(stack: Stack, *, identity: str, bindings: dict[str, int],
                extra: dict[str, str] | None = None) -> dict[str, str]:
    """The env a gateway unit runs with, pointed at the private stack."""
    return {
        "TG_GATEWAY_BOT_IDENTITY": identity,
        "TG_GATEWAY_BOT_USERNAME": "chain_bot",
        "TG_GATEWAY_BOT_TOKEN_ENV": "CHAIN_BOT_TOKEN",
        "CHAIN_BOT_TOKEN": "123:chain",
        "TG_GATEWAY_ROUTE_BINDINGS_JSON": json.dumps(bindings),
        "TG_GATEWAY_ALLOWED_CHAT_ID": str(CHAT_ID),
        "TG_GATEWAY_ALLOWED_USER_IDS": str(OWNER_ID),
        "TG_GATEWAY_BOT_SESSION_PATH": str(stack.root / "bot.session"),
        "TG_GATEWAY_STAGING_DIR": str(stack.root / "staging"),
        "TG_GATEWAY_DATABASE_URL": stack.dsn(),
        "TG_GATEWAY_BOT_API_BASE_URL": stack.edge.url,
        "TELEGRAM_API_ID": "1",
        "TELEGRAM_API_HASH": "chain",
        "RUNTIME_CONTROLLER_API_URL": stack.controller_url,
        "RUNTIME_CONTROLLER_BEARER_TOKEN": TOKEN,
        "TG_TOOLS_PROGRESS_ENABLED": "0",
        # The rss identity adapter loads rss-audio settings, whose account values are required config.
        "RSS_AUDIO_BASE_URL": stack.edge.url,
        "RSS_AUDIO_B2_BUCKET": "chain-bucket",
        **(extra or {}),
    }


def seal_dotenv(monkeypatch: Any) -> None:
    """Stop transcribe.py from merging the host's Azure credentials into the test.

    It uses setdefault, so explicit env wins, but any key a chain leaves unset
    would otherwise arrive from the host secrets file.
    """
    loaders = []
    try:
        from episode_enrichment.stt import load_transcribe_module
        loaders.append(load_transcribe_module)
    except ImportError:
        pass
    try:
        from transcribe_endpoint import _load_transcribe_audio
        loaders.append(_load_transcribe_audio)
    except ImportError:
        pass
    for load in loaders:
        monkeypatch.setattr(load(), "_load_local_env", lambda: None)


def azure_stt(stack: Stack, transcript: str) -> dict[str, str]:
    """Route Entra token + Azure OpenAI transcription to the edge; returns the env that points there."""
    stack.edge.route("POST", r"/chain-tenant/oauth2/v2\.0/token$",
                     lambda req: (200, {"access_token": "chain-stt", "expires_in": 3600}))
    stack.edge.route("POST", r"/openai/deployments/[^/]+/audio/transcriptions",
                     lambda req: (200, {"text": transcript}))
    return {"AZURE_AUTHORITY_HOST": stack.edge.url, "AZURE_OPENAI_TRANSCRIPTION_ENDPOINT": stack.edge.url,
            "AZURE_OPENAI_TRANSCRIPTION_MINI_DEPLOYMENT": "chain-mini",
            "AZURE_OPENAI_TRANSCRIPTION_FULL_DEPLOYMENT": "chain-full",
            "AZURE_OPENAI_TRANSCRIPTION_TENANT_ID": "chain-tenant",
            "AZURE_OPENAI_TRANSCRIPTION_CLIENT_ID": "chain-client",
            "AZURE_OPENAI_TRANSCRIPTION_CLIENT_SECRET": "chain-secret"}


def build_gateway(monkeypatch: Any, env: dict[str, str], files: dict[str, bytes] | None = None) -> Any:
    """The production gateway runtime, with MTProto replaced by FakeTelegram."""
    from tg_gateway import daemon
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    seal_dotenv(monkeypatch)
    monkeypatch.setattr(daemon, "TelethonGateway", FakeTelegram)
    FakeTelegram.files, FakeTelegram.replies = dict(files or {}), []
    return daemon.build_gateway_runtime()


def inbound(*, thread_id: int, message_id: int, text: str = "", attachment: dict[str, Any] | None = None,
            reply_to: int | None = None) -> Any:
    from tg_gateway.models import InboundAttachment, InboundMessage
    attachments = (InboundAttachment(**attachment),) if attachment else ()
    message = InboundMessage(receipt_key=f"chain:{message_id}", chat_id=CHAT_ID, thread_id=thread_id,
                             message_id=message_id, user_id=OWNER_ID, text=text, attachments=attachments,
                             reply_to_message_id=reply_to if reply_to is not None else thread_id)
    return type("Event", (), {"message": message})()


def callback(*, thread_id: int, message_id: int, data: str) -> Any:
    from tg_gateway.models import InboundCallback
    return type("Event", (), {"callback": InboundCallback(
        receipt_key=f"chain:cb:{message_id}", chat_id=CHAT_ID, thread_id=thread_id, callback_query_id="cb-1",
        message_id=message_id, user_id=OWNER_ID, data=data)})()


def codex_responses(stack: Stack, reply: Callable[[dict[str, Any]], Any]) -> str:
    """Serve the ChatGPT codex responses SSE stream; `reply(request_json)` returns the JSON object to stream."""
    def handler(req: EdgeRequest) -> tuple[int, Any]:
        text = json.dumps(reply(req.json()))
        event = {"type": "response.output_text.delta", "delta": text}
        completed = {"type": "response.completed", "response": {"status": "completed"}}
        return 200, f"data: {json.dumps(event)}\n\ndata: {json.dumps(completed)}\n\ndata: [DONE]\n\n".encode()
    stack.edge.route("POST", r"/backend-api/codex/responses$", handler)
    return stack.edge.url + "/backend-api/codex/responses"


async def deliver_all(runtime: Any) -> list[Any]:
    """Drain the gateway's runtime deliveries to the Bot API edge until idle."""
    import asyncio
    from tg_tools_common.rich_render import RichCapability
    outcomes = []
    while True:
        outcome = await asyncio.to_thread(runtime.deliveries.drain_once,
                                          capability=RichCapability(final_rich_supported=True, draft_rich_supported=False))
        if outcome.status == "idle":
            return outcomes
        outcomes.append(outcome)


async def run_until(task_factory: Callable[[], Any], done: Callable[[], bool], timeout: float = 120,
                    describe: Callable[[], Any] = lambda: "") -> None:
    """Run a production worker loop until `done()` holds, then cancel it."""
    import asyncio
    task = asyncio.create_task(task_factory())
    deadline = time.monotonic() + timeout
    try:
        while not done():
            if task.done():
                task.result()
                raise AssertionError("worker loop exited before the chain completed")
            if time.monotonic() > deadline:
                raise AssertionError(f"chain did not complete in time: {describe()}")
            await asyncio.sleep(0.2)
    finally:
        task.cancel()
        try:
            await task
        except BaseException:
            pass


# --------------------------------------------------------------------------
# Media


def tone_pcm(seconds: float = 1.0, rate: int = 24000) -> bytes:
    import array
    import math
    samples = array.array("h", (int(8000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(int(seconds * rate))))
    return samples.tobytes()


def tone_mp3(path: Path, seconds: float = 2.0) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:a", "libmp3lame", "-b:a", "64k", str(path)], check=True)
    return path
