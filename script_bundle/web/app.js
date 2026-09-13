let currentStatus = null;

function el(id) {
  return document.getElementById(id);
}

function clearNode(node) {
  if (!node) return;
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

function appendOption(select, value, label) {
  if (!select) return;
  const option = document.createElement("option");
  option.value = value;
  option.textContent = label;
  select.appendChild(option);
}

function setSelectOptions(selectId, items, selectedValue, formatter, emptyLabel = "Not Set") {
  const select = el(selectId);
  if (!select) return;
  clearNode(select);
  appendOption(select, "", emptyLabel);
  for (const item of items || []) {
    const option = document.createElement("option");
    option.value = item.value;
    option.textContent = formatter(item);
    select.appendChild(option);
  }
  select.value = selectedValue || "";
}

async function getJSON(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    throw new Error(await readError(response));
  }
  return response.json();
}

async function readError(response) {
  try {
    const payload = await response.json();
    return payload.detail || `Request failed: ${response.status}`;
  } catch (_) {
    return `Request failed: ${response.status}`;
  }
}

function renderDefinitionList(targetId, rows) {
  const box = el(targetId);
  if (!box) return;
  clearNode(box);
  for (const [label, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value || "-";
    box.appendChild(dt);
    box.appendChild(dd);
  }
}

function buildLocalUrl(ip, sslEngine) {
  if (!ip) return "";
  const scheme = sslEngine === "plain_http" ? "http" : "https";
  return `${scheme}://${ip}`;
}

function setFeedback(id, message, kind = "") {
  const node = el(id);
  node.textContent = message;
  if (kind) {
    node.dataset.kind = kind;
  } else {
    delete node.dataset.kind;
  }
}

function updateStepVisibility(connected) {
  el("stepConnect").classList.toggle("hidden", connected);
  el("stepSettings").classList.toggle("hidden", !connected);
}

function renderInfo(iot) {
  renderDefinitionList("iotInfo", [
    ["Identifier", iot?.identifier || "-"],
    ["Address", iot?.ip || "-"],
    ["Version", iot?.version || "-"],
  ]);
}

function renderConnection(connection) {
  renderDefinitionList("serverConnection", [
    ["Paired", connection?.connected ? "yes" : "no"],
    ["Cloud URL", connection?.url || "-"],
    ["Database", connection?.db_name || "-"],
    ["DB UUID", connection?.db_uuid || "-"],
    ["Last Sync", connection?.last_sync_ok ? "ok" : "pending or failed"],
    ["Message", connection?.last_sync_message || "-"],
  ]);
}

function renderCloudBridge(cloudBridge) {
  renderDefinitionList("cloudBridge", [
    ["WebSocket", cloudBridge?.connected ? "connected" : "disconnected"],
    ["Server", cloudBridge?.server_url || "-"],
    ["Channel", cloudBridge?.iot_channel || "-"],
    ["TLS Verify", cloudBridge?.ssl_verify ? "enabled" : "disabled"],
    ["Last Error", cloudBridge?.last_error || "-"],
  ]);
}

function renderCertificates(certificates) {
  const parts = [];
  if (certificates?.crt_ready) parts.push("CRT ready");
  if (certificates?.p12_ready) parts.push("P12 ready");
  if (certificates?.password_file) parts.push(`Password file: ${certificates.password_file}`);
  if (certificates?.startup_error) parts.push(`Startup error: ${certificates.startup_error}`);
  setFeedback("certHint", parts.join(" | ") || "Certificates unavailable", parts.length ? "success" : "");
}

function renderDevices(devices) {
  const list = el("deviceList");
  if (!list) return;
  clearNode(list);

  if (!devices?.length) {
    const empty = document.createElement("p");
    empty.className = "feedback";
    empty.textContent = "No devices reported by the runtime.";
    list.appendChild(empty);
    return;
  }

  for (const device of devices) {
    const item = document.createElement("article");
    item.className = "device-item";

    const copy = document.createElement("div");
    copy.className = "device-copy";

    const icon = document.createElement("span");
    icon.className = "device-icon";
    icon.textContent = deviceTypeIcon(device.type);
    icon.title = device.type || "device";

    const textBox = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = device.name || device.identifier;
    const primary = document.createElement("p");
    primary.textContent = `${device.identifier} | ${device.type || "unknown"} | ${device.connection || "unknown"}`;

    const meta = document.createElement("p");
    meta.className = "device-meta";
    const queue = device?.metadata?.cups_queue || device?.metadata?.queue || "";
    meta.textContent = queue
      ? `Queue: ${queue}${device?.metadata?.is_default ? " | default" : ""}`
      : `Subtype: ${device.subtype || "-"}`;

    textBox.appendChild(title);
    textBox.appendChild(primary);
    textBox.appendChild(meta);
    copy.appendChild(icon);
    copy.appendChild(textBox);

    const badge = document.createElement("span");
    badge.className = `device-state ${device.status || "unknown"}`;
    badge.textContent = device.status || "unknown";

    item.appendChild(copy);
    item.appendChild(badge);
    list.appendChild(item);

  }
}

function renderRuntimeChips(status) {
  const chip = el("runtimeModeChip");
  const connected = Boolean(status?.server_connection?.connected);
  chip.textContent = connected ? "Bound" : "Local only";
  chip.className = `state-chip ${connected ? "state-chip-ok" : "state-chip-warn"}`;
}

function extractPrinterOptions(devices) {
  return (devices || [])
    .filter((device) => device.type === "printer")
    .map((device) => ({
      value: device.identifier,
      label: device.name || device.identifier,
      queue: device?.metadata?.cups_queue || device?.metadata?.queue || "",
    }));
}

function renderSettings(status) {
  currentStatus = status;
  const localConfig = status.local_config || {};
  const sslEngine = localConfig.ssl_engine || "secure_https";
  const printerOptions = extractPrinterOptions(status.devices);
  const queueOptions = printerOptions
    .filter((item) => item.queue)
    .map((item) => ({ value: item.queue, label: item.queue }));

  el("sslEngine").value = sslEngine;
  el("localUrl").value = localConfig.local_url || buildLocalUrl(status.iot?.ip, sslEngine);
  setSelectOptions(
    "printerIdentifier",
    printerOptions.map((item) => ({ value: item.value, label: item.label })),
    localConfig.printer_identifier || "",
    (item) => item.label,
    "Auto"
  );
  setSelectOptions(
    "primaryPrinterQueue",
    queueOptions,
    localConfig.primary_printer_queue || "",
    (item) => item.label,
    "Auto"
  );
  renderRuntimeChips(status);
  updateStepVisibility(Boolean(status.server_connection?.connected));
}

async function load() {
  const data = await getJSON("/api/status");
  renderInfo(data.iot);
  renderConnection(data.server_connection);
  renderCloudBridge(data.cloud_bridge);
  renderCertificates(data.certificates);
  renderDevices(data.devices);
  renderSettings(data);
  el("syncResult").textContent = data?.server_connection?.last_sync_message || "Waiting for sync";
  el("helloStatus").textContent = "online";
  el("helloStatus").classList.add("status-ok");
}

async function saveSettings() {
  const payload = {
    ssl_engine: el("sslEngine").value,
    local_url: el("localUrl").value.trim(),
    printer_identifier: el("printerIdentifier").value,
    primary_printer_queue: el("primaryPrinterQueue").value,
  };
  await getJSON("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

async function connectWithTokenUrl(tokenUrl) {
  await getJSON("/api/connect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token_url: tokenUrl }),
  });
}

async function disconnectServer() {
  await getJSON("/api/disconnect", { method: "POST" });
}

el("sslEngine").addEventListener("change", () => {
  const input = el("localUrl");
  if (!input.value.trim() && currentStatus?.iot?.ip) {
    input.value = buildLocalUrl(currentStatus.iot.ip, el("sslEngine").value);
  }
});

el("connectForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const tokenUrl = el("tokenUrl").value.trim();
  if (!tokenUrl) {
    setFeedback("formFeedback", "Token URL is required.", "error");
    return;
  }

  setFeedback("formFeedback", "Saving local settings and linking to Odoo...");
  try {
    await saveSettings();
    await connectWithTokenUrl(tokenUrl);
    await load();
    setFeedback("formFeedback", "IoT binding saved successfully.", "success");
  } catch (error) {
    console.error(error);
    setFeedback("formFeedback", error.message || "Binding failed.", "error");
  }
});

el("settingsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await saveSettings();
    await load();
    setFeedback("settingsFeedback", "Settings saved.", "success");
  } catch (error) {
    console.error(error);
    setFeedback("settingsFeedback", error.message || "Failed to save settings.", "error");
  }
});

