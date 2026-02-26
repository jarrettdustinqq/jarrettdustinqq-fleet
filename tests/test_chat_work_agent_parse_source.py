#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "ops" / "chat_work_agent.py"
SPEC = importlib.util.spec_from_file_location("chat_work_agent", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load module spec from {MODULE_PATH}")
CHAT_WORK_AGENT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CHAT_WORK_AGENT
SPEC.loader.exec_module(CHAT_WORK_AGENT)


class ParseSourceTests(unittest.TestCase):
    def test_primary_source_defaults(self) -> None:
        self.assertEqual(CHAT_WORK_AGENT.parse_source(""), (None, "primary"))
        self.assertEqual(CHAT_WORK_AGENT.parse_source("cli"), (None, "primary"))

    def test_non_json_and_non_dict_sources(self) -> None:
        self.assertEqual(CHAT_WORK_AGENT.parse_source("not-json"), (None, "other"))
        self.assertEqual(CHAT_WORK_AGENT.parse_source("[]"), (None, "other"))
        self.assertEqual(CHAT_WORK_AGENT.parse_source('{"subagent":"review"}'), (None, "other"))

    def test_subagent_parent_thread_is_detected(self) -> None:
        source = '{"subagent":{"thread_spawn":{"parent_thread_id":"abc123"}}}'
        self.assertEqual(CHAT_WORK_AGENT.parse_source(source), ("abc123", "subagent"))

    def test_subagent_parent_thread_is_stringified(self) -> None:
        source = '{"subagent":{"thread_spawn":{"parent_thread_id":42}}}'
        self.assertEqual(CHAT_WORK_AGENT.parse_source(source), ("42", "subagent"))

    def test_missing_parent_thread_id_returns_other(self) -> None:
        source = '{"subagent":{"thread_spawn":{}}}'
        self.assertEqual(CHAT_WORK_AGENT.parse_source(source), (None, "other"))


if __name__ == "__main__":
    unittest.main()
