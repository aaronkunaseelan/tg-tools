"""Contained, headless execution of one Pi prompt."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from tg_tools_common.progress import current_progress

from .roles import resolve_role

# Unset -> pi's global home (OAuth credentials live there); set TTS_PI_HOME
# to point model steps at a dedicated pi home.
HARNESS_HOME = os.environ.get("TTS_PI_HOME") or None
LOG_CAP_BYTES = int(os.environ.get("TTS_PI_LOG_CAP", 4 * 1024 * 1024))
KILL_GRACE_S = 10
FAULT_HOOK = None

ENV_KEEP = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG",
            "TMPDIR", "TZ"}
# Base prefixes every Pi child needs (locale, XDG dirs, Pi home/auth, TLS).
ENV_KEEP_PREFIX = ("LC_", "XDG_", "PI_",
                   "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                   "SSL_", "CURL_CA", "REQUESTS_CA")
# Provider-specific credential prefixes: the child receives only the
# variables required by the selected provider.
#
# openai-codex is deliberately EMPTY. That provider authenticates by OAuth
# out of the pi/codex auth store, never from the environment - verified: a
# bogus OPENAI_API_KEY in the child's env does not change or break the call.
# Forwarding OPENAI_* would therefore buy nothing and would leave a silent
# downgrade path open the moment anyone sets an API key in a systemd unit or
# a shell profile. Keep it empty; see assert_codex_oauth().
PROVIDER_ENV_PREFIX = {
    "azure-foundry-chat": ("AZURE_",),
    "openai-codex": (),
    "anthropic": ("ANTHROPIC_",),
    "google": ("GOOGLE_",),
    # `claude -p` authenticates from the subscription login under HOME.
    # An ANTHROPIC_API_KEY would silently take precedence and bill the API.
    "claude": (),
}
# Never forward these to any child, whatever the provider. An API key present
# in the environment is an OAuth-bypass risk, not a convenience.
ENV_DENY = ("OPENAI_API_KEY", "OPENAI_API_KEY_PATH", "CODEX_API_KEY")
CLAUDE_ENV_DENY = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _hit(point, tag):
    if FAULT_HOOK:
        FAULT_HOOK(point, tag)


def pruned_env(provider: str | None = None) -> dict:
    extra = {n for n in os.environ.get("TTS_PI_ENV_ALLOW", "").split(",") if n}
    prefixes = ENV_KEEP_PREFIX + PROVIDER_ENV_PREFIX.get(provider or "", ())
    env = {k: v for k, v in os.environ.items()
           if k in ENV_KEEP or k in extra or k.startswith(prefixes)}
    # Deny-list wins over every allow path, including TTS_PI_ENV_ALLOW.
    for name in ENV_DENY:
        env.pop(name, None)
    if provider == "claude":
        for name in CLAUDE_ENV_DENY:
            env.pop(name, None)
    return env


def claude_cmd(model: str, effort: str, system_prompt: str | None, flags: list[str], *,
               stream: bool = False, session_id: str | None = None,
               resume: bool = False) -> list[str]:
    """A closed `claude -p` turn on the subscription login: no tools, settings,
    skills or MCP. With ``session_id`` the turn starts (or ``resume``s) a stored
    session; claude keys stored sessions by cwd, so resume from the same cwd."""
    cmd = ["claude", "-p", "--model", model, "--effort", effort,
           "--setting-sources", "", "--tools", "", "--strict-mcp-config",
           "--disable-slash-commands"]
    cmd += (["--output-format", "stream-json", "--input-format", "stream-json", "--verbose"]
            if stream else ["--output-format", "json"])
    if system_prompt:
        cmd += ["--system-prompt", system_prompt]
    if session_id is None:
        cmd.append("--no-session-persistence")
    else:
        cmd += ["--resume" if resume else "--session-id", session_id]
    return cmd + list(flags)


def pi_cmd(provider: str, model: str, thinking: str, system_prompt: str | None,
           flags: list[str], *, session_file: str | None = None,
           allow_tools: bool = False, json_mode: bool = False) -> list[str]:
    """A headless `pi -p` turn. A ``session_file`` is created on first use and
    continued by every later turn that names it."""
    cmd = ["pi", "-p"]
    if json_mode:
        cmd += ["--mode", "json"]
    cmd += ["--session", session_file] if session_file else ["--no-session"]
    cmd += ["--provider", provider, "--model", model,
            "--models", provider + "/" + model, "--thinking", thinking]
    # Without this, pi builds its default *coding assistant* system prompt
    # (~1590 chars: "expert coding assistant", "show file paths clearly",
    # and an index of pi's own docs) and sends it on every call. That
    # persona fights a --no-tools spoken-prose task; see roles.py.
    if system_prompt:
        cmd += ["--system-prompt", system_prompt]
    # Tool disabling is not a sandbox: also disable extensions, skills,
    # prompt templates, and context-file discovery.
    cmd += ["--no-extensions", "--no-skills", "--no-prompt-templates",
            "--no-context-files"]
    cmd += list(flags)
    if not allow_tools:
        cmd.append("--no-tools")
    return cmd


def pi_env(provider: str) -> dict:
    env = pruned_env(provider)
    if HARNESS_HOME:
        env["PI_CODING_AGENT_DIR"] = HARNESS_HOME
    else:
        env.pop("PI_CODING_AGENT_DIR", None)
    env.update(PI_TELEMETRY="0", TERM="xterm-256color")
    return env


def _truncate(path: str, cap: int) -> None:
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size <= cap:
        return
    with open(path, "r+b") as log:
        log.truncate(cap)
        log.seek(0, os.SEEK_END)
        log.write(b"\n[truncated: log exceeded %d bytes (was %d)]\n" % (cap, size))


def _kill_group(p: subprocess.Popen, grace_s: float) -> None:
    """SIGTERM the whole group, grace, SIGKILL, reap."""
    try:
        pgid = os.getpgid(p.pid)
    except ProcessLookupError:
        p.wait()
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        p.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    p.wait()


def run_contained(cmd: list[str], prompt: str, attempt_dir: str, env: dict,
                  timeout_s: int, cap_bytes: int = LOG_CAP_BYTES,
                  grace_s: float = KILL_GRACE_S,
                  tag: str = "") -> tuple[int | None, bool, str, str]:
    """Run in a new session, with stdin prompt and capped file logs."""
    _hit("attempt.before-start", tag)
    os.makedirs(attempt_dir, exist_ok=True)
    out_p = os.path.join(attempt_dir, "stdout.log")
    err_p = os.path.join(attempt_dir, "stderr.log")
    timed_out = False
    with open(out_p, "wb") as stdout, open(err_p, "wb") as stderr:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=stdout,
                             stderr=stderr, cwd=attempt_dir, env=env,
                             shell=False, start_new_session=True,
                             close_fds=True)
        _hit("attempt.after-start", tag)
        try:
            sink = current_progress()
            deadline = time.monotonic() + timeout_s
            input_bytes = prompt.encode("utf-8")
            while True:
                sink.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout_s)
                try:
                    p.communicate(input=input_bytes, timeout=min(5, remaining))
                    break
                except subprocess.TimeoutExpired:
                    # communicate retains buffered input across timeout retries.
                    input_bytes = None
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(p, grace_s)
        except BaseException:
            _kill_group(p, grace_s)
            raise
    _truncate(out_p, cap_bytes)
    _truncate(err_p, cap_bytes)
    _hit("attempt.after-capture", tag)
    return p.returncode, timed_out, out_p, err_p


@dataclass
class StepResult:
    text: str
    duration_s: float
    ok: bool
    error: str = ""
    attempt_dir: str = ""
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    model_usage: dict = field(default_factory=dict)


class PiRunner:
    """Run one prompt headlessly and return its stdout."""

    def __init__(self, role: str, cwd: str, mock: bool = False,
                 session_dir: str | None = None):
        config = resolve_role(role)
        self.provider = config["provider"]
        self.model = config["model"]
        self.thinking = config["thinking"]
        self.flags = config["flags"] or []
        self.system_prompt = config.get("system_prompt")
        self.home = HARNESS_HOME
        self.cwd = cwd
        self.mock = mock or os.environ.get("TTS_PI_MOCK") == "1"
        self.session_dir = session_dir

    def run(self, prompt: str, *, allow_tools: bool, timeout_s: int,
            session_name: str | None = None,
            attempt_dir: str | None = None,
            content_blocks: list[dict] | None = None) -> StepResult:
        if self.mock:
            _hit("attempt.before-start", session_name or "")
            return self._mock(prompt)
        if self.provider == "claude":
            return self._run_claude(prompt, pruned_env(self.provider), timeout_s, session_name,
                                    attempt_dir, content_blocks)
        env = pi_env(self.provider)
        session_file = None
        if self.session_dir and session_name:
            os.makedirs(self.session_dir, exist_ok=True)
            session_file = os.path.join(self.session_dir, artifacts_safe(session_name) + ".jsonl")
        cmd = pi_cmd(self.provider, self.model, self.thinking, self.system_prompt, self.flags,
                     session_file=session_file, allow_tools=allow_tools)
        if attempt_dir is None:
            # Contain default attempt dirs under the caller-supplied cwd so a
            # cleaned-up cwd removes probe artifacts too.
            attempt_dir = tempfile.mkdtemp(
                prefix="tts-pi-attempt-",
                dir=self.cwd if self.cwd and os.path.isdir(self.cwd) else None)
        started = time.monotonic()
        rc, timed_out, out_p, err_p = run_contained(
            cmd, prompt, attempt_dir, env, timeout_s, tag=session_name or "")
        duration = time.monotonic() - started
        stdout = _read_capped(out_p, LOG_CAP_BYTES)
        if timed_out:
            return StepResult("", duration, False,
                              "step timed out after %ss (process group terminated)" % timeout_s,
                              attempt_dir)
        if rc != 0:
            stderr = _read_capped(err_p, LOG_CAP_BYTES).strip()[-2000:]
            return StepResult(stdout, duration, False, stderr or "exit %s" % rc,
                              attempt_dir)
        return StepResult(stdout.strip(), duration, True, "", attempt_dir)

    def _run_claude(self, prompt: str, env: dict, timeout_s: int,
                    session_name: str | None, attempt_dir: str | None,
                    content_blocks: list[dict] | None = None) -> StepResult:
        """One closed `claude -p` turn: no tools, settings, MCP, or session."""
        cmd = claude_cmd(self.model, self.thinking, self.system_prompt, self.flags,
                         stream=content_blocks is not None)
        if content_blocks is not None:
            prompt = json.dumps({"type": "user", "message": {
                "role": "user", "content": content_blocks}}, ensure_ascii=False) + "\n"
        if attempt_dir is None:
            attempt_dir = tempfile.mkdtemp(
                prefix="tts-claude-attempt-",
                dir=self.cwd if self.cwd and os.path.isdir(self.cwd) else None)
        started = time.monotonic()
        rc, timed_out, out_p, err_p = run_contained(
            cmd, prompt, attempt_dir, env, timeout_s, tag=session_name or "")
        duration = time.monotonic() - started
        if timed_out:
            return StepResult("", duration, False,
                              "step timed out after %ss (process group terminated)" % timeout_s,
                              attempt_dir)
        stdout = _read_capped(out_p, LOG_CAP_BYTES)
        try:
            if content_blocks is not None:
                events = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
                reply = next(event for event in reversed(events) if event.get("type") == "result")
            else:
                reply = json.loads(stdout)
        except (json.JSONDecodeError, StopIteration):
            stderr = _read_capped(err_p, LOG_CAP_BYTES).strip()[-2000:]
            return StepResult("", duration, False,
                              stderr or stdout[-2000:] or "exit %s" % rc, attempt_dir)
        text = str(reply.get("result") or "")
        if rc != 0 or reply.get("is_error"):
            return StepResult("", duration, False, text[-2000:] or "exit %s" % rc,
                              attempt_dir, reply.get("usage", {}), reply.get("total_cost_usd"), reply.get("modelUsage", {}))
        return StepResult(text.strip(), duration, True, "", attempt_dir,
                          reply.get("usage", {}), reply.get("total_cost_usd"),
                          reply.get("modelUsage", {}))

    def _mock(self, prompt: str) -> StepResult:
        first_line = prompt.splitlines()[0] if prompt else ""
        return StepResult(json.dumps({"mock": True, "step": first_line}),
                          0.01, True)


def artifacts_safe(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")


def _read_capped(path: str, cap_bytes: int = LOG_CAP_BYTES) -> str:
    """Read at most cap_bytes (+truncation-marker slack) from a log file.

    Callers must pass the same cap they gave run_contained so the read
    limit matches the on-disk truncation limit.
    """
    try:
        with open(path, "rb") as source:
            return source.read(cap_bytes + 4096).decode("utf-8", errors="replace")
    except OSError:
        return ""