el("backToConnect").addEventListener("click", () => {
  updateStepVisibility(false);
  setFeedback("formFeedback", "Paste a new token URL to pair another service.");
});

el("disconnectServer").addEventListener("click", async () => {
  const confirmed = window.confirm(
    "Unbind the current Odoo server from this runtime?\n\n" +
      "This also clears the printer, scale and VFD configuration so the box is " +
      "ready for a new deployment. The receipt layouts are kept."
  );
  if (!confirmed) return;

  try {
    await disconnectServer();
    el("tokenUrl").value = "";
    await load();
    // The unbind cleared the machine configuration, which /api/status does not
    // carry.  Re-read these panels too, otherwise they keep showing the
    // pre-unbind values and the next save would write them straight back.
    await loadScaleConfig();
    await loadScalePorts();
    updateStepVisibility(false);
    setFeedback("settingsFeedback", "Current server unbound. Configuration cleared.", "success");
    setFeedback("formFeedback", "Runtime is ready for a new token URL.", "success");
  } catch (error) {
    console.error(error);
    setFeedback("settingsFeedback", error.message || "Failed to unbind server.", "error");
  }
});

load().catch((error) => {
  console.error(error);
  const message = error?.message || "Failed to load runtime status";
  setFeedback("formFeedback", message, "error");
  setFeedback("settingsFeedback", message, "error");
  el("helloStatus").textContent = "offline";
  window.alert(`Failed to load runtime status: ${message}`);
});

function deviceTypeIcon(type) {
  if (type === "printer") return "PR";
  if (type === "scale") return "SC";
  return "IO";
}

async function loadScaleConfig() {
  try {
    const cfg = await getJSON("/api/scale/config");
    el("scaleBrand").value = cfg.brand || "zfoc";
    el("scaleBaudrate").value = cfg.baudrate || 9600;
    el("scaleTimeout").value = cfg.timeout || 1.2;
    el("scaleMonitorStatus").textContent = cfg.is_monitor_running ? "监控中" : "未运行";
    el("scaleMonitorStatus").style.color = cfg.is_monitor_running ? "#10B981" : "#94A3B8";
    return cfg;
  } catch (_) {
    return null;
  }
}

async function loadScalePorts() {
  try {
    const result = await getJSON("/api/scale/ports");
    const select = el("scalePort");
    clearNode(select);
    appendOption(select, "", "请选择串口");
    for (const p of result.ports || []) {
      appendOption(select, p.device, `${p.device} - ${p.description || ""}`);
    }
    return result.ports;
  } catch (_) {
    return [];
  }
}

async function refreshScaleWeight() {
  try {
    const result = await getJSON("/api/scale/weight");
    if (result.status === "success") {
      el("scaleWeight").textContent = `${result.weight_kg?.toFixed(3)} kg`;
      el("scaleWeight").style.color = "#10B981";
    } else {
      el("scaleWeight").textContent = result.message || "--";
      el("scaleWeight").style.color = "#94A3B8";
    }
  } catch (error) {
    el("scaleWeight").textContent = error.message || "--";
    el("scaleWeight").style.color = "#EF4444";
  }
}

async function saveScaleConfig() {
  const payload = {
    scale_brand: el("scaleBrand").value,
    scale_port: el("scalePort").value,
    scale_baudrate: parseInt(el("scaleBaudrate").value) || 9600,
    scale_timeout: parseFloat(el("scaleTimeout").value) || 1.2,
  };
  await getJSON("/api/scale/save_config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  setFeedback("scaleFeedback", "电子秤配置已保存", "success");
  await loadScaleConfig();
}

loadScaleConfig();
loadScalePorts();

el("scaleForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  setFeedback("scaleFeedback", "正在保存...", "warn");
  try {
    await saveScaleConfig();
    await refreshScaleWeight();
  } catch (error) {
    setFeedback("scaleFeedback", error.message || "保存失败", "error");
  }
});

el("scaleRefresh").addEventListener("click", async () => {
  setFeedback("scaleFeedback", "正在刷新...", "warn");
  try {
    await loadScalePorts();
    const result = await getJSON("/api/scale/refresh", { method: "POST" });
    setFeedback("scaleFeedback", `监控${result.is_running ? "已启动" : "未启动"}`, "success");
    await loadScaleConfig();
  } catch (error) {
    setFeedback("scaleFeedback", error.message || "刷新失败", "error");
  }
});

el("scaleReadWeight").addEventListener("click", refreshScaleWeight);

