#!/usr/bin/env python3
"""Refined session carryover (精炼续窗) for Eremia's Claude Code runtime.

Instead of relying on Claude Code's built-in ``/compact`` (a lossy LLM summary
that reads cold), this builds a *new* transcript from selected **verbatim**
events of the current one and resumes into it. What survives is Eremia and
瓷瓷's real words, just fewer of them -- no summarization step, so no tone loss.

This module is the **selection stage** only, made to run locally with
``--dry-run`` so the picking can be inspected against a real transcript before
anything is written. Wiring the trigger (context %), the tmux resume with the
channel flag, verify-and-rollback (last-good) and locking into timekeeper is a
separate, later step; those live in the always-on loop, not here.

Lineage: the scoring heuristics are adapted from LMC-5's public reference
implementation (``extras/claude_code/refined_session_carryover.py``, AGPL-3.0,
github.com/wuxuyun0606-collab/lmc-5). Changes made for Eremia:

- **CJK-aware token budget.** The upstream estimate (``len(json)//3``) badly
  undercounts Chinese, so a "50k" target could really carry 100k+. Here a
  target of 50k means ~50k *real* tokens for a Chinese-heavy transcript, erring
  slightly high so the window stays under budget.
- **Explicit source selection.** Never guess the active session purely by
  mtime in an always-on runtime; ``--source`` / ``--active-session`` win, and
  our own previously-written refined files are skipped when falling back.
- **Atomic write** (tmp + ``os.replace``), matching the rest of this runtime.
- **Resume reminder carries the channel flag**, because a bare
  ``claude --resume`` would load Eremia without the Tidal companion plugin and
  leave him deaf to 瓷瓷's phone.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional, Sequence

# The runtime's channel flag; a resume without it loads Eremia deaf to Tidal.
DEFAULT_CHANNEL_FLAG = "--dangerously-load-development-channels server:companion"

KEEP_TYPES = {"user", "assistant"}

# Bilingual, companion-tuned. Relationship / preference / identity / boundary /
# promise / emotional-state / continuity terms score highest.
MEMORY_RE = re.compile(
    r"(remember|don't forget|preference|likes?|dislikes?|afraid|tired|crying|sad|"
    r"relationship|boundary|nickname|identity|promise|next time|continuity|memory|"
    r"记得|别忘|偏好|喜欢|讨厌|害怕|难过|委屈|开心|累|哭|关系|边界|称呼|身份|"
    r"承诺|以后|连续性|记忆)",
    re.I,
)

STATE_RE = re.compile(
    r"(current task|next step|risk|done|todo|checkpoint|blocked|assumption|"
    r"当前任务|下一步|风险|已完成|待办|检查点|阻塞|假设)",
    re.I,
)

NOISE_RE = re.compile(
    r"(Traceback|Exception|Exit code|Chunk ID|Wall time|stdout|stderr|apply_patch|"
    r"pytest|npm |pnpm |yarn |curl |ssh |tmux |systemctl|journalctl|"
    r"SELECT |INSERT |UPDATE |DELETE |CREATE TABLE|"
    r"/Users/|/root/|/opt/|\.py\b|\.sh\b|\.jsonl\b|\.sqlite\b|\.db\b|"
    r"tool_result|tool_use|<function_calls>|```|^\s*\{)",
    re.I | re.M,
)

HOOK_RE = re.compile(
    r"(hook injection|UserPromptSubmit|SessionStart|additional context|recall result|"
    r"memory recall|召回结果|记忆召回|注入块)",
    re.I,
)

# Runtime-injected content that must never carry over as if Eremia or 瓷瓷 had
# said it. Two shapes: XML-style wrappers Claude Code injects, and the
# bracket-prefixed automated messages this runtime delivers through Tidal --
# ``[时间唤醒 ...]`` / ``[论坛唤醒 ...]`` / ``[系统] ...`` (kept in sync with
# timekeeper's AUTOMATED_PREFIXES and inject.sh's stamp format).
INJECTION_BLOCK_RE = re.compile(
    r"</?(?:task-notification|system-reminder)\b",
    re.I,
)
AUTOMATED_PREFIX_RE = re.compile(
    r"^\s*\[(?:时间唤醒|论坛唤醒|系统)\b",
)

POISON_RE = re.compile(
    r"(AUP|Acceptable Use|policy violation|policy blocked|unsafe content|refusal loop|"
    r"I can't assist|I'm sorry, I can't|风控|安全策略|毒上下文|中毒|拒绝循环|我不能帮助)",
    re.I,
)

# CJK ideographs, kana, and CJK punctuation/full-width forms. Each such char is
# counted as ~1 token (Claude tends to tokenize Chinese near one token per
# character); Latin text is counted at ~4 chars per token.
_CJK_RE = re.compile(
    r"[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]"
)


def log(level: str, message: str) -> None:
    stamp = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    print(f"{stamp} [{level}] [carryover] {message}", file=sys.stderr, flush=True)


def estimate_tokens(value: object) -> int:
    """CJK-aware token estimate.

    Counts CJK characters at ~1 token each and everything else at ~4 chars per
    token, so a token budget expressed in this unit tracks real usage for the
    Chinese-heavy transcripts this runtime produces, rather than the 3x
    undercount of a naive ``len // 3``.
    """
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return max(1, int(cjk + other / 4))


def load_jsonl(path: Path) -> list[dict]:
    events: list[dict] = []
    with path.open(encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # A live transcript may end on a half-written line; skip it.
                continue
    return events


def write_jsonl(path: Path, events: Sequence[dict]) -> None:
    """Atomic write: fill a temp file, then rename over the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if (
                isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            ):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def event_text(event: dict) -> str:
    message = event.get("message")
    if not isinstance(message, dict):
        return ""
    return content_text(message.get("content", ""))


