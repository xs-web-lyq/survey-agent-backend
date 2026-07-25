import unittest

from backend.memory.context import _normalize_history
from backend.memory.extract import _safe
from backend.memory.models import MemoryBundle
from backend.memory.rewrite import rewrite_question


class MemoryRewriteTests(unittest.TestCase):
    def test_followup_is_rewritten_with_previous_topic(self):
        history = [
            {"role": "user", "content": "中间包电磁搅拌技术发展"},
            {"role": "assistant", "content": "该技术包括CF中间包等路线。"},
        ]
        result = rewrite_question("技术发展的脉络是什么？", history)
        self.assertIn("中间包电磁搅拌技术发展", result.standalone_query)
        self.assertNotEqual(result.standalone_query, "技术发展的脉络是什么？")

    def test_nested_followup_skips_generic_previous_question(self):
        history = [
            {"role": "user", "content": "中间包电磁搅拌技术发展"},
            {"role": "assistant", "content": "回答"},
            {"role": "user", "content": "技术发展的脉络是什么？"},
            {"role": "assistant", "content": "回答"},
        ]
        result = rewrite_question("它后续是如何发展的？", history)
        self.assertIn("中间包电磁搅拌技术发展", result.standalone_query)

    def test_explicit_topic_shift_does_not_inherit_old_topic(self):
        history = [{"role": "user", "content": "中间包电磁搅拌技术发展"}]
        result = rewrite_question("换个话题，介绍一下25MnB钢", history)
        self.assertTrue(result.topic_shift)
        self.assertNotIn("中间包", result.standalone_query)

    def test_history_is_normalized_for_anthropic(self):
        normalized = _normalize_history([
            {"role": "assistant", "content": "orphan"},
            {"role": "user", "content": "one"},
            {"role": "user", "content": "two"},
            {"role": "assistant", "content": "answer"},
        ])
        self.assertEqual([m["role"] for m in normalized], ["user", "assistant"])
        self.assertIn("two", normalized[0]["content"])

    def test_memory_context_keeps_durable_memory_separate(self):
        bundle = MemoryBundle(
            conv_id="conv-test",
            original_question="继续",
            standalone_query="继续讨论EMS",
            thread_state={"current_topic": "EMS"},
            durable_memories=[{"kind": "preference", "content": "优先英文原始研究"}],
        )
        rendered = bundle.system_context()
        self.assertIn("当前会话状态", rendered)
        self.assertIn("长期记忆", rendered)

    def test_secret_like_text_is_not_memory_eligible(self):
        self.assertFalse(_safe("请记住 API_KEY=sk-1234567890abcdef"))


if __name__ == "__main__":
    unittest.main()
