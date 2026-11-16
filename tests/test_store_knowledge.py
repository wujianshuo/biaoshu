from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from uuid import uuid4

from biaoshu.knowledge import KnowledgeStore
from biaoshu.store import ConflictError, NotFoundError, Store


class IsolatedPostgresStore(Store):
    def __init__(self, dsn):
        super().__init__(dsn)
        self.isolated_schema = "biaoshu_test_" + uuid4().hex
        with super().transaction() as conn:
            conn.execute(f"CREATE SCHEMA {self.isolated_schema}")

    @contextmanager
    def transaction(self, *, write=False):
        with super().transaction(write=write) as conn:
            conn.execute(f"SET LOCAL search_path TO {self.isolated_schema}")
            yield conn

    def drop_test_schema(self):
        with super().transaction() as conn:
            conn.execute(f"DROP SCHEMA {self.isolated_schema} CASCADE")


class VersionedDataTest(unittest.TestCase):
    def setUp(self):
        dsn = os.getenv("BIAOSHU_TEST_POSTGRES_DSN")
        if dsn:
            if not dsn.startswith(("postgresql://", "postgres://")):
                raise ValueError("BIAOSHU_TEST_POSTGRES_DSN 必须指向可创建隔离 schema 的 PostgreSQL 测试库。")
            self.store = IsolatedPostgresStore(dsn)
            self.addCleanup(self.store.drop_test_schema)
        else:
            sandbox = TemporaryDirectory()
            self.addCleanup(sandbox.cleanup)
            self.store = Store("sqlite:///" + str(Path(sandbox.name) / "bid.db"))
        self.store.init()
        self.store.init()
        self.kb = KnowledgeStore(self.store)

    def test_workflow_snapshots_atomically_reject_changed_inputs(self):
        project = self.store.create_project("tenant-one", "测试投标项目", actor="writer")["project_id"]
        original = self.store.put_record("tenant-one", project, "document", "a", {"version": 1}, actor="writer")
        self.assertIsNone(self.store.get_record("tenant-two", project, "document", "a"))
        with self.assertRaises(NotFoundError):
            self.store.put_record("tenant-two", project, "document", "a", {"version": 9}, actor="outsider")
        first = self.store.put_record("tenant-one", project, "document", "a", {"version": 2}, actor="writer")
        repeated = self.store.put_record("tenant-one", project, "document", "a", {"version": 2}, actor="writer")
        self.assertEqual(first, repeated)
        self.assertEqual(first["revision"], 2)
        self.assertEqual(self.store.get_record("tenant-one", project, "document", "a", revision=1), original)
        with self.assertRaises(ConflictError):
            self.store.put_records("tenant-one", project,
                                   [{"kind": "analysis", "key": "latest", "data": {"fact": 10}}],
                                   actor="worker", preconditions=[{"kind": "document", "key": "a", "revision": 1}])
        self.assertIsNone(self.store.get_record("tenant-one", project, "analysis", "latest"))
        with self.assertRaises(ConflictError):
            self.store.put_records("tenant-one", project, [
                {"kind": "requirement", "key": "r1", "data": {"condition": "工期"}},
                {"kind": "document", "key": "a", "data": {"version": 3}, "expected_revision": 1},
            ], actor="worker")
        self.assertEqual(self.store.list_records("tenant-one", project, "requirement"), [])

    def test_concurrent_expected_revision_prevents_lost_update(self):
        project = self.store.create_project("company", "新项目", actor="writer")["project_id"]
        barrier = threading.Barrier(2)

        def update(part):
            barrier.wait(timeout=5)
            try:
                return self.store.put_record("company", project, "chapter", "design", {"draft": part},
                                             actor="editor", expected_revision=0)["data"]["draft"]
            except ConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            completed = list(pool.map(update, ["东段", "西段"]))
        self.assertEqual(sum(value is not None for value in completed), 1)
        self.assertEqual(len(self.store.list_revisions("company", project, "chapter", "design")), 1)

    @staticmethod
    def technique(text, *, category="KB03", status="internal", specialty="市政", vector=None):
        chunk = {"content": text, "locator": {"page": 7, "section": "压实作业"}}
        if vector is not None:
            chunk.update(embedding=vector, embedding_model="local-v1")
        return {
            "category": category, "title": "工法样例", "source": "原件/方案.docx",
            "sha256": sha256(text.encode()).hexdigest(), "source_version": "2026-02",
            "confidentiality": status, "metadata": {"specialty": specialty, "region": "通用"},
            "chunks": [chunk],
        }

    def test_document_provenance_and_publication_gate(self):
        create = self.kb.create_knowledge_version
        review = self.kb.review_knowledge_version
        search = self.kb.search_knowledge
        draft = create("one", "road", self.technique("压实施工使用分层检查方案甲。"), actor="author")
        self.assertEqual(search("one", query="压实施工"), [])
        self.assertEqual(draft["source_version"], "2026-02")
        review("one", "road", 1, actor="reviewer", decision="approve")
        create("one", "road", self.technique("压实施工改用签认检查方案乙。"), actor="author")
        create("two", "road", self.technique("压实施工的其他租户资料。"), actor="author")
        review("two", "road", 1, actor="reviewer", decision="approve")
        hit = search("one", query="压实施工", filters={"specialty": "市政", "region": "河南"})
        self.assertEqual([(r["key"], r["version"]) for r in hit], [("road", 1)])
        self.assertEqual(hit[0]["sha256"], draft["sha256"])
        self.assertEqual(hit[0]["locator"]["page"], 7)
        self.assertEqual(self.kb.invalid_knowledge_references("one", hit), [])
        self.assertEqual(search("one", query="压实施工", filters={"specialty": "房建"}), [])
        review("one", "road", 2, actor="reviewer", decision="approve")
        self.assertEqual(search("one", query="压实施工")[0]["version"], 2)
        self.assertEqual(self.kb.get_knowledge_version("one", "road", 1)["status"], "superseded")
        self.assertEqual(self.kb.invalid_knowledge_references("one", hit), hit)
        with self.assertRaises(ConflictError):
            review("one", "road", 1, actor="reviewer", decision="approve")
        review("one", "road", 2, actor="reviewer", decision="retire")
        self.assertEqual(search("one", query="压实施工"), [])

    def test_semantic_and_metadata_filters_against_unapproved_or_restricted_sources(self):
        for key, category, status in [("history", "KB04", "internal"), ("restricted", "KB03", "restricted"),
                                      ("cost", "KB08", "internal")]:
            body = self.technique("砂砾压实方案。", category=category, status=status, vector=[3.0, 4.0])
            self.kb.create_knowledge_version("company", key, body, actor="author")
            self.kb.review_knowledge_version("company", key, 1, actor="reviewer", decision="approve")
        hits = self.kb.search_knowledge("company", query="不同用词", filters={"mode": "semantic"},
                                        query_vector=[3.0, 4.0], embedding_model="local-v1")
        self.assertEqual([r["key"] for r in hits], ["history"])
        self.assertTrue(hits[0]["reference_only"])
        self.assertEqual(self.kb.search_knowledge("company", query="压实", filters={"knowledge_base": "KB08"}), [])
        with self.assertRaises(ValueError):
            self.kb.create_knowledge_version("company", "project-source",
                                             dict(self.technique("模拟材料"), project_id="active-project"), actor="author")
        with self.assertRaises(ConflictError):
            future = self.technique("模拟未来规范", category="KB01")
            future["valid_from"] = "2099-01-01"
            self.kb.create_knowledge_version("company", "future-rule", future, actor="author")
            self.kb.review_knowledge_version("company", "future-rule", 1, actor="reviewer", decision="approve")

    def test_certificate_matching_requires_approval_active_status_and_verified_validity(self):
        record = {
            "evidence_type": "qualification", "title": "某模拟企业资质", "specialty": "市政",
            "subject_status": "active", "source": "证据/模拟.pdf", "sha256": "a" * 64,
            "valid_until": "2099-01-01", "details": {"qualification_grade": "一级"},
        }
        draft = self.kb.create_evidence_version("company", "Q1", record, actor="author")
        self.assertEqual(self.kb.list_evidence("company"), [])
        self.kb.review_evidence_version("company", draft["key"], 1, actor="reviewer", decision="approve")
        current = self.kb.list_evidence("company", filters={"specialty": "市政"})
        self.assertEqual(len(current), 1)
        self.assertEqual(self.kb.invalid_evidence_references("company", current), [])
        self.assertEqual(self.kb.list_evidence("company", filters={"as_of": "2099-01-02"}), [])
        self.assertEqual(self.kb.list_evidence("other"), [])
        self.kb.create_evidence_version("company", "Q1", dict(record, subject_status="inactive"), actor="author")
        self.kb.review_evidence_version("company", "Q1", 2, actor="reviewer", decision="approve")
        self.assertEqual(self.kb.list_evidence("company"), [])
        self.assertEqual(self.kb.invalid_evidence_references("company", current), current)


if __name__ == "__main__":
    unittest.main()
