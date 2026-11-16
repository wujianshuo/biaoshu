import hashlib
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .config import BusinessError


PROMPT_ROOT = Path(__file__).resolve().parents[2] / "prompts"
FACT_KEYS = {"project_name", "scope", "scale", "duration", "quality", "deadline", "location", "lot"}
EVIDENCE_TYPES = {"qualification", "personnel", "performance", "equipment", "management"}


def prompt(name: str) -> str:
    return (PROMPT_ROOT / name).read_text(encoding="utf-8")


def compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:20]


def string(value, maximum: int = 1000) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:maximum]


def evidence_type(value):
    return value if isinstance(value, str) and value in EVIDENCE_TYPES else None


def source_snippets(documents):
    snippets = {}
    for document in documents:
        raw = document["data"]
        for index, part in enumerate(raw["parse"]["snippets"], 1):
            text = part.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            # Overlap keeps items near a page or table boundary eligible for extraction.
            start = 0
            while start < len(text):
                chunk = text[start:start + 2400]
                key = "SRC-" + checksum(f'{document["key"]}:{document["revision"]}:{index}:{start}')
                snippets[key] = {
                    "id": key, "file_id": document["key"], "file_version": document["revision"],
                    "file_name": raw["file_name"], "sha256": raw["digest"],
                    "file_category": raw["category"], "page": part.get("page"),
                    "locator": part.get("locator") or part.get("section") or f"片段 {index}", "text": chunk,
                }
                if start + 2400 >= len(text):
                    break
                start += 2200
    return snippets


def batches(snippets):
    batch, used = [], 0
    for row in snippets.values():
        cost = len(row["text"])
        if batch and used + cost > 8000:
            yield batch
            batch, used = [], 0
        batch.append(row)
        used += cost
    if batch:
        yield batch


def _points(value, excerpt):
    if value is None or isinstance(value, bool):
        return None
    try:
        points = Decimal(str(value))
        explicit = {Decimal(p) for p in re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*分", excerpt)}
        if points.is_finite() and points >= 0 and points in explicit:
            return float(points)
    except (InvalidOperation, ValueError, OverflowError):
        pass
    return None


def validate_candidates(items, supplied):
    results, bad_items = {}, 0
    for item in items:
        if not isinstance(item, dict):
            bad_items += 1
            continue
        source = supplied.get(string(item.get("source_id")))
        quote = string(item.get("quote"), 1600)
        kind = item.get("kind")
        if (not source or not quote or compact(quote) not in compact(source["text"])
                or kind not in ("fact", "requirement", "score", "chapter", "amendment")):
            bad_items += 1
            continue
        attached = {k: v for k, v in source.items() if k != "text"}
        key = "REQ-" + checksum(f'{source["file_id"]}:{source["file_version"]}:{source["locator"]}:{kind}:{compact(quote)}')
        row = {"id": key, "kind": kind, "source": attached, "quote": quote,
               "title": string(item.get("title"), 180) or quote[:180]}
        if kind == "fact":
            fact_key, value = item.get("fact_key"), string(item.get("value"), 350)
            if not isinstance(fact_key, str) or fact_key not in FACT_KEYS or not value or compact(value) not in compact(quote):
                bad_items += 1
                continue
            row.update(fact_key=fact_key, value=value)
        if kind == "requirement":
            category = item.get("category")
            if category not in ("A", "B", "C", "D", "E"):
                bad_items += 1
                continue
            row.update(category=category, mandatory=item.get("mandatory") is True or category == "A",
                       evidence_type=evidence_type(item.get("evidence_type")),
                       chapter_hint=string(item.get("chapter_hint"), 160))
        if kind == "score":
            points = _points(item.get("points"), quote)
            if item.get("points") is not None and points is None:
                bad_items += 1
                continue
            row.update(points=points, criteria=quote, evidence_type=evidence_type(item.get("evidence_type")),
                       chapter_hint=string(item.get("chapter_hint"), 160))
        if kind == "chapter":
            row["title"] = quote
        results[key] = row
    return results, bad_items


def extract(model, documents):
    snippets = source_snippets(documents)
    if not snippets:
        raise BusinessError(422, "确认的文件中没有可解析文本")
    if len(snippets) > 1600 or sum(len(row["text"]) for row in snippets.values()) > 280000:
        raise BusinessError(422, "本轮资料超出 MVP 解析范围，请按标段或资料用途分批整理后重试")
    results, bad_items = {}, 0
    for batch in batches(snippets):
        supplied = {s["id"]: s for s in batch}
        output = model.complete(prompt("WF02_招标解析.txt"), {"snippets": batch}, 4500)
        items = output.get("items")
        if not isinstance(items, list) or len(items) > 300:
            raise BusinessError(502, "本地模型的招标解析结果缺少有效 items")
        valid, skipped = validate_candidates(items, supplied)
        results.update(valid)
        bad_items += skipped
    if not any(item["kind"] in ("requirement", "fact", "score") for item in results.values()):
        raise BusinessError(422, "未获得可核验的招标条款，请复查文件与模型输出")
    return list(results.values()), bad_items


def materialize(items, decisions):
    by_id = {row["id"]: row for row in items}
    blocked = set()
    changes = decisions.get("supersessions", [])
    if not isinstance(changes, list) or any(not isinstance(change, dict) for change in changes):
        raise BusinessError(422, "supersessions 应为替换关系数组")
    for change in changes:
        old_id, new_id = string(change.get("old_id")), string(change.get("new_id"))
        old, new = by_id.get(old_id), by_id.get(new_id)
        if (not old or not new or old_id == new_id or old_id in blocked or old["kind"] != new["kind"]
                or new["source"]["file_category"] != "amendment" or
                (old["kind"] == "fact" and old["fact_key"] != new["fact_key"])):
            raise BusinessError(422, "替换关系必须连接同类条款，生效条款应来自补遗，且原条款只能确认一次")
        blocked.add(old_id)
    if blocked.intersection(string(change.get("new_id")) for change in changes):
        raise BusinessError(422, "请将被替换记录直接指向最终生效的补遗条款")
    effective = [row for row in items if row["id"] not in blocked]
    choices = decisions.get("fact_choices", {})
    if not isinstance(choices, dict):
        raise BusinessError(422, "fact_choices 应为事实键到条款 ID 的对象")
    facts = {}
    for key in sorted(FACT_KEYS):
        group = [row for row in effective if row["kind"] == "fact" and row["fact_key"] == key]
        variants = {compact(row["value"]) for row in group}
        explicit = choices.get(key)
        if explicit is not None:
            selected = next((row for row in group if row["id"] == explicit), None)
            if selected is None:
                raise BusinessError(422, f"事实 {key} 的人工选择缺少有效来源")
        elif len(variants) > 1:
            raise BusinessError(409, {"message": f"请确认冲突事实 {key}", "alternatives": group})
        else:
            selected = group[0] if group else None
        if selected:
            facts[key] = selected
    return {"items": effective, "facts": facts, "supersessions": changes}
