"""Reviewed source versions, traceable chunks and structured evidence."""

from datetime import date, datetime, timedelta, timezone
import json
from math import hypot, isfinite
import re

from .store import ConflictError, NotFoundError, Store, json_text, timestamp


CATEGORIES = {f"KB{i:02}" for i in range(1, 9)}
APPLICABILITY_FIELDS = ("industry", "specialty", "region", "project_type", "scale", "process")
DESCRIPTIVE_FIELDS = ("project_name", "file_type", *APPLICABILITY_FIELDS)
EVIDENCE_TYPES = {"qualification", "personnel", "performance", "equipment", "management"}


def _required(value, label, limit=500):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{label}须提供有效文本，最长 {limit} 字。")
    return value.strip()


def _source(data):
    source = _required(data.get("source") or data.get("source_ref"), "原始来源", 2000)
    digest = data.get("sha256") or data.get("source_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise ValueError("原件 SHA-256 须为 64 位十六进制字符串。")
    return source, digest.lower()


def _today() -> str:
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()


def _date(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日期格式须为 YYYY-MM-DD。")
    return date.fromisoformat(value).isoformat()


def _validity(data):
    begins, ends = _date(data.get("valid_from")), _date(data.get("valid_until"))
    if begins and ends and begins > ends:
        raise ValueError("生效日期不得晚于失效日期。")
    return begins, ends


def _vector(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 4096:
        raise ValueError("向量维度须在 1 到 4096 之间。")
    if any(type(n) not in (float, int) or abs(n) > 1e100 or not isfinite(n) for n in values):
        raise ValueError("向量只能包含有限数值。")
    length = hypot(*values)
    if length == 0:
        raise ValueError("向量长度不能为零。")
    return [n / length for n in values]


def _locator(value):
    if isinstance(value, str):
        return {"section": _required(value, "来源定位")}
    if not isinstance(value, dict) or not value:
        raise ValueError("切片至少需要一个来源位置。")
    if "page" in value and (type(value["page"]) is not int or value["page"] < 1):
        raise ValueError("来源页码须为正整数。")
    if "page" not in value and not any(isinstance(value.get(k), str) and value[k].strip()
                                     for k in ("section", "clause", "sheet", "table", "drawing")):
        raise ValueError("填写页码、章节、条号、图号或表格位置。")
    return value


def _raw_chunks(data, category):
    chunks = data.get("chunks")
    if chunks is not None:
        if not isinstance(chunks, list) or not chunks or len(chunks) > 1000:
            raise ValueError("每个知识版本须提供 1 到 1000 个切片。")
        return chunks
    if category == "KB01":
        raise ValueError("法规和规范按原文条款提供切片，并标注条号或页码。")
    content = _required(data.get("content"), "来源正文", 120_000)
    blocks = [block.strip() for block in re.split(r"\n\s*\n", content) if block.strip()]
    result = []
    for number, block in enumerate(blocks, 1):
        for offset in range(0, len(block), 2000):
            label = f"段落 {number}" + (f"，第 {offset + 1} 字起" if offset else "")
            result.append({"content": block[offset:offset + 2000], "locator": label})
    if len(result) > 1000:
        raise ValueError("切片超过 1000 个，请在上传前按章节拆分。")
    return result


def _matches(actual, wanted):
    if not wanted:
        return True
    actual = actual if isinstance(actual, list) else [actual]
    wanted = wanted if isinstance(wanted, list) else [wanted]
    values = {str(value).strip().casefold() for value in actual if value}
    targets = {str(value).strip().casefold() for value in wanted if value}
    return bool(values & (targets | {"all", "通用"}))


def _tokens(query):
    words = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", query.casefold())
    terms = set()
    for word in words:
        if len(word) > 2 and "\u3400" <= word[0] <= "\u9fff":
            terms.update(word[i:i + 2] for i in range(len(word) - 1))
        else:
            terms.add(word)
    return terms


def _keyword_score(text, query, terms):
    folded = text.casefold()
    if query and query in folded:
        return 1.0
    return .85 * (sum(term in folded for term in terms) / len(terms)) if terms else 0.0


class KnowledgeStore:
    def __init__(self, store: Store):
        self.store = store

    def _lock_source(self, conn, tenant, key, actor, *, evidence=False, create=False):
        table, column = ("enterprise_evidence_sources", "evidence_id") if evidence else (
            "knowledge_sources", "knowledge_key")
        if create:
            self.store.execute(conn, f"""
                INSERT INTO {table} (tenant_id, {column}, created_at, created_by)
                VALUES (?, ?, ?, ?) ON CONFLICT (tenant_id, {column}) DO NOTHING
            """, (tenant, key, timestamp(), actor))
        row = self.store.execute(conn, f"SELECT 1 FROM {table} WHERE tenant_id = ? AND {column} = ?"
                                 + self.store.for_update, (tenant, key)).fetchone()
        if row is None:
            raise NotFoundError("当前账户下找不到此资料。")

    def _version(self, conn, tenant, key, version=None, *, evidence=False):
        table, column = ("enterprise_evidence", "evidence_id") if evidence else (
            "knowledge_versions", "knowledge_key")
        query = f"SELECT * FROM {table} WHERE tenant_id = ? AND {column} = ?"
        if version is None:
            return self.store.execute(conn, query + " ORDER BY version DESC LIMIT 1", (tenant, key)).fetchone()
        return self.store.execute(conn, query + " AND version = ?", (tenant, key, version)).fetchone()

    @staticmethod
    def _row(row, *, evidence=False):
        if row is None:
            return None
        value = dict(row)
        value.pop("tenant_id", None)
        value["key"] = value.pop("evidence_id" if evidence else "knowledge_key")
        value["sha256"] = value.pop("source_sha256")
        if evidence:
            value["details"] = json.loads(value.pop("details_json"))
        else:
            value["category"] = value.pop("category_id")
            value["metadata"] = json.loads(value.pop("metadata_json"))
        return value

    def create_knowledge_version(self, tenant, key, data, *, actor) -> dict:
        key = _required(key, "知识资料编号", 160)
        category = data.get("category") or data.get("knowledge_base")
        if category not in CATEGORIES:
            raise ValueError("知识分类须为 KB01 至 KB08。")
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("知识元数据需为 JSON 对象。")
        if data.get("project_id") or metadata.get("project_id") or data.get("scope", "enterprise") != "enterprise":
            raise ValueError("在办项目资料应进入项目档案，企业知识库只接收面向企业审核的资料。")
        source, digest = _source(data)
        source_version = _required(data.get("source_version") or data.get("edition"), "原件版本")
        title = _required(data.get("title") or data.get("file_name"), "资料名称")
        begins, ends = _validity(data)
        if category == "KB01" and not begins:
            raise ValueError("法规和规范须登记生效日期与原件版本。")
        confidentiality = data.get("confidentiality", "internal")
        if confidentiality not in {"public", "internal", "restricted"}:
            raise ValueError("保密级别须为 public、internal 或 restricted。")
        metadata = {**dict.fromkeys(DESCRIPTIVE_FIELDS), **metadata}
        for field in APPLICABILITY_FIELDS:
            value = metadata[field]
            if value is not None and not isinstance(value, str) and not (
                isinstance(value, list) and all(isinstance(item, str) for item in value)
            ):
                raise ValueError(f"知识元数据 {field} 须为文本或文本数组。")
        chunks = []
        default_model = data.get("embedding_model")
        for item in _raw_chunks(data, category):
            if not isinstance(item, dict):
                raise ValueError("切片需为 JSON 对象。")
            body = _required(item.get("content"), "切片正文", 12000)
            locator = _locator(item.get("locator"))
            vector = item.get("embedding")
            model = None
            if vector is not None:
                model = _required(item.get("embedding_model") or default_model, "向量模型名")
                vector = _vector(vector)
            chunks.append((json_text(locator), body, model, json_text(vector) if vector is not None else None))

        with self.store.transaction(write=True) as conn:
            self._lock_source(conn, tenant, key, actor, create=True)
            previous = self._version(conn, tenant, key)
            if previous and previous["category_id"] != category:
                raise ConflictError("同一资料编号的知识分类固定，换分类时请另登记资料编号。")
            version = previous["version"] + 1 if previous else 1
            self.store.execute(conn, """
                INSERT INTO knowledge_versions (tenant_id, knowledge_key, version, source_version,
                    category_id, title, source, source_sha256, valid_from, valid_until,
                    confidentiality, status, metadata_json, created_at, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)
            """, (tenant, key, version, source_version, category, title, source, digest, begins, ends,
                  confidentiality, json_text(metadata), timestamp(), actor))
            for ordinal, values in enumerate(chunks, start=1):
                self.store.execute(conn, """INSERT INTO knowledge_chunks
                    (tenant_id, knowledge_key, version, ordinal, locator_json, content, embedding_model, embedding_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (tenant, key, version, ordinal, *values))
            self.store.add_audit(conn, tenant, actor, "knowledge.version.created", key,
                                 metadata={"version": version, "source_sha256": digest})
            result = self._row(self._version(conn, tenant, key, version))
            result["chunk_count"] = len(chunks)
            return result

    def create_evidence_version(self, tenant, key, data, *, actor) -> dict:
        key = _required(key, "企业证据编号", 160)
        evidence_type = data.get("evidence_type")
        if evidence_type not in EVIDENCE_TYPES:
            raise ValueError("企业证据类型无效。")
        source, digest = _source(data)
        title = _required(data.get("title"), "证据名称")
        specialty = _required(data.get("specialty") or "通用", "适用专业")
        subject_status = data.get("subject_status", "unknown")
        if subject_status not in {"active", "inactive", "unknown"}:
            raise ValueError("证据主体状态无效。")
        valid_from, valid_until = _validity(data)
        details = data.get("details", {})
        if not isinstance(details, dict):
            raise ValueError("企业证据详情需为 JSON 对象。")
        with self.store.transaction(write=True) as conn:
            self._lock_source(conn, tenant, key, actor, evidence=True, create=True)
            previous = self._version(conn, tenant, key, evidence=True)
            if previous and previous["evidence_type"] != evidence_type:
                raise ConflictError("同一资料编号的证据类型固定，换类型时请另登记资料编号。")
            version = previous["version"] + 1 if previous else 1
            self.store.execute(conn, """
                INSERT INTO enterprise_evidence (tenant_id, evidence_id, version, evidence_type,
                    title, specialty, subject_status, valid_from, valid_until, source, source_sha256,
                    details_json, status, created_at, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """, (tenant, key, version, evidence_type, title, specialty, subject_status, valid_from,
                  valid_until, source, digest, json_text(details), timestamp(), actor))
            self.store.add_audit(conn, tenant, actor, "evidence.version.created", key,
                                 metadata={"version": version, "source_sha256": digest})
            return self._row(self._version(conn, tenant, key, version, evidence=True), evidence=True)

    def _review(self, tenant, key, version, *, actor, decision, evidence=False):
        transitions = {"approve": ("draft", "approved"), "reject": ("draft", "rejected"),
                       "retire": ("approved", "retired")}
        if decision not in transitions:
            raise ValueError("审核动作须为 approve、reject 或 retire。")
        old_status, new_status = transitions[decision]
        table, column, label = ("enterprise_evidence", "evidence_id", "evidence") if evidence else (
            "knowledge_versions", "knowledge_key", "knowledge")
        condition = f"tenant_id = ? AND {column} = ?"
        with self.store.transaction(write=True) as conn:
            self._lock_source(conn, tenant, key, actor, evidence=evidence)
            row = self._version(conn, tenant, key, version, evidence=evidence)
            if row is None:
                raise NotFoundError("当前账户下找不到该资料版本。")
            if row["status"] == new_status and row["reviewed_by"] == actor:
                return self._row(row, evidence=evidence)
            if row["status"] != old_status:
                raise ConflictError("该版本已处理或不适用于此审核动作。")
            if decision == "approve":
                today = _today()
                if (row["valid_from"] and row["valid_from"] > today) or (row["valid_until"] and row["valid_until"] < today):
                    raise ConflictError("该版本当前不在有效期内，不能发布到检索库。")
                prior = self.store.execute(conn, f"SELECT MAX(version) AS latest FROM {table} WHERE {condition}"
                                            " AND status IN ('approved','superseded','retired')", (tenant, key)).fetchone()
                if prior["latest"] is not None and prior["latest"] >= version:
                    raise ConflictError("已有较新版本发布，旧版本不能重新发布。")
                superseded = self.store.execute(conn, f"SELECT version FROM {table} WHERE {condition}"
                                                  " AND status = 'approved'", (tenant, key)).fetchone()
                if superseded:
                    self.store.execute(conn, f"UPDATE {table} SET status = 'superseded' WHERE {condition}"
                                       " AND version = ?", (tenant, key, superseded["version"]))
                    self.store.add_audit(conn, tenant, actor, f"{label}.version.superseded", key,
                                         metadata={"version": superseded["version"], "by_version": version})
            self.store.execute(conn, f"UPDATE {table} SET status = ?, reviewed_at = ?, reviewed_by = ? WHERE {condition}"
                               " AND version = ?", (new_status, timestamp(), actor, tenant, key, version))
            self.store.add_audit(conn, tenant, actor, f"{label}.version.{new_status}", key,
                                 metadata={"version": version})
            return self._row(self._version(conn, tenant, key, version, evidence=evidence), evidence=evidence)

    def review_knowledge_version(self, tenant, key, version, *, actor, decision) -> dict:
        return self._review(tenant, key, version, actor=actor, decision=decision)

    def review_evidence_version(self, tenant, key, version, *, actor, decision) -> dict:
        return self._review(tenant, key, version, actor=actor, decision=decision, evidence=True)

    def get_knowledge_version(self, tenant, key, version) -> dict | None:
        with self.store.transaction() as conn:
            return self._row(self._version(conn, tenant, key, version))

    def get_evidence_version(self, tenant, key, version) -> dict | None:
        with self.store.transaction() as conn:
            return self._row(self._version(conn, tenant, key, version, evidence=True), evidence=True)

    def list_knowledge_versions(self, tenant, key) -> list[dict]:
        with self.store.transaction() as conn:
            rows = self.store.execute(conn, "SELECT * FROM knowledge_versions WHERE tenant_id = ? AND knowledge_key = ?"
                                      " ORDER BY version DESC", (tenant, key))
            return [self._row(row) for row in rows]

    def list_knowledge(self, tenant, *, limit=100) -> list[dict]:
        with self.store.transaction() as conn:
            rows = self.store.execute(conn, """SELECT * FROM (
                SELECT *, row_number() OVER (PARTITION BY knowledge_key ORDER BY version DESC) AS row_rank
                FROM knowledge_versions WHERE tenant_id = ?
            ) AS latest WHERE row_rank = 1 ORDER BY created_at DESC LIMIT ?""", (tenant, min(200, max(1, limit))))
            values = []
            for row in rows:
                value = self._row(row)
                value.pop("row_rank", None)
                values.append(value)
            return values

    def list_evidence(self, tenant, *, filters=None, limit=50) -> list[dict]:
        filters = filters or {}
        as_of = _date(filters.get("as_of")) or _today()
        sql = """SELECT * FROM enterprise_evidence
            WHERE tenant_id = ? AND status = 'approved' AND subject_status = 'active'
              AND (valid_from IS NULL OR valid_from <= ?) AND (valid_until IS NULL OR valid_until >= ?)"""
        params = (tenant, as_of, as_of)
        if filters.get("evidence_type"):
            sql += " AND evidence_type = ?"
            params += (filters["evidence_type"],)
        with self.store.transaction() as conn:
            values = []
            for row in self.store.execute(conn, sql + " ORDER BY evidence_id", params):
                value = self._row(row, evidence=True)
                if not _matches(value["specialty"], filters.get("specialty")):
                    continue
                if value["evidence_type"] in {"qualification", "personnel"} and not value["valid_until"]:
                    if value["details"].get("permanent") is not True:
                        continue
                values.append(value)
                if len(values) >= min(max(limit, 1), 200):
                    break
            return values

    def invalid_knowledge_references(self, tenant, references, *, as_of=None) -> list[dict]:
        """Check saved references before auditing or exporting a reviewed draft."""
        as_of = _date(as_of) or _today()
        invalid = []
        with self.store.transaction() as conn:
            for ref in references:
                valid = self.store.execute(conn, """
                    SELECT 1 FROM knowledge_versions AS v
                    JOIN knowledge_categories AS k ON k.category_id = v.category_id
                    JOIN knowledge_chunks AS c ON (c.tenant_id, c.knowledge_key, c.version)
                      = (v.tenant_id, v.knowledge_key, v.version)
                    WHERE v.tenant_id = ? AND v.knowledge_key = ? AND v.version = ?
                      AND c.ordinal = ? AND v.source_sha256 = ? AND v.status = 'approved'
                      AND k.permitted_in_writing = 1 AND v.confidentiality IN ('public','internal')
                      AND (v.valid_from IS NULL OR v.valid_from <= ?) AND (v.valid_until IS NULL OR v.valid_until >= ?)
                """, (tenant, ref.get("key"), ref.get("version"), ref.get("ordinal"), ref.get("sha256"),
                      as_of, as_of)).fetchone()
                if valid is None:
                    invalid.append(ref)
        return invalid

    def invalid_evidence_references(self, tenant, references, *, as_of=None) -> list[dict]:
        as_of = _date(as_of) or _today()
        invalid = []
        with self.store.transaction() as conn:
            for ref in references:
                key, version = ref.get("key"), ref.get("version")
                if not key or type(version) is not int:
                    invalid.append(ref)
                    continue
                value = self._row(self._version(conn, tenant, key, version, evidence=True), evidence=True)
                if (value is None or value["status"] != "approved" or value["subject_status"] != "active"
                        or value["sha256"] != ref.get("sha256")
                        or (value["valid_from"] and value["valid_from"] > as_of)
                        or (value["valid_until"] and value["valid_until"] < as_of)
                        or (value["evidence_type"] in {"qualification", "personnel"} and not value["valid_until"]
                            and value["details"].get("permanent") is not True)):
                    invalid.append(ref)
        return invalid

    def search_knowledge(self, tenant, *, query, filters=None, limit=8, query_vector=None, embedding_model=None):
        filters = filters or {}
        as_of = _date(filters.get("as_of")) or _today()
        query = _required(query, "检索词", 1200).casefold()
        mode = filters.get("mode", "hybrid" if query_vector is not None else "keyword")
        if mode not in {"keyword", "semantic", "hybrid"}:
            raise ValueError("检索方式须为 keyword、semantic 或 hybrid。")
        if mode != "keyword" and query_vector is None:
            raise ValueError("语义或混合检索需要同一内网模型的查询向量。")
        vector = _vector(query_vector) if mode != "keyword" else None
        if vector is not None:
            embedding_model = _required(embedding_model, "向量模型名")
        categories = filters.get("knowledge_base") or filters.get("category") or filters.get("categories")
        categories = categories if isinstance(categories, list) else [categories] if categories else []
        if any(category not in CATEGORIES for category in categories):
            raise ValueError("知识分类须为 KB01 至 KB08。")
        sql = """
            SELECT v.*, c.ordinal, c.locator_json, c.content, c.embedding_model, c.embedding_json
            FROM knowledge_versions AS v
            JOIN knowledge_categories AS k ON k.category_id = v.category_id
            JOIN knowledge_chunks AS c ON (c.tenant_id, c.knowledge_key, c.version)
              = (v.tenant_id, v.knowledge_key, v.version)
            WHERE v.tenant_id = ? AND v.status = 'approved' AND k.permitted_in_writing = 1
              AND v.confidentiality IN ('public','internal')
              AND (v.valid_from IS NULL OR v.valid_from <= ?) AND (v.valid_until IS NULL OR v.valid_until >= ?)
        """
        params = (tenant, as_of, as_of)
        if categories:
            sql += " AND v.category_id IN (" + ",".join("?" for _ in categories) + ")"
            params += tuple(categories)
        terms = _tokens(query)
        results = []
        with self.store.transaction() as conn:
            for row in self.store.execute(conn, sql, params):
                metadata = json.loads(row["metadata_json"])
                if any(not _matches(metadata.get(field), filters.get(field)) for field in APPLICABILITY_FIELDS):
                    continue
                keyword = _keyword_score(row["title"] + "\n" + row["content"], query, terms)
                semantic = 0.0
                if vector is not None and row["embedding_model"] == embedding_model:
                    candidate = json.loads(row["embedding_json"])
                    if len(candidate) == len(vector):
                        semantic = max(0.0, min(1.0, sum(a * b for a, b in zip(vector, candidate))))
                score = {"keyword": keyword, "semantic": semantic, "hybrid": .55 * keyword + .45 * semantic}[mode]
                if score <= 0:
                    continue
                category = row["category_id"]
                results.append({
                    "key": row["knowledge_key"], "version": row["version"], "source_version": row["source_version"],
                    "category": category, "title": row["title"], "source": row["source"],
                    "sha256": row["source_sha256"], "locator": json.loads(row["locator_json"]),
                    "content": row["content"], "ordinal": row["ordinal"], "metadata": metadata,
                    "status": row["status"], "valid_from": row["valid_from"], "valid_until": row["valid_until"],
                    "score": round(score, 6), "reference_only": category in {"KB02", "KB04"},
                })
        results.sort(key=lambda row: (-row["score"], row["key"], row["ordinal"]))
        return results[:min(100, max(1, limit))]
