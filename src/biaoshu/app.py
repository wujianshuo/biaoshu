import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path, PureWindowsPath
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from .config import BusinessError, Settings
from .formats import FormatError, parse_document
from .model import LocalModel
from .object_s3 import S3ObjectFiles
from .objects import ObjectFiles
from .store import ConflictError, NotFoundError, Store
from .workflows import CATEGORIES, TenderWorkflows, string


LOG = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"
RECORD_KINDS = {
    "document", "archive", "analysis", "requirement", "score", "match", "chapter", "audit", "artifact", "approval", "run",
}


def create_app(settings: Settings | None = None, model=None, store: Store | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or Store(settings.dsn)
    model = model or LocalModel()
    storage = os.getenv("BIAOSHU_STORAGE_BACKEND", "filesystem")
    if storage not in ("filesystem", "s3"):
        raise ValueError("BIAOSHU_STORAGE_BACKEND 需为 filesystem 或 s3")
    objects = S3ObjectFiles.from_env() if storage == "s3" else ObjectFiles(settings.files)
    service = TenderWorkflows(store, model, objects)

    @asynccontextmanager
    async def lifespan(_):
        store.init()
        if storage == "s3":
            objects.probe()
        yield

    app = FastAPI(title="工程招投标第一阶段 API", version="0.2.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def request_guard(request: Request, call_next):
        total = request.headers.get("content-length")
        if request.method in ("POST", "PUT"):
            try:
                limit = max(2 * settings.upload_limit, 2_097_152) + 524288 if "multipart/" in request.headers.get(
                    "content-type", "",
                ) else 2_097_152
                if total is None or not 0 <= int(total) <= limit:
                    return JSONResponse({"detail": "请求没有长度或超出本服务限制"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Content-Length 无效"}, status_code=400)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(BusinessError)
    async def business_error(_request, error):
        return JSONResponse({"detail": error.detail}, status_code=error.status,
                            headers={"WWW-Authenticate": "Bearer"} if error.status == 401 else None)

    @app.exception_handler(ConflictError)
    async def conflict(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(NotFoundError)
    async def not_found(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.exception_handler(ValueError)
    async def bad_value(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=422)

    def authenticate(authorization: Annotated[str | None, Header()] = None):
        return settings.authenticate(authorization)

    User = Annotated[object, Depends(authenticate)]

    def read_record(actor, project_id, kind, key, revision):
        service.project(actor, project_id)
        require_kind(kind)
        record = store.get_record(actor.tenant, project_id, kind, key, revision=revision)
        if not record:
            raise BusinessError(404, "找不到记录版本")
        return record

    def require_kind(kind):
        if kind not in RECORD_KINDS:
            raise BusinessError(404, "未知记录类型")

    def extract_upload(file: UploadFile):
        data = file.file.read(settings.upload_limit + 1)
        if len(data) > settings.upload_limit:
            raise BusinessError(413, "单个文件超过上传限制")
        return data

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html", headers={
            "Content-Security-Policy": "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
        })

    @app.get("/assets/{name}", include_in_schema=False)
    def asset(name: str):
        if name not in {"app.js", "styles.css"}:
            raise BusinessError(404, "未找到前端资源")
        return FileResponse(WEB_DIR / name)

    @app.get("/healthz")
    def health():
        with store.transaction():
            pass
        return {"status": "ready", "storage": storage, "model_configured": bool(getattr(model, "configured", True))}

    @app.get("/v1/projects")
    def list_projects(actor: User):
        return {"projects": [row for row in store.list_projects(actor.tenant) if actor.allows(row["project_id"])]}

    @app.post("/v1/projects", status_code=201)
    def create_project(payload: dict, actor: User):
        return service.new_project(actor, payload)

    @app.get("/v1/projects/{project_id}")
    def get_project(project_id: str, actor: User):
        return service.project(actor, project_id)

    @app.post("/v1/projects/{project_id}/documents", status_code=201)
    def upload_document(project_id: str, actor: User, file: Annotated[UploadFile, File()],
                        category: Annotated[str | None, Form()] = None,
                        document_id: Annotated[str | None, Form()] = None,
                        expected_revision: Annotated[int | None, Form()] = None):
        service.project(actor, project_id)
        return service.upload(actor, project_id, file.filename, extract_upload(file), category=category,
                              document_id=document_id, expected_revision=expected_revision)

    @app.get("/v1/projects/{project_id}/records/{kind}")
    def list_records(project_id: str, kind: str, actor: User):
        service.project(actor, project_id)
        require_kind(kind)
        return {"records": store.list_records(actor.tenant, project_id, kind)}

    @app.get("/v1/projects/{project_id}/records/{kind}/{key}")
    def get_record(project_id: str, kind: str, key: str, actor: User, revision: int | None = None):
        return read_record(actor, project_id, kind, key, revision)

    @app.get("/v1/projects/{project_id}/records/{kind}/{key}/versions")
    def version_history(project_id: str, kind: str, key: str, actor: User):
        service.project(actor, project_id)
        require_kind(kind)
        return {"versions": store.list_record_versions(actor.tenant, project_id, kind, key)}

    @app.post("/v1/projects/{project_id}/records/{kind}/{key}/review")
    def approve_record(project_id: str, kind: str, key: str, payload: dict, actor: User):
        return service.review(actor, project_id, kind, key, payload)

    @app.get("/v1/projects/{project_id}/technical-plan")
    def technical_plan(project_id: str, actor: User):
        service.project(actor, project_id)
        return {"chapters": service.plan(actor, project_id)}

    @app.get("/v1/projects/{project_id}/analysis-sources")
    def analysis_sources(project_id: str, actor: User, offset: Annotated[int, Query(ge=0)] = 0,
                         limit: Annotated[int, Query(ge=1, le=40)] = 20):
        known = list(service.analysis_sources(actor, project_id).values())
        return {"total": len(known), "snippets": known[offset:offset + limit]}

    @app.put("/v1/projects/{project_id}/analysis")
    def revise_analysis(project_id: str, payload: dict, actor: User):
        return service.revise_analysis(actor, project_id, payload)

    @app.put("/v1/projects/{project_id}/chapters/{chapter_id}")
    def revise_chapter(project_id: str, chapter_id: str, payload: dict, actor: User):
        return service.revise_chapter(actor, project_id, chapter_id, payload)

    def download(actor, project_id, kind, key, revision):
        record = read_record(actor, project_id, kind, key, revision)
        values = record["data"]
        if kind == "artifact" and key.startswith("technical-reviewed"):
            service._assert_latest_delivery(actor, project_id, record)
        name = values.get("filename") or values.get("file_name") or "download"
        data = service.objects.read(actor.tenant, project_id, values["digest"])
        with store.transaction(write=True) as conn:
            store.add_audit(conn, actor.tenant, actor.name, "file.download", key, project_id=project_id,
                            metadata={"kind": kind, "revision": record["revision"], "sha256": values["digest"]})
        return Response(data, media_type=values.get("content_type", "application/octet-stream"),
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}"})

    @app.get("/v1/projects/{project_id}/artifacts/{key}/download")
    def artifact_download(project_id: str, key: str, actor: User, revision: int | None = None):
        return download(actor, project_id, "artifact", key, revision)

    @app.get("/v1/projects/{project_id}/documents/{key}/download")
    def document_download(project_id: str, key: str, actor: User, revision: int | None = None):
        return download(actor, project_id, "document", key, revision)

    @app.post("/v1/workflows/{workflow}/run")
    def run(workflow: str, payload: dict, actor: User):
        return service.run(workflow, actor, payload)

    @app.post("/v1/workflows/WF01/upload")
    def workflow_upload(actor: User, files: Annotated[list[UploadFile], File()],
                        project_id: Annotated[str, Form()] = "",
                        request_json: Annotated[str, Form()] = "{}"):
        actor.require("editor", "admin")
        try:
            payload = json.loads(request_json)
        except ValueError as exc:
            raise BusinessError(422, "request_json 不是有效 JSON 对象") from exc
        if not isinstance(payload, dict) or "project_id" in payload:
            raise BusinessError(422, "request_json 应为不含 project_id 的对象")
        if not 1 <= len(files) <= 8:
            raise BusinessError(422, "一次可上传 1 至 8 个文件")
        kinds = payload.pop("document_kinds", {})
        if not isinstance(kinds, dict) or any(not isinstance(value, str) or value not in CATEGORIES for value in kinds.values()):
            raise BusinessError(422, "document_kinds 应为文件名到资料类型的映射")
        supplied = [(file.filename, extract_upload(file)) for file in files]
        if sum(len(blob) for _, blob in supplied) > 2 * settings.upload_limit:
            raise BusinessError(413, "合计上传量超过限制")
        if project_id:
            service.project(actor, project_id)
        else:
            project_id = service.new_project(actor, payload)["project_id"]
        results = []
        for filename, blob in supplied:
            try:
                results.append(service.upload(actor, project_id, filename, blob, category=kinds.get(filename)))
            except BusinessError as exc:
                raise BusinessError(exc.status, {"message": exc.detail, "project_id": project_id,
                    "saved_documents": results, "failed_file": filename}) from exc
        if "document_ids" in payload:
            chosen = payload["document_ids"]
            if not isinstance(chosen, list) or any(not isinstance(key, str) for key in chosen):
                raise BusinessError(422, "document_ids 必须为文件编号数组")
            payload["document_ids"] = list(dict.fromkeys(chosen + [part["document_id"] for part in results]))
        return {**service.run("WF01", actor, {"project_id": project_id, **payload}), "uploaded": results}

    def library(actor, *roles):
        actor.require(*roles)
        if "*" not in actor.projects:
            raise BusinessError(403, "凭证没有企业资料库权限")

    @app.get("/v1/knowledge")
    def knowledge_list(actor: User):
        library(actor, "editor", "reviewer", "admin")
        return {"items": service.knowledge.list_knowledge(actor.tenant)}

    @app.get("/v1/knowledge/{key}/versions")
    def knowledge_versions(key: str, actor: User):
        library(actor, "editor", "reviewer", "admin")
        return {"versions": service.knowledge.list_knowledge_versions(actor.tenant, key)}

    @app.post("/v1/knowledge/{key}/versions", status_code=201)
    def publish_candidate(key: str, payload: dict, actor: User):
        library(actor, "editor", "admin")
        metadata = payload.get("metadata") or {}
        if isinstance(metadata, dict) and ({"stored_original", "source_sha256_verified"} & metadata.keys()):
            raise BusinessError(422, "原件校验标记为服务端入库字段")
        if payload.get("build_embeddings") is True:
            chunks = payload.get("chunks")
            if not isinstance(chunks, list) or not 1 <= len(chunks) <= 120 or any(
                    not isinstance(chunk, dict) or not isinstance(chunk.get("content"), str)
                    or not 1 <= len(chunk["content"]) <= 12000 for chunk in chunks):
                raise BusinessError(422, "本地建向量需要 1 至 120 个带文本的片段")
            enriched = []
            for start in range(0, len(chunks), 12):
                batch = chunks[start:start + 12]
                vectors = model.embed([chunk["content"] for chunk in batch])
                enriched.extend({**chunk, "embedding": vector, "embedding_model": model.embedding_name}
                                for chunk, vector in zip(batch, vectors, strict=True))
            payload = {**payload, "chunks": enriched}
        return service.knowledge.create_knowledge_version(actor.tenant, key, payload, actor=actor.name)

    @app.post("/v1/knowledge/{key}/ingest", status_code=201)
    def ingest_knowledge(key: str, actor: User, file: Annotated[UploadFile, File()],
                         metadata_json: Annotated[str, Form()]):
        library(actor, "editor", "admin")
        try:
            data = json.loads(metadata_json)
        except ValueError as exc:
            raise BusinessError(422, "metadata_json 不是有效 JSON") from exc
        if not isinstance(data, dict) or not isinstance(data.get("metadata", {}), dict):
            raise BusinessError(422, "入库元数据应为 JSON 对象")
        filename = PureWindowsPath(file.filename or "").name
        blob = extract_upload(file)
        try:
            extraction = parse_document(filename, blob)
        except FormatError as exc:
            raise BusinessError(422, str(exc)) from exc
        parts = extraction["snippets"]
        if not 1 <= len(parts) <= 400:
            raise BusinessError(422, "无可用文本或资料过长，请分册或按工序拆分归档")
        chunks = []
        for part in parts:
            content = part["text"]
            for start in range(0, len(content), 4000):
                text = content[start:start + 4000].strip()
                if not text:
                    continue
                locator = {"section": part.get("locator") or part.get("section") or "原件片段"}
                if isinstance(part.get("page"), int):
                    locator["page"] = part["page"]
                chunks.append({"content": text, "locator": locator})
        if not chunks or len(chunks) > 400:
            raise BusinessError(422, "资料没有可用切片或超过 400 项，请整理后上传")
        digest = service.objects.write(actor.tenant, "__knowledge__", blob)
        meta = {**data.get("metadata", {}), "stored_original": filename,
                "parse_status": extraction["status"], "source_sha256_verified": True}
        if (data.get("category") or data.get("knowledge_base")) == "KB04":
            meta.pop("reference_use", None)
        return service.knowledge.create_knowledge_version(actor.tenant, key, {
            **data, "metadata": meta, "sha256": digest, "chunks": chunks,
        }, actor=actor.name)

    @app.get("/v1/knowledge/{key}/versions/{version}/original")
    def knowledge_original(key: str, version: int, actor: User):
        library(actor, "editor", "reviewer", "admin")
        source = service.knowledge.get_knowledge_version(actor.tenant, key, version)
        filename = source.get("metadata", {}).get("stored_original") if source else None
        if not filename:
            raise BusinessError(404, "该版本通过外部来源登记，原件请到登记位置查阅")
        digest = source["sha256"]
        data = service.objects.read(actor.tenant, "__knowledge__", digest)
        with store.transaction(write=True) as conn:
            store.add_audit(conn, actor.tenant, actor.name, "knowledge.original.download", key,
                            metadata={"version": version, "sha256": digest})
        return Response(data, media_type="application/octet-stream",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}"})

    @app.post("/v1/knowledge/{key}/versions/{version}/review")
    def review_knowledge(key: str, version: int, payload: dict, actor: User):
        library(actor, "reviewer", "admin")
        return service.knowledge.review_knowledge_version(actor.tenant, key, version, actor=actor.name,
                                                          decision=string(payload.get("decision"), 24))

    @app.post("/v1/knowledge/search")
    def find_knowledge(payload: dict, actor: User):
        library(actor, "editor", "reviewer", "admin")
        search = service.knowledge.search_knowledge(actor.tenant, query=payload.get("query"),
            filters=payload.get("filters") or {}, query_vector=payload.get("query_vector"),
            embedding_model=payload.get("embedding_model"))
        return {"results": search}

    @app.get("/v1/evidence")
    def evidence_list(actor: User, evidence_type: str | None = None, as_of: str | None = None):
        library(actor, "editor", "reviewer", "admin")
        return {"items": service.knowledge.list_evidence(actor.tenant,
                filters={"evidence_type": evidence_type, "as_of": as_of})}

    @app.post("/v1/evidence/{key}/versions", status_code=201)
    def evidence_candidate(key: str, payload: dict, actor: User):
        library(actor, "editor", "admin")
        return service.knowledge.create_evidence_version(actor.tenant, key, payload, actor=actor.name)

    @app.post("/v1/evidence/{key}/versions/{version}/review")
    def review_evidence(key: str, version: int, payload: dict, actor: User):
        library(actor, "reviewer", "admin")
        return service.knowledge.review_evidence_version(actor.tenant, key, version, actor=actor.name,
                                                         decision=string(payload.get("decision"), 24))

    return app
