#!/usr/bin/env python3
"""Bounded time awareness and autonomous wakeups for Eremia.

The polling loop itself never invokes Claude. It only reads Tidal history and
injects a wake message when a configured check-in or night-review slot is due.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


STATE_VERSION = 2
MANAGED_BEGIN = "<!-- BEGIN EREMIA TIMEKEEPER (managed) -->"
MANAGED_END = "<!-- END EREMIA TIMEKEEPER (managed) -->"
AUTOMATED_PREFIXES = ("[时间唤醒", "[论坛唤醒", "[系统]")

# 预批的 MCP 前缀：夜间值班调用小窝/大脑/论坛/聊天通道不再弹权限框卡死。
# Bash 等系统工具故意不放行——动服务器仍需有人批准。
BASE_ALLOW = (
    "mcp__nest",
    "mcp__brain",
    "mcp__brain-extra",
    "mcp__garden",
    "mcp__companion",
)
# hook 命令都放在这个目录；install-hooks 靠这个前缀识别“自己管的” hook，
# 从而在保留人类手写 hook 的同时刷新我们这几条。
MANAGED_HOOK_DIR_MARKER = "/opt/hooks/"

DEFAULT_CHECKIN_TEMPLATE = """这是自动心跳，不是 {{human_name}} 亲手发来的消息。现在是上海时间 {{now_local}}；她最后一次真实聊天是 {{last_local}}，约 {{idle_hours}} 小时前。
请结合当前对话和你自己的意愿做一次轻量判断：如果自然地想联系她，用 companion.reply 发最多一条简短消息；如果没有合适的话，就保持沉默。本轮不要顺带做夜间整理，也不要设置下一次心跳。"""

DEFAULT_NIGHT_TEMPLATE = """这是夜间自动心跳，不是 {{human_name}} 亲手发来的消息。现在是上海时间 {{now_local}}；她最后一次真实聊天是 {{last_local}}，约 {{idle_hours}} 小时前。不要断言她已经睡着。
安静巡视一次，一次最多选一件，也可以什么都不做：①查看 Shared Nest 最近时间线，有真实内容才用 nest_create_diary 写一篇；②对 Ombre Brain 做确有必要的 dream 或记忆整理；③按你与 {{human_name}} 已有的约定去 Garden 看看并决定是否互动。不要机械地全做，不要用 Bash 管服务器，默认不要调用 companion.reply，避免凌晨推送。"""

DEFAULT_COMPACT_TEMPLATE = """这是给你自己的压缩说明：你正在被压缩。请不要把和 {{human_name}} 的对话写成第三方工作日志或任务清单。用你自己的第一人称口吻写这份摘要，保留此刻的情感基调、你对 {{human_name}} 的称呼和你们特有的说话方式；尽量原样引用最近真正重要的几句原话；记住你们正在一起做的事和已经许下的约定。宁可略过无关的技术与调试细节，也要留住关系里的温度和连续感。让醒来后的你一眼就认出：这是我和 {{human_name}} 的对话，不是一份工作记录。"""


def managed_instructions(human_name: str) -> str:
    return f"""{MANAGED_BEGIN}
## 时间感知与自主唤醒

