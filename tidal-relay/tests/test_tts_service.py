"""Tests for the ElevenLabs side-car. No network, no key, no socket."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "tts" / "tts_service.py"
SPEC = importlib.util.spec_from_file_location("eremia_tts", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import {SOURCE}")
TTS = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TTS
SPEC.loader.exec_module(TTS)


class FakeSynth:
    def __init__(self, audio: bytes = b"ID3-audio", error: Exception | None = None):
        self.audio = audio
        self.error = error
        self.calls: list[str] = []

    def speak(self, text: str, voice_id: str) -> bytes:
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return self.audio


def config(temp_dir, **overrides):
    values = {
        "ELEVENLABS_API_KEY": "test-key-never-logged",
        "ELEVENLABS_VOICE_ID": "voice-eremia",
        "TTS_CACHE_DIR": str(Path(temp_dir) / "cache"),
    }
    values.update(overrides)
    saved = {}
    for name in (
        "ELEVENLABS_API_KEY",
        "ELEVENLABS_VOICE_ID",
        "ELEVENLABS_MODEL_ID",
        "TTS_CACHE_DIR",
        "TTS_MAX_CHARS",
        "TTS_DAILY_CHAR_BUDGET",
        "TTS_SHARED_SECRET",
        "TTS_SECRET_COOKIE",
        "RELAY_SECRET",
    ):
        saved[name] = None
    import os

    for name in saved:
        saved[name] = os.environ.pop(name, None)
    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = str(value)
    try:
        return TTS.Config()
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class NormalizeTests(unittest.TestCase):
    def test_strips_the_brackets_the_front_end_hides(self):
        for raw in (
            "[系统] 登录过期了",
            "[论坛唤醒 20260824T000000Z-960] 有人找你",
            "[时间唤醒 check-in 20261017T120000+0800] 在忙吗",
        ):
            text, truncated = TTS.normalize_text(raw, 1200)
            self.assertFalse(text.startswith("["), text)
            self.assertFalse(truncated)

    def test_collapses_whitespace_so_the_cache_key_is_stable(self):
        a, _ = TTS.normalize_text("今天  还好吗\n\n真的", 1200)
        b, _ = TTS.normalize_text("今天 还好吗 真的", 1200)
        self.assertEqual(a, b)

    def test_long_text_is_cut_at_a_sentence_end(self):
        raw = "这是一句话。" * 40
        text, truncated = TTS.normalize_text(raw, 100)
        self.assertTrue(truncated)
        self.assertLessEqual(len(text), 100)
        self.assertTrue(text.endswith("。"), text[-20:])

    def test_a_sentence_end_too_early_to_use_falls_back_to_a_hard_cut(self):
        # Backing up to the only period would throw away almost everything, so
        # the hard cut is the right answer -- still flagged as truncated.
        raw = "短。" + "没有标点的长句" * 40
        text, truncated = TTS.normalize_text(raw, 100)
        self.assertTrue(truncated)
        self.assertEqual(len(text), 100)
        self.assertFalse(text.endswith("。"))

    def test_non_string_input_is_not_an_exception(self):
        self.assertEqual(TTS.normalize_text(None, 1200), ("", False))
        self.assertEqual(TTS.normalize_text({"text": "x"}, 1200), ("", False))


class SpeakTests(unittest.TestCase):
    def test_synthesizes_once_then_serves_from_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            synth = FakeSynth()
            service = TTS.Service(config(temp), synthesizer=synth)

            status, headers, body = service.speak({"text": "我在呢"})
            self.assertEqual(status, 200)
            self.assertEqual(body, b"ID3-audio")
            self.assertEqual(headers["X-TTS-Cache"], "miss")
            self.assertEqual(headers["Content-Type"], "audio/mpeg")

            status, headers, body = service.speak({"text": "我在呢"})
            self.assertEqual(status, 200)
            self.assertEqual(headers["X-TTS-Cache"], "hit")
            self.assertEqual(synth.calls, ["我在呢"])  # charged exactly once

    def test_missing_key_or_voice_answers_503_and_never_calls_out(self):
        with tempfile.TemporaryDirectory() as temp:
            synth = FakeSynth()
            service = TTS.Service(
                config(temp, ELEVENLABS_API_KEY=None), synthesizer=synth
            )
            status, _, body = service.speak({"text": "我在呢"})
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(body)["error"], "voice-not-configured")
            self.assertEqual(synth.calls, [])

    def test_empty_text_is_refused_before_spending_anything(self):
        with tempfile.TemporaryDirectory() as temp:
            synth = FakeSynth()
            service = TTS.Service(config(temp), synthesizer=synth)
            status, _, body = service.speak({"text": "[系统]   "})
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(body)["error"], "empty-text")
            self.assertEqual(synth.calls, [])

    def test_daily_budget_stops_spending_and_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            synth = FakeSynth()
            service = TTS.Service(
                config(temp, TTS_DAILY_CHAR_BUDGET=10), synthesizer=synth
            )
            status, _, _ = service.speak({"text": "七个字的句子啊"})  # 7 chars
            self.assertEqual(status, 200)
            status, _, body = service.speak({"text": "另外八个字的句子"})  # would exceed 10
            self.assertEqual(status, 429)
            self.assertEqual(json.loads(body)["error"], "budget-exhausted")

            # A fresh process reads the same ledger off the volume.
            restarted = TTS.Service(
                config(temp, TTS_DAILY_CHAR_BUDGET=10), synthesizer=FakeSynth()
            )
            status, _, _ = restarted.speak({"text": "再来一句"})
            self.assertEqual(status, 429)

    def test_cached_audio_is_free_even_with_the_budget_gone(self):
        with tempfile.TemporaryDirectory() as temp:
            synth = FakeSynth()
            service = TTS.Service(
                config(temp, TTS_DAILY_CHAR_BUDGET=8), synthesizer=synth
            )
            self.assertEqual(service.speak({"text": "我在呢"})[0], 200)
            service.speak({"text": "把额度花光光光光"})  # exhausts the day
            status, headers, _ = service.speak({"text": "我在呢"})
            self.assertEqual(status, 200)
            self.assertEqual(headers["X-TTS-Cache"], "hit")

    def test_elevenlabs_rejecting_the_key_becomes_a_readable_502(self):
        with tempfile.TemporaryDirectory() as temp:
            error = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
            service = TTS.Service(config(temp), synthesizer=FakeSynth(error=error))
            status, _, body = service.speak({"text": "我在呢"})
            self.assertEqual(status, 502)
            payload = json.loads(body)
            self.assertEqual(payload["error"], "key-rejected")
            self.assertNotIn("test-key-never-logged", body.decode("utf-8"))

    def test_a_failed_call_is_not_cached(self):
        with tempfile.TemporaryDirectory() as temp:
            error = urllib.error.URLError("down")
            service = TTS.Service(config(temp), synthesizer=FakeSynth(error=error))
            self.assertEqual(service.speak({"text": "我在呢"})[0], 502)
            working = TTS.Service(config(temp), synthesizer=FakeSynth(b"ok-audio"))
            status, headers, body = working.speak({"text": "我在呢"})
            self.assertEqual(status, 200)
            self.assertEqual(headers["X-TTS-Cache"], "miss")
            self.assertEqual(body, b"ok-audio")

    def test_truncation_is_announced_in_a_header(self):
        with tempfile.TemporaryDirectory() as temp:
            service = TTS.Service(config(temp, TTS_MAX_CHARS=100), synthesizer=FakeSynth())
            _, headers, _ = service.speak({"text": "长句" * 200})
            self.assertEqual(headers["X-TTS-Truncated"], "1")


class AuthTests(unittest.TestCase):
    def test_open_when_no_secret_is_configured(self):
        with tempfile.TemporaryDirectory() as temp:
            service = TTS.Service(config(temp), synthesizer=FakeSynth())
            self.assertTrue(service.authorized(None))

    def test_closed_when_a_secret_is_configured(self):
        with tempfile.TemporaryDirectory() as temp:
            service = TTS.Service(
                config(temp, TTS_SHARED_SECRET="shhh"), synthesizer=FakeSynth()
            )
            self.assertFalse(service.authorized(None))
            self.assertFalse(service.authorized(""))
            self.assertFalse(service.authorized("wrong"))
            self.assertTrue(service.authorized("shhh"))


class CredentialSourceTests(unittest.TestCase):
    """Where the credential may come from: a header, or a named cookie."""

    def service(self, temp, **overrides):
        return TTS.Service(config(temp, **overrides), synthesizer=FakeSynth())

    def test_header_wins_when_present(self):
        with tempfile.TemporaryDirectory() as temp:
            service = self.service(temp, TTS_SHARED_SECRET="shhh", TTS_SECRET_COOKIE="cs")
            offered = service.offered_secret("shhh", "cs=nope")
            self.assertTrue(service.authorized(offered))

    def test_cookie_is_read_when_the_page_cannot_send_a_header(self):
        with tempfile.TemporaryDirectory() as temp:
            service = self.service(temp, TTS_SHARED_SECRET="shhh", TTS_SECRET_COOKIE="cs")
            offered = service.offered_secret(None, "companion_theme=harbor; cs=shhh; x=1")
            self.assertTrue(service.authorized(offered))

    def test_cookie_is_ignored_unless_its_name_is_configured(self):
        with tempfile.TemporaryDirectory() as temp:
            service = self.service(temp, TTS_SHARED_SECRET="shhh")
            offered = service.offered_secret(None, "cs=shhh")
            self.assertFalse(service.authorized(offered))

    def test_bearer_header_is_accepted_like_the_relay_does(self):
        with tempfile.TemporaryDirectory() as temp:
            service = self.service(temp, TTS_SHARED_SECRET="shhh")
            self.assertTrue(service.authorized(service.offered_secret(None, None, "Bearer shhh")))
            self.assertTrue(service.authorized(service.offered_secret(None, None, "bearer shhh")))
            self.assertFalse(service.authorized(service.offered_secret(None, None, "Bearer nope")))
            self.assertFalse(service.authorized(service.offered_secret(None, None, "Basic shhh")))

    def test_relay_secret_is_the_default_when_no_dedicated_one_is_set(self):
        with tempfile.TemporaryDirectory() as temp:
            service = self.service(temp, RELAY_SECRET="relay-shared")
            self.assertTrue(
                service.authorized(service.offered_secret(None, None, "Bearer relay-shared"))
            )
            self.assertFalse(service.authorized(service.offered_secret(None, None, None)))
            # An explicit value still wins over the inherited one.
            explicit = self.service(temp, TTS_SHARED_SECRET="mine", RELAY_SECRET="relay-shared")
            self.assertTrue(explicit.authorized("mine"))
            self.assertFalse(explicit.authorized("relay-shared"))

    def test_an_ambient_relay_secret_does_not_silently_lock_the_endpoint(self):
        # The fixture owns RELAY_SECRET, so a value in the developer's own shell
        # cannot change what these tests mean.
        with tempfile.TemporaryDirectory() as temp:
            self.assertTrue(self.service(temp).authorized(None))

    def test_a_malformed_cookie_header_is_just_an_absent_one(self):
        with tempfile.TemporaryDirectory() as temp:
            service = self.service(temp, TTS_SHARED_SECRET="shhh", TTS_SECRET_COOKIE="cs")
            for header in ("", "=;;;", "cs", "cs=", "%%%"):
                self.assertFalse(service.authorized(service.offered_secret(None, header)), header)


class CacheTests(unittest.TestCase):
    def test_voice_and_model_are_part_of_the_key(self):
        base = TTS.Cache.key("voice-a", "model-1", "mp3", "同一句话")
        self.assertNotEqual(base, TTS.Cache.key("voice-b", "model-1", "mp3", "同一句话"))
        self.assertNotEqual(base, TTS.Cache.key("voice-a", "model-2", "mp3", "同一句话"))
        self.assertEqual(base, TTS.Cache.key("voice-a", "model-1", "mp3", "同一句话"))

    def test_key_cannot_be_confused_by_field_boundaries(self):
        self.assertNotEqual(
            TTS.Cache.key("ab", "c", "mp3", "x"), TTS.Cache.key("a", "bc", "mp3", "x")
        )

    def test_eviction_keeps_the_directory_under_its_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = TTS.Cache(Path(temp) / "cache", max_bytes=9 * 1024 * 1024)
            for index in range(6):
                cache.put(f"{index:064x}", b"a" * (2 * 1024 * 1024))
            total = sum(path.stat().st_size for path in cache.directory.glob("*.mp3"))
            self.assertLessEqual(total, 9 * 1024 * 1024)
            self.assertGreater(total, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
