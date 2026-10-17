#!/usr/bin/env python3
"""ElevenLabs text-to-speech side-car for Eremia's relay container.

Eremia speaks through the Tidal Echo web app, whose backend is pinned upstream
and never patched here. So her voice arrives the way her face did: a side-car
beside the relay plus an overlay in the front end.

This process is the only thing that ever sees ``ELEVENLABS_API_KEY``. The
browser asks *it* for audio and never ElevenLabs directly, so the key cannot
leak into a page, a service worker cache, or a URL.

Stdlib only, deliberately: the image's Python environment belongs to the pinned
upstream backend, and a voice feature must not drag a dependency into it.

Two things protect the credits behind that key, because the endpoint sits on the
same origin as a PWA and upstream's own auth is not ours to extend:
  - an optional shared secret (TTS_SHARED_SECRET); when set, a request without
    it is refused;
  - a daily character budget, persisted on the volume so a restart cannot reset
    it, which caps the worst case whether or not the secret is configured.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The same bracket envelopes the front end strips on screen: a wake stamp or a
# [系统] prefix is plumbing, and hearing it read aloud in her voice is worse than
# useless. Stripped here too, so the cache key matches what is actually spoken.
PREFIX_RE = re.compile(
    r"^(?:\[系统\]|\[论坛唤醒(?:\s+[^\]]+)?\]|\[时间唤醒\s+\S+\s+[^\]]+\])\s*"
)
WHITESPACE_RE = re.compile(r"\s+")
SENTENCE_END = "。！？!?…"
MAX_BODY_BYTES = 64 * 1024


def log(level: str, message: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    print(f"{stamp} [{level}] [tts] {message}", flush=True)


def env_int(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log("WARN", f"{name}={raw!r} is not a number; using {default}")
        return default
    return max(low, min(high, value))


class Config:
    def __init__(self) -> None:
        self.api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        self.voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
        # eleven_multilingual_v2 handles Chinese well and is the stable default;
        # swap in a newer or faster model with the env var alone.
        self.model_id = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2").strip()
        self.output_format = os.environ.get("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128").strip()
        self.api_base = os.environ.get("ELEVENLABS_API_BASE", "https://api.elevenlabs.io").rstrip("/")
        # The web app authenticates to the relay with `Authorization: Bearer
        # <secret>`, and that secret is the relay's own. So if this container
        # already has RELAY_SECRET, reuse it rather than asking 瓷瓷 to copy the
        # same value into a second variable. TTS_SHARED_SECRET still wins when set.
        self.shared_secret = (
            os.environ.get("TTS_SHARED_SECRET", "").strip()
            or os.environ.get("RELAY_SECRET", "").strip()
        )
        # If the web app keeps its credential in an HttpOnly cookie, the page
        # cannot read it to send a header -- but the browser attaches it to a
        # same-origin fetch anyway. Name that cookie here and the check works
        # with no front-end involvement at all.
        self.secret_cookie = os.environ.get("TTS_SECRET_COOKIE", "").strip()
        self.port = env_int("TTS_PORT", 3012, 1024, 65535)
        self.max_chars = env_int("TTS_MAX_CHARS", 1200, 100, 5000)
        self.timeout = env_int("TTS_TIMEOUT_SECONDS", 60, 5, 300)
        self.daily_char_budget = env_int("TTS_DAILY_CHAR_BUDGET", 50_000, 0, 10_000_000)
        self.cache_dir = Path(os.environ.get("TTS_CACHE_DIR", "/data/tts-cache"))
        self.cache_max_bytes = env_int(
            "TTS_CACHE_MAX_BYTES", 512 * 1024 * 1024, 8 * 1024 * 1024, 8 * 1024 * 1024 * 1024
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.voice_id)


def normalize_text(raw: object, max_chars: int) -> tuple[str, bool]:
    """Return speakable text, and whether it had to be cut short.

    A cut falls back to the last sentence end so a trimmed message stops
    somewhere that sounds deliberate instead of mid-word.
    """
    text = raw if isinstance(raw, str) else ""
    text = PREFIX_RE.sub("", text.strip())
    text = WHITESPACE_RE.sub(" ", text).strip()
    if len(text) <= max_chars:
        return text, False
    head = text[:max_chars]
    cut = max((head.rfind(char) for char in SENTENCE_END), default=-1)
    if cut >= max_chars // 3:
        head = head[: cut + 1]
    return head.rstrip(), True


class Budget:
    """A per-day character allowance, persisted so a restart cannot reset it."""

    def __init__(self, path: Path, limit: int):
        self.path = path
        self.limit = limit
        self._lock = threading.Lock()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def spent(self) -> int:
        data = self._load()
        if data.get("day") != self._today():
            return 0
        value = data.get("chars")
        return int(value) if isinstance(value, (int, float)) else 0

    def remaining(self) -> int:
        if self.limit <= 0:
            return sys.maxsize
        return max(0, self.limit - self.spent())

    def charge(self, chars: int) -> bool:
        """Spend ``chars`` if today still allows it."""
        if self.limit <= 0:
            return True
        with self._lock:
            spent = self.spent()
            if spent + chars > self.limit:
                return False
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}")
                temporary.write_text(
                    json.dumps({"day": self._today(), "chars": spent + chars}),
                    encoding="utf-8",
                )
                os.replace(temporary, self.path)
            except OSError as exc:
                # A volume that cannot record spending must not become a volume
                # with no spending limit.
                log("ERROR", f"budget write failed, refusing to spend: {exc}")
                return False
            return True


class Cache:
    """Content-addressed mp3 cache on the persistent volume.

    The same sentence in the same voice is synthesized once, ever. Eviction is
    oldest-first by mtime, which for a chat log is close enough to least-used.
    """

    def __init__(self, directory: Path, max_bytes: int):
        self.directory = directory
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    @staticmethod
    def key(voice_id: str, model_id: str, output_format: str, text: str) -> str:
        digest = hashlib.sha256()
        for part in (voice_id, model_id, output_format, text):
            digest.update(part.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()

    def path_for(self, key: str) -> Path:
        return self.directory / f"{key}.mp3"

    def get(self, key: str) -> bytes | None:
        path = self.path_for(key)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if not data:
            return None
        try:  # keep mtime meaningful for eviction order
            os.utime(path, None)
        except OSError:
            pass
        return data

    def put(self, key: str, audio: bytes) -> None:
        with self._lock:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                path = self.path_for(key)
                temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
                temporary.write_bytes(audio)
                os.replace(temporary, path)
            except OSError as exc:
                log("WARN", f"cache write failed: {exc}")
                return
            self._evict()

    def _evict(self) -> None:
        try:
            files = sorted(
                (path for path in self.directory.glob("*.mp3") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
            )
            total = sum(path.stat().st_size for path in files)
        except OSError:
            return
        if total <= self.max_bytes:
            return
        target = int(self.max_bytes * 0.9)
        for path in files:
            if total <= target:
                break
            try:
                size = path.stat().st_size
                path.unlink()
                total -= size
            except OSError:
                continue
        log("INFO", f"cache trimmed to {total} bytes")


class Synthesizer:
    """The one place that talks to ElevenLabs, and the only holder of the key."""

    def __init__(self, config: Config):
        self.config = config

    def speak(self, text: str, voice_id: str) -> bytes:
        url = (
            f"{self.config.api_base}/v1/text-to-speech/"
            f"{urllib.parse.quote(voice_id, safe='')}"
            f"?output_format={urllib.parse.quote(self.config.output_format, safe='')}"
        )
        body = json.dumps(
            {"text": text, "model_id": self.config.model_id}, ensure_ascii=False
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "xi-api-key": self.config.api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
        )
        with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
            return response.read()


class Service:
    """Request handling with no HTTP in it, so it can be tested without a socket."""

    def __init__(self, config: Config, synthesizer: Synthesizer | None = None):
        self.config = config
        self.cache = Cache(config.cache_dir, config.cache_max_bytes)
        self.budget = Budget(config.cache_dir / "budget.json", config.daily_char_budget)
        self.synthesizer = synthesizer or Synthesizer(config)

    def health(self) -> dict:
        return {
            "ok": self.config.configured,
            "voice_configured": bool(self.config.voice_id),
            "key_configured": bool(self.config.api_key),
            "auth_required": bool(self.config.shared_secret),
            "auth_source": "cookie" if self.config.secret_cookie else "header",
            "model": self.config.model_id,
            "budget_remaining_today": min(self.budget.remaining(), 10_000_000),
        }

    def offered_secret(
        self,
        header: str | None,
        cookie_header: str | None = None,
        authorization: str | None = None,
    ) -> str:
        """Pull the credential from X-Relay-Secret, then Bearer, then the cookie."""
        if header:
            return header
        if authorization:
            prefix = "bearer "
            if authorization.lower().startswith(prefix):
                return authorization[len(prefix):].strip()
        name = self.config.secret_cookie
        if not name or not cookie_header:
            return ""
        from http.cookies import SimpleCookie

        try:
            jar = SimpleCookie()
            jar.load(cookie_header)
        except Exception:  # a malformed Cookie header is just an absent one
            return ""
        morsel = jar.get(name)
        return morsel.value if morsel is not None else ""

    def authorized(self, offered: str | None) -> bool:
        if not self.config.shared_secret:
            return True
        if not offered:
            return False
        # Length-independent comparison: a timing oracle on a chat toy is still
        # a timing oracle.
        import hmac

        return hmac.compare_digest(offered, self.config.shared_secret)

    def speak(self, payload: object) -> tuple[int, dict, bytes]:
        """Return (status, headers, body) for a speak request."""
        if not self.config.configured:
            return self._error(503, "voice-not-configured", "语音还没配置好")
        if not isinstance(payload, dict):
            return self._error(400, "bad-request", "请求格式不对")
        text, truncated = normalize_text(payload.get("text"), self.config.max_chars)
        if not text:
            return self._error(400, "empty-text", "没有可朗读的内容")

        voice_id = self.config.voice_id
        key = self.cache.key(voice_id, self.config.model_id, self.config.output_format, text)
        cached = self.cache.get(key)
        if cached is not None:
            log("INFO", f"cache hit {key[:12]} ({len(text)} chars)")
            return 200, self._audio_headers(key, True, truncated), cached

        if not self.budget.charge(len(text)):
            log("WARN", f"daily character budget exhausted; refused {len(text)} chars")
            return self._error(429, "budget-exhausted", "今天的朗读额度用完了")

        try:
            audio = self.synthesizer.speak(text, voice_id)
        except urllib.error.HTTPError as exc:
            detail = {401: "key-rejected", 403: "key-rejected", 422: "text-rejected"}.get(
                exc.code, "upstream-error"
            )
            human = {
                "key-rejected": "ElevenLabs 不认这个 key",
                "text-rejected": "ElevenLabs 不接受这段文本",
            }.get(detail, "ElevenLabs 这次没答应")
            log("ERROR", f"elevenlabs HTTP {exc.code} ({detail})")
            return self._error(502, detail, human)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            log("ERROR", f"elevenlabs unreachable: {type(exc).__name__}")
            return self._error(502, "upstream-unreachable", "连不上 ElevenLabs")

        if not audio:
            return self._error(502, "empty-audio", "ElevenLabs 返回了空音频")
        self.cache.put(key, audio)
        log("INFO", f"synthesized {key[:12]} ({len(text)} chars, {len(audio)} bytes)")
        return 200, self._audio_headers(key, False, truncated), audio

    @staticmethod
    def _audio_headers(key: str, cached: bool, truncated: bool) -> dict:
        return {
            "Content-Type": "audio/mpeg",
            "Cache-Control": "no-store",
            "X-TTS-Cache": "hit" if cached else "miss",
            "X-TTS-Truncated": "1" if truncated else "0",
            "X-TTS-Key": key[:12],
        }

    @staticmethod
    def _error(status: int, code: str, message: str) -> tuple[int, dict, bytes]:
        body = json.dumps({"error": code, "message": message}, ensure_ascii=False)
        return (
            status,
            {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
            body.encode("utf-8"),
        )


def make_handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "eremia-tts"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib hook
            pass  # the service logs what matters; this would echo request lines

        def _respond(self, status: int, headers: dict, body: bytes) -> None:
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._respond(
                status,
                {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
                body,
            )

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            if path in ("/tts/health", "/health"):
                self._json(200, service.health())
                return
            self._json(404, {"error": "not-found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            if path not in ("/tts/speak", "/speak"):
                self._json(404, {"error": "not-found"})
                return
            offered = service.offered_secret(
                self.headers.get("X-Relay-Secret"),
                self.headers.get("Cookie"),
                self.headers.get("Authorization"),
            )
            if not service.authorized(offered):
                self._json(401, {"error": "unauthorized", "message": "凭证不对"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY_BYTES:
                self._json(413, {"error": "bad-length", "message": "请求体大小不对"})
                return
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._json(400, {"error": "bad-json", "message": "请求格式不对"})
                return
            status, headers, body = service.speak(payload)
            self._respond(status, headers, body)

    return Handler


def main() -> int:
    config = Config()
    service = Service(config)
    if not config.configured:
        log(
            "WARN",
            "ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID not both set: /tts/speak will "
            "answer 503 until they are. Everything else keeps working.",
        )
    if not config.shared_secret:
        log(
            "WARN",
            "TTS_SHARED_SECRET is not set: /tts/speak is open to anyone who can "
            f"reach it, capped only by the {config.daily_char_budget}-character daily budget.",
        )
    server = ThreadingHTTPServer(("127.0.0.1", config.port), make_handler(service))
    server.daemon_threads = True
    log("INFO", f"listening on 127.0.0.1:{config.port} (model {config.model_id})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
