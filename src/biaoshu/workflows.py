import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import PureWindowsPath
from uuid import uuid4

from .config import Actor, BusinessError
from .formats import FormatError, parse_document, render_docx, render_matrix, render_pdf
from .interpretation import checksum, compact, extract, materialize, prompt, source_snippets, string, validate_candidates
from .knowledge import KnowledgeStore
from .objects import ObjectFiles


LOG = logging.getLogger(__name__)
CATEGORIES = {"tender", "amendment", "drawing", "boq", "template", "other"}
REVIEW_KINDS = {"archive", "analysis", "match", "chapter", "audit", "artifact"}
TECHNICAL_KBS = ["KB01", "KB03", "KB04", "KB07"]


def string_list(value, name):
    if not isinstance(value, list) or any(not string(item, 200) or len(item) > 200 for item in value):
        raise BusinessError(422, f"{name} 应为非空字符串组成的数组")
    if len(value) != len(set(value)):
        raise BusinessError(422, f"{name} 含重复值")
    return value


def as_date(value):
    if value is None:
        return datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise BusinessError(422, "as_of 应为 YYYY-MM-DD") from exc


def revision(row):
    return row["revision"] if row else None


def record_ref(kind, row):
    return {"kind": kind, "key": row["key"], "revision": row["revision"]}


def _category(filename):
    if any(marker in filename for marker in ("补遗", "答疑", "澄清", "更正")):
        return "amendment"
    if "工程量清单" in filename:
        return "boq"
    if "图纸" in filename or filename.lower().endswith((".dwg", ".dxf")):
        return "drawing"
    if "模板" in filename or "格式样本" in filename:
        return "template"
    if "招标" in filename:
        return "tender"
    return "other"


def _template_fields(pairs):
    return [("id", "编号"), *pairs, ("file_name", "来源文件"), ("locator", "来源位置"),
            ("file_version", "文件版本"), ("quote", "原文")]


