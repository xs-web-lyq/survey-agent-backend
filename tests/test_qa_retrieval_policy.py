import unittest

from backend.pipelines.qa import (
    _expand_english_query,
    _select_bilingual_chunks,
    _source_language,
)


def chunk(source: str, content: str = "") -> dict:
    return {"file_path": source, "content": content, "chunk_id": source}


class RetrievalPolicyTests(unittest.TestCase):
    def test_domain_query_gets_english_expansion(self):
        expanded = _expand_english_query("中间包电磁搅拌技术发展")
        self.assertIn("tundish electromagnetic stirring", expanded)
        self.assertIn("technology development history", expanded)

    def test_source_language_prefers_filename(self):
        self.assertEqual(_source_language(chunk("CN_结晶器研究.pdf", "English abstract")), "zh")
        self.assertEqual(_source_language(chunk("2024_Mold_flow_control.pdf", "中文摘要")), "en")

    def test_default_policy_deduplicates_and_balances_sources(self):
        native = [
            chunk("CN_甲.pdf", "中文证据"),
            chunk("CN_甲.pdf", "同一文献的另一块"),
            chunk("CN_乙.pdf", "中文证据"),
            chunk("CN_丙.pdf", "中文证据"),
        ]
        english = [chunk(f"202{i}_English_{i}.pdf", "English evidence") for i in range(5)]

        selected = _select_bilingual_chunks(native, english, "电磁搅拌技术发展")

        self.assertEqual(len(selected), 8)
        self.assertEqual(len({c["file_path"] for c in selected}), 8)
        self.assertEqual(sum(_source_language(c) == "en" for c in selected), 5)
        self.assertEqual(sum(_source_language(c) == "zh" for c in selected), 3)

    def test_domestic_intent_prefers_chinese_sources(self):
        native = [chunk(f"CN_中文_{i}.pdf", "中文证据") for i in range(6)]
        english = [chunk(f"202{i}_English_{i}.pdf", "English evidence") for i in range(5)]

        selected = _select_bilingual_chunks(native, english, "中国电磁搅拌技术发展")

        self.assertEqual(sum(_source_language(c) == "en" for c in selected), 2)
        self.assertEqual(sum(_source_language(c) == "zh" for c in selected), 6)


if __name__ == "__main__":
    unittest.main()
