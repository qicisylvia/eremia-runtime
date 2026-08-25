from __future__ import annotations

import importlib.util
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

    def test_night_wake_runs_once_and_never_forces_a_reply_text(self):
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
            self.assertIn("默认不要调用 companion.reply", relay.sent[0])
            self.assertIn("瓷瓷", relay.sent[0])
            self.assertEqual(keeper.state["night_last_date"], "2026-08-26")

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
            self.assertEqual(second.count(MODULE.MANAGED_BEGIN), 1)
            self.assertEqual(second.count(MODULE.MANAGED_END), 1)


if __name__ == "__main__":
    unittest.main()