class TenderWorkflows:
    def __init__(self, store, model, objects: ObjectFiles):
        self.store = store
        self.knowledge = KnowledgeStore(store)
        self.model = model
        self.objects = objects
        self.handlers = {
            "WF01": self.wf01, "WF02": self.wf02, "WF03": self.wf03, "WF04": self.wf04,
            "WF05": self.wf05, "WF06": self.wf06, "WF09": self.wf09, "WF10": self.wf10,
        }

    def project(self, actor: Actor, project_id: str):
        if not isinstance(project_id, str) or not actor.allows(project_id):
            raise BusinessError(404, "找不到项目")
        project = self.store.get_project(actor.tenant, project_id)
        if project is None:
            raise BusinessError(404, "找不到项目")
        return project

    def new_project(self, actor, payload):
        actor.require("editor", "admin")
        if "*" not in actor.projects:
            raise BusinessError(403, "当前凭证没有创建新项目的权限")
        name = string(payload.get("project_name"), 200)
        if not name:
            raise BusinessError(422, "创建项目需要 project_name")
        return self.store.create_project(actor.tenant, name, actor=actor.name, profile={
            "industry": string(payload.get("industry"), 100),
            "specialty": string(payload.get("specialty"), 100),
            "region": string(payload.get("region"), 100),
        })

    def run(self, workflow, actor, payload):
        actor.require("editor", "admin")
        handler = self.handlers.get(workflow)
        if handler is None:
            raise BusinessError(404, "该工作流未纳入第一阶段")
        project_id = payload.get("project_id")
        if workflow == "WF01" and not project_id:
            project_id = self.new_project(actor, payload)["project_id"]
        self.project(actor, project_id)
        run_id = "RUN-" + uuid4().hex
        error = None
        try:
            result = handler(actor, project_id, payload)
        except Exception as exc:
            error = exc
            raise
        finally:
            status = f"error:{getattr(error, 'status', 500)}" if error else result["status"]
            LOG.info("workflow=%s project=%s run=%s status=%s actor=%s", workflow, project_id, run_id, status, actor.name)
            try:
                self.store.put_record(actor.tenant, project_id, "run", run_id, {
                    "workflow": workflow, "status": status, "error_type": type(error).__name__ if error else None,
                }, actor=actor.name)
            except Exception:
                LOG.error("workflow audit write failed: run=%s project=%s", run_id, project_id)
                if error is None:
                    raise
        return {"project_id": project_id, "workflow": workflow, "run_id": run_id, **result}

    def get(self, actor, project_id, kind, key="latest", required=True):
        row = self.store.get_record(actor.tenant, project_id, kind, key)
        if row is None and required:
            raise BusinessError(409, f"请先生成 {kind}/{key}")
        return row

    def rows(self, actor, project_id, kind):
        return self.store.list_records(actor.tenant, project_id, kind)

    def put(self, actor, project_id, kind, key, data, previous, *, inputs=()):
        return self.store.put_records(actor.tenant, project_id, [{
            "kind": kind, "key": key, "data": data, "expected_revision": revision(previous) or 0,
        }], actor=actor.name, preconditions=inputs)[0]

    def approval(self, actor, project_id, kind, key, row):
        sidecar = self.get(actor, project_id, "approval", f"{kind}__{key}", required=False)
        if not sidecar or sidecar["data"]["revision"] != row["revision"]:
            return None
        return sidecar["data"] if sidecar["data"]["decision"] == "approve" else None

    def require_approved(self, actor, project_id, kind, row):
        proof = self.approval(actor, project_id, kind, row["key"], row)
        if not proof:
            raise BusinessError(409, f"{kind}/{row['key']} 当前版本待人工确认")
        return proof

    def check_refs(self, actor, project_id, refs):
        for ref in refs:
            current = self.get(actor, project_id, ref["kind"], ref["key"], required=False)
            if (revision(current) or 0) != ref["revision"]:
                raise BusinessError(409, f"{ref['kind']}/{ref['key']} 已变更，请更新下游成果")

    def review(self, actor, project_id, kind, key, payload):
        actor.require("reviewer", "admin")
        self.project(actor, project_id)
        if kind not in REVIEW_KINDS:
            raise BusinessError(422, "该类型无独立审核步骤")
        target = self.get(actor, project_id, kind, key)
        if target["created_by"] == actor.name:
            raise BusinessError(403, "编制人与复核人应为不同账号")
        target_revision, decision = payload.get("revision"), payload.get("decision")
        if type(target_revision) is not int or target_revision != target["revision"]:
            raise BusinessError(409, "审核版本不匹配，请刷新后确认")
        if decision not in ("approve", "reject"):
            raise BusinessError(422, "decision 应为 approve 或 reject")
        note = string(payload.get("note"), 2500)
        if not note:
            raise BusinessError(422, "请填写人工审核意见")
        resolutions = payload.get("resolutions", {})
        if not isinstance(resolutions, dict):
            raise BusinessError(422, "resolutions 必须为对象")
        if decision == "approve":
            self.check_refs(actor, project_id, target["data"].get("inputs", []))
            if kind == "archive":
                if target["data"]["blocking"]:
                    raise BusinessError(409, "资料档案仍有阻断项，请先补齐或重新解析")
                recorded = set(string_list(resolutions.get("parse_confirmations", []), "parse_confirmations"))
                if recorded != set(target["data"]["needs_parse_review"]):
                    raise BusinessError(409, "请逐份确认 OCR 或低质量文本与源文件一致")
            if kind == "analysis":
                if target["data"]["has_amendment"] and resolutions.get("amendments_reviewed") is not True:
                    raise BusinessError(409, "请核对补遗效力并标记 amendments_reviewed")
                if target["data"]["unverified_model_items"] and resolutions.get("sources_reviewed") is not True:
                    raise BusinessError(409, "存在已剔除的不可追溯条目，请复核候选清单并标记 sources_reviewed")
                materialize(target["data"]["items"], resolutions)
            if kind == "match":
                self.validate_evidence_decisions(target, resolutions)
            if kind == "chapter" and target["data"]["pending_items"]:
                raise BusinessError(409, "章节还有待确认项，请修订正文与证据后再审核")
            if kind == "audit" and target["data"]["blocking_count"]:
                raise BusinessError(409, "审查报告仍有阻断项，请整改后重跑")
            if kind == "artifact" and key != "technical-draft":
                raise BusinessError(422, "最终确认应作用于技术标草稿")
        approval_key = f"{kind}__{key}"
        previous = self.get(actor, project_id, "approval", approval_key, required=False)
        entry = self.put(actor, project_id, "approval", approval_key, {
            "kind": kind, "key": key, "revision": target_revision, "decision": decision, "reviewer": actor.name,
            "note": note, "resolutions": resolutions,
        }, previous, inputs=[record_ref(kind, target), *target["data"].get("inputs", [])])
        return {"status": "approved" if decision == "approve" else "rejected", "review": entry}

    @staticmethod
    def validate_evidence_decisions(record, resolutions):
        decisions = resolutions.get("evidence_decisions", {})
        rows = {row["item_id"]: row for row in record["data"]["rows"]}
        if not isinstance(decisions, dict) or set(decisions) != set(rows):
            raise BusinessError(422, "请对每一项匹配记录填写 evidence_decisions")
        for item_id, result in decisions.items():
            choices = set(rows[item_id]["candidate_keys"])
            if not isinstance(result, dict) or result.get("status") not in ("accepted", "missing", "pending"):
                raise BusinessError(422, "证据决定状态应为 accepted、missing 或 pending")
            references = string_list(result.get("keys", []), "keys")
            if not set(references) <= choices or (result["status"] == "accepted") != bool(references):
                raise BusinessError(422, f"证据决定 {item_id} 包含错误或者遗漏的来源版本")
        return decisions

    def upload(self, actor, project_id, filename, data, *, category=None, document_id=None, expected_revision=None):
        actor.require("editor", "admin")
        self.project(actor, project_id)
        name = PureWindowsPath(filename or "").name.strip()
        if not name or len(name) > 240 or any(ord(c) < 32 for c in name):
            raise BusinessError(422, "无效文件名")
        category = category or _category(name)
        if not isinstance(category, str) or category not in CATEGORIES:
            raise BusinessError(422, "资料类型不正确")
        if not data:
            raise BusinessError(422, "上传的文件为空")
        previous = self.get(actor, project_id, "document", document_id, required=False) if document_id else None
        if document_id and (not previous or expected_revision != previous["revision"]):
            raise BusinessError(409, "资料版本已变更或不存在，请重新确认版本")
        try:
            parsed = parse_document(name, data)
        except FormatError as exc:
            raise BusinessError(422, str(exc)) from exc
        digest = self.objects.write(actor.tenant, project_id, data)
        if not document_id:
            for record in self.rows(actor, project_id, "document"):
                current = record["data"]
                if (current["digest"], current["file_name"], current["category"]) == (digest, name, category):
                    return self.document_summary(record)
        key = document_id or "DOC-" + uuid4().hex
        record = self.put(actor, project_id, "document", key, {
            "file_name": name, "category": category, "digest": digest, "bytes": len(data), "parse": parsed,
        }, previous)
        return self.document_summary(record)

    @staticmethod
    def document_summary(record):
        data = record["data"]
        return {"document_id": record["key"], "revision": record["revision"], "file_name": data["file_name"],
                "category": data["category"], "sha256": data["digest"], "parse_status": data["parse"]["status"],
                "warnings": data["parse"]["warnings"], "snippet_count": len(data["parse"]["snippets"])}

    def wf01(self, actor, project_id, payload):
        previous = self.get(actor, project_id, "archive", required=False)
        documents = self.rows(actor, project_id, "document")
        if "document_ids" in payload:
            wanted = string_list(payload["document_ids"], "document_ids")
            available = {row["key"]: row for row in documents}
            if set(wanted) - set(available):
                raise BusinessError(422, "档案包含未知的项目资料 ID")
            missing = [row["key"] for row in documents
                       if row["key"] not in wanted and row["data"]["category"] in ("tender", "amendment")]
            if missing:
                raise BusinessError(422, "档案不能排除已上传的正文或补遗")
            documents = [available[k] for k in wanted]
        items = [self.document_summary(row) for row in documents]
        blocking, needs_parse_review = [], []
        if not any(row["category"] == "tender" for row in items):
            blocking.append("缺少已分类为招标正文的项目资料")
        for row in items:
            if row["category"] not in ("tender", "amendment"):
                continue
            if not row["snippet_count"]:
                blocking.append(f'{row["file_name"]} 需要重新解析')
            elif row["parse_status"] != "parsed":
                needs_parse_review.append(f'{row["document_id"]}@{row["revision"]}')
        refs = [record_ref("document", row) for row in documents]
        record = self.put(actor, project_id, "archive", "latest", {
            "documents": items, "blocking": blocking, "needs_parse_review": needs_parse_review, "inputs": refs,
        }, previous, inputs=refs)
        return {"status": "needs_review" if blocking else "pending_review", "document_count": len(items),
                "blocking_count": len(blocking), "record": record_ref("archive", record)}

    def _archive(self, actor, project_id):
        archive = self.get(actor, project_id, "archive")
        self.require_approved(actor, project_id, "archive", archive)
        self.check_refs(actor, project_id, archive["data"]["inputs"])
        sealed = {row["document_id"] for row in archive["data"]["documents"]}
        fresh = [row for row in self.rows(actor, project_id, "document")
                 if row["key"] not in sealed and row["data"]["category"] in ("tender", "amendment")]
        if fresh:
            raise BusinessError(409, "新增了招标正文或补遗，请重新归档并确认")
        return archive

    def wf02(self, actor, project_id, payload):
        archive = self._archive(actor, project_id)
        previous = self.get(actor, project_id, "analysis", required=False)
        docs = archive["data"]["documents"]
        all_ids = {row["document_id"] for row in docs}
        required = {row["document_id"] for row in docs if row["category"] in ("tender", "amendment")}
        wanted = set(string_list(payload["document_ids"], "document_ids")) if "document_ids" in payload else all_ids
        if not required <= wanted <= all_ids:
            raise BusinessError(422, "分析必须包含已确认档案中的全部正文与补遗")
        inputs = [row for row in docs if row["document_id"] in wanted and row["snippet_count"]
                  and row["category"] in ("tender", "amendment", "drawing", "boq")
                  and (row["parse_status"] == "parsed" or row["category"] in ("tender", "amendment"))]
        entries = [self.get(actor, project_id, "document", row["document_id"]) for row in inputs]
        upstream = [record_ref("archive", archive), *archive["data"]["inputs"],
                    record_ref("approval", self.get(actor, project_id, "approval", "archive__latest"))]
        items, bad_count = extract(self.model, entries)
        result = self.put(actor, project_id, "analysis", "latest", {
            "items": items, "has_amendment": any(row["category"] == "amendment" for row in inputs),
            "unverified_model_items": bad_count, "inputs": upstream,
        }, previous, inputs=upstream)
        return {"status": "pending_review", "candidate_count": len(items), "discarded_count": bad_count,
                "record": record_ref("analysis", result)}

    def analysis_sources(self, actor, project_id):
        self.project(actor, project_id)
        archive = self._archive(actor, project_id)
        entries = [self.get(actor, project_id, "document", row["document_id"])
                   for row in archive["data"]["documents"] if row["snippet_count"]
                   and row["category"] in ("tender", "amendment", "drawing", "boq")
                   and (row["parse_status"] == "parsed" or row["category"] in ("tender", "amendment"))]
        return source_snippets(entries)

    def revise_analysis(self, actor, project_id, payload):
        actor.require("editor", "admin")
        self.project(actor, project_id)
        current = self.get(actor, project_id, "analysis")
        if type(payload.get("revision")) is not int or payload["revision"] != current["revision"]:
            raise BusinessError(409, "解析报告版本已变更，请刷新")
        note = string(payload.get("note"), 2500)
        if not note:
            raise BusinessError(422, "请说明本次条款修订")
        refs = current["data"]["inputs"]
        additions = payload.get("items", [])
        if not isinstance(additions, list) or len(additions) > 300:
            raise BusinessError(422, "items 应为不超过 300 项的条款数组")
        approved_sources = self.analysis_sources(actor, project_id)
        valid, bad = validate_candidates(additions, approved_sources)
        if bad:
            raise BusinessError(422, "条款原文、分值或位置无法从档案核实，请检查 source_id 和 quote")
        removed = set(string_list(payload.get("remove_ids", []), "remove_ids"))
        existing = {row["id"]: row for row in current["data"]["items"]}
        if not removed <= set(existing) or not (valid or removed):
            raise BusinessError(422, "修改需包含可定位的增加、替换或删除项")
        values = {key: value for key, value in existing.items() if key not in removed}
        values.update(valid)
        result = self.put(actor, project_id, "analysis", "latest", {
            **current["data"], "items": list(values.values()), "correction_note": note,
        }, current, inputs=refs)
        return {"status": "pending_review", "candidate_count": len(values), "record": record_ref("analysis", result)}

    def _analysis(self, actor, project_id):
        archive = self._archive(actor, project_id)
        analysis = self.get(actor, project_id, "analysis")
        if record_ref("archive", archive) not in analysis["data"]["inputs"]:
            raise BusinessError(409, "资料档案已变更，请更新招标解析")
        proof = self.require_approved(actor, project_id, "analysis", analysis)
        self.check_refs(actor, project_id, analysis["data"]["inputs"])
        approved = materialize(analysis["data"]["items"], proof["resolutions"])
        if not any(row["kind"] == "requirement" for row in approved["items"]):
            raise BusinessError(409, "确认的解析报告没有招标要求，请核对或重新解析")
        return approved, [record_ref("analysis", analysis), {
            "kind": "approval", "key": "analysis__latest",
            "revision": self.get(actor, project_id, "approval", "analysis__latest")["revision"],
        }]

    def _artifact_data(self, actor, project_id, data, filename, content_type, inputs):
        digest = self.objects.write(actor.tenant, project_id, data)
        return {"digest": digest, "filename": filename, "content_type": content_type, "bytes": len(data), "inputs": inputs}

    def _matrix(self, actor, project_id, data, inputs, kind, key, title, fields):
        rows = []
        for item in data:
            rows.append({**item, "file_name": item["source"]["file_name"], "file_version": item["source"]["file_version"],
                         "locator": item["source"]["locator"]})
        matrix = render_matrix(rows, title, fields)
        artifact = self._artifact_data(actor, project_id, matrix, title + ".xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", inputs)
        current = self.get(actor, project_id, kind, required=False)
        prior_file = self.get(actor, project_id, "artifact", key, required=False)
        records = self.store.put_records(actor.tenant, project_id, [
            {"kind": kind, "key": "latest", "data": {"rows": data, "inputs": inputs}, "expected_revision": revision(current) or 0},
            {"kind": "artifact", "key": key, "data": artifact, "expected_revision": revision(prior_file) or 0},
        ], actor=actor.name, preconditions=inputs)
        return {"status": "ready", "rows": len(rows), "record": record_ref(kind, records[0]), "artifact_key": key}

    def wf03(self, actor, project_id, payload):
        material, inputs = self._analysis(actor, project_id)
        requirements = [row for row in material["items"] if row["kind"] == "requirement"]
        scores = [row for row in material["items"] if row["kind"] == "score"]
        plan = self._chapter_plan(material, requirements, scores)
        places = {req["id"]: chapter["title"] for chapter in plan for req in chapter["requirements"]}
        rows = [{**row, "status": "待响应", "risk": "高" if row["mandatory"] else "待核",
                 "response_position": places.get(row["id"], "待分配"), "assignee": "", "response_condition": row["quote"],
                 "scoring_status": "有明示关联" if any(
                     score["source"]["id"] == row["source"]["id"] and
                     (compact(score["quote"]) in compact(row["quote"]) or compact(row["quote"]) in compact(score["quote"]))
                     for score in scores) else "待核对"} for row in requirements]
        fields = _template_fields([("category", "分类"), ("title", "要求"), ("mandatory", "实质性"),
            ("scoring_status", "关联评分"),
            ("response_condition", "响应条件"), ("response_position", "响应位置"), ("status", "状态"),
            ("risk", "风险"), ("assignee", "责任人"), ("evidence_type", "证据类别")])
        return self._matrix(actor, project_id, rows, inputs, "requirement", "requirements", "招标要求响应矩阵", fields)

    def wf04(self, actor, project_id, payload):
        material, inputs = self._analysis(actor, project_id)
        requirements = [row for row in material["items"] if row["kind"] == "requirement"]
        scores = [row for row in material["items"] if row["kind"] == "score"]
        plan = self._chapter_plan(material, requirements, scores)
        places = {score["id"]: chapter["title"] for chapter in plan for score in chapter["scores"]}
        rows = [{**row, "status": "待审核", "evidence": "待匹配" if row["evidence_type"] else "",
                 "missing": "待核验", "response_position": places.get(row["id"], "待分配")} for row in scores]
        fields = _template_fields([("title", "评分项"), ("points", "明示分值"), ("criteria", "标准"),
            ("evidence_type", "资料类别"), ("evidence", "已有资料"), ("missing", "待补"),
            ("response_position", "响应章节"), ("status", "审核状态")])
        return self._matrix(actor, project_id, rows, inputs, "score", "scores", "投标评分响应矩阵", fields)

    def _matrices(self, actor, project_id):
        material, analysis_refs = self._analysis(actor, project_id)
        requirements, scores = (self.get(actor, project_id, kind) for kind in ("requirement", "score"))
        for row in (requirements, scores):
            if row["data"]["inputs"] != analysis_refs:
                raise BusinessError(409, "解析确认结果已更新，请重新生成响应与评分矩阵")
        return material, requirements, scores, [*analysis_refs, record_ref("requirement", requirements), record_ref("score", scores)]

    def wf05(self, actor, project_id, payload):
        _, requirements, scores, inputs = self._matrices(actor, project_id)
        prev = self.get(actor, project_id, "match", required=False)
        at = as_date(payload.get("as_of"))
        project = self.project(actor, project_id)
        profile = project.get("profile") or {}
        evidence = {}
        rows = []
        for item in requirements["data"]["rows"] + scores["data"]["rows"]:
            kind = item["evidence_type"]
            if not kind:
                continue
            if kind not in evidence:
                filters = {"evidence_type": kind, "as_of": at}
                if profile.get("specialty"):
                    filters["specialty"] = profile["specialty"]
                evidence[kind] = self.knowledge.list_evidence(actor.tenant, filters=filters, limit=20)
            candidates = evidence[kind]
            rows.append({"item_id": item["id"], "title": item["title"], "source": item["source"],
                         "condition": item["quote"], "status": "待人工核验" if candidates else "缺资料",
                         "candidates": candidates,
                         "candidate_keys": [f'{row["key"]}@{row["version"]}' for row in candidates]})
        flat = [{**row, "candidate_versions": ", ".join(row["candidate_keys"])} for row in rows]
        report = render_matrix(flat, "企业投标资料匹配报告", [("item_id", "条款编号"), ("title", "要求"),
            ("condition", "匹配条件"), ("candidate_versions", "候选证据及版本"), ("status", "状态")])
        prior_file = self.get(actor, project_id, "artifact", "evidence-matches", required=False)
        props = self._artifact_data(actor, project_id, report, "企业投标资料匹配报告.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", inputs)
        record = self.store.put_records(actor.tenant, project_id, [
            {"kind": "match", "key": "latest", "data": {"rows": rows, "as_of": at, "inputs": inputs},
             "expected_revision": revision(prev) or 0},
            {"kind": "artifact", "key": "evidence-matches", "data": props,
             "expected_revision": revision(prior_file) or 0},
        ], actor=actor.name, preconditions=inputs)[0]
        return {"status": "pending_review", "matched_items": sum(bool(item["candidates"]) for item in rows),
                "missing_items": sum(not item["candidates"] for item in rows), "artifact_key": "evidence-matches",
                "record": record_ref("match", record)}

    @staticmethod
    def _chapter_plan(material, requirements, scores):
        explicit, seen = [], set()
        for item in material["items"]:
            if item["kind"] == "chapter" and item["title"] not in seen:
                explicit.append(item)
                seen.add(item["title"])
        if len(explicit) > 25:
            raise BusinessError(422, "指定目录超过 25 章，请按分册拆分")
        names = [item["title"] for item in explicit] or ["工程概况与编制依据", "施工部署与主要施工方案", "进度、质量与安全保证措施"]
        plan = [{"id": f"CH-{index:02d}", "title": name, "requirements": [], "scores": [],
                 "source": explicit[index - 1]["source"] if explicit else None}
                for index, name in enumerate(names, 1)]
        for bucket, rows in (("requirements", requirements), ("scores", scores)):
            for row in rows:
                if bucket == "requirements" and row["category"] not in ("A", "C", "E"):
                    continue
                hint = compact(row["chapter_hint"])
                target = None
                if hint:
                    target = next((chapter for chapter in plan
                                   if hint in compact(chapter["title"]) or compact(chapter["title"]) in hint), None)
                if target is None:
                    text = row["title"] + row["quote"]
                    score = [sum(token in text for token in re.findall(r"[\u4e00-\u9fff]{2}", chapter["title"]))
                             for chapter in plan]
                    target = plan[score.index(max(score))] if any(score) else plan[min(1, len(plan) - 1)]
                target[bucket].append(row)
        return plan

    def _generation_inputs(self, actor, project_id):
        material, requirements, scores, inputs = self._matrices(actor, project_id)
        match = self.get(actor, project_id, "match")
        if match["data"]["inputs"] != inputs:
            raise BusinessError(409, "企业资料匹配结果已过期，请重新运行 WF05")
        if match["data"]["rows"]:
            self.require_approved(actor, project_id, "match", match)
            inputs.append(record_ref("approval", self.get(actor, project_id, "approval", "match__latest")))
        inputs.append(record_ref("match", match))
        plan = self._chapter_plan(material, requirements["data"]["rows"], scores["data"]["rows"])
        return material, inputs, plan

    @staticmethod
    def _select_ids(output, field, offered):
        value = output.get(field, [])
        if (not isinstance(value, list) or any(not isinstance(item, str) or item not in offered for item in value)):
            raise BusinessError(502, f"模型返回的 {field} 包含不存在的引用")
        return list(dict.fromkeys(value))

    def _compile_chapter(self, actor, project_id, chapter, material, inputs):
        project = self.project(actor, project_id)
        profile = project.get("profile") or {}
        filters = {"categories": TECHNICAL_KBS, "as_of": as_date(None)}
        filters.update({key: profile[key] for key in ("industry", "specialty", "region") if profile.get(key)})
        query = " ".join([chapter["title"], *(row["title"] for row in chapter["requirements"])])
        embedding_model = getattr(self.model, "embedding_name", "")
        vector = self.model.embed([query[:1200]])[0] if embedding_model else None
        search = self.knowledge.search_knowledge(actor.tenant, query=query[:1200], filters=filters, limit=8,
                                                 query_vector=vector, embedding_model=embedding_model)
        # Historical originals remain searchable, but only reviewed method excerpts reach chapter writers.
        search = [item for item in search if item["category"] != "KB04"
                  or item["metadata"].get("reference_use") == "reviewed_method_excerpt"]
        refs = {f'{item["key"]}@{item["version"]}:{item["ordinal"]}': item for item in search}
        facts = material["facts"]
        offered_requirements = {item["id"]: item for item in chapter["requirements"]}
        offered_scores = {item["id"]: item for item in chapter["scores"]}
        match = self.get(actor, project_id, "match")
        proof = self.approval(actor, project_id, "match", "latest", match)
        decisions = proof["resolutions"].get("evidence_decisions", {}) if proof else {}
        current_ids = set(offered_requirements) | set(offered_scores)
        confirmed_evidence = {}
        for item in match["data"]["rows"]:
            decision = decisions.get(item["item_id"], {})
            if item["item_id"] not in current_ids or decision.get("status") != "accepted":
                continue
            for candidate in item["candidates"]:
                key = f'{candidate["key"]}@{candidate["version"]}'
                if key in decision["keys"]:
                    confirmed_evidence[key] = {k: v for k, v in candidate.items() if k != "details"}
        invalid = self.knowledge.invalid_evidence_references(actor.tenant, list(confirmed_evidence.values()))
        if invalid:
            raise BusinessError(409, "企业证据已变更或失效，请重新匹配并确认")
        model_data = {
            "chapter_name": chapter["title"], "facts": facts,
            "requirements": list(offered_requirements.values()), "scores": list(offered_scores.values()),
            "experience": [{"reference_id": key, "category": item["category"], "title": item["title"],
                            "content": item["content"][:1200]} for key, item in refs.items()],
            "enterprise_evidence": [{"evidence_id": key, "title": value["title"], "type": value["evidence_type"]}
                                    for key, value in confirmed_evidence.items()],
        }
        if sum(len(row["quote"]) for row in list(offered_requirements.values()) + list(offered_scores.values())) > 14000:
            raise BusinessError(422, f"章节 {chapter['title']} 的关联条款过多，请细分目录后重试")
        output = self.model.complete(prompt("WF06_技术章节.txt"), model_data, 7000)
        content = output.get("content")
        if not isinstance(content, str) or not content.strip() or len(content) > 40000:
            raise BusinessError(502, "技术章正文为空或超长")
        responded = self._select_ids(output, "responded_requirement_ids", offered_requirements)
        score_ids = self._select_ids(output, "responded_score_ids", offered_scores)
        referred = self._select_ids(output, "reference_ids", refs)
        evidence_ids = self._select_ids(output, "evidence_ids", confirmed_evidence)
        used_facts = self._select_ids(output, "confirmed_fact_keys", facts)
        pending = output.get("pending_items", [])
        if not isinstance(pending, list) or len(pending) > 100 or any(not string(item, 250) for item in pending):
            raise BusinessError(502, "技术章待确认项结构不正确")
        for placeholder in re.findall(r"【待确认(?:[：:][^】\n]+)?】", content):
            if placeholder not in pending:
                pending.append(placeholder)
        sources = {row["source"]["id"]: row["source"] for row in (
            [facts[key] for key in used_facts] + [offered_requirements[key] for key in responded]
            + [offered_scores[key] for key in score_ids])}
        if chapter["source"]:
            sources[chapter["source"]["id"]] = chapter["source"]
        return {
            "project_id": project_id, "chapter_id": chapter["id"], "chapter_name": chapter["title"],
            "content": content.strip(), "source_documents": list(sources.values()),
            "enterprise_references": [{k: v for k, v in refs[key].items() if k != "content"} for key in referred],
            "enterprise_evidence": [confirmed_evidence[key] for key in evidence_ids],
            "applicable_standards": [key for key in referred if refs[key]["category"] == "KB01"],
            "confirmed_facts": [facts[key] for key in used_facts], "derived_calculations": [],
            "pending_items": [string(item, 250) for item in pending],
            "responded_requirement_ids": responded, "responded_score_ids": score_ids,
            "review_status": "pending", "inputs": inputs,
        }

    def wf06(self, actor, project_id, payload):
        material, inputs, plan = self._generation_inputs(actor, project_id)
        chosen = set(string_list(payload["chapter_ids"], "chapter_ids")) if "chapter_ids" in payload else {row["id"] for row in plan}
        if not chosen or not chosen <= {row["id"] for row in plan}:
            raise BusinessError(422, "chapter_ids 必须取自当前技术标目录，可先 GET 目录")
        revisions = {row["key"]: row for row in self.rows(actor, project_id, "chapter")}
        files = {row["key"]: row for row in self.rows(actor, project_id, "artifact")}
        generated = []
        for row in plan:
            if row["id"] not in chosen:
                continue
            item = self._compile_chapter(actor, project_id, row, material, inputs)
            key = row["id"]
            self.check_refs(actor, project_id, inputs)
            body = render_docx(row["title"], [item])
            artifact = self._artifact_data(actor, project_id, body, f"{key}.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document", inputs)
            saved = self.store.put_records(actor.tenant, project_id, [
                {"kind": "chapter", "key": key, "data": item, "expected_revision": revision(revisions.get(key)) or 0},
                {"kind": "artifact", "key": key, "data": artifact, "expected_revision": revision(files.get(key)) or 0},
            ], actor=actor.name, preconditions=inputs)[0]
            generated.append({"chapter_id": key, "title": row["title"], "revision": saved["revision"],
                              "pending_items": len(item["pending_items"]), "artifact_key": key})
        return {"status": "pending_review", "chapters": generated}

    def plan(self, actor, project_id):
        _, _, plan = self._generation_inputs(actor, project_id)
        return [{"chapter_id": row["id"], "title": row["title"], "source": row["source"],
                 "requirement_ids": [item["id"] for item in row["requirements"]],
                 "score_ids": [item["id"] for item in row["scores"]]} for row in plan]

    def revise_chapter(self, actor, project_id, chapter_id, payload):
        actor.require("editor", "admin")
        self.project(actor, project_id)
        current = self.get(actor, project_id, "chapter", chapter_id)
        version = payload.get("revision")
        if type(version) is not int or version != current["revision"]:
            raise BusinessError(409, "章节版本已变更，请刷新")
        text = payload.get("content")
        reason = string(payload.get("note"), 2500)
        if not isinstance(text, str) or not text.strip() or len(text) > 40000 or not reason:
            raise BusinessError(422, "请输入正文和人工修订说明")
        _, inputs, plan = self._generation_inputs(actor, project_id)
        section = next((row for row in plan if row["id"] == chapter_id), None)
        if not section or current["data"]["inputs"] != inputs:
            raise BusinessError(409, "生成依据已变更，请先重跑本章")
        data = {**current["data"], "content": text.strip(), "review_status": "pending", "edit_note": reason}
        pending = string_list(payload.get("pending_items", []), "pending_items")
        data["pending_items"] = list(dict.fromkeys(pending + re.findall(r"【待确认(?:[：:][^】\n]+)?】", text)))
        sources = {source["id"]: source for source in data["source_documents"]}
        for field, bucket in (("responded_requirement_ids", "requirements"), ("responded_score_ids", "scores")):
            if field in payload:
                choices = string_list(payload[field], field)
                available = {row["id"]: row["source"] for row in section[bucket]}
                if not set(choices) <= set(available):
                    raise BusinessError(422, f"{field} 包含未分配到本章的条款")
                data[field] = choices
                sources.update({available[key]["id"]: available[key] for key in choices})
        data["source_documents"] = list(sources.values())
        body = render_docx(section["title"], [data])
        prop = self._artifact_data(actor, project_id, body, f"{chapter_id}.docx",
                                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document", inputs)
        previous_file = self.get(actor, project_id, "artifact", chapter_id, required=False)
        records = self.store.put_records(actor.tenant, project_id, [
            {"kind": "chapter", "key": chapter_id, "data": data, "expected_revision": version},
            {"kind": "artifact", "key": chapter_id, "data": prop, "expected_revision": revision(previous_file) or 0},
        ], actor=actor.name, preconditions=inputs)
        return {"status": "pending_review", "record": record_ref("chapter", records[0]), "artifact_key": chapter_id}

    def _invalid_sources(self, actor, chapter):
        data = chapter["data"]
        return (self.knowledge.invalid_knowledge_references(actor.tenant, data.get("enterprise_references", []))
                + self.knowledge.invalid_evidence_references(actor.tenant, data.get("enterprise_evidence", [])))

    def _current_chapters(self, actor, project_id):
        material, inputs, plan = self._generation_inputs(actor, project_id)
        rows = {row["key"]: row for row in self.rows(actor, project_id, "chapter")}
        return material, inputs, plan, [rows.get(chapter["id"]) for chapter in plan]

    def wf09(self, actor, project_id, payload):
        material, inputs, plan, chapters = self._current_chapters(actor, project_id)
        previous = self.get(actor, project_id, "audit", required=False)
        match = self.get(actor, project_id, "match")
        proof = self.approval(actor, project_id, "match", "latest", match)
        reviewed_evidence = proof["resolutions"].get("evidence_decisions", {}) if proof else {}
        issues = []

        def flag(code, level, location, basis, advice):
            issues.append({"id": "AUD-" + checksum(f"{code}:{location}:{basis}"), "code": code, "risk": level,
                           "location": location, "basis": basis, "advice": advice, "remediation_status": "open"})

        for match_item in match["data"]["rows"]:
            resolution = reviewed_evidence.get(match_item["item_id"], {})
            if resolution.get("status") != "accepted":
                flag("evidence_missing", "high", match_item["item_id"], match_item["source"],
                     "补齐原件并重新匹配，由审核人确认适用条件。")
                continue
            evidence_refs = [candidate for candidate in match_item["candidates"]
                             if f'{candidate["key"]}@{candidate["version"]}' in resolution["keys"]]
            if self.knowledge.invalid_evidence_references(actor.tenant, evidence_refs):
                flag("evidence_expired", "high", match_item["item_id"], match_item["source"],
                     "证据已过期或变更，请重新匹配。")
        for outline, chapter in zip(plan, chapters, strict=True):
            key = outline["id"]
            if chapter is None:
                flag("chapter_missing", "high", key, outline["source"], "生成该指定章节。")
                continue
            data = chapter["data"]
            inputs_for_chapter = data["inputs"]
            if inputs_for_chapter != inputs or data["chapter_name"] != outline["title"]:
                flag("chapter_stale", "high", key, outline["source"], "基于当前版本重跑该章节。")
                continue
            if not self.approval(actor, project_id, "chapter", key, chapter):
                flag("chapter_review_pending", "high", key, outline["source"], "由专业人员复核并确认该版本。")
            for invalid in self._invalid_sources(actor, chapter):
                flag("source_expired", "high", key, invalid, "重检资料库现行版本，重新编写或更新引用。")
            for row in data["pending_items"]:
                flag("uncertain", "high", key, {"detail": row}, "核实资料后补齐正文。")
            for requirement in outline["requirements"]:
                if requirement["id"] not in data["responded_requirement_ids"]:
                    flag("requirement_uncovered", "high" if requirement["mandatory"] else "medium", key,
                         requirement["source"], f'核对条款 {requirement["id"]} 是否真实响应。')
            for item in outline["scores"]:
                if item["id"] not in data["responded_score_ids"]:
                    flag("score_uncovered", "medium", key, item["source"], f'复核评分点 {item["id"]}。')
            duration = material["facts"].get("duration")
            if duration:
                expected = re.search(r"\d+", duration["value"])
                promises = re.findall(r"总工期(?:为|[:：]|约|为期)?\s*(\d+)\s*(?:日历天|天|日)", data["content"])
                if expected and any(p != expected.group() for p in promises):
                    flag("duration_conflict", "high", key, duration["source"], "按确认版本统一总工期。")
            if not data["source_documents"]:
                flag("citation_missing", "medium", key, outline["source"], "补齐文件版本与出处。")
        evidence_checks = [
            "施工工艺、专项参数与图纸适用性需要专业人员确认",
            "已列企业证据的适用性以资料复核记录为准",
            "暗标、签章、交易平台电子格式和页面视觉需逐项目验收",
            "商务标和报价不在第一阶段审查范围",
        ]
        chapter_refs = [*inputs, *(record_ref("chapter", row) for row in chapters if row)]
        refs = list(chapter_refs)
        for row in (chapter for chapter in chapters if chapter):
            sidecar_key = f'chapter__{row["key"]}'
            review = self.get(actor, project_id, "approval", sidecar_key, required=False)
            refs.append({"kind": "approval", "key": sidecar_key, "revision": revision(review) or 0})
        blocking = sum(issue["risk"] == "high" for issue in issues)
        report_data = {"issues": issues, "blocking_count": blocking, "manual_checks": evidence_checks,
                       "inputs": refs, "chapter_inputs": chapter_refs}
        props = self._artifact_data(actor, project_id, render_matrix(issues, "投标文件审查报告", [
            ("id", "编号"), ("code", "问题类型"), ("risk", "风险等级"), ("location", "位置"),
            ("basis", "招标依据"), ("advice", "整改建议"), ("remediation_status", "整改状态"),
        ]), "投标文件审查报告.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", refs)
        prior = self.get(actor, project_id, "artifact", "review-report", required=False)
        report = self.store.put_records(actor.tenant, project_id, [
            {"kind": "audit", "key": "latest", "data": report_data, "expected_revision": revision(previous) or 0},
            {"kind": "artifact", "key": "review-report", "data": props, "expected_revision": revision(prior) or 0},
        ], actor=actor.name, preconditions=refs)[0]
        return {"status": "needs_review" if blocking else "pending_review", "issue_count": len(issues),
                "blocking_count": blocking, "record": record_ref("audit", report), "artifact_key": "review-report"}

    def wf10(self, actor, project_id, payload):
        _, inputs, plan, chapters = self._current_chapters(actor, project_id)
        if any(row is None for row in chapters):
            raise BusinessError(409, "请先生成目录要求的全部技术章")
        for outline, chapter in zip(plan, chapters, strict=True):
            if chapter["data"]["chapter_name"] != outline["title"] or chapter["data"]["inputs"] != inputs:
                raise BusinessError(409, f'{outline["id"]} 已过期，请重新编写')
        mode = payload.get("mode", "draft")
        if mode not in ("draft", "reviewed"):
            raise BusinessError(422, "mode 必须为 draft 或 reviewed")
        chapter_refs = [*inputs, *(record_ref("chapter", row) for row in chapters)]
        refs = list(chapter_refs)
        key = "technical-draft" if mode == "draft" else "technical-reviewed"
        filename = "技术标草稿" if mode == "draft" else "技术标审核稿"
        if mode == "reviewed":
            audit = self.get(actor, project_id, "audit")
            draft = self.get(actor, project_id, "artifact", "technical-draft")
            if audit["data"]["chapter_inputs"] != chapter_refs or draft["data"]["chapter_inputs"] != chapter_refs:
                raise BusinessError(409, "审查报告或草稿不对应当前技术标版本")
            self.check_refs(actor, project_id, audit["data"]["inputs"])
            self.check_refs(actor, project_id, draft["data"]["inputs"])
            for record_kind, row in (("audit", audit), ("artifact", draft)):
                self.require_approved(actor, project_id, record_kind, row)
                refs += [record_ref(record_kind, row),
                          record_ref("approval", self.get(actor, project_id, "approval", f'{record_kind}__{row["key"]}'))]
            for row in chapters:
                self.require_approved(actor, project_id, "chapter", row)
                if self._invalid_sources(actor, row):
                    raise BusinessError(409, "章节引用的知识或企业证据已失效，请重新审查")
            refs = _unique_refs([*refs, *audit["data"]["inputs"], *draft["data"]["inputs"]])
            if "template_document_id" in payload:
                raise BusinessError(422, "审核稿直接使用已确认草稿，调整模板须重新生成草稿")
            data = self.objects.read(actor.tenant, project_id, draft["data"]["digest"])
        else:
            title = self.project(actor, project_id)["name"] + " · 技术标"
            template = self._word_template(actor, project_id, payload.get("template_document_id"))
            if template:
                refs.append(record_ref("document", template))
            raw_template = self.objects.read(actor.tenant, project_id, template["data"]["digest"]) if template else None
            try:
                data = render_docx(title, [row["data"] for row in chapters], template=raw_template)
            except FormatError as exc:
                raise BusinessError(422, str(exc)) from exc
        types = [(key, filename + ".docx", data, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")]
        if payload.get("include_pdf") is True:
            try:
                types.append((key + "-pdf", filename + ".pdf", render_pdf(data), "application/pdf"))
            except FormatError as exc:
                raise BusinessError(422, str(exc)) from exc
        entries = []
        for artifact_key, file_name, binary, mime in types:
            previous = self.get(actor, project_id, "artifact", artifact_key, required=False)
            values = self._artifact_data(actor, project_id, binary, file_name, mime, refs)
            values["chapter_inputs"] = chapter_refs
            entries.append({"kind": "artifact", "key": artifact_key, "data": values,
                            "expected_revision": revision(previous) or 0})
        records = self.store.put_records(actor.tenant, project_id, entries, actor=actor.name, preconditions=refs)
        return {"status": "draft_ready" if mode == "draft" else "reviewed_ready", "chapter_count": len(chapters),
                "artifact_key": key, "artifact_keys": [row["key"] for row in records],
                "record": record_ref("artifact", records[0])}

    def _word_template(self, actor, project_id, selection):
        archive = self._archive(actor, project_id)
        valid = [row for row in archive["data"]["documents"] if row["category"] == "template"
                 and row["file_name"].lower().endswith(".docx")]
        if selection is not None:
            if selection not in [row["document_id"] for row in valid]:
                raise BusinessError(422, "Word 模板须取自已确认的项目资料档案")
        elif len(valid) > 1:
            raise BusinessError(422, "项目档案有多个 Word 模板，请提供 template_document_id")
        elif valid:
            selection = valid[0]["document_id"]
        return self.get(actor, project_id, "document", selection) if selection else None

    def _assert_latest_delivery(self, actor, project_id, record):
        self.check_refs(actor, project_id, record["data"]["inputs"])
        _, _, _, chapters = self._current_chapters(actor, project_id)
        if any(row is None or self._invalid_sources(actor, row) for row in chapters):
            raise BusinessError(409, "引用资料变更，审核稿已失效，请重新审查")


def _unique_refs(items):
    result = {}
    for row in items:
        key = row["kind"], row["key"]
        if key in result and result[key]["revision"] != row["revision"]:
            raise BusinessError(409, "上游版本不一致，请重新审查")
        result[key] = row
    return list(result.values())
