import tempfile
import unittest
from pathlib import Path

from backend import db
from backend.config import settings
from backend.research_briefs import (
    merge_brief,
    normalize_brief,
    response,
    to_context,
    validation_errors,
)


def _brief(topic: str = "连铸电磁搅拌对凝固组织与偏析的影响") -> dict:
    return normalize_brief({
        "topic": topic,
        "summary": "聚焦结晶器电磁搅拌的作用机制、工艺窗口和质量响应。",
        "section_hints": ["作用机理", "工艺参数", "质量响应"],
        "doc_keywords": ["EMS", "偏析"],
        "research_questions": ["搅拌强度如何影响流场？", "流场如何影响偏析？"],
        "inclusion_criteria": ["纳入连铸过程研究"],
        "exclusion_criteria": ["排除非连铸凝固"],
        "evidence_gaps": ["工业尺度对照数据不足"],
        "readiness_score": 82,
        "readiness_reason": "研究问题和边界已明确",
        "evidence_documents": 18,
        "search_rounds": 3,
        "doc_scope": ["a.pdf", "b.pdf"],
    })


class ResearchBriefTests(unittest.TestCase):
    def setUp(self):
        self._old_data_dir = settings.data_dir
        self._temp = tempfile.TemporaryDirectory()
        settings.data_dir = Path(self._temp.name)
        db.init_db()
        self.conv_id = db.create_conversation("brainstorm")

    def tearDown(self):
        settings.data_dir = self._old_data_dir
        self._temp.cleanup()

    def test_versioned_brief_lifecycle_and_idempotent_lookup(self):
        first = db.create_research_brief(
            self.conv_id, _brief(), scope={"search_rounds": 3}
        )
        second = db.create_research_brief(
            self.conv_id, _brief("连铸末端电磁搅拌研究综述")
        )

        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertEqual(
            db.get_latest_research_brief(self.conv_id)["id"], second["id"]
        )
        self.assertEqual(len(db.list_research_briefs(self.conv_id)), 2)

        updated_brief = merge_brief(
            second["brief"],
            {"summary": "编辑后的摘要", "research_questions": [
                "末端搅拌如何影响中心偏析？",
                "不同钢种的最优参数是否一致？",
            ]},
        )
        updated = db.update_research_brief(second["id"], updated_brief)
        self.assertEqual(updated["status"], "draft")
        self.assertEqual(updated["brief"]["summary"], "编辑后的摘要")

        confirmed = db.confirm_research_brief(second["id"])
        self.assertEqual(confirmed["status"], "confirmed")
        handed_off = db.mark_research_brief_handed_off(
            second["id"], "survey-brief01"
        )
        self.assertEqual(handed_off["status"], "handed_off")
        self.assertEqual(handed_off["task_id"], "survey-brief01")
        self.assertIsNone(db.update_research_brief(second["id"], updated_brief))

        payload = response(handed_off)
        self.assertEqual(payload["brief_id"], second["id"])
        self.assertEqual(payload["research_questions"], updated_brief["research_questions"])

    def test_confirmation_rules_and_structured_context(self):
        brief = _brief()
        self.assertEqual(validation_errors(brief), [])
        invalid = merge_brief(brief, {
            "topic": "短题",
            "research_questions": ["只有一个问题"],
            "inclusion_criteria": [],
        })
        self.assertEqual(len(validation_errors(invalid)), 3)

        context = to_context(brief)
        self.assertIn("核心研究问题", context)
        self.assertIn("纳入边界", context)
        self.assertIn("工业尺度对照数据不足", context)

    def test_conversation_purge_removes_bound_briefs(self):
        created = db.create_research_brief(self.conv_id, _brief())
        self.assertTrue(db.delete_conversation(self.conv_id))
        self.assertTrue(db.purge_conversation(self.conv_id))
        self.assertIsNone(db.get_research_brief(created["id"]))


if __name__ == "__main__":
    unittest.main()
