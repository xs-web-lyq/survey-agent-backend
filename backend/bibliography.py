"""文献元数据解析与参考文献格式化。

知识库的 chunk 只保存文件路径，不能直接满足论文引用要求。本模块从
doc_status、academic_index 和 PDF 首页摘要中恢复元数据；若存在 DOI，
再用 Crossref 做可选增强。所有无法确认的字段都显式标为待补充，禁止猜测。
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from backend.config import settings

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_PAGE_RE = re.compile(r"Vol\.?\s*(\d+).*?(?:No\.?|Issue)\s*(\d+).*?(?:p|pp)\.?\s*([\d\-–]+)", re.I)
_ARTICLE_NO_RE = re.compile(
    r"文章编号\s*[:：]?\s*\d{4}-\d{4}\s*[（(]\s*(?:19|20)\d{2}\s*[）)]"
    r"\s*0*(\d+)\s*[-—–]\s*0*(\d+)\s*[-—–]\s*0*(\d+)",
    re.I,
)
_VOLUME_ISSUE_RES = (
    re.compile(r"第\s*(\d+)\s*卷\s*第?\s*(\d+)\s*期"),
    re.compile(r"Vol\.?\s*(\d+)\s*(?:No\.?|Issue)\s*(\d+)", re.I),
)
_ISSUE_ONLY_RE = re.compile(r"(?:19|20)\d{2}\s*年?\s*第\s*(\d+)\s*期")
_ISSUE_STANDALONE_RE = re.compile(r"^第\s*(\d+)\s*期$", re.M)
_PRINTED_PAGE_RE = re.compile(r"^[·•\s]*(\d{1,5})[·•\s]*$", re.M)
_GENERIC_TITLES = {
    "硕士学位论文", "博士学位论文", "工程博士学位论文", "辽宁科技大学",
    "重庆科技学院", "重慶科技學院", "北京科技大学", "昆明理工大学",
}
_INSTITUTION_TOKENS = (
    "大学", "学院", "研究所", "研究院", "公司", "大學", "學院",
)
_MISSING_FIELD_LABELS = {
    "title": "题名",
    "authors": "作者",
    "year": "年份",
    "journal": "期刊名",
    "volume": "卷号",
    "issue": "期号",
    "pages": "页码",
    "institution": "学位授予单位",
    "publication_place": "出版地",
}


def _basename(path: str) -> str:
    return str(path or "").replace("\\", "/").rsplit("/", 1)[-1]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _parsed_text(paper: dict[str, Any], *, max_blocks: int = 200) -> str:
    source = str(paper.get("source_content_list") or "").strip()
    if not source:
        return ""
    raw = _read_json(Path(source))
    if not isinstance(raw, list):
        return ""
    parts = []
    for block in raw[:max_blocks]:
        if not isinstance(block, dict):
            continue
        value = block.get("text") or block.get("content")
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts)


def _clean_lines(text: str) -> list[str]:
    lines = []
    for raw in (text or "").splitlines():
        line = re.sub(r"^\s*\[page\s+\d+\]\s*", "", raw, flags=re.I).strip()
        line = re.sub(r"\s+", " ", line)
        line = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", line)
        if line:
            lines.append(line)
    return lines


def _source_fallback_title(source: str) -> str:
    name = re.sub(r"\.pdf$", "", _basename(source), flags=re.I)
    name = re.sub(r"^(?:CN[_-]|EN[_-])", "", name)
    name = re.sub(r"^\d{4}[_-]", "", name)
    return name.replace("_", " ").strip()


def _looks_like_institution(value: str) -> bool:
    compact = re.sub(r"\s+", "", value or "").strip(" ()（）")
    if not compact:
        return False
    if any(token in compact for token in _INSTITUTION_TOKENS):
        return True
    return bool(re.search(r"(?:university|college|institute|academy)", value, re.I))


def _is_real_title(title: str | None) -> bool:
    if not title:
        return False
    compact = re.sub(r"\s+", "", title).strip()
    if not compact or compact in {re.sub(r"\s+", "", x) for x in _GENERIC_TITLES}:
        return False
    if _looks_like_institution(title):
        return False
    if re.search(r"学位论文|學位論文|doctoral(?:thesis|dissertation)|master.?sthesis", compact, re.I):
        return False
    return True


def _first_value(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    return str(value or "").strip()


def _extract_doi(text: str) -> str:
    match = _DOI_RE.search(text or "")
    return match.group(0).rstrip(".,;)]").lower() if match else ""


def _extract_year(source: str, text: str) -> int | None:
    source_year = _YEAR_RE.search(_basename(source))
    if source_year:
        return int(source_year.group(0))
    years = [int(x) for x in _YEAR_RE.findall(text or "")]
    return years[0] if years else None


def _split_authors(value: str) -> list[str]:
    value = re.sub(r"\$?\^\{[^}]+\}\$?", "", value)
    value = value.replace("\\*", "")
    value = re.sub(
        r"(?<=[A-Za-z\u4e00-\u9fff])\s*\d+(?:\s*[,，]\s*\d+)*\)?\s*[*#✉†‡苣]*",
        "",
        value,
    )
    value = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", value)
    value = value.strip(" ,，、;；*#✉†‡")
    if not value:
        return []
    if re.search(r"[\u4e00-\u9fff]", value):
        parts = re.split(r"\s*[，、；;]\s*", value)
    else:
        parts = re.split(r"\s+(?:and|&)\s+|(?<=\w),\s+(?=[A-Z])", value, flags=re.I)
    return [part.strip(" ,，、;；*#✉†‡") for part in parts if part.strip(" ,，、;；*#✉†‡")]


def _extract_authors(lines: list[str], title: str, doc_type: str) -> list[str]:
    text = "\n".join(lines)
    patterns = (r"^(?:作者姓名|研究生姓名|论文作者|作者(?!简介)|Authors?)\s*[:：]?\s*([^\n]+)",)
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.M)
        if match:
            value = re.sub(r"\s+", " ", match.group(1)).strip(" ：:")
            value = re.split(r"\s*(?:学号|指导教师|导师|专业名称|学科|工作单位)\s*[:：]", value)[0]
            if value and len(value) < 120:
                return _split_authors(value)

    try:
        index = next(i for i, line in enumerate(lines) if line == title)
    except StopIteration:
        index = 0
    for line in lines[index + 1:index + 6]:
        candidate = line.strip(" *·")
        if not candidate or any(x in candidate for x in ("摘要", "关键词", "大学", "学院", "研究所", "作者")):
            continue
        if doc_type == "journal" and ("," in candidate or "，" in candidate or re.search(r"[A-Z][a-z]+", candidate)):
            return _split_authors(candidate)
        if doc_type == "journal" and 2 <= len(candidate) <= 30 and not re.search(r"[。:：()（）]", candidate):
            return _split_authors(candidate)
        if doc_type == "lecture" and 2 <= len(candidate) <= 30 and not re.search(r"[。:：]", candidate):
            return _split_authors(candidate)
    return []


def _extract_institution(lines: list[str]) -> str:
    for line in lines[:80]:
        match = re.search(r"(?:学位授予单位|授予单位)\s*[:：]?\s*(.+)$", line)
        if match:
            institution = match.group(1).strip(" ()（）:_ ")
            if institution:
                return institution
    for line in lines[:30]:
        lower = line.lower()
        if any(token in line for token in _INSTITUTION_TOKENS) or "institute" in lower or "university" in lower:
            if "摘要" not in line and "关键词" not in line:
                institution = re.sub(
                    r"(?:硕士|博士|工程硕士|工程博士)?\s*学位论文.*$",
                    "",
                    line,
                    flags=re.I,
                ).strip(" ()（）")
                institution = re.sub(r"^(?:工作单位|所在单位)\s*[:：]?\s*", "", institution)
                if institution:
                    return institution
    return ""


def _extract_journal(lines: list[str], source: str, doi: str) -> str:
    if "Continuous CastingVol" in source or "j.boyuan.issn1005-4006" in doi:
        return "连铸"
    if "La Revue de Métallurgie" in source:
        return "La Revue de Métallurgie-CIT"
    for line in lines[:100]:
        compact = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", line).strip()
        if 2 <= len(compact) <= 80 and re.search(r"学报|期刊|杂志", compact):
            return compact
    for line in lines[:100]:
        if 5 <= len(line) <= 100 and re.search(r"\bjournal\b", line, re.I):
            return line.strip()
    return ""


def _extract_volume_issue(text: str) -> tuple[str, str]:
    for pattern in _VOLUME_ISSUE_RES:
        match = pattern.search(text or "")
        if match:
            return match.group(1), match.group(2)
    issue_match = _ISSUE_ONLY_RE.search(text or "")
    issue_match = issue_match or _ISSUE_STANDALONE_RE.search(text or "")
    return ("", issue_match.group(1)) if issue_match else ("", "")


def _extract_printed_pages(text: str) -> str:
    values = [int(value) for value in _PRINTED_PAGE_RE.findall(text or "")]
    if len(values) < 2:
        return ""
    first, last = values[0], values[-1]
    return f"{first}-{last}" if last >= first else ""


def _required_fields(document_type: str) -> tuple[str, ...]:
    if document_type == "thesis":
        return "title", "authors", "year", "institution", "publication_place"
    if document_type == "lecture":
        return "title", "authors", "year"
    return "title", "authors", "year", "journal", "volume", "issue", "pages"


def _missing_fields(record: dict[str, Any]) -> list[str]:
    required = list(_required_fields(str(record.get("document_type") or "journal")))
    if record.get("document_type") == "journal" and record.get("volume_not_applicable"):
        required.remove("volume")
    return [field for field in required if not record.get(field)]


def _build_record(source: str, row: dict[str, Any], paper: dict[str, Any]) -> dict[str, Any]:
    summary = str(row.get("content_summary") or "")
    parsed_text = _parsed_text(paper)
    metadata_text = "\n".join(x for x in (parsed_text, summary) if x)
    lines = _clean_lines(metadata_text)
    indexed_title = str(paper.get("title") or "").strip()
    title = indexed_title if _is_real_title(indexed_title) else ""
    fallback_title = _source_fallback_title(source)
    if not title and _is_real_title(fallback_title):
        title = fallback_title
    if not title:
        for line in lines[:25]:
            candidate = re.sub(r"^(?:论文题目|论文题名|题目)\s*[:：]?\s*", "", line).strip()
            if candidate in _GENERIC_TITLES or len(candidate) < 5:
                continue
            if any(token in candidate for token in ("摘要", "关键词", "DOI", "学号", "作者", "指导教师")):
                continue
            if not _is_real_title(candidate):
                continue
            title = candidate
            break
    title = title or fallback_title

    compact_summary = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", metadata_text)
    is_thesis = bool(re.search(r"学位论文|硕士论文|博士论文|Master.?s Thesis|Doctoral Thesis", compact_summary, re.I))
    is_lecture = "技术讲座" in metadata_text or "Lesson One" in metadata_text
    author_doc_type = "thesis" if is_thesis else ("lecture" if is_lecture else "journal")
    doi = _extract_doi(metadata_text)
    year = _extract_year(source, metadata_text)
    authors = _extract_authors(lines, title, author_doc_type)
    institution = _extract_institution(lines) if is_thesis else ""
    pages = volume = issue = ""
    page_match = _PAGE_RE.search(_basename(source))
    if page_match:
        volume, issue, pages = page_match.groups()
    parsed_volume, parsed_issue = _extract_volume_issue(metadata_text)
    volume = volume or parsed_volume
    issue = issue or parsed_issue
    article_match = _ARTICLE_NO_RE.search(metadata_text)
    if article_match:
        issue = issue or str(int(article_match.group(1)))
        first_page = int(article_match.group(2))
        page_count = int(article_match.group(3))
        pages = pages or f"{first_page}-{first_page + page_count - 1}"
    pages = pages or _extract_printed_pages(parsed_text)
    language = "en" if re.search(r"[\u4e00-\u9fff]", title) is None else "zh"
    journal = "" if is_thesis else _extract_journal(lines, source, doi)
    doc_type = "thesis" if is_thesis else ("lecture" if is_lecture and not journal else "journal")
    if is_lecture:
        subtitle = next((line for line in lines[:15] if re.match(r"第.+讲\s*[:：]", line)), "")
        if subtitle and subtitle not in title:
            title = f"{title}——{subtitle}"
    record = {
        "source": _basename(source),
        "title": title,
        "authors": authors,
        "year": year,
        "journal": journal,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
        "institution": institution,
        "publication_place": "",
        "volume_not_applicable": bool(
            issue and not volume
            and (_ISSUE_ONLY_RE.search(metadata_text) or _ISSUE_STANDALONE_RE.search(metadata_text))
        ),
        "document_type": doc_type,
        "language": language,
        "keywords": paper.get("keywords") or [],
        "page_count": paper.get("page_count") or row.get("page_count"),
        "evidence": [x for x in (
            "document_first_page" if summary else "",
            "parsed_document" if parsed_text else "",
            "academic_index" if paper else "",
        ) if x],
    }
    record["missing_fields"] = _missing_fields(record)
    record["metadata_status"] = "complete" if not record["missing_fields"] else "partial"
    return record


def _load_rows() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    status_path = settings.rag_storage_dir / "kv_store_doc_status.json"
    status_raw = _read_json(status_path)
    rows: dict[str, dict[str, Any]] = {}
    for value in (status_raw.values() if isinstance(status_raw, dict) else []):
        if isinstance(value, dict) and value.get("file_path"):
            rows[_basename(value["file_path"])] = value
    paper_path = (settings.academic_index_path.parent / "papers.json") if settings.academic_index_path else Path("")
    papers_raw = _read_json(paper_path) if paper_path else {}
    papers: dict[str, dict[str, Any]] = {}
    for value in (papers_raw.values() if isinstance(papers_raw, dict) else []):
        if isinstance(value, dict) and value.get("source_file"):
            papers[_basename(value["source_file"])] = value
    return rows, papers


def _apply_override(record: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = {**record, **{k: v for k, v in override.items() if v not in (None, "", [])}}
    merged["evidence"] = list(dict.fromkeys([
        *(record.get("evidence") or []),
        *(override.get("evidence") or []),
    ]))
    merged["missing_fields"] = _missing_fields(merged)
    merged["metadata_status"] = "complete" if not merged["missing_fields"] else "partial"
    return merged


def _crossref_fetch(doi: str) -> dict[str, Any]:
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
    request = urllib.request.Request(url, headers={"User-Agent": "ResearchCopilot/1.0 (bibliography enrichment)"})
    with urllib.request.urlopen(request, timeout=12) as response:
        message = json.loads(response.read().decode("utf-8")).get("message", {})
    return {
        "title": _first_value(message.get("title")),
        "authors": [" ".join(x for x in (a.get("given"), a.get("family")) if x).strip() for a in message.get("author", [])],
        "journal": _first_value(message.get("container-title")),
        "year": ((message.get("issued", {}).get("date-parts") or [[None]])[0][0]),
        "volume": message.get("volume") or "",
        "issue": message.get("issue") or "",
        "pages": message.get("page") or "",
        "doi": str(message.get("DOI") or doi).lower(),
        "evidence": ["crossref"],
    }


async def resolve_sources(sources: list[str]) -> list[dict[str, Any]]:
    rows, papers = _load_rows()
    overrides_path = settings.data_dir / "bibliography_overrides.json"
    overrides = _read_json(overrides_path)
    if not isinstance(overrides, dict):
        overrides = {}
    cache_path = settings.data_dir / "bibliography_crossref_cache.json"
    cache = _read_json(cache_path)
    if not isinstance(cache, dict):
        cache = {}
    records = [_apply_override(_build_record(source, rows.get(_basename(source), {}), papers.get(_basename(source), {})), overrides.get(_basename(source), {})) for source in sources]

    async def enrich(record: dict[str, Any]) -> dict[str, Any]:
        doi = record.get("doi")
        if not doi:
            return record
        external = cache.get(doi)
        if not external:
            try:
                external = await asyncio.to_thread(_crossref_fetch, doi)
                cache[doi] = external
            except Exception:
                return record
        return _apply_override(record, external)

    records = await asyncio.gather(*(enrich(record) for record in records))
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return list(records)


def format_reference(number: int, record: dict[str, Any]) -> str:
    authors = "，".join(record.get("authors") or []) or "作者待补充"
    title = record.get("title") or "题名待补充"
    year = record.get("year") or "年份待补充"
    missing = record.get("missing_fields") or []
    if record.get("document_type") == "thesis":
        place = record.get("publication_place") or "出版地待补充"
        institution = record.get("institution") or "授予单位待补充"
        body = f"{authors}. {title}[D]. {place}: {institution}, {year}."
    elif record.get("document_type") == "lecture":
        body = f"{authors}. {title}[Z]. {year}."
    else:
        journal = record.get("journal") or "期刊待补充"
        issue = record.get("issue") or "期号待补充"
        if record.get("volume_not_applicable"):
            tail = f"{year}({issue})"
        else:
            volume = record.get("volume") or "卷号待补充"
            tail = f"{year}, {volume}({issue})"
        if record.get("pages"):
            tail += f": {record['pages']}"
        else:
            tail += ": 页码待补充"
        body = f"{authors}. {title}[J]. {journal}, {tail}."
    if record.get("doi"):
        body += f" DOI: {record['doi']}."
    if missing:
        labels = (_MISSING_FIELD_LABELS.get(field, field) for field in missing)
        body += f" 【元数据待补充：{'、'.join(labels)}】"
    return f"[{number}] {body}"


def format_references(records: list[dict[str, Any]], *, failed: bool = False) -> str:
    lines = ["## 参考文献", ""]
    lines.extend(format_reference(i, record) for i, record in enumerate(records, 1))
    complete = sum(record.get("metadata_status") == "complete" for record in records)
    lines.extend(["", f"> 元数据完整度：{complete}/{len(records)}。"])
    if failed:
        lines.extend(["> ⚠ 标记的引用未通过原文核查，请人工复核。"])
    return "\n".join(lines)
