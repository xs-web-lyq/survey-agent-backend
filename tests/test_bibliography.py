import json

from backend.bibliography import _build_record, _split_authors, format_reference


def test_extract_journal_metadata_from_first_page_and_filename():
    record = _build_record(
        "2017-Continuous CastingVol.42，No.4，p1-5.pdf",
        {
            "content_summary": (
                "[page 1] DOI：10.13228/j.boyuan.issn1005-4006.20170009\n"
                "[page 1] 连铸结晶器电磁搅拌技术的新发展\n"
                "[page 1] 肖 红 1 ， 易 兵 1 ， 龙 萌 1 ， 毛 斌 2，3\n"
            )
        },
        {"title": "连铸结晶器电磁搅拌技术的新发展", "keywords": ["电磁搅拌"]},
    )
    assert record["title"] == "连铸结晶器电磁搅拌技术的新发展"
    assert record["year"] == 2017
    assert record["journal"] == "连铸"
    assert record["volume"] == "42"
    assert record["issue"] == "4"
    assert record["pages"] == "1-5"
    assert record["doi"] == "10.13228/j.boyuan.issn1005-4006.20170009"
    assert record["authors"] == ["肖红", "易兵", "龙萌", "毛斌"]
    assert record["metadata_status"] == "complete"


def test_extract_thesis_metadata_and_mark_missing_fields():
    record = _build_record(
        "CN_特殊钢连铸大圆坯凝固末端螺旋电磁搅拌技术应用基础研究.pdf",
        {
            "content_summary": (
                "[page 1] 特殊钢连铸大圆坯凝固末端螺旋电磁搅拌技术应用基础研究\n"
                "[page 1] 作者姓名：吴红健\n"
                "[page 1] 学科：冶金工程\n"
                "[page 1] 硕士学位论文\n"
                "[page 2] 单位代码 10146\n"
                "[page 2] 2023 年 6 月\n"
            )
        },
        {"title": "特殊钢连铸大圆坯凝固末端螺旋电磁搅拌技术应用基础研究"},
    )
    assert record["document_type"] == "thesis"
    assert record["year"] == 2023
    assert record["authors"] == ["吴红健"]
    assert "institution" in record["missing_fields"]
    assert "publication_place" in record["missing_fields"]
    assert "[D]" in format_reference(1, record)


def test_reject_institution_as_thesis_title_and_clean_institution_suffix():
    record = _build_record(
        "CN_电磁搅拌下连铸大圆坯结晶器内多物理场传输行为研究.pdf",
        {
            "content_summary": (
                "[page 1] 重慶科技學院\n"
                "[page 1] 硕士学位论文\n"
                "[page 1] 电磁搅拌下连铸大圆坯结晶器内多物理场传输行为研究\n"
                "[page 1] 论 文 作 者 安 号\n"
                "[page 2] 重庆科技学院硕士学位论文（专业学位）\n"
                "[page 2] 2023年5月\n"
            )
        },
        {"title": "重慶科技學院"},
    )
    assert record["title"] == "电磁搅拌下连铸大圆坯结晶器内多物理场传输行为研究"
    assert record["institution"] == "重慶科技學院"
    assert record["authors"] == ["安号"]


def test_reject_english_university_as_thesis_title():
    record = _build_record(
        "CN_水口结构和电磁搅拌对大圆坯弧形连铸过程流场及凝固的影响.pdf",
        {
            "content_summary": (
                "[page 1] XI'ANUNIVERSITY OF ARCHITECTURE AND TECHNOLOGY\n"
                "[page 1] 硕士学位论文\n"
                "[page 1] 作者姓名：朱佳雨\n"
                "[page 1] 2024年06月02日\n"
            )
        },
        {"title": "XI'ANUNIVERSITY OF ARCHITECTURE AND TECHNOLOGY"},
    )
    assert record["title"] == "水口结构和电磁搅拌对大圆坯弧形连铸过程流场及凝固的影响"
    assert record["institution"] == "XI'ANUNIVERSITY OF ARCHITECTURE AND TECHNOLOGY"


def test_parse_article_number_and_mark_missing_volume():
    record = _build_record(
        "2015-article.pdf",
        {
            "content_summary": (
                "[page 1] DOI:10.13228/j.boyuan.issn1005-4006.20150004\n"
                "[page 1] 中国连铸电磁搅拌技术已进入世界前列\n"
                "[page 1] 毛斌1,3，肖红2，易兵2\n"
                "[page 1] 文章编号:1005-4006（2015）01-0001-06\n"
            )
        },
        {"title": "中国连铸电磁搅拌技术已进入世界前列"},
    )
    assert record["authors"] == ["毛斌", "肖红", "易兵"]
    assert record["issue"] == "1"
    assert record["pages"] == "1-6"
    assert record["missing_fields"] == ["volume"]
    assert "卷号待补充" in format_reference(1, record)


def test_split_authors_removes_affiliation_superscripts():
    assert _split_authors("肖红 1，易兵 1，龙萌 1，毛斌 2，3") == ["肖红", "易兵", "龙萌", "毛斌"]


def test_enrich_journal_from_structured_parsed_document(tmp_path):
    content_path = tmp_path / "content_list.json"
    content_path.write_text(
        json.dumps([
            {"page_idx": 0, "text": "复合磁场作用下板坯结晶器内流场与温度场的数值模拟"},
            {"page_idx": 0, "text": "杨宇威，苏志坚，陈进，范围"},
            {"page_idx": 0, "text": "文章编号: 1671-6620( 2021) 03-0185-07"},
            {"page_idx": 0, "text": "第 20 卷第 3 期"},
            {"page_idx": 0, "text": "材料与冶金学报"},
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    record = _build_record(
        "2021-复合磁场作用下板坯结晶器内流场与温度场的数值模拟_杨宇威.pdf",
        {"content_summary": ""},
        {
            "title": "复合磁场作用下板坯结晶器内流场与温度场的数值模拟",
            "source_content_list": str(content_path),
        },
    )
    assert record["journal"] == "材料与冶金学报"
    assert record["authors"] == ["杨宇威", "苏志坚", "陈进", "范围"]
    assert record["volume"] == "20"
    assert record["issue"] == "3"
    assert record["pages"] == "185-191"
    assert record["metadata_status"] == "complete"


def test_issue_only_journal_does_not_require_volume(tmp_path):
    content_path = tmp_path / "lecture_content.json"
    content_path.write_text(
        json.dumps([
            {"page_idx": 0, "text": "连铸电磁冶金技术"},
            {"page_idx": 0, "text": "第一讲:中间罐电磁搅拌和电磁制动技术"},
            {"page_idx": 0, "text": "毛斌"},
            {"page_idx": 0, "text": "DOI:10.13228/j.boyuan.issn1005-4006.1999.03.012"},
            {"page_idx": 0, "text": "· 38 ·"},
            {"page_idx": 0, "text": "1999年第3期"},
            {"page_idx": 4, "text": "· 42 ·"},
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    record = _build_record(
        "连铸电磁冶金技术.pdf",
        {"content_summary": "[page 1] ·技术讲座·"},
        {"title": "连铸电磁冶金技术", "source_content_list": str(content_path)},
    )
    assert record["document_type"] == "journal"
    assert record["volume_not_applicable"] is True
    assert record["issue"] == "3"
    assert record["pages"] == "38-42"
    assert record["metadata_status"] == "complete"