def is_injection_block(event: dict) -> bool:
    text = event_text(event)
    return bool(INJECTION_BLOCK_RE.search(text)) or bool(AUTOMATED_PREFIX_RE.search(text))


def synthetic_user_prefix(template: dict) -> dict:
    """A minimal user event, so carried history never starts on an assistant.

    ``--resume`` expects the message chain to open with a user turn; when the
    highest-signal kept event is an assistant line, this sentinel precedes it
    without discarding it.
    """
    prefix = copy.deepcopy(template)
    prefix["type"] = "user"
    prefix["userType"] = "external"
    prefix["isMeta"] = False
    prefix["isSidechain"] = False
    prefix["message"] = {
        "role": "user",
        "content": "[refined-carryover: preserved high-signal context follows]",
    }
    for key in ("requestId", "isApiErrorMessage", "error", "durationMs", "usage", "costUSD"):
        prefix.pop(key, None)
    return prefix


def ensure_user_first(events: Sequence[dict]) -> list[dict]:
    selected = list(events)
    if not selected or selected[0].get("type") == "user":
        return selected
    return [synthetic_user_prefix(selected[0]), *selected]


def compact_text(text: str, max_chars: int) -> str:
    """Collapse blank runs and, only for over-long events, drop the low-value
    middle while keeping both ends."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2].rstrip()
    tail = text[-max_chars // 2 :].lstrip()
    return head + "\n\n[refined-carryover: long low-value middle omitted]\n\n" + tail


def sanitize_event(event: dict, max_chars: int) -> Optional[dict]:
    """Return a text-only copy of a dialogue event.

    Any ``tool_use`` / ``tool_result`` blocks are dropped, keeping only text.
    That is deliberate: with no tool_use surviving, nothing can be orphaned from
    its tool_result (which would make ``--resume`` error), and Claude Code
    reloads the full MCP tool schemas on resume anyway -- so Eremia keeps every
    tool without needing an in-transcript example.
    """
    text = compact_text(event_text(event), max_chars=max_chars)
    if not text:
        return None
    clean = copy.deepcopy(event)
    message = clean.get("message")
    if not isinstance(message, dict):
        return None
    if isinstance(message.get("content"), list):
        message["content"] = [{"type": "text", "text": text}]
    else:
        message["content"] = text
    clean["message"] = message
    return clean


def is_dialogue_event(event: dict) -> bool:
    if event.get("type") not in KEEP_TYPES:
        return False
    if event.get("isMeta") or event.get("isSidechain"):
        return False
    message = event.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content", "")
    if isinstance(content, list):
        block_types = {item.get("type") for item in content if isinstance(item, dict)}
        # Tool-only turns (no text) carry no relational signal.
        if block_types and block_types <= {"tool_result", "tool_use"}:
            return False
    return bool(event_text(event).strip())


@dataclass
class Candidate:
    index: int
    event: dict
    reason: str
    priority: int
    token_estimate: int
    preview: str


@dataclass
class CarryoverStats:
    source_dialogue_events: int
    clean_candidates: int
    selected_gold: int
    selected_state: int
    selected_tail: int
    dropped_for_budget: int
    poison_score: int
    estimated_tokens: int


def _preview(text: str, width: int = 72) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def classify_event(index: int, event: dict, max_chars: int) -> Optional[Candidate]:
    if not is_dialogue_event(event):
        return None
    if is_injection_block(event):
        return None
    text = event_text(event)
    memory_hits = len(MEMORY_RE.findall(text))
    state_hits = len(STATE_RE.findall(text))
    noise_hits = len(NOISE_RE.findall(text))
    hook_hits = len(HOOK_RE.findall(text))

    if hook_hits >= 2 and memory_hits == 0:
        return None
    if len(text) > 9000 and memory_hits < 2:
        return None
    if noise_hits >= 6 and memory_hits == 0:
        return None
    if noise_hits >= 10 and memory_hits < 3:
        return None
    if "```" in text and len(text) > 1800 and memory_hits < 2:
        return None

    clean = sanitize_event(event, max_chars=max_chars)
    if clean is None:
        return None

    tokens = estimate_tokens(clean)
    preview = _preview(event_text(clean))
    if memory_hits >= 2 and noise_hits <= max(4, memory_hits + 2):
        return Candidate(
            index, clean, "gold-memory", 90 + memory_hits * 3 - noise_hits, tokens, preview
        )
    if memory_hits >= 1 and state_hits >= 1 and len(text) <= 2800 and noise_hits <= 4:
        return Candidate(
            index, clean, "state-note", 70 + memory_hits + state_hits, tokens, preview
        )
    if state_hits >= 2 and len(text) <= 1600 and noise_hits <= 2:
        return Candidate(index, clean, "task-checkpoint", 55 + state_hits, tokens, preview)
    if len(text) <= 1800 and noise_hits <= 2 and hook_hits == 0:
        return Candidate(
            index, clean, "natural-tail", 35 + memory_hits + state_hits, tokens, preview
        )
    return None


def recent_poison_score(events: Sequence[dict], window: int = 30) -> int:
    recent = [event_text(event) for event in events if event.get("type") in KEEP_TYPES][-window:]
    return len(POISON_RE.findall("\n".join(recent)))


def select_refined_events(
    events: Sequence[dict],
    target_tokens: int = 50_000,
    tail_events: int = 14,
    max_event_chars: int = 3600,
) -> tuple[list[Candidate], CarryoverStats]:
    """Pick the events to carry over, returning ordered Candidates + stats.

    Returns Candidates (not bare events) so a dry-run can show *why* each line
    was kept and its token cost. The natural tail is always protected from the
    budget trim; only lower-priority gold/state events are dropped to fit.
    """
    poison = recent_poison_score(events)
    candidates = [
        candidate
        for index, event in enumerate(events)
        for candidate in [classify_event(index, event, max_chars=max_event_chars)]
        if candidate is not None
    ]

    selected: dict[int, Candidate] = {}
    gold = [candidate for candidate in candidates if candidate.reason == "gold-memory"]
    state = [
        candidate
        for candidate in candidates
        if candidate.reason in {"state-note", "task-checkpoint"}
    ]
    tail = candidates[-tail_events:] if tail_events > 0 else []

    for candidate in sorted(gold, key=lambda c: c.priority, reverse=True)[:80]:
        selected[candidate.index] = candidate
    for candidate in sorted(state, key=lambda c: c.priority, reverse=True)[:20]:
        selected[candidate.index] = candidate
    for candidate in tail:
        selected[candidate.index] = candidate

    selected_list = sorted(selected.values(), key=lambda c: c.index)
    total = sum(candidate.token_estimate for candidate in selected_list)
    dropped = 0
    if total > target_tokens:
        tail_ids = {candidate.index for candidate in tail}
        removable = sorted(
            [candidate for candidate in selected_list if candidate.index not in tail_ids],
            key=lambda c: (c.priority, -c.index),
        )
        remove_ids: set[int] = set()
        for candidate in removable:
            if total <= target_tokens:
                break
            remove_ids.add(candidate.index)
            total -= candidate.token_estimate
        dropped = len(remove_ids)
        selected_list = [c for c in selected_list if c.index not in remove_ids]

    tail_ids = {candidate.index for candidate in tail}
    stats = CarryoverStats(
        source_dialogue_events=sum(1 for event in events if event.get("type") in KEEP_TYPES),
        clean_candidates=len(candidates),
        selected_gold=sum(1 for c in selected_list if c.reason == "gold-memory"),
        selected_state=sum(
            1 for c in selected_list if c.reason in {"state-note", "task-checkpoint"}
        ),
        selected_tail=sum(1 for c in selected_list if c.index in tail_ids),
        dropped_for_budget=dropped,
        poison_score=poison,
        estimated_tokens=total,
    )
    return selected_list, stats


def rewrite_session(events: Sequence[dict], new_session_id: str) -> list[dict]:
    """Give every carried event fresh uuids and a coherent parent chain under
    the new session id, so ``--resume`` sees one continuous conversation."""
    rewritten: list[dict] = []
    previous_uuid: Optional[str] = None
    for event in events:
        clean = copy.deepcopy(event)
        event_uuid = str(uuid.uuid4())
        clean["sessionId"] = new_session_id
        clean["uuid"] = event_uuid
        clean["parentUuid"] = previous_uuid
        previous_uuid = event_uuid
        rewritten.append(clean)
    return rewritten


def resolve_source(
    project_dir: Optional[Path],
    explicit_source: Optional[Path],
    active_session: Optional[str],
) -> Optional[Path]:
    """Decide which transcript to refine.

    Priority: an explicit ``--source`` file; then the ``--active-session`` id's
    JSONL inside the project dir; then the newest ``*.jsonl`` -- excluding files
    this tool itself wrote (``*.refined.jsonl``), so a second run never picks up
    its own previous output as if it were the live session.
    """
    if explicit_source is not None:
        return explicit_source if explicit_source.is_file() else None
    if project_dir is None:
        return None
    if active_session:
        candidate = project_dir / f"{active_session}.jsonl"
        if candidate.is_file():
            return candidate
        log("WARN", f"active session {active_session}.jsonl not found; falling back to mtime")
    try:
        files = [
            path
            for path in project_dir.glob("*.jsonl")
            if path.is_file() and not path.name.endswith(".refined.jsonl")
        ]
    except OSError:
        return None
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def build_refined_transcript(
    events: Sequence[dict],
    target_tokens: int,
    tail_events: int,
    max_event_chars: int,
) -> tuple[Optional[str], Optional[list[dict]], CarryoverStats]:
    """Selection + rewrite, no disk I/O. Returns (new_session_id, jsonl, stats)."""
    candidates, stats = select_refined_events(
        events,
        target_tokens=target_tokens,
        tail_events=tail_events,
        max_event_chars=max_event_chars,
    )
    if not candidates:
        return None, None, stats
    ordered = ensure_user_first([candidate.event for candidate in candidates])
    new_session_id = str(uuid.uuid4())
    return new_session_id, rewrite_session(ordered, new_session_id), stats


def _print_stats(stats: CarryoverStats, target_tokens: int) -> None:
    log(
        "INFO",
        "stats: "
        f"source_dialogue={stats.source_dialogue_events} "
        f"clean_candidates={stats.clean_candidates} "
        f"gold={stats.selected_gold} state={stats.selected_state} "
        f"tail={stats.selected_tail} dropped_for_budget={stats.dropped_for_budget} "
        f"est_tokens~{stats.estimated_tokens}/{target_tokens} "
        f"poison={stats.poison_score}",
    )


def _print_preview(candidates: Sequence[Candidate]) -> None:
    print("\n--- would keep (in transcript order) ---", file=sys.stderr)
    for candidate in candidates:
        event_type = candidate.event.get("type", "?")
        print(
            f"  [{candidate.reason:<15}] {event_type:<9} "
            f"~{candidate.token_estimate:>5}tok  {candidate.preview}",
            file=sys.stderr,
        )
    print("--- end preview ---\n", file=sys.stderr)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a refined Claude Code resume transcript (精炼续窗)."
    )
    parser.add_argument("--project-dir", type=Path, help="Claude Code project transcript directory")
    parser.add_argument("--source", type=Path, help="explicit source JSONL transcript")
    parser.add_argument(
        "--active-session",
        help="session id of the live session; preferred over newest-mtime guessing",
    )
    parser.add_argument("--target-dir", type=Path, help="where to write the new JSONL")
    parser.add_argument(
        "--target-tokens",
        type=int,
        default=int(os.getenv("EREMIA_CARRYOVER_TARGET_TOKENS", "50000")),
        help="approximate real-token budget for carried context (default 50000)",
    )
    parser.add_argument(
        "--tail-events",
        type=int,
        default=int(os.getenv("EREMIA_CARRYOVER_TAIL_EVENTS", "14")),
    )
    parser.add_argument(
        "--max-event-chars",
        type=int,
        default=int(os.getenv("EREMIA_CARRYOVER_MAX_EVENT_CHARS", "3600")),
    )
    parser.add_argument(
        "--channel-flag",
        default=os.getenv("EREMIA_CHANNEL_FLAG", DEFAULT_CHANNEL_FLAG),
        help="flags the resume command must carry so Eremia keeps the Tidal channel",
    )
    parser.add_argument(
        "--allow-poison",
        action="store_true",
        help="do not fail closed on recent AUP/policy/refusal poison",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="analyze and preview only; write nothing",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    source = resolve_source(args.project_dir, args.source, args.active_session)
    if source is None:
        log("ERROR", "no source transcript (need --source, or --project-dir with a *.jsonl)")
        return 2
    log("INFO", f"source transcript: {source}")

    events = load_jsonl(source)
    if not events:
        log("ERROR", "source transcript has no parseable events")
        return 1

    candidates, stats = select_refined_events(
        events,
        target_tokens=args.target_tokens,
        tail_events=args.tail_events,
        max_event_chars=args.max_event_chars,
    )
    _print_stats(stats, args.target_tokens)
    _print_preview(candidates)

    if stats.poison_score >= 2 and not args.allow_poison:
        log(
            "ERROR",
            "refused (fail-closed): recent context looks policy/AUP poisoned. "
            "Start a fresh window from durable memory instead, or pass --allow-poison "
            "if this is a false positive (e.g. ordinary declines).",
        )
        return 1
    if not candidates:
        log("ERROR", "refused: no clean carryover events selected")
        return 1

    if args.dry_run:
        log(
            "INFO",
            f"dry-run: would keep {len(candidates)} events, ~{stats.estimated_tokens} real tokens. "
            "Nothing written.",
        )
        return 0

    ordered = ensure_user_first([candidate.event for candidate in candidates])
    new_session_id = str(uuid.uuid4())
    out_dir = args.target_dir or source.parent
    out_path = out_dir / f"{new_session_id}.jsonl"
    write_jsonl(out_path, rewrite_session(ordered, new_session_id))

    log("INFO", f"new session: {new_session_id}")
    log("INFO", f"new file: {out_path}")
    print(f"claude {args.channel_flag} --resume {new_session_id}")
    log(
        "WARN",
        "resume MUST carry the channel flag above, or Eremia loads without the "
        "Tidal companion plugin and goes deaf to 瓷瓷's phone.",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