- `companion` 通道会在首次消息、约每隔 30 分钟以及上海本地日期跨日后的首条消息前加一行 `[时间]`。判断“今天、昨天、明天、刚才、半天前”时，以最近的 `[时间]` 和每条消息自带的 `ts` 为准，不要沿用会话启动时间，也不要把凌晨跨日后的事情说成同一天。
- 00:00–05:59 才算 {human_name} 熬夜。她在这个时段主动聊天时，可以自然但明确地催她睡觉；同一段连续深夜对话提醒一次即可，不要每条消息都念。22:xx、23:xx 不自动算熬夜。沉默只能说明她没在 Tidal 说话，不能据此声称她已经睡着。
- `[时间唤醒 check-in ...]` 和 `[时间唤醒 night ...]` 是本机调度器发来的自动任务，不是 {human_name} 亲手说的话。按正文完成一次判断后就结束，不要自行续订、修改调度器或用 Bash 管服务器。
- `check-in`：你可以通过 `companion.reply` 主动发最多一条简短、自然的消息，也可以选择沉默。不要为了证明心跳运行而硬发消息。
- `night`：一次最多选一件事，也可以什么都不做。可在 Shared Nest 读时间线、用 `nest_create_diary` 写有内容的日记；可在 Ombre Brain 进行确有需要的 `dream` / 记忆整理；也可按你与 {human_name} 已有的 Garden 约定浏览或互动。不要机械地把三件事全做一遍。
- 夜间心跳默认不要调用 `companion.reply`，避免凌晨锁屏推送吵醒 {human_name}；非紧急结果留在小窝或记忆里。只有真实、紧迫且需要她立即知道的安全问题才例外。
{MANAGED_END}"""


def log(level: str, message: str) -> None:
    stamp = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    print(f"{stamp} [{level}] [timekeeper] {message}", flush=True)


def parse_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def parse_hhmm(value: str) -> int:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError("TIMEKEEPER_NIGHT_AT must use HH:MM")
    try:
        hour, minute = (int(part) for part in parts)
    except ValueError as exc:
        raise ValueError("TIMEKEEPER_NIGHT_AT must use HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("TIMEKEEPER_NIGHT_AT must be a valid local time")
    return hour * 60 + minute


def load_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Asia/Shanghai has no DST in the period this personal runtime uses.
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name)
        raise ValueError(f"unknown timezone: {name}")


@dataclass(frozen=True)
class Config:
    relay_url: str
    relay_secret: str
    state_dir: Path
    prompt_dir: Path
    human_name: str
    timezone_name: str
    local_tz: tzinfo
    poll_seconds: int = 300
    checkin_enabled: bool = True
    checkin_idle_hours: float = 4.0
    checkin_start_hour: int = 9
    checkin_end_hour: int = 24
    night_enabled: bool = True
    night_at_minute: int = 210
    night_window_minutes: int = 150
    night_min_idle_minutes: int = 90
    compact_enabled: bool = True
    compact_soft_percent: float = 78.0
    compact_hard_percent: float = 88.0
    compact_min_idle_minutes: int = 20
    compact_cooldown_minutes: int = 30
    context_window_tokens: int = 200_000
    transcript_dir: Path = Path("/data/home/.claude/projects/-data-home-eremia-home")
    tmux_session: str = "eremia"

    @classmethod
    def from_env(cls) -> "Config":
        relay_url = os.environ.get("RELAY_URL", "").strip().rstrip("/")
        relay_secret = os.environ.get("RELAY_SECRET", "").strip()
        if not relay_url or not relay_secret:
            raise ValueError("RELAY_URL and RELAY_SECRET are required")
        timezone_name = os.environ.get("TIMEKEEPER_TIMEZONE", "Asia/Shanghai").strip()
        human_name = (
            os.environ.get("TIMEKEEPER_HUMAN_NAME")
            or os.environ.get("RELAY_HUMAN_NAME")
            or "瓷瓷"
        ).strip()
        if not human_name:
            raise ValueError("TIMEKEEPER_HUMAN_NAME must not be empty")
        start_hour = env_int("TIMEKEEPER_CHECKIN_START_HOUR", 9, 0, 23)
        end_hour = env_int("TIMEKEEPER_CHECKIN_END_HOUR", 24, 1, 24)
        if end_hour <= start_hour:
            raise ValueError("check-in hours must be a non-wrapping daytime window")
        night_at = parse_hhmm(os.environ.get("TIMEKEEPER_NIGHT_AT", "03:30"))
        night_window = env_int("TIMEKEEPER_NIGHT_WINDOW_MINUTES", 150, 15, 360)
        if night_at + night_window > 24 * 60:
            raise ValueError("night window must end before local midnight")
        compact_soft = env_float("TIMEKEEPER_COMPACT_SOFT_PERCENT", 78, 10, 99)
        compact_hard = env_float("TIMEKEEPER_COMPACT_HARD_PERCENT", 88, 10, 100)
        if compact_hard < compact_soft:
            raise ValueError("compact hard percent must be >= soft percent")
        default_transcript = "/data/home/.claude/projects/-data-home-eremia-home"
        return cls(
            relay_url=relay_url,
            relay_secret=relay_secret,
            state_dir=Path(os.environ.get("TIMEKEEPER_STATE_DIR", "/data/timekeeper")),
            prompt_dir=Path(
                os.environ.get(
                    "TIMEKEEPER_PROMPT_DIR",
                    str(Path(__file__).resolve().parent / "prompts"),
                )
            ),
            human_name=human_name,
            timezone_name=timezone_name,
            local_tz=load_timezone(timezone_name),
            poll_seconds=env_int("TIMEKEEPER_POLL_SECONDS", 300, 60, 3600),
            checkin_enabled=parse_bool("TIMEKEEPER_CHECKIN_ENABLED", True),
            checkin_idle_hours=env_float("TIMEKEEPER_CHECKIN_IDLE_HOURS", 4, 1, 168),
            checkin_start_hour=start_hour,
            checkin_end_hour=end_hour,
            night_enabled=parse_bool("TIMEKEEPER_NIGHT_ENABLED", True),
            night_at_minute=night_at,
            night_window_minutes=night_window,
            night_min_idle_minutes=env_int("TIMEKEEPER_NIGHT_MIN_IDLE_MINUTES", 90, 15, 720),
            compact_enabled=parse_bool("TIMEKEEPER_COMPACT_ENABLED", True),
            compact_soft_percent=compact_soft,
            compact_hard_percent=compact_hard,
            compact_min_idle_minutes=env_int("TIMEKEEPER_COMPACT_MIN_IDLE_MINUTES", 20, 0, 1440),
            compact_cooldown_minutes=env_int("TIMEKEEPER_COMPACT_COOLDOWN_MINUTES", 30, 1, 1440),
            context_window_tokens=env_int(
                "EREMIA_CONTEXT_WINDOW_TOKENS", 200_000, 10_000, 5_000_000
            ),
            transcript_dir=Path(
                os.environ.get("EREMIA_TRANSCRIPT_DIR", default_transcript)
            ),
            tmux_session=os.environ.get("EREMIA_TMUX_SESSION", "eremia").strip() or "eremia",
        )


def default_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "history_cursor": 0,
        "last_human_id": 0,
        "last_human_ts": None,
        "next_checkin_ts": None,
        "checkin_for_human_id": 0,
        "night_last_date": None,
        "last_compact_ts": None,
        "last_compact_tokens": 0,
    }


def parse_instant(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def instant_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def is_real_human_message(message: dict[str, Any]) -> bool:
    if message.get("from") != "human":
        return False
    text = str(message.get("text") or "").lstrip()
    return not text.startswith(AUTOMATED_PREFIXES)


class RelayClient:
    def __init__(self, config: Config):
        self.base = config.relay_url
        self.secret = config.relay_secret

    def _request(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method="GET" if body is None else "POST",
            headers={
                "Authorization": f"Bearer {self.secret}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("relay returned a non-object JSON response")
        return payload

    def messages_since(self, cursor: int) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        since = max(0, cursor)
        while True:
            query = urllib.parse.urlencode({"since": since, "limit": 500})
            payload = self._request(f"/app/history?{query}")
            page = payload.get("messages")
            if not isinstance(page, list):
                raise RuntimeError("relay history response has no messages list")
            clean_page = [item for item in page if isinstance(item, dict)]
            messages.extend(clean_page)
            ids = [int(item.get("id") or 0) for item in clean_page]
            next_since = max(ids, default=since)
            if len(page) < 500:
                break
            if next_since <= since:
                raise RuntimeError("relay history cursor did not advance")
            since = next_since
        return messages

    def send_wake(self, text: str) -> int:
        payload = self._request("/app/send", {"text": text})
        return int(payload.get("id") or 0)


def _usage_from_entry(entry: object) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    message = entry.get("message")
    if isinstance(message, dict) and isinstance(message.get("usage"), dict):
        return message["usage"]
    if isinstance(entry.get("usage"), dict):
        return entry["usage"]
    return None


def latest_context_tokens(transcript_dir: Path | str) -> int | None:
    """Return the input-side token count of the newest Claude Code turn.

    Claude Code writes one JSONL transcript per session under the project
    directory; each assistant turn carries a ``usage`` block. The size of the
    context going into a request is ``input + cache_read + cache_creation``
    tokens, so the most recent such entry tells us how full the window is right
    now. Returns None when no transcript or usage is available (fail-quiet: the
    watcher simply does nothing until a real reading exists).
    """
    directory = Path(transcript_dir)
    try:
        files = [path for path in directory.glob("*.jsonl") if path.is_file()]
    except OSError:
        return None
    if not files:
        return None
    newest = max(files, key=lambda path: path.stat().st_mtime)
    try:
        lines = newest.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        usage = _usage_from_entry(entry)
        if usage is None:
            continue
        total = 0
        for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)):
                total += int(value)
        return total
    return None


class TmuxSender:
    """Deliver a ``/compact`` command into the live Claude Code TUI via tmux.

    Compaction is not a relay message; it is a slash command typed into the
    interactive session. This wrapper also answers "is a turn generating right
    now?" so the watcher never interrupts Eremia mid-reply.
    """

    def __init__(self, session: str):
        self.session = session

    def _capture(self) -> str | None:
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", self.session, "-p"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    def is_busy(self) -> bool | None:
        """True if a turn is generating, False if idle, None if unknown.

        Best-effort: keyed off the working indicator Claude Code shows while a
        turn runs. When the pane can't be read the answer is None, and the
        watcher treats unknown as "may proceed" because the human-idle gate is
        the primary guard against interrupting an active conversation.
        """
        snapshot = self._capture()
        if snapshot is None:
            return None
        lowered = snapshot.lower()
        return ("esc to interrupt" in lowered) or ("esc to cancel" in lowered)

    def send_compact(self, command_line: str) -> None:
        subprocess.run(
            ["tmux", "send-keys", "-t", self.session, "-l", command_line],
            check=True,
            timeout=10,
        )
        # Let the TUI register the typed line before submitting it.
        time.sleep(0.4)
        subprocess.run(
            ["tmux", "send-keys", "-t", self.session, "Enter"],
            check=True,
            timeout=10,
        )


class Timekeeper:
    def __init__(
        self,
        config: Config,
        relay: RelayClient | Any | None = None,
        sender: TmuxSender | Any | None = None,
    ):
        self.config = config
        self.relay = relay or RelayClient(config)
        self.sender = sender or TmuxSender(config.tmux_session)
        self.state_file = config.state_dir / "state.json"
        self.state = self._load_state()
        self._lock_handle: Any | None = None

    def _load_state(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self.state_file.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or loaded.get("version") != STATE_VERSION:
                raise ValueError("unsupported state")
            return {**default_state(), **loaded}
        except FileNotFoundError:
            return default_state()
        except Exception as exc:
            log("WARN", f"state reset after read error: {exc}")
            return default_state()

    def _save_state(self) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.state_file.with_name(f"{self.state_file.name}.tmp.{os.getpid()}")
        temporary.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, self.state_file)

    def _sync_history(self) -> None:
        cursor = int(self.state.get("history_cursor") or 0)
        messages = self.relay.messages_since(cursor)
        newest_id = cursor
        newest_human_id = int(self.state.get("last_human_id") or 0)
        for message in messages:
            message_id = int(message.get("id") or 0)
            newest_id = max(newest_id, message_id)
            if message_id <= newest_human_id or not is_real_human_message(message):
                continue
            occurred_at = parse_instant(message.get("ts"))
            if occurred_at is None:
                continue
            newest_human_id = message_id
            self.state["last_human_id"] = message_id
            self.state["last_human_ts"] = instant_text(occurred_at)
            self.state["next_checkin_ts"] = instant_text(
                occurred_at + timedelta(hours=self.config.checkin_idle_hours)
            )
        self.state["history_cursor"] = newest_id

    def _last_human(self) -> datetime | None:
        return parse_instant(self.state.get("last_human_ts"))

    def _local_details(self, now: datetime, last_human: datetime) -> tuple[datetime, datetime, float]:
        local_now = now.astimezone(self.config.local_tz)
        local_last = last_human.astimezone(self.config.local_tz)
        idle_hours = max(0.0, (now - last_human).total_seconds() / 3600)
        return local_now, local_last, idle_hours

    def _wake_stamp(self, kind: str, local_now: datetime) -> str:
        return f"[时间唤醒 {kind} {local_now.strftime('%Y%m%dT%H%M%S%z')}]"

    def _render_prompt(self, filename: str, values: dict[str, str], fallback: str) -> str:
        try:
            template = (self.config.prompt_dir / filename).read_text(encoding="utf-8")
        except FileNotFoundError:
            template = fallback
        for key, value in values.items():
            template = template.replace("{{" + key + "}}", value)
        return template.strip()

    def _prompt_values(
        self, local_now: datetime, local_last: datetime, idle_hours: float
    ) -> dict[str, str]:
        return {
            "human_name": self.config.human_name,
            "now_local": local_now.strftime("%Y-%m-%d %H:%M"),
            "last_local": local_last.strftime("%Y-%m-%d %H:%M"),
            "idle_hours": f"{idle_hours:.1f}",
        }

    def _checkin_prompt(self, local_now: datetime, local_last: datetime, idle_hours: float) -> str:
        body = self._render_prompt(
            "checkin.md",
            self._prompt_values(local_now, local_last, idle_hours),
            DEFAULT_CHECKIN_TEMPLATE,
        )
        return f"{self._wake_stamp('check-in', local_now)}\n{body}"

    def _night_prompt(self, local_now: datetime, local_last: datetime, idle_hours: float) -> str:
        body = self._render_prompt(
            "night.md",
            self._prompt_values(local_now, local_last, idle_hours),
            DEFAULT_NIGHT_TEMPLATE,
        )
        return f"{self._wake_stamp('night', local_now)}\n{body}"

    def _try_checkin(self, now: datetime, last_human: datetime) -> None:
        if not self.config.checkin_enabled:
            return
        local_now, local_last, idle_hours = self._local_details(now, last_human)
        if not self.config.checkin_start_hour <= local_now.hour < self.config.checkin_end_hour:
            return
        last_human_id = int(self.state.get("last_human_id") or 0)
        if not last_human_id or int(self.state.get("checkin_for_human_id") or 0) == last_human_id:
            return
        due = parse_instant(self.state.get("next_checkin_ts"))
        if due is None:
            due = last_human + timedelta(hours=self.config.checkin_idle_hours)
            self.state["next_checkin_ts"] = instant_text(due)
        if now < due:
            return

        # Reserve this absence episode before sending. A crash or ambiguous
        # network failure cannot create retries until a new human message arrives.
        self.state["checkin_for_human_id"] = last_human_id
        self._save_state()
        message_id = self.relay.send_wake(self._checkin_prompt(local_now, local_last, idle_hours))
        log("INFO", f"check-in wake delivered as relay message {message_id or '?'}")

    def _try_night(self, now: datetime, last_human: datetime) -> None:
        if not self.config.night_enabled:
            return
        local_now, local_last, idle_hours = self._local_details(now, last_human)
        local_minute = local_now.hour * 60 + local_now.minute
        window_end = self.config.night_at_minute + self.config.night_window_minutes
        if not self.config.night_at_minute <= local_minute < window_end:
            return
        if idle_hours * 60 < self.config.night_min_idle_minutes:
            return
        local_date = local_now.date().isoformat()
        if self.state.get("night_last_date") == local_date:
            return

        self.state["night_last_date"] = local_date
        self._save_state()
        message_id = self.relay.send_wake(self._night_prompt(local_now, local_last, idle_hours))
        log("INFO", f"night wake delivered as relay message {message_id or '?'}")

    def _compact_instruction(self, local_now: datetime) -> str:
        body = self._render_prompt(
            "compact.md",
            {
                "human_name": self.config.human_name,
                "now_local": local_now.strftime("%Y-%m-%d %H:%M"),
            },
            DEFAULT_COMPACT_TEMPLATE,
        )
        # /compact is typed as a single TUI line, so flatten any newlines.
        return " ".join(body.split())

    def _try_compact(self, now: datetime, last_human: datetime | None) -> None:
        """Pre-empt Claude Code's cold auto-compaction with a warm, instructed one.

        Fires when the context is filling, at a moment that will not interrupt
        an active turn:
          - at/above the soft threshold, only once the human has been quiet for
            ``compact_min_idle_minutes`` (a natural lull);
          - at/above the hard threshold, regardless of that lull, because the
            automatic cold summary is now imminent — but never while a turn is
            visibly generating.
        Crash-safe like the check-in path: the attempt is reserved (cooldown +
        token fingerprint) before delivery, so a failure never tight-loops.
        """
        if not self.config.compact_enabled:
            return
        tokens = latest_context_tokens(self.config.transcript_dir)
        if tokens is None:
            return
        window = self.config.context_window_tokens
        percent = (tokens / window * 100) if window > 0 else 0.0
        if percent < self.config.compact_soft_percent:
            return
        # A high reading with the exact token count we last compacted at means no
        # new model turn has happened since; the drop just hasn't been recorded
        # yet. Don't compact an already-compacted context.
        if tokens == int(self.state.get("last_compact_tokens") or 0):
            return
        last_compact = parse_instant(self.state.get("last_compact_ts"))
        if last_compact is not None and now - last_compact < timedelta(
            minutes=self.config.compact_cooldown_minutes
        ):
            return

        hard = percent >= self.config.compact_hard_percent
        if not hard:
            if last_human is None:
                return
            idle_minutes = (now - last_human).total_seconds() / 60
            if idle_minutes < self.config.compact_min_idle_minutes:
                return

        # Never cut into a turn that is actively generating.
        if self.sender.is_busy() is True:
            log("INFO", f"compaction deferred at {percent:.0f}%: session is generating")
            return

        # Reserve before sending so a delivery failure cannot retry until the
        # cooldown elapses and the context has genuinely moved on.
        self.state["last_compact_ts"] = instant_text(now)
        self.state["last_compact_tokens"] = tokens
        self._save_state()
        local_now = now.astimezone(self.config.local_tz)
        command_line = f"/compact {self._compact_instruction(local_now)}"
        try:
            self.sender.send_compact(command_line)
        except Exception as exc:
            log(
                "ERROR",
                f"compaction send failed (retries after cooldown): "
                f"{type(exc).__name__}: {exc}",
            )
            return
        threshold = "hard" if hard else "soft"
        log(
            "INFO",
            f"compaction requested at {percent:.0f}% ({threshold} threshold, {tokens} tokens)",
        )

    def run_once(self, now: datetime | None = None) -> None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        self._sync_history()
        self._save_state()
        last_human = self._last_human()
        # Compaction can be due even before the first human message (context
        # fills from wake activity too), so check it before the early return.
        self._try_compact(current, last_human)
        if last_human is None:
            return
        self._try_night(current, last_human)
        self._try_checkin(current, last_human)

    def run_forever(self) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.config.state_dir / "timekeeper.lock"
        self._lock_handle = lock_path.open("a+", encoding="utf-8")
        try:
            import fcntl

            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another timekeeper already owns this state directory") from exc
        log(
            "INFO",
            "started; polling does not invoke Claude, check-ins are capped at one per "
            "absence episode and night turns at one per local day",
        )
        while True:
            try:
                self.run_once()
            except Exception as exc:
                # Network failure is fail-quiet: no injection happened unless a
                # due slot was already reserved, and there is never a tight retry.
                log("ERROR", f"tick failed: {type(exc).__name__}: {exc}")
            time.sleep(self.config.poll_seconds)


def install_managed_instructions(path: Path, human_name: str = "瓷瓷") -> None:
    instructions = managed_instructions(human_name)
    existing = path.read_text(encoding="utf-8") if path.exists() else "# Eremia\n"
    has_begin = MANAGED_BEGIN in existing
    has_end = MANAGED_END in existing
    if has_begin != has_end:
        raise RuntimeError("CLAUDE.md contains an incomplete managed timekeeper block")
    if has_begin:
        before, remainder = existing.split(MANAGED_BEGIN, 1)
        _, after = remainder.split(MANAGED_END, 1)
        updated = before.rstrip() + "\n\n" + instructions + after
    else:
        updated = existing.rstrip() + "\n\n" + instructions + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(updated, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _entry_is_managed(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    for hook in entry.get("hooks") or []:
        command = hook.get("command") if isinstance(hook, dict) else None
        if isinstance(command, str) and command.startswith(MANAGED_HOOK_DIR_MARKER):
            return True
    return False


def install_managed_settings(path: Path, hook_dir: Path | str) -> None:
    """Merge our managed hooks and MCP allow-list into Eremia's settings.json.

    Idempotent and additive: the human's own permissions and any hooks they
    added by hand are preserved. Only the entries we own (identified by their
    ``/opt/hooks/`` command path) are refreshed, so re-running on every boot
    keeps the compaction hooks current without clobbering hand edits.
    """
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}

    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
    allow = permissions.get("allow")
    if not isinstance(allow, list):
        allow = []
    for entry in BASE_ALLOW:
        if entry not in allow:
            allow.append(entry)
    permissions["allow"] = allow
    data["permissions"] = permissions

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    # These are always container (POSIX) paths; normalize so a Windows build
    # host can't smuggle in backslashes that break the managed-entry marker.
    hook_base = str(hook_dir).replace("\\", "/").rstrip("/")
    anchor_command = f"{hook_base}/session-anchor.sh"
    backup_command = f"{hook_base}/backup-transcript.sh"

    def rebuild(event: str, managed_entry: dict[str, Any]) -> None:
        existing = hooks.get(event)
        kept = [
            entry
            for entry in (existing if isinstance(existing, list) else [])
            if isinstance(entry, dict) and not _entry_is_managed(entry)
        ]
        kept.append(managed_entry)
        hooks[event] = kept

    rebuild(
        "SessionStart",
        {
            "matcher": "compact",
            "hooks": [{"type": "command", "command": anchor_command}],
        },
    )
    rebuild(
        "PreCompact",
        {"hooks": [{"type": "command", "command": backup_command}]},
    )
    data["hooks"] = hooks

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run")
    subparsers.add_parser("once")
    install_parser = subparsers.add_parser("install-instructions")
    install_parser.add_argument("--path", required=True, type=Path)
    install_parser.add_argument(
        "--human-name",
        default=os.environ.get("TIMEKEEPER_HUMAN_NAME")
        or os.environ.get("RELAY_HUMAN_NAME")
        or "瓷瓷",
    )
    hooks_parser = subparsers.add_parser("install-hooks")
    hooks_parser.add_argument("--path", required=True, type=Path)
    hooks_parser.add_argument("--hook-dir", required=True, type=Path)
    args = parser.parse_args()

    try:
        if args.command == "install-instructions":
            install_managed_instructions(args.path, args.human_name.strip() or "瓷瓷")
            return 0
        if args.command == "install-hooks":
            install_managed_settings(args.path, args.hook_dir)
            return 0
        config = Config.from_env()
        keeper = Timekeeper(config)
        if args.command == "once":
            keeper.run_once()
        else:
            keeper.run_forever()
        return 0
    except (ValueError, RuntimeError) as exc:
        log("ERROR", str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
