const element = (id) => document.getElementById(id);
const projectInput = element("project");
const result = element("result");
const notice = element("notice");
let token = "";

function display(value, message, isError = false) {
  result.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  notice.textContent = message;
  notice.dataset.error = isError;
}

async function api(path, { method = "GET", body, raw = false } = {}) {
  const headers = { Authorization: "Bearer " + token };
  if (body !== undefined && !(body instanceof FormData)) {
    body = JSON.stringify(body);
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, { method, headers, body });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === "string" ? error.detail : JSON.stringify(error.detail ?? error));
  }
  if (raw) return response;
  return response.json();
}

function project() {
  if (!projectInput.value) throw new Error("请先选择项目");
  return "/v1/projects/" + encodeURIComponent(projectInput.value);
}

async function loadProjects(selected = projectInput.value) {
  const value = await api("/v1/projects");
  projectInput.replaceChildren(new Option("请选择项目", ""));
  for (const row of value.projects) projectInput.add(new Option(row.name, row.project_id));
  if (value.projects.some((row) => row.project_id === selected)) projectInput.value = selected;
  return value;
}

function jsonObject(value) {
  const parsed = JSON.parse(value || "{}");
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("请输入 JSON 对象");
  return parsed;
}

function action(id, event, handler) {
  element(id).addEventListener(event, async (e) => {
    e.preventDefault();
    const button = e.submitter ?? (e.currentTarget.tagName === "BUTTON" ? e.currentTarget : null);
    if (button) button.disabled = true;
    notice.textContent = "正在执行";
    notice.dataset.error = false;
    try {
      const value = await handler(e.currentTarget);
      display(value ?? "操作已完成", "操作已完成");
    } catch (error) {
      display(error.message, "请求未完成", true);
    } finally {
      if (button) button.disabled = false;
    }
  });
}

action("connection", "submit", async () => {
  token = element("token").value;
  const projects = await loadProjects();
  element("token").value = "";
  return projects;
});

action("new-project", "submit", async (form) => {
  const row = await api("/v1/projects", { method: "POST", body: Object.fromEntries(new FormData(form)) });
  await loadProjects(row.project_id);
  return row;
});

action("upload", "submit", (form) =>
  api(project() + "/documents", { method: "POST", body: new FormData(form) }));

action("run", "submit", (form) => {
  project();
  const values = new FormData(form);
  return api("/v1/workflows/" + values.get("workflow") + "/run", {
    method: "POST", body: { ...jsonObject(values.get("payload")), project_id: projectInput.value },
  });
});

action("show-plan", "click", () => api(project() + "/technical-plan"));

function recordPath() {
  const key = element("key").value.trim();
  return project() + "/records/" + element("kind").value + (key ? "/" + encodeURIComponent(key) : "");
}

action("inspect", "submit", async () => {
  const row = await api(recordPath());
  if (row.revision) element("review").elements.revision.value = row.revision;
  return row;
});

action("review", "submit", (form) => {
  if (!element("key").value.trim()) throw new Error("请填写要复核的记录编号");
  const values = Object.fromEntries(new FormData(form));
  return api(recordPath() + "/review", {
    method: "POST",
    body: { ...values, revision: Number(values.revision), resolutions: jsonObject(values.resolutions) },
  });
});

action("download", "click", async () => {
  const kind = element("kind").value;
  const key = element("key").value.trim();
  if (!key || !["artifact", "document"].includes(kind)) throw new Error("请选择导出文件或原始资料，并填入编号");
  const response = await api(project() + "/" + (kind === "artifact" ? "artifacts" : "documents") +
    "/" + encodeURIComponent(key) + "/download", { raw: true });
  const name = response.headers.get("Content-Disposition")?.split("UTF-8''")[1];
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = name ? decodeURIComponent(name) : key;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

fetch("/healthz").then((response) => {
  if (!response.ok) throw new Error();
  return response.json();
}).then((info) => {
  element("health").textContent = info.model_configured ? "后端和模型已配置" : "后端在线 · 模型未配置";
}).catch(() => {
  element("health").textContent = "后端未就绪";
});
