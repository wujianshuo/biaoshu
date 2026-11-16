CREATE TABLE IF NOT EXISTS projects (
  tenant_id text NOT NULL,
  project_id text NOT NULL,
  name text NOT NULL CHECK (length(name) > 0),
  profile_json text NOT NULL,
  created_at text NOT NULL,
  created_by text NOT NULL,
  PRIMARY KEY (tenant_id, project_id)
);

-- A logical record preserves history across project document, analysis, scoring,
-- evidence matching, chapter, audit and artifact revisions.
CREATE TABLE IF NOT EXISTS project_records (
  tenant_id text NOT NULL,
  project_id text NOT NULL,
  kind text NOT NULL CHECK (length(kind) > 0),
  record_key text NOT NULL CHECK (length(record_key) > 0),
  revision integer NOT NULL CHECK (revision > 0),
  payload_json text NOT NULL,
  payload_sha256 text NOT NULL CHECK (length(payload_sha256) = 64),
  created_at text NOT NULL,
  created_by text NOT NULL,
  PRIMARY KEY (tenant_id, project_id, kind, record_key, revision),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, project_id)
);

CREATE TABLE IF NOT EXISTS knowledge_categories (
  category_id text PRIMARY KEY,
  name text NOT NULL,
  permitted_in_writing integer NOT NULL CHECK (permitted_in_writing IN (0, 1))
);

CREATE TABLE IF NOT EXISTS knowledge_sources (
  tenant_id text NOT NULL,
  knowledge_key text NOT NULL,
  created_at text NOT NULL,
  created_by text NOT NULL,
  PRIMARY KEY (tenant_id, knowledge_key)
);

CREATE TABLE IF NOT EXISTS knowledge_versions (
  tenant_id text NOT NULL,
  knowledge_key text NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  source_version text NOT NULL,
  category_id text NOT NULL REFERENCES knowledge_categories (category_id),
  title text NOT NULL,
  source text NOT NULL,
  source_sha256 text NOT NULL CHECK (length(source_sha256) = 64),
  valid_from text,
  valid_until text,
  confidentiality text NOT NULL CHECK (confidentiality IN ('public', 'internal', 'restricted')),
  status text NOT NULL CHECK (status IN ('draft', 'approved', 'rejected', 'superseded', 'retired')),
  metadata_json text NOT NULL,
  created_at text NOT NULL,
  created_by text NOT NULL,
  reviewed_at text,
  reviewed_by text,
  PRIMARY KEY (tenant_id, knowledge_key, version),
  FOREIGN KEY (tenant_id, knowledge_key) REFERENCES knowledge_sources (tenant_id, knowledge_key),
  CHECK (valid_from IS NULL OR valid_until IS NULL OR valid_from <= valid_until)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_approved_knowledge_version
  ON knowledge_versions (tenant_id, knowledge_key) WHERE status = 'approved';
CREATE INDEX IF NOT EXISTS knowledge_eligible_lookup
  ON knowledge_versions (tenant_id, category_id, status, confidentiality);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
  tenant_id text NOT NULL,
  knowledge_key text NOT NULL,
  version integer NOT NULL,
  ordinal integer NOT NULL CHECK (ordinal > 0),
  locator_json text NOT NULL,
  content text NOT NULL CHECK (length(content) > 0),
  embedding_model text,
  embedding_json text,
  PRIMARY KEY (tenant_id, knowledge_key, version, ordinal),
  FOREIGN KEY (tenant_id, knowledge_key, version)
    REFERENCES knowledge_versions (tenant_id, knowledge_key, version),
  CHECK ((embedding_model IS NULL AND embedding_json IS NULL)
     OR (embedding_model IS NOT NULL AND embedding_json IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS enterprise_evidence_sources (
  tenant_id text NOT NULL,
  evidence_id text NOT NULL,
  created_at text NOT NULL,
  created_by text NOT NULL,
  PRIMARY KEY (tenant_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS enterprise_evidence (
  tenant_id text NOT NULL,
  evidence_id text NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  evidence_type text NOT NULL CHECK (evidence_type IN ('qualification', 'personnel', 'performance', 'equipment', 'management')),
  title text NOT NULL,
  specialty text NOT NULL,
  subject_status text NOT NULL CHECK (subject_status IN ('active', 'inactive', 'unknown')),
  valid_from text,
  valid_until text,
  source text NOT NULL,
  source_sha256 text NOT NULL CHECK (length(source_sha256) = 64),
  details_json text NOT NULL,
  status text NOT NULL CHECK (status IN ('draft', 'approved', 'rejected', 'superseded', 'retired')),
  created_at text NOT NULL,
  created_by text NOT NULL,
  reviewed_at text,
  reviewed_by text,
  PRIMARY KEY (tenant_id, evidence_id, version),
  FOREIGN KEY (tenant_id, evidence_id) REFERENCES enterprise_evidence_sources (tenant_id, evidence_id),
  CHECK (valid_from IS NULL OR valid_until IS NULL OR valid_from <= valid_until)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_approved_evidence_version
  ON enterprise_evidence (tenant_id, evidence_id) WHERE status = 'approved';
CREATE INDEX IF NOT EXISTS enterprise_evidence_lookup
  ON enterprise_evidence (tenant_id, evidence_type, status, specialty, subject_status);

CREATE TABLE IF NOT EXISTS audit_events (
  event_id text PRIMARY KEY,
  tenant_id text NOT NULL,
  project_id text,
  actor text NOT NULL,
  event_type text NOT NULL,
  target_key text NOT NULL,
  metadata_json text NOT NULL,
  occurred_at text NOT NULL,
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, project_id)
);

CREATE INDEX IF NOT EXISTS audit_project_time
  ON audit_events (tenant_id, project_id, occurred_at);
