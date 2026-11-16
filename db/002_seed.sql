INSERT INTO knowledge_categories (category_id, name, permitted_in_writing) VALUES
  ('KB01', '国家法规及工程规范', 1),
  ('KB02', '招标文件范本', 1),
  ('KB03', '企业施工技术方案', 1),
  ('KB04', '企业历史投标文件', 1),
  ('KB05', '企业工程业绩', 0),
  ('KB06', '企业资质和人员', 0),
  ('KB07', '企业管理制度及编制标准', 1),
  ('KB08', '企业造价及成本参考', 0)
ON CONFLICT (category_id) DO NOTHING;