const query = new URLSearchParams(window.location.search);
if (query.get("token") && query.get("db_uuid")) {
  const autoTokenUrl = `${window.location.origin}?${query.toString()}`;
  const sourceOrigin = query.get("source") || query.get("origin") || "";
  const tokenUrl =
    sourceOrigin && sourceOrigin.startsWith("http")
      ? `${sourceOrigin}?${query.toString()}`
      : autoTokenUrl;
  el("tokenUrl").value = tokenUrl;
  connectWithTokenUrl(tokenUrl)
    .then(load)
    .then(() => {
      setFeedback("formFeedback", "IoT binding saved successfully.", "success");
      updateStepVisibility(true);
    })
    .catch((error) => {
      console.error(error);
      setFeedback("formFeedback", error.message || "Auto connect failed.", "error");
    });
}

// ── Visual ESC/POS receipt editor ───────────────────────────────────

let receiptTemplate = null;
let selectedReceiptBlockId = null;
let draggedReceiptBlockId = null;
let receiptHistory = [];
let receiptFuture = [];
let receiptPreviewTimer = null;
let receiptTemplateMode = "receipt";
let receiptTemplateDirty = false;
let printerProfile = null;
let receiptHasOverflow = false;

const RECEIPT_GROUPS = [
  ["抬头与订单", ["portal_prompt", "logo", "company", "customer", "table", "invoice", "tracking", "order_info"]],
  ["商品", ["product_header", "products"]],
  ["金额与付款", ["promotions", "totals", "payments", "redsys", "coupons", "vouchers", "loyalty"]],
  ["页脚与营销", ["footer", "delivery", "qr"]],
  ["自定义", []],
];

function cloneReceiptTemplate(value) {
  return JSON.parse(JSON.stringify(value));
}

function setReceiptDirty(dirty) {
  receiptTemplateDirty = dirty;
  const chip = el("receiptSaveState");
  chip.textContent = dirty ? "有未保存修改" : "已保存";
  chip.className = `state-chip ${dirty ? "state-chip-warn" : "state-chip-ok"}`;
}

function receiptTemplateEndpoint(suffix = "") {
  const base = receiptTemplateMode === "kitchen" ? "/api/kitchen-template" : "/api/receipt-template";
  return `${base}${suffix}`;
}

function updateReceiptModeUI() {
  const kitchen = receiptTemplateMode === "kitchen";
  el("receiptEditorTitle").textContent = kitchen ? "厨房单可视化编辑器" : "小票可视化编辑器";
  el("receiptBlockHeading").textContent = kitchen ? "厨房单区块" : "小票区块";
  el("receiptPreviewHeading").textContent = kitchen ? "厨房单实时预览" : "顾客小票实时预览";
  el("kitchenLineSettings").classList.toggle("hidden", !kitchen);
  for (const button of document.querySelectorAll("[data-receipt-mode]")) {
    button.classList.toggle("is-active", button.dataset.receiptMode === receiptTemplateMode);
  }
}

// Line types the server offers for per-line sizing, as [{id, label}].  Fetched
// rather than hardcoded so a new line type in the builder needs no change here.
let kitchenLineClasses = [];
const KITCHEN_SIZE_LABELS = { 1: "标准", 2: "中号", 3: "大号" };
const KITCHEN_BOLD_CHOICES = [["inherit", "保持原样"], ["true", "加粗"], ["false", "不加粗"]];

function updateKitchenLineSetting(kind, lineClass, value) {
  if (!receiptTemplate) return;
  const key = kind === "bold" ? "line_bold" : "line_font_sizes";
  const map = receiptTemplate[key] || (receiptTemplate[key] = {});
  const next = value === null ? undefined : value;
  if (map[lineClass] === next) return;
  rememberReceiptTemplate();
  // An absent entry means "leave the line's own weight alone", so removing the
  // key is how the editor expresses 保持原样.
  if (next === undefined) delete map[lineClass];
  else map[lineClass] = next;
  receiptTemplateChanged();
}

function renderKitchenLineSettings() {
  const container = el("kitchenLineSettingsRows");
  if (!container || !receiptTemplate) return;
  // Only the line types the selected block actually prints, so the panel reads
  // as "settings for this block" rather than a global list.
  const wanted = kitchenLineClasses.filter((entry) => entry.block === selectedReceiptBlockId);
  const key = wanted.map((entry) => entry.id).join("|");
  if (container.dataset.lineKey !== key) {
    container.dataset.lineKey = key;
    clearNode(container);
    for (const { id, label } of wanted) {
      const row = document.createElement("label");
      row.className = "field kitchen-line-row";
      row.dataset.lineClass = id;

      const name = document.createElement("span");
      name.textContent = label;

      const size = document.createElement("select");
      for (const value of Object.keys(KITCHEN_SIZE_LABELS)) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = KITCHEN_SIZE_LABELS[value];
        size.append(option);
      }
      size.addEventListener("change", () => {
        updateKitchenLineSetting("size", id, Number(size.value) || 1);
      });

      const bold = document.createElement("select");
      for (const [value, text] of KITCHEN_BOLD_CHOICES) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = text;
        bold.append(option);
      }
      bold.addEventListener("change", () => {
        updateKitchenLineSetting("bold", id, bold.value === "inherit" ? null : bold.value === "true");
      });

      row.append(name, size, bold);
      container.append(row);
    }
  }
  const sizes = receiptTemplate.line_font_sizes || {};
  const boldMap = receiptTemplate.line_bold || {};
  for (const row of container.children) {
    const id = row.dataset.lineClass;
    const [size, bold] = row.querySelectorAll("select");
    size.value = String(sizes[id] || 1);
    const weight = boldMap[id];
    bold.value = weight === true ? "true" : weight === false ? "false" : "inherit";
  }
}

function rememberReceiptTemplate() {
  if (!receiptTemplate) return;
  receiptHistory.push(cloneReceiptTemplate(receiptTemplate));
  receiptFuture = [];
  if (receiptHistory.length > 30) receiptHistory.shift();
  el("receiptUndo").disabled = receiptHistory.length === 0;
}

function receiptBlockById(blockId) {
  return receiptTemplate?.blocks?.find((block) => block.id === blockId) || null;
}

function receiptProductHeaderBlock() {
  return receiptBlockById("product_header");
}

