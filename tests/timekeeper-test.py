from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path


SOURCE = Path(sys.argv.pop(1) if len(sys.argv) > 1 else Path(__file__).parents[1] / "timekeeper" / "timekeeper.py")
SPEC = importlib.util.spec_from_file_location("eremia_timekeeper", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import {SOURCE}")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeRelay:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    def messages_since(self, cursor):
        return [message for message in self.messages if int(message["id"]) > cursor]

    def send_wake(self, text):
        self.sent.append(text)
        return 1000 + len(self.sent)


class FailingRelay(FakeRelay):
    def send_wake(self, text):
        self.sent.append(text)
        raise OSError("simulated relay outage")


class FakeSender:
    def __init__(self, busy=False):
        self._busy = busy
        self.sent = []

    def is_busy(self):
        return self._busy

    def send_compact(self, command_line):
        self.sent.append(command_line)


def write_transcript(directory, input_tokens=0, cache_read=0, cache_creation=0, name="session.jsonl"):
    path = Path(directory) / name
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "usage": {
                        "input_tokens": input_tokens,
                        "cache_read_input_tokens": cache_read,
                        "cache_creation_input_tokens": cache_creation,
                        "output_tokens": 10,
                    },
                },
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TimekeeperTests(unittest.TestCase):
    def config(self, state_dir: Path, **overrides):
        values = dict(
            relay_url="http://relay/relay",
            relay_secret="test-secret",
            state_dir=state_dir,
            prompt_dir=SOURCE.parent / "prompts",
            human_name="瓷瓷",
            timezone_name="Asia/Shanghai",
            local_tz=MODULE.load_timezone("Asia/Shanghai"),
            poll_seconds=300,
            checkin_enabled=True,
            checkin_idle_hours=4,
            checkin_start_hour=9,
            checkin_end_hour=24,
            night_enabled=True,
            night_at_minute=210,
            night_window_minutes=150,
            night_min_idle_minutes=90,
        )
        values.update(overrides)
        return MODULE.Config(**values)

    def test_checkin_runs_once_per_absence_and_ignores_automated_messages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime(2026, 8, 25, 13, 0, tzinfo=UTC)  # 21:00 Shanghai
            human_at = now - timedelta(hours=4, minutes=5)
            relay = FakeRelay([
                {"id": 1, "from": "human", "text": "我先去忙啦", "ts": MODULE.instant_text(human_at)},
                {"id": 2, "from": "human", "text": "[论坛唤醒 x] turn", "ts": MODULE.instant_text(now)},
            ])
            keeper = MODULE.Timekeeper(self.config(Path(temp_dir)), relay=relay)
            keeper.run_once(now)
            keeper.run_once(now + timedelta(minutes=5))
            keeper.run_once(now + timedelta(days=1))

            self.assertEqual(len(relay.sent), 1)
            self.assertTrue(relay.sent[0].startswith("[时间唤醒 check-in"))
            self.assertIn("瓷瓷", relay.sent[0])
            self.assertIn("一件自己想做的小事", relay.sent[0])
            self.assertIn("anchors.md", relay.sent[0])
            self.assertEqual(keeper.state["last_human_id"], 1)
            self.assertEqual(keeper.state["checkin_for_human_id"], 1)

    def test_new_real_message_rearms_checkin_for_the_next_absence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime(2026, 8, 25, 13, 0, tzinfo=UTC)  # 21:00 Shanghai
            relay = FakeRelay([
                {
                    "id": 1,
                    "from": "human",
                    "text": "先忙一会儿",
                    "ts": MODULE.instant_text(now - timedelta(hours=4, minutes=5)),
                },
            ])
            keeper = MODULE.Timekeeper(
                self.config(Path(temp_dir), night_enabled=False), relay=relay
            )
            keeper.run_once(now)

            returned_at = now + timedelta(minutes=5)
            relay.messages.append({
                "id": 2,
                "from": "human",
                "text": "我回来啦",
                "ts": MODULE.instant_text(returned_at),
            })
            keeper.run_once(returned_at)
            keeper.run_once(datetime(2026, 8, 26, 1, 5, tzinfo=UTC))  # 次日 09:05

            self.assertEqual(len(relay.sent), 2)
            self.assertEqual(keeper.state["last_human_id"], 2)
            self.assertEqual(keeper.state["checkin_for_human_id"], 2)

    def test_checkin_stays_quiet_after_midnight(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime(2026, 8, 25, 17, 0, tzinfo=UTC)  # 01:00 Shanghai
            relay = FakeRelay([
                {"id": 1, "from": "human", "text": "晚点见", "ts": MODULE.instant_text(now - timedelta(hours=13))},
            ])
            keeper = MODULE.Timekeeper(self.config(Path(temp_dir), night_enabled=False), relay=relay)
            keeper.run_once(now)
            self.assertEqual(relay.sent, [])

    def test_night_wake_runs_once_and_renders_the_editable_template(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime(2026, 8, 25, 20, 0, tzinfo=UTC)  # 04:00 Shanghai
            relay = FakeRelay([
                {"id": 1, "from": "human", "text": "睡啦", "ts": MODULE.instant_text(now - timedelta(hours=3))},
            ])
            keeper = MODULE.Timekeeper(self.config(Path(temp_dir)), relay=relay)
            keeper.run_once(now)
            keeper.run_once(now + timedelta(minutes=10))

            self.assertEqual(len(relay.sent), 1)
            self.assertTrue(relay.sent[0].startswith("[时间唤醒 night"))
            self.assertIn("瓷瓷", relay.sent[0])
            self.assertIn("2026-08-26 04:00", relay.sent[0])
            self.assertNotIn("{{human_name}}", relay.sent[0])
            self.assertIn("有想说的话也可以给她发消息", relay.sent[0])
            self.assertIn("两者都做", relay.sent[0])
            self.assertEqual(keeper.state["night_last_date"], "2026-08-26")

    def test_night_wake_has_a_safe_fallback_when_prompt_file_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime(2026, 8, 25, 20, 0, tzinfo=UTC)  # 04:00 Shanghai
            relay = FakeRelay([
                {"id": 1, "from": "human", "text": "睡啦", "ts": MODULE.instant_text(now - timedelta(hours=3))},
            ])
            keeper = MODULE.Timekeeper(
                self.config(
                    Path(temp_dir),
                    prompt_dir=Path(temp_dir) / "missing-prompts",
                    checkin_enabled=False,
                ),
                relay=relay,
            )
            keeper.run_once(now)

            self.assertEqual(len(relay.sent), 1)
            self.assertIn("有想说的话也可以给她发消息", relay.sent[0])
            self.assertIn("两者都做", relay.sent[0])
            self.assertIn("瓷瓷", relay.sent[0])

    def test_failed_delivery_is_reserved_instead_of_retried_in_a_loop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime(2026, 8, 25, 13, 0, tzinfo=UTC)
            messages = [
                {"id": 1, "from": "human", "text": "出门啦", "ts": MODULE.instant_text(now - timedelta(hours=5))},
            ]
            relay = FailingRelay(messages)
            keeper = MODULE.Timekeeper(self.config(Path(temp_dir), night_enabled=False), relay=relay)
            with self.assertRaises(OSError):
                keeper.run_once(now)

            relay.send_wake = FakeRelay.send_wake.__get__(relay, FailingRelay)
            keeper.run_once(now + timedelta(minutes=5))
            self.assertEqual(len(relay.sent), 1)
            self.assertEqual(keeper.state["checkin_for_human_id"], 1)

    def test_managed_instructions_preserve_persona_and_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "CLAUDE.md"
            path.write_text("# Eremia\n\n我的自定义人格。\n", encoding="utf-8")
            MODULE.install_managed_instructions(path, "瓷瓷")
            first = path.read_text(encoding="utf-8")
            MODULE.install_managed_instructions(path, "瓷瓷")
            second = path.read_text(encoding="utf-8")

            self.assertEqual(first, second)
            self.assertIn("我的自定义人格。", second)
            self.assertIn("瓷瓷", second)
            self.assertNotIn("Sylvia", second)
            self.assertIn("无需因为凌晨时段而禁用", second)
            self.assertIn("两者都做", second)
            self.assertEqual(second.count(MODULE.MANAGED_BEGIN), 1)
            self.assertEqual(second.count(MODULE.MANAGED_END), 1)

    def test_install_migrates_only_the_exact_legacy_base_template(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "CLAUDE.md"
            path.write_text(
                MODULE.LEGACY_BASE_TEMPLATE + "\n我的自定义人格。\n",
                encoding="utf-8",
            )

            MODULE.install_managed_instructions(path, "瓷瓷")
            content = path.read_text(encoding="utf-8")

            self.assertIn(MODULE.BASE_TEMPLATE.rstrip(), content)
            self.assertNotIn("在论坛开局或做承诺时", content)
            self.assertIn("我的自定义人格。", content)

    def test_install_does_not_rewrite_a_user_edited_base(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "CLAUDE.md"
            custom = MODULE.LEGACY_BASE_TEMPLATE.replace(
                "先 breath 睁眼，再看小窝", "先抱抱瓷瓷，再 breath 睁眼"
            )
            path.write_text(custom, encoding="utf-8")

            MODULE.install_managed_instructions(path, "瓷瓷")
            content = path.read_text(encoding="utf-8")

            self.assertIn("先抱抱瓷瓷，再 breath 睁眼", content)
            self.assertIn("在论坛开局或做承诺时", content)


class ContextTokenTests(unittest.TestCase):
    def test_reads_last_usage_and_sums_context_side_tokens(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            write_transcript(temp_dir, input_tokens=1000, cache_read=50000, cache_creation=2000)
            self.assertEqual(MODULE.latest_context_tokens(temp_dir), 53000)

    def test_picks_the_most_recently_modified_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old = write_transcript(temp_dir, input_tokens=10000, name="old.jsonl")
            new = write_transcript(temp_dir, input_tokens=80000, name="new.jsonl")
            os.utime(old, (1_000, 1_000))
            os.utime(new, (2_000, 2_000))
            self.assertEqual(MODULE.latest_context_tokens(temp_dir), 80000)

    def test_returns_none_without_transcripts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertIsNone(MODULE.latest_context_tokens(temp_dir))


class CompactionTests(unittest.TestCase):
    def config(self, state_dir: Path, transcript_dir: Path, **overrides):
        values = dict(
            relay_url="http://relay/relay",
            relay_secret="test-secret",
            state_dir=state_dir,
            prompt_dir=SOURCE.parent / "prompts",
            human_name="瓷瓷",
            timezone_name="Asia/Shanghai",
            local_tz=MODULE.load_timezone("Asia/Shanghai"),
            checkin_enabled=False,
            night_enabled=False,
            transcript_dir=transcript_dir,
            context_window_tokens=100000,
            compact_soft_percent=78.0,
            compact_hard_percent=88.0,
            compact_min_idle_minutes=20,
            compact_cooldown_minutes=30,
        )
        values.update(overrides)
        return MODULE.Config(**values)

    def keeper(self, state_dir, transcript_dir, sender, **overrides):
        return MODULE.Timekeeper(
            self.config(state_dir, transcript_dir, **overrides),
            relay=FakeRelay([]),
            sender=sender,
        )

    def test_soft_threshold_waits_for_a_lull_then_fires_in_erermias_voice(self):
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as tx:
            write_transcript(tx, input_tokens=80000)  # 80%
            now = datetime(2026, 8, 25, 13, 0, tzinfo=UTC)
            sender = FakeSender()
            keeper = self.keeper(Path(state), Path(tx), sender)

            keeper._try_compact(now, now - timedelta(minutes=5))
            self.assertEqual(sender.sent, [])

            keeper._try_compact(now, now - timedelta(minutes=25))
            self.assertEqual(len(sender.sent), 1)
            self.assertTrue(sender.sent[0].startswith("/compact "))
            self.assertIn("瓷瓷", sender.sent[0])
            self.assertNotIn("\n", sender.sent[0])
            self.assertEqual(keeper.state["last_compact_tokens"], 80000)

    def test_below_soft_threshold_does_nothing(self):
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as tx:
            write_transcript(tx, input_tokens=50000)  # 50%
            sender = FakeSender()
            keeper = self.keeper(Path(state), Path(tx), sender)
            keeper._try_compact(datetime(2026, 8, 25, 13, 0, tzinfo=UTC), None)
            self.assertEqual(sender.sent, [])

    def test_hard_threshold_fires_without_a_lull_or_a_human_message(self):
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as tx:
            write_transcript(tx, input_tokens=90000)  # 90%
            sender = FakeSender()
            keeper = self.keeper(Path(state), Path(tx), sender)
            keeper._try_compact(datetime(2026, 8, 25, 13, 0, tzinfo=UTC), None)
            self.assertEqual(len(sender.sent), 1)

    def test_never_interrupts_a_generating_turn(self):
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as tx:
            write_transcript(tx, input_tokens=95000)  # 95%, above hard
            sender = FakeSender(busy=True)
            keeper = self.keeper(Path(state), Path(tx), sender)
            keeper._try_compact(datetime(2026, 8, 25, 13, 0, tzinfo=UTC), None)
            self.assertEqual(sender.sent, [])

    def test_disabled_watcher_is_silent(self):
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as tx:
            write_transcript(tx, input_tokens=95000)
            sender = FakeSender()
            keeper = self.keeper(Path(state), Path(tx), sender, compact_enabled=False)
            keeper._try_compact(datetime(2026, 8, 25, 13, 0, tzinfo=UTC), None)
            self.assertEqual(sender.sent, [])

    def test_cooldown_and_stale_reading_prevent_double_compaction(self):
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as tx:
            write_transcript(tx, input_tokens=90000)  # 90%
            now = datetime(2026, 8, 25, 13, 0, tzinfo=UTC)
            sender = FakeSender()
            keeper = self.keeper(Path(state), Path(tx), sender)

            keeper._try_compact(now, None)
            self.assertEqual(len(sender.sent), 1)

            # Same token count = no new turn since; the drop just isn't recorded.
            keeper._try_compact(now + timedelta(minutes=5), None)
            self.assertEqual(len(sender.sent), 1)

            # New reading but still inside the cooldown window: still held.
            write_transcript(tx, input_tokens=91000)
            keeper._try_compact(now + timedelta(minutes=10), None)
            self.assertEqual(len(sender.sent), 1)

            # New reading after the cooldown elapses: fires again.
            keeper._try_compact(now + timedelta(minutes=40), None)
            self.assertEqual(len(sender.sent), 2)

    def test_send_failure_is_reserved_not_retried_in_a_loop(self):
        class BrokenSender(FakeSender):
            def send_compact(self, command_line):
                raise OSError("tmux gone")

        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as tx:
            write_transcript(tx, input_tokens=90000)
            now = datetime(2026, 8, 25, 13, 0, tzinfo=UTC)
            keeper = self.keeper(Path(state), Path(tx), BrokenSender())
            keeper._try_compact(now, None)  # must not raise
            # cooldown reserved so the next tick doesn't hammer a broken tmux
            self.assertIsNotNone(keeper.state["last_compact_ts"])


class SettingsTests(unittest.TestCase):
    def test_install_hooks_from_empty_writes_managed_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.json"
            MODULE.install_managed_settings(path, "/opt/hooks")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["permissions"]["allow"], list(MODULE.BASE_ALLOW))
            self.assertIn("Edit(/anchors.md)", data["permissions"]["allow"])
            ss = data["hooks"]["SessionStart"]
            self.assertEqual(ss[0]["matcher"], "compact")
            self.assertEqual(ss[0]["hooks"][0]["command"], "/opt/hooks/session-anchor.sh")
            pc = data["hooks"]["PreCompact"]
            self.assertEqual(pc[0]["hooks"][0]["command"], "/opt/hooks/backup-transcript.sh")

    def test_install_hooks_is_idempotent_and_preserves_user_edits(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "permissions": {"allow": ["mcp__custom"]},
                        "hooks": {
                            "SessionStart": [
                                {"hooks": [{"type": "command", "command": "/usr/local/bin/mine.sh"}]}
                            ]
                        },
                        "model": "opus",
                    }
                ),
                encoding="utf-8",
            )
            MODULE.install_managed_settings(path, "/opt/hooks")
            first = json.loads(path.read_text(encoding="utf-8"))
            MODULE.install_managed_settings(path, "/opt/hooks")
            second = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(first, second)
            self.assertIn("mcp__custom", second["permissions"]["allow"])
            for entry in MODULE.BASE_ALLOW:
                self.assertIn(entry, second["permissions"]["allow"])
            self.assertEqual(second["model"], "opus")

            commands = [
                hook["command"]
                for entry in second["hooks"]["SessionStart"]
                for hook in entry["hooks"]
            ]
            self.assertIn("/usr/local/bin/mine.sh", commands)
            self.assertIn("/opt/hooks/session-anchor.sh", commands)
            managed = [
                entry
                for entry in second["hooks"]["SessionStart"]
                if any(h["command"].startswith("/opt/hooks/") for h in entry["hooks"])
            ]
            self.assertEqual(len(managed), 1)
            self.assertEqual(managed[0]["matcher"], "compact")


if __name__ == "__main__":
    unittest.main()
