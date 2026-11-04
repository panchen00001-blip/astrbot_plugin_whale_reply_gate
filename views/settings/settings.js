const bridge = window.AstrBotPluginView;
const form = document.getElementById("settings-form");
const fields = document.getElementById("settings-fields");
const configFields = document.getElementById("config-fields");
const saveButton = document.getElementById("save-settings");
const feedback = document.getElementById("feedback");
const currentStatus = document.getElementById("current-status");
const controls = new Map();
let schema;
let hasSavedKey = false;

function showFeedback(message, tone = "") {
  feedback.textContent = message;
  feedback.dataset.tone = tone;
}

function createControl(key, meta, value) {
  const input = document.createElement(meta.type === "list" ? "textarea" : "input");
  input.id = key;
  input.name = key;
  if (meta.type === "list") {
    input.rows = 3;
    input.value = value.join("\n");
    input.placeholder = key === "test_group_ids" ? "123456789\n987654321" : "每行填写一项";
  } else if (meta.type === "bool") {
    input.type = "checkbox";
    input.checked = value;
    input.setAttribute("role", "switch");
  } else {
    input.type = meta.secret ? "password" : "text";
    input.value = meta.secret ? "" : value;
    if (meta.type === "int" || meta.type === "float") {
      input.type = "number";
      input.step = meta.type === "int" ? "1" : "0.01";
      if (meta.min !== undefined) input.min = meta.min;
      if (meta.max !== undefined) input.max = meta.max;
    }
  }
  if (key === "api_base") input.type = "url";
  if (key === "typesafe_api_key") {
    input.autocomplete = "new-password";
    input.placeholder = hasSavedKey ? "已保存，留空保留原密钥" : "请输入 API Key";
  }
  input.required = ["api_base", "model", "bot_names"].includes(key)
    || meta.type === "int" || meta.type === "float";
  controls.set(key, input);
  return input;
}

function renderFields(items, values, parent, prefix = "") {
  for (const [name, meta] of Object.entries(items)) {
    const key = prefix + name;
    const section = document.createElement(meta.type === "object" ? "fieldset" : "div");
    section.className = meta.type === "object" ? "config-group" : "config-item";
    if (meta.type === "object") {
      const legend = document.createElement("legend");
      legend.textContent = meta.description;
      section.append(legend);
      renderFields(meta.items, values[name], section, key + ".");
    } else {
      const label = document.createElement("label");
      label.htmlFor = key;
      label.textContent = meta.description;
      const input = createControl(key, meta, values[name]);
      if (meta.type === "bool") {
        label.className = "toggle";
        label.prepend(input);
        section.append(label);
      } else {
        section.append(label, input);
      }
      const hint = document.createElement("p");
      hint.className = "hint";
      hint.id = key + "-hint";
      hint.textContent = meta.hint || "";
      if (key === "typesafe_api_key") {
        hint.textContent = hasSavedKey
          ? "密钥已保存且不回显；留空保留，填写新值则替换。"
          : "填写模型服务的 API Key。";
      }
      if (key === "model") {
        const list = document.createElement("select");
        list.id = "model-list";
        list.setAttribute("aria-label", "从可用模型列表中选择");
        list.addEventListener("change", () => { input.value = list.value; });
        const button = document.createElement("button");
        button.id = "fetch-models";
        button.type = "button";
        button.textContent = "获取模型";
        button.addEventListener("click", fetchModels);
        section.append(button, list);
        hint.textContent = "点击获取模型后，在下拉框中选择；也可直接填写模型 ID。";
      }
      input.setAttribute("aria-describedby", hint.id);
      section.append(hint);
    }
    parent.append(section);
  }
}

function applySettings(data) {
  schema = data.schema;
  hasSavedKey = data.has_api_key;
  controls.clear();
  configFields.replaceChildren();
  renderFields(schema, data, configFields);
  updateModelList(data.models);
  currentStatus.textContent = data.status_message;
}

function readFields(items, prefix = "") {
  const values = {};
  for (const [name, meta] of Object.entries(items)) {
    const key = prefix + name;
    if (meta.type === "object") {
      values[name] = readFields(meta.items, key + ".");
    } else {
      const input = controls.get(key);
      if (meta.type === "bool") {
        values[name] = input.checked;
      } else if (meta.type === "list") {
        values[name] = input.value.split("\n")
          .map(line => line.trim())
          .filter(Boolean);
      } else if (meta.type === "int" || meta.type === "float") {
        values[name] = Number(input.value);
      } else {
        values[name] = input.value.trim();
      }
    }
  }
  return values;
}

function connectionSettings() {
  return {
    api_base: controls.get("api_base").value.trim(),
    api_key: controls.get("typesafe_api_key").value.trim(),
  };
}

function updateModelList(models) {
  const list = document.getElementById("model-list");
  list.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = models.length ? "请选择一个模型" : "点击“获取模型”加载列表";
  list.append(placeholder);
  for (const model of models) {
    const option = document.createElement("option");
    option.value = model.id;
    option.textContent = model.description;
    list.append(option);
  }
  list.value = models.some(model => model.id === controls.get("model").value)
    ? controls.get("model").value : "";
}

async function fetchModels() {
  if (!controls.get("api_base").reportValidity()) return;
  const connection = connectionSettings();
  if (!connection.api_key && !hasSavedKey) {
    showFeedback("请先填写 API Key，再获取模型列表。", "error");
    controls.get("typesafe_api_key").focus();
    return;
  }
  fields.disabled = true;
  showFeedback("正在获取模型列表…");
  try {
    const data = await bridge.apiPost("models", connection);
    controls.get("api_base").value = data.api_base;
    updateModelList(data.models);
    showFeedback(`已获取 ${data.models.length} 个模型。选择后保存，其他未保存的参数保持在表单中。`);
  } catch (error) {
    showFeedback(error.message, "error");
  } finally {
    fields.disabled = false;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!form.reportValidity()) return;
  const body = readFields(schema);
  body.api_key = body.typesafe_api_key;
  delete body.typesafe_api_key;
  fields.disabled = true;
  saveButton.textContent = "正在保存…";
  try {
    const data = await bridge.apiPost("settings", body);
    applySettings(data);
    showFeedback("全部设置已保存并应用。");
  } catch (error) {
    showFeedback(error.message, "error");
  } finally {
    fields.disabled = false;
    saveButton.textContent = "保存并应用";
  }
});

async function loadSettings() {
  try {
    await bridge.ready();
    const data = await bridge.apiGet("settings");
    applySettings(data);
    fields.disabled = false;
  } catch (error) {
    currentStatus.textContent = "配置加载失败";
    showFeedback(error.message, "error");
  }
}

loadSettings();