function renderReceiptBlockList() {
  const list = el("receiptBlockList");
  clearNode(list);
  const query = (el("receiptBlockSearch")?.value || "").trim().toLocaleLowerCase();
  for (const [groupLabel, ids] of RECEIPT_GROUPS) {
    const blocks = (receiptTemplate?.blocks || []).filter((block) => {
      const inGroup = ids.length ? ids.includes(block.id) : block.kind !== "builtin";
      return inGroup && (!query || block.label.toLocaleLowerCase().includes(query));
    });
    if (!blocks.length) continue;
    const group = document.createElement("details");
    group.className = "receipt-block-group";
    group.open = true;
    const summary = document.createElement("summary");
    summary.textContent = `${groupLabel} · ${blocks.length}`;
    const groupList = document.createElement("div");
    groupList.className = "receipt-block-group-list";
    for (const block of blocks) {
    const item = document.createElement("article");
    item.className = "receipt-block-item";
    if (!block.enabled) item.classList.add("is-disabled");
    if (block.id === selectedReceiptBlockId) item.classList.add("is-selected");
    item.dataset.blockId = block.id;
    item.draggable = true;

    const handle = document.createElement("span");
    handle.className = "receipt-drag-handle";
    handle.textContent = "⋮⋮";
    handle.title = "拖动排序";

    const name = document.createElement("span");
    name.className = "receipt-block-name";
    name.textContent = block.label;
    name.title = block.label;

    const visibility = document.createElement("button");
    visibility.type = "button";
    visibility.className = "receipt-visibility";
    visibility.textContent = block.enabled ? "显示" : "隐藏";
    visibility.title = block.enabled ? "点击隐藏" : "点击显示";
    visibility.addEventListener("click", (event) => {
      event.stopPropagation();
      rememberReceiptTemplate();
      block.enabled = !block.enabled;
      receiptTemplateChanged();
    });

    const orderActions = document.createElement("span");
    orderActions.className = "receipt-order-actions";
    for (const [label, delta] of [["上移", -1], ["下移", 1]]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "receipt-order-action";
      button.textContent = delta < 0 ? "↑" : "↓";
      button.title = label;
      button.setAttribute("aria-label", `${block.label}${label}`);
      const index = receiptTemplate.blocks.findIndex((entry) => entry.id === block.id);
      button.disabled = delta < 0 ? index === 0 : index === receiptTemplate.blocks.length - 1;
      button.addEventListener("click", (event) => { event.stopPropagation(); moveReceiptBlock(block.id, delta); });
      orderActions.append(button);
    }
    item.append(handle, name, orderActions, visibility);
    item.addEventListener("click", () => selectReceiptBlock(block.id));
    item.addEventListener("dragstart", (event) => {
      draggedReceiptBlockId = block.id;
      item.classList.add("is-dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", block.id);
    });
    item.addEventListener("dragend", () => {
      draggedReceiptBlockId = null;
      item.classList.remove("is-dragging");
    });
    item.addEventListener("dragover", (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
    });
    item.addEventListener("drop", (event) => {
      event.preventDefault();
      const sourceId = draggedReceiptBlockId || event.dataTransfer.getData("text/plain");
      if (!sourceId || sourceId === block.id) return;
      const sourceIndex = receiptTemplate.blocks.findIndex((entry) => entry.id === sourceId);
      const targetIndex = receiptTemplate.blocks.findIndex((entry) => entry.id === block.id);
      if (sourceIndex < 0 || targetIndex < 0) return;
      rememberReceiptTemplate();
      const [moved] = receiptTemplate.blocks.splice(sourceIndex, 1);
      receiptTemplate.blocks.splice(targetIndex, 0, moved);
      receiptTemplateChanged();
    });
    groupList.appendChild(item);
    }
    group.append(summary, groupList);
    list.appendChild(group);
  }
}

function selectReceiptBlock(blockId) {
  selectedReceiptBlockId = blockId;
  const block = receiptBlockById(blockId);
  el("receiptInspectorEmpty").classList.toggle("hidden", Boolean(block));
  el("receiptInspectorForm").classList.toggle("hidden", !block);
  if (block) {
    const kind = block.kind || "builtin";
    const editableBuiltins = receiptTemplateMode === "kitchen"
      ? ["order_type", "status", "order_meta", "location", "time"]
      : ["company", "invoice", "order_info", "product_header", "footer", "portal_prompt"];
    const canOverrideContent = kind === "builtin" && editableBuiltins.includes(block.id);
    el("receiptInspectorHint").textContent = block.label;
    el("receiptBlockEnabled").checked = Boolean(block.enabled);
    el("receiptBlockAlign").value = block.align || "inherit";
    el("receiptBlockBold").value = String(block.bold ?? "inherit");
    el("receiptHorizontalOffsetRange").value = Number(block.horizontal_offset || 0);
    el("receiptHorizontalOffset").value = Number(block.horizontal_offset || 0);
    el("receiptBlockSpacing").value = Number(block.spacing_after || 0);
    el("receiptBlockSeparatorAfter").checked = Boolean(block.separator_after);
    el("receiptBlockSeparatorAfterCharacter").value = block.separator_after_character || "-";
    el("receiptBlockContentField").classList.toggle("hidden", !canOverrideContent);
    const usesProductColumns = ["product_header", "products"].includes(block.id);
    const headerBlock = receiptProductHeaderBlock() || block;
    el("receiptHorizontalOffsetField").classList.toggle("hidden", usesProductColumns);
    el("receiptProductHeaderFields").classList.toggle("hidden", !usesProductColumns);
    el("receiptCustomTextField").classList.toggle("hidden", kind !== "text");
    el("receiptSeparatorField").classList.toggle("hidden", kind !== "separator");
    el("receiptSpacerField").classList.toggle("hidden", kind !== "spacer");
    // The size dropdown is the only size control for built-in lines.  The old
    // "取餐号双倍字号" checkbox is gone: it wrote a field the renderer never
    // honoured for the table, and the dropdown covers the same ground.
    const receiptFontTarget =
      receiptTemplateMode === "receipt" && ["tracking", "table", "products"].includes(block.id);
    el("receiptFontSizeField").classList.toggle("hidden", !receiptFontTarget);
    // A custom text block has no dropdown, so it keeps the double-size toggle.
    el("receiptDoubleSizeField").classList.toggle("hidden", kind !== "text");
    el("receiptDeleteBlock").classList.toggle("hidden", kind === "builtin");
    el("receiptBlockContent").value = block.id === "portal_prompt"
      ? "{{ portal_title }}"
      : (block.content || "");
    el("receiptBlockContent").readOnly = block.id === "portal_prompt";
    el("receiptQtyLabel").value = headerBlock.qty_label || "Uds.";
    el("receiptProductLabel").value = headerBlock.product_label || "Producto";
    el("receiptAmountLabel").value = headerBlock.amount_label || "Importe";
    el("receiptQtyColumns").value = Number(headerBlock.qty_columns || 6);
    el("receiptAmountColumns").value = Number(headerBlock.amount_columns || 10);
    el("receiptProductColumns").value = Number(headerBlock.product_columns || 30);
    el("receiptColumnGutter").value = Number(headerBlock.gutter_columns ?? 2);
    el("receiptCustomText").value = block.text || "";
    el("receiptSeparatorCharacter").value = block.character || "-";
    el("receiptSpacerLines").value = Number(block.lines || 1);
    el("receiptDoubleSize").checked = Boolean(block.double_size);
    const productDetailFont = receiptTemplateMode === "receipt" && block.id === "products";
    el("receiptFontSizeField").querySelector("span").textContent = productDetailFont ? "商品明细字号" : "字号大小";
    el("receiptFontSize").value = String(Math.min(5, block.font_size || 1));
  }
  renderKitchenLineSettings();
  renderReceiptBlockList();
}

function receiptTemplateChanged() {
  setReceiptDirty(true);
  el("receiptUndo").disabled = receiptHistory.length === 0;
  renderReceiptBlockList();
  if (selectedReceiptBlockId) selectReceiptBlock(selectedReceiptBlockId);
  renderKitchenLineSettings();
  scheduleReceiptPreview();
}

function updateSelectedReceiptBlock(field, value) {
  const block = receiptBlockById(selectedReceiptBlockId);
  if (!block || block[field] === value) return;
  rememberReceiptTemplate();
  block[field] = value;
  receiptTemplateChanged();
}

function addReceiptBlock(kind) {
  const suffix = `${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
  const defaults = {
    text: {label: "自定义文字", text: "在这里输入文字", align: "center", double_size: false},
    separator: {label: "自定义分隔线", character: "-", align: "left"},
    spacer: {label: "自定义空行", lines: 1, align: "left"},
  };
  rememberReceiptTemplate();
  const block = {
    id: `custom_${suffix}`,
    kind,
    enabled: true,
    bold: "inherit",
    horizontal_offset: 0,
    spacing_after: 0,
    ...defaults[kind],
  };
  const selectedIndex = receiptTemplate.blocks.findIndex((item) => item.id === selectedReceiptBlockId);
  receiptTemplate.blocks.splice(selectedIndex >= 0 ? selectedIndex + 1 : receiptTemplate.blocks.length, 0, block);
  selectedReceiptBlockId = block.id;
  receiptTemplateChanged();
  el(kind === "text" ? "receiptCustomText" : kind === "separator" ? "receiptSeparatorCharacter" : "receiptSpacerLines").focus();
}

function captureTextEditHistory(event) {
  if (event.target.dataset.historyCaptured === "yes") return;
  rememberReceiptTemplate();
  event.target.dataset.historyCaptured = "yes";
}

function finishTextEditHistory(event) {
  delete event.target.dataset.historyCaptured;
}

function scheduleReceiptPreview() {
  window.clearTimeout(receiptPreviewTimer);
  receiptPreviewTimer = window.setTimeout(previewReceiptTemplate, 120);
}

function receiptCellWidth(value) {
  let width = 0;
  for (const character of Array.from(String(value || ""))) {
    const code = character.codePointAt(0);
    if ((code >= 0x0300 && code <= 0x036f) || (code >= 0xfe00 && code <= 0xfe0f)) continue;
    const wide =
      code >= 0x1100 &&
      (code <= 0x115f ||
        code === 0x2329 || code === 0x232a ||
        (code >= 0x2e80 && code <= 0xa4cf) ||
        (code >= 0xac00 && code <= 0xd7a3) ||
        (code >= 0xf900 && code <= 0xfaff) ||
        (code >= 0xfe10 && code <= 0xfe6f) ||
        (code >= 0xff00 && code <= 0xff60) ||
        (code >= 0xffe0 && code <= 0xffe6) ||
        (code >= 0x1f300 && code <= 0x1faff));
    width += wide ? 2 : 1;
  }
  return width;
}

function receiptLineText(line, width) {
  if (line.type === "image") {
    return `[ 图片 · ${line.image_kind || "image"} ]`;
  }
  if (line.type === "header_meta_line") {
    const left = String(line.left_text || "");
    const right = String(line.right_text || "");
    return `${left}${" ".repeat(Math.max(1, width - receiptCellWidth(left) - receiptCellWidth(right)))}${right}`;
  }
  if (line.type === "product_line") {
    const left = `${line.qty || ""} x ${line.name || ""}`.trim();
    const right = String(line.total || "");
    return `${left}${" ".repeat(Math.max(1, width - receiptCellWidth(left) - receiptCellWidth(right)))}${right}`;
  }
  return String(line.text || "");
}

function moveReceiptBlock(blockId, delta) {
  const index = receiptTemplate.blocks.findIndex((block) => block.id === blockId);
  const target = index + delta;
  if (index < 0 || target < 0 || target >= receiptTemplate.blocks.length) return;
  rememberReceiptTemplate();
  const [moved] = receiptTemplate.blocks.splice(index, 1);
  receiptTemplate.blocks.splice(target, 0, moved);
  receiptTemplateChanged();
}

function receiptPreviewOptionText(option, fallbackQty = "") {
  if (option && typeof option === "object") {
    const name = String(option.name || option.label || option.display_name || "").trim();
    if (!name) return "";
    const qty = String(option.qty ?? option.quantity ?? fallbackQty ?? "").trim();
    const price = String(option.unit_price ?? option.price ?? option.price_extra ?? "").trim();
    const normalizedQty = qty.replace(",", ".");
    const parsedQty = Number(normalizedQty);
    const showQty = qty && (!Number.isFinite(parsedQty) || parsedQty !== 1);
    return `+ ${showQty ? `${qty} X ` : ""}${name}${price ? ` (+${price.replace(/^\+/, "")})` : ""}`;
  }
  const text = String(option || "").trim().replace(/^\+\s*/, "");
  const prefixMatch = text.match(/^(\d+(?:[.,]\d+)?)\s*[xX×]\s*(.+)$/);
  if (prefixMatch) {
    const qty = prefixMatch[1];
    const parsedQty = Number(qty.replace(",", "."));
    return `+ ${Number.isFinite(parsedQty) && parsedQty === 1 ? "" : `${qty} X `}${prefixMatch[2].trim()}`;
  }
  return `+ ${text}`;
}

function renderReceiptDiagnostics(diagnostics) {
  const problems = (diagnostics || []).filter((item) => item.overflow);
  receiptHasOverflow = problems.length > 0;
  el("receiptDiagnosticsSummary").textContent = problems.length
    ? `${problems.length} 行会被裁切；保存与打印前请修正。`
    : `检查通过，共 ${(diagnostics || []).length} 行。`;
  const list = el("receiptDiagnosticsList");
  clearNode(list);
  for (const problem of problems.slice(0, 20)) {
    const item = document.createElement("li");
    item.textContent = `第 ${problem.line} 行：${problem.used}/${problem.available} 列 · ${problem.text.slice(0, 42)}`;
    list.append(item);
  }
  el("receiptSave").disabled = receiptHasOverflow;
  el("receiptPrintPreview").disabled = receiptHasOverflow || receiptTemplateMode !== "receipt";
}

function renderReceiptPreview(lines, width, diagnostics = []) {
  const paper = el("receiptPaper");
  clearNode(paper);
  paper.style.setProperty("--paper-chars", width);
  el("receiptRuler").style.setProperty("--paper-chars", width);
  el("receiptRuler").textContent = Array.from({length: width}, (_, index) => (index + 1) % 10).join("");
  el("receiptPreviewWidth").textContent = `${width} 列 · ${printerProfile?.printable_width_dots || 576} dots`;
  for (const [lineIndex, line] of (lines || []).entries()) {
    const row = document.createElement("div");
    row.className = "receipt-preview-line";
    if (line.align === "center") row.classList.add("align-center");
    if (line.align === "right") row.classList.add("align-right");
    if (line.bold) row.classList.add("is-bold");
    if (line.double_width || line.double_height) row.classList.add("is-double");
    if (line.width_multiplier >= 2 || line.height_multiplier >= 2) row.classList.add(`is-size-${Math.max(line.width_multiplier || 1, line.height_multiplier || 1)}`);
    if (line.type === "image") row.classList.add("is-image");
    if (diagnostics.find((item) => item.line === lineIndex + 1)?.overflow) row.classList.add("is-overflow");
    if (line.type === "image" && line.image_kind === "barcode") {
      row.classList.add("is-barcode");
      const bars = document.createElement("span");
      bars.className = "receipt-barcode-bars";
      row.append(bars);
    } else if (line.type === "image" && String(line.src || "").startsWith("data:image/")) {
      const image = document.createElement("img");
      image.className = `receipt-preview-image receipt-preview-image-${line.image_kind || "generic"}`;
      image.src = line.src;
      image.alt = line.image_kind === "logo" ? "Company logo" : "Receipt image";
      if (line.image_kind === "logo") {
        image.addEventListener("load", () => {
          const printerWidthDots = 576;
          image.style.width = `${Math.min(100, (image.naturalWidth / printerWidthDots) * 100)}%`;
        }, {once: true});
      }
      row.append(image);
    } else {
      row.textContent = receiptLineText(line, width) || " ";
    }
    paper.appendChild(row);
    for (const option of line.combo_items || []) {
      const optionRow = document.createElement("div");
      optionRow.className = "receipt-preview-line is-bold";
      optionRow.textContent = `  ${receiptPreviewOptionText(option, line.qty)}`;
      paper.appendChild(optionRow);
    }
  }
  renderReceiptDiagnostics(diagnostics);
}

async function previewReceiptTemplate() {
  if (!receiptTemplate) return;
  const requested = cloneReceiptTemplate(receiptTemplate);
  requested.name = el("receiptTemplateName").value.trim() || "自定义小票";
  requested.paper_width = Number(el("receiptPaperWidth").value) || 48;
  try {
    const result = await getJSON(receiptTemplateEndpoint("/preview"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requested),
    });
    printerProfile = result.profile || printerProfile;
    const width = printerProfile
      ? Number(printerProfile[printerProfile.font === "b" ? "columns_font_b" : "columns_font_a"])
      : requested.paper_width;
    renderReceiptPreview(result.lines, width, result.diagnostics);
    setFeedback("receiptFeedback", "预览已更新。", "success");
  } catch (error) {
    setFeedback("receiptFeedback", error.message || "预览失败", "error");
  }
}

const PROFILE_FIELDS = {
  paper_width_mm: "profilePaperWidth", printable_width_dots: "profileDots",
  columns_font_a: "profileColumnsA", columns_font_b: "profileColumnsB",
  font: "profileFont", cjk_width: "profileCjkWidth",
  margin_left_dots: "profileMarginLeft", margin_right_dots: "profileMarginRight",
  feed_lines: "profileFeedLines", cut: "profileCut",
};

function profileFromForm() {
  const profile = {};
  for (const [key, id] of Object.entries(PROFILE_FIELDS)) {
    const field = el(id);
    profile[key] = field.type === "checkbox" ? field.checked : (key === "font" ? field.value : Number(field.value));
  }
  return profile;
}

function fillProfileForm(profile) {
  printerProfile = profile;
  for (const [key, id] of Object.entries(PROFILE_FIELDS)) {
    const field = el(id);
    if (field.type === "checkbox") field.checked = Boolean(profile[key]);
    else field.value = String(profile[key]);
  }
}

async function loadPrinterProfile() {
  const result = await getJSON("/api/printer-profile");
  fillProfileForm(result.profile);
}

function versionStorageKey() { return `iotbox.receiptVersions.${receiptTemplateMode}`; }
function receiptVersions() {
  try { return JSON.parse(localStorage.getItem(versionStorageKey()) || "[]"); }
  catch { return []; }
}
function refreshReceiptVersions() {
  const select = el("receiptVersions");
  clearNode(select);
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "历史版本";
  select.append(placeholder);
  for (const [index, version] of receiptVersions().entries()) {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = new Date(version.savedAt).toLocaleString("zh-CN");
    select.append(option);
  }
}
function snapshotReceiptVersion() {
  const versions = [{savedAt: Date.now(), template: cloneReceiptTemplate(receiptTemplate)}, ...receiptVersions()].slice(0, 12);
  localStorage.setItem(versionStorageKey(), JSON.stringify(versions));
  refreshReceiptVersions();
}

function confirmReceiptAction(message) {
  const dialog = el("receiptConfirmDialog");
  el("receiptConfirmMessage").textContent = message;
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {once: true}));
}

async function loadReceiptEditor() {
  try {
    if (!printerProfile) await loadPrinterProfile();
    updateReceiptModeUI();
    const result = await getJSON(receiptTemplateEndpoint());
    receiptTemplate = result.template;
    kitchenLineClasses = result.line_classes || kitchenLineClasses;
    receiptHistory = [];
    receiptFuture = [];
    el("receiptTemplateName").value = receiptTemplate.name;
    el("receiptPaperWidth").value = String(receiptTemplate.paper_width);
    selectedReceiptBlockId = receiptTemplate.blocks?.[0]?.id || null;
    selectReceiptBlock(selectedReceiptBlockId);
    renderKitchenLineSettings();
    setReceiptDirty(false);
    el("receiptUndo").disabled = true;
    el("receiptRedo").disabled = true;
    refreshReceiptVersions();
    await previewReceiptTemplate();
  } catch (error) {
    setFeedback("receiptFeedback", error.message || "无法加载模板", "error");
  }
}

el("receiptBlockEnabled").addEventListener("change", (event) => {
  updateSelectedReceiptBlock("enabled", event.target.checked);
});
el("receiptBlockAlign").addEventListener("change", (event) => {
  updateSelectedReceiptBlock("align", event.target.value);
});
el("receiptBlockBold").addEventListener("change", (event) => {
  const value = event.target.value === "inherit" ? "inherit" : event.target.value === "true";
  updateSelectedReceiptBlock("bold", value);
});
function updateReceiptHorizontalOffset(value) {
  const offset = Math.max(-12, Math.min(12, Number(value) || 0));
  el("receiptHorizontalOffsetRange").value = offset;
  el("receiptHorizontalOffset").value = offset;
  updateSelectedReceiptBlock("horizontal_offset", offset);
}
el("receiptHorizontalOffsetRange").addEventListener("change", (event) => {
  updateReceiptHorizontalOffset(event.target.value);
});
el("receiptHorizontalOffset").addEventListener("change", (event) => {
  updateReceiptHorizontalOffset(event.target.value);
});
el("receiptBlockSpacing").addEventListener("change", (event) => {
  updateSelectedReceiptBlock("spacing_after", Math.max(0, Math.min(4, Number(event.target.value) || 0)));
});
el("receiptBlockSeparatorAfter").addEventListener("change", (event) => {
  updateSelectedReceiptBlock("separator_after", Boolean(event.target.checked));
});
el("receiptBlockSeparatorAfterCharacter").addEventListener("change", (event) => {
  updateSelectedReceiptBlock("separator_after_character", event.target.value);
});
el("receiptBlockContent").addEventListener("input", (event) => {
  const block = receiptBlockById(selectedReceiptBlockId);
  if (!block) return;
  captureTextEditHistory(event);
  block.content = event.target.value;
  setReceiptDirty(true);
  scheduleReceiptPreview();
});
el("receiptBlockContent").addEventListener("blur", finishTextEditHistory);
function updateProductHeaderColumns(changedField, rawValue) {
  const block = receiptProductHeaderBlock();
  if (!block || !["product_header", "products"].includes(selectedReceiptBlockId)) return;
  rememberReceiptTemplate();
  let qty = Math.max(5, Math.min(12, Number(block.qty_columns || 6)));
  let product = Math.max(12, Math.min(32, Number(block.product_columns || 30)));
  let amount = Math.max(8, Math.min(16, Number(block.amount_columns || 10)));
  let gutter = Math.max(0, Math.min(12, Number(block.gutter_columns ?? 2)));
  if (changedField === "qty_columns") qty = Math.max(5, Math.min(12, Number(rawValue) || 6));
  if (changedField === "product_columns") product = Math.max(12, Math.min(32, Number(rawValue) || 30));
  if (changedField === "amount_columns") amount = Math.max(8, Math.min(16, Number(rawValue) || 10));
  if (changedField === "gutter_columns") gutter = Math.max(0, Math.min(12, Number(rawValue) || 0));
  gutter = Math.min(gutter, Math.max(0, 48 - qty - amount - 12));
  product = Math.min(product, 48 - qty - gutter - amount);
  block.qty_columns = qty;
  block.product_columns = product;
  block.amount_columns = amount;
  block.gutter_columns = gutter;
  receiptTemplateChanged();
}
el("receiptQtyColumns").addEventListener("change", (event) => {
  updateProductHeaderColumns("qty_columns", event.target.value);
});
el("receiptAmountColumns").addEventListener("change", (event) => {
  updateProductHeaderColumns("amount_columns", event.target.value);
});
el("receiptProductColumns").addEventListener("change", (event) => {
  updateProductHeaderColumns("product_columns", event.target.value);
});
el("receiptColumnGutter").addEventListener("change", (event) => {
  updateProductHeaderColumns("gutter_columns", event.target.value);
});
for (const [elementId, field] of [
  ["receiptQtyLabel", "qty_label"],
  ["receiptProductLabel", "product_label"],
  ["receiptAmountLabel", "amount_label"],
]) {
  el(elementId).addEventListener("input", (event) => {
    const block = receiptProductHeaderBlock();
    if (!block || !["product_header", "products"].includes(selectedReceiptBlockId)) return;
    captureTextEditHistory(event);
    block[field] = event.target.value;
    setReceiptDirty(true);
    scheduleReceiptPreview();
  });
  el(elementId).addEventListener("blur", finishTextEditHistory);
}
el("receiptCustomText").addEventListener("input", (event) => {
  const block = receiptBlockById(selectedReceiptBlockId);
  if (!block) return;
  captureTextEditHistory(event);
  block.text = event.target.value;
  block.label = event.target.value.split("\n").find((line) => line.trim())?.trim().slice(0, 24) || "自定义文字";
  setReceiptDirty(true);
  renderReceiptBlockList();
  scheduleReceiptPreview();
});
el("receiptCustomText").addEventListener("blur", finishTextEditHistory);
el("receiptSeparatorCharacter").addEventListener("change", (event) => {
  updateSelectedReceiptBlock("character", event.target.value);
});
el("receiptSpacerLines").addEventListener("change", (event) => {
  updateSelectedReceiptBlock("lines", Math.max(1, Math.min(6, Number(event.target.value) || 1)));
});
el("receiptDoubleSize").addEventListener("change", (event) => {
  updateSelectedReceiptBlock("double_size", event.target.checked);
});
el("receiptFontSize").addEventListener("change", (event) => {
  updateSelectedReceiptBlock("font_size", Math.max(1, Math.min(5, Number(event.target.value) || 1)));
});
el("receiptAddText").addEventListener("click", () => addReceiptBlock("text"));
el("receiptAddSeparator").addEventListener("click", () => addReceiptBlock("separator"));
el("receiptAddSpacer").addEventListener("click", () => addReceiptBlock("spacer"));
el("receiptDeleteBlock").addEventListener("click", () => {
  const index = receiptTemplate.blocks.findIndex((block) => block.id === selectedReceiptBlockId);
  if (index < 0 || receiptTemplate.blocks[index].kind === "builtin") return;
  rememberReceiptTemplate();
  receiptTemplate.blocks.splice(index, 1);
  selectedReceiptBlockId = receiptTemplate.blocks[Math.min(index, receiptTemplate.blocks.length - 1)]?.id || null;
  receiptTemplateChanged();
});
el("receiptTemplateName").addEventListener("change", () => {
  const value = el("receiptTemplateName").value.trim() || "自定义小票";
  if (receiptTemplate.name === value) return;
  rememberReceiptTemplate();
  receiptTemplate.name = value;
  receiptTemplateChanged();
});
el("receiptPaperWidth").addEventListener("change", () => {
  const value = Number(el("receiptPaperWidth").value) || 48;
  if (receiptTemplate.paper_width === value) return;
  rememberReceiptTemplate();
  receiptTemplate.paper_width = value;
  receiptTemplateChanged();
});
el("receiptUndo").addEventListener("click", () => {
  const previous = receiptHistory.pop();
  if (!previous) return;
  receiptFuture.push(cloneReceiptTemplate(receiptTemplate));
  receiptTemplate = previous;
  el("receiptTemplateName").value = receiptTemplate.name;
  el("receiptPaperWidth").value = String(receiptTemplate.paper_width);
  receiptTemplateChanged();
  el("receiptUndo").disabled = receiptHistory.length === 0;
  el("receiptRedo").disabled = receiptFuture.length === 0;
});
el("receiptRedo").addEventListener("click", () => {
  const next = receiptFuture.pop();
  if (!next) return;
  receiptHistory.push(cloneReceiptTemplate(receiptTemplate));
  receiptTemplate = next;
  el("receiptTemplateName").value = receiptTemplate.name;
  receiptTemplateChanged();
  el("receiptRedo").disabled = receiptFuture.length === 0;
});
el("receiptSave").addEventListener("click", async () => {
  if (receiptHasOverflow) return;
  receiptTemplate.name = el("receiptTemplateName").value.trim() || "自定义小票";
  receiptTemplate.paper_width = Number(el("receiptPaperWidth").value) || 48;
  setFeedback("receiptFeedback", "正在保存模板…", "warn");
  try {
    const result = await getJSON(receiptTemplateEndpoint(), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(receiptTemplate),
    });
    receiptTemplate = result.template;
    snapshotReceiptVersion();
    receiptHistory = [];
    el("receiptUndo").disabled = true;
    setReceiptDirty(false);
    const target = receiptTemplateMode === "kitchen" ? "厨房单" : "顾客小票";
    setFeedback("receiptFeedback", `模板已保存，下一张${target}会使用新布局。`, "success");
  } catch (error) {
    setFeedback("receiptFeedback", error.message || "保存失败", "error");
  }
});
el("receiptReset").addEventListener("click", async () => {
  const target = receiptTemplateMode === "kitchen" ? "厨房单" : "顾客小票";
  if (!await confirmReceiptAction(`恢复默认${target}布局？当前已保存的模板会被替换。`)) return;
  rememberReceiptTemplate();
  try {
    const result = await getJSON(receiptTemplateEndpoint(), { method: "DELETE" });
    receiptTemplate = result.template;
    el("receiptTemplateName").value = receiptTemplate.name;
    el("receiptPaperWidth").value = String(receiptTemplate.paper_width);
    selectedReceiptBlockId = receiptTemplate.blocks?.[0]?.id || null;
    selectReceiptBlock(selectedReceiptBlockId);
    setReceiptDirty(false);
    await previewReceiptTemplate();
    setFeedback("receiptFeedback", "已恢复默认模板。", "success");
  } catch (error) {
    setFeedback("receiptFeedback", error.message || "恢复失败", "error");
  }
});

for (const button of document.querySelectorAll("[data-receipt-mode]")) {
  button.addEventListener("click", async () => {
    const nextMode = button.dataset.receiptMode;
    if (!nextMode || nextMode === receiptTemplateMode) return;
    if (receiptTemplateDirty && !await confirmReceiptAction("当前模板有未保存修改，切换后将丢失。继续切换？")) return;
    receiptTemplateMode = nextMode;
    receiptTemplate = null;
    selectedReceiptBlockId = null;
    receiptHistory = [];
    await loadReceiptEditor();
  });
}

el("receiptBlockSearch").addEventListener("input", renderReceiptBlockList);
el("profileSave").addEventListener("click", async () => {
  setFeedback("profileFeedback", "正在保存 Profile…", "warn");
  try {
    const result = await getJSON("/api/printer-profile", {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(profileFromForm())});
    fillProfileForm(result.profile);
    setFeedback("profileFeedback", `Profile 已保存，当前 ${result.active_columns} 列。`, "success");
    await previewReceiptTemplate();
  } catch (error) { setFeedback("profileFeedback", error.message || "保存失败", "error"); }
});
el("profilePreviewCalibration").addEventListener("click", async () => {
  try {
    const result = await getJSON("/api/printer-profile/calibration/preview", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(profileFromForm())});
    fillProfileForm(result.profile);
    renderReceiptPreview(result.lines, result.profile[result.profile.font === "b" ? "columns_font_b" : "columns_font_a"], result.diagnostics);
    setFeedback("profileFeedback", "正在预览校准票；未触发打印。", "success");
  } catch (error) { setFeedback("profileFeedback", error.message || "校准预览失败", "error"); }
});
el("profilePrintCalibration").addEventListener("click", async () => {
  if (!await confirmReceiptAction("将向当前打印机发送一张 RAW ESC/POS 校准票，并可能切纸。继续吗？")) return;
  try {
    await getJSON("/api/printer-profile/calibration/print", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(profileFromForm())});
    setFeedback("profileFeedback", "校准票已提交到打印队列。", "success");
  } catch (error) { setFeedback("profileFeedback", error.message || "打印失败", "error"); }
});
el("receiptPrintPreview").addEventListener("click", async () => {
  if (receiptHasOverflow || receiptTemplateMode !== "receipt") return;
  if (!await confirmReceiptAction("将使用当前未保存的布局打印示例小票。继续吗？")) return;
  try {
    await getJSON("/api/receipt-template/print-preview", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(receiptTemplate)});
    setFeedback("receiptFeedback", "当前预览已提交到打印队列。", "success");
  } catch (error) { setFeedback("receiptFeedback", error.message || "打印失败", "error"); }
});
el("receiptExport").addEventListener("click", () => {
  const blob = new Blob([JSON.stringify(receiptTemplate, null, 2)], {type: "application/json"});
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `${receiptTemplateMode}-template.json`;
  link.click();
  URL.revokeObjectURL(link.href);
});
el("receiptImport").addEventListener("click", () => el("receiptImportFile").click());
el("receiptImportFile").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    rememberReceiptTemplate();
    receiptTemplate = JSON.parse(await file.text());
    selectedReceiptBlockId = receiptTemplate.blocks?.[0]?.id || null;
    receiptTemplateChanged();
    setFeedback("receiptFeedback", "模板已导入，请检查预览后保存。", "success");
  } catch { setFeedback("receiptFeedback", "导入失败：文件不是有效模板 JSON。", "error"); }
  event.target.value = "";
});
el("receiptRestoreVersion").addEventListener("click", () => {
  const version = receiptVersions()[Number(el("receiptVersions").value)];
  if (!version) return;
  rememberReceiptTemplate();
  receiptTemplate = cloneReceiptTemplate(version.template);
  selectedReceiptBlockId = receiptTemplate.blocks?.[0]?.id || null;
  receiptTemplateChanged();
});

loadReceiptEditor();
