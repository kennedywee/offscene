const $ = (id) => document.getElementById(id);
let state = null;
let pending = false;
let imageURL = null;
let settingsQueue = Promise.resolve();
let uiError = null;
let backgroundPicker = false;
let disconnected = false;
let previewSocket = null;
let previewFrames = 0;
let previewPeriod = performance.now();

async function api(path, options = {}) {
  const response = await fetch(`/api/${path}`, {
    signal: AbortSignal.timeout(15000),
    ...options,
    headers: {
      "X-Offscene": "1",
      ...(options.body && !(options.body instanceof FormData)
        ? { "Content-Type": "application/json" }
        : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : `Request failed (${response.status}).`,
    );
  }
  return response;
}

function showError(error) {
  uiError = error.message || String(error);
  $("error").textContent = uiError;
  $("error").hidden = false;
}

function updateFrameRates(resolution, fps) {
  const rates = ["2560x1440", "3840x2160"].includes(resolution)
    ? [30]
    : [30, 60];
  const dropdown = dropdowns.get("fps-select");
  if (dropdown.options.length !== rates.length) {
    dropdown.setOptions(
      rates.map((rate) => ({ value: String(rate), label: `${rate} fps` })),
    );
  }
  dropdown.setValue(rates.includes(Number(fps)) ? fps : 30);
}

function render(next, sync = false) {
  state = next;
  const running = next.phase === "running";
  const active = ["loading", "running", "stopping"].includes(next.phase);
  $("phase").textContent = {
    stopped: "Camera off",
    loading: "Loading GPU model…",
    running: "Camera live",
    stopping: "Stopping camera…",
    error: "Camera error",
  }[next.phase];
  $("live-dot").classList.toggle("live", running);
  $("start").textContent = active
    ? next.phase === "stopping"
      ? "Stopping…"
      : "Stop camera"
    : "Start camera";
  $("start").classList.toggle("running", active);
  $("start").disabled = pending || next.phase === "stopping";
  document
    .querySelectorAll("[data-capture]")
    .forEach((el) => (el.disabled = active));
  $("refresh").disabled = active;
  if (active) dropdowns.forEach((dropdown) => {
    if (dropdown.trigger.disabled) dropdown.close();
  });
  $("fps").textContent = running ? next.fps.toFixed(1) : "—";
  $("capture-fps").textContent = running ? next.capture_fps.toFixed(1) : "—";
  $("latency").textContent = running ? next.processing_ms.toFixed(1) : "—";
  if (!running) $("preview-fps").textContent = "—";
  $("capture-status").textContent =
    next.capture_warning ||
    (running && next.capture_fps > 0 && next.capture_fps < next.settings.fps * 0.8
      ? `Capture below ${next.settings.fps} fps. Check exposure or lighting.`
      : "");
  $("capture-status").hidden = !$("capture-status").textContent;
  $("resolution-label").hidden = !running;
  $("resolution-label").textContent = running
    ? `${next.resolution} · ${next.settings.fps} fps requested`
    : "Preview";
  if (next.gpu) $("gpu").textContent = next.gpu;
  syncCameraControls(next);
  $("error").textContent = next.error || uiError || "";
  $("error").hidden = !next.error && !uiError;
  $("warning").textContent = next.warning || "";
  $("warning").hidden = !next.warning;
  $("output-status").textContent = next.virtual_device
    ? `Sending to ${next.virtual_device}. Select Offscene in your meeting app.`
    : next.settings.virtual_camera
      ? "Output requested. The virtual device opens after processing starts."
      : "Enable to send the processed video to your meeting or streaming app.";
  $("empty-title").textContent =
    next.phase === "loading"
      ? "Preparing your GPU"
      : next.phase === "error"
        ? "Let’s reconnect your camera"
        : "Your camera is off";
  $("empty-copy").textContent =
    next.phase === "loading"
      ? "The first start downloads the model and warms up CUDA. This can take a moment."
      : "Choose your background, then start your camera. Everything is processed locally on your GPU.";
  if (!running) {
    hideFrame();
    $("preview-status").textContent =
      "Preview uses the full selected output resolution. Mirror preview changes only your view.";
  } else connectPreview();
  if (sync) {
    const s = next.settings;
    updateFrameRates(s.resolution, s.fps);
    for (const [id, key] of Object.entries({
      camera: "camera",
      resolution: "resolution",
      model: "model",
      exposure: "exposure",
      color: "color",
      blur: "blur",
      "output-device": "output_device",
    }))
      if (dropdowns.has(id)) dropdowns.get(id).setValue(s[key]);
      else $(id).value = s[key];
    $("cleanup").value = Math.round(s.cleanup * 100);
    $("shrink").value = s.shrink;
    $("virtual-camera").checked = s.virtual_camera;
    $("blur-value").textContent = `${s.blur}%`;
    $("cleanup-value").textContent = `${Math.round(s.cleanup * 100)}%`;
    $("shrink-value").textContent = `${s.shrink} px`;
  }
  document.querySelectorAll('input[name="quality"]').forEach((input) => {
    input.checked = input.value === next.settings.quality;
  });
  document
    .querySelectorAll("[data-view]")
    .forEach((el) =>
      el.setAttribute(
        "aria-pressed",
        el.dataset.view === next.settings.preview,
      ),
    );
  document
    .querySelectorAll("[data-effect]")
    .forEach((el) =>
      el.setAttribute(
        "aria-pressed",
        el.dataset.effect ===
          (backgroundPicker ? "image" : next.settings.effect),
      ),
    );
  for (const effect of ["blur", "color", "image"])
    $(`${effect}-control`).hidden =
      effect !== (backgroundPicker ? "image" : next.settings.effect);
  $("preview-label").textContent = {
    output: "PROCESSED OUTPUT",
    original: "ORIGINAL · PREVIEW ONLY",
    matte: "ALPHA MATTE · PREVIEW ONLY",
  }[next.settings.preview];
}

function patch(values) {
  settingsQueue = settingsQueue.then(async () => {
    try {
      const next = await (
        await api("settings", { method: "PATCH", body: JSON.stringify(values) })
      ).json();
      uiError = null;
      render(next);
    } catch (error) {
      showError(error);
      if (state) render(state, true);
    }
  });
  return settingsQueue;
}

function selectPanel(name) {
  dropdowns.forEach((dropdown) => dropdown.close());
  document.querySelectorAll("[data-panel]").forEach((tab) => {
    const selected = tab.dataset.panel === name;
    tab.setAttribute("aria-selected", selected);
    tab.tabIndex = selected ? 0 : -1;
    $(`${tab.dataset.panel}-panel`).hidden = !selected;
  });
  document.querySelector(".controls").scrollTop = 0;
}

const panelTabs = [...document.querySelectorAll("[data-panel]")];
panelTabs.forEach((tab, index) => {
  tab.addEventListener("click", () => selectPanel(tab.dataset.panel));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next =
      event.key === "Home"
        ? panelTabs[0]
        : event.key === "End"
          ? panelTabs.at(-1)
          : panelTabs[
              (index + (event.key === "ArrowLeft" ? -1 : 1) + panelTabs.length) %
                panelTabs.length
            ];
    selectPanel(next.dataset.panel);
    next.focus();
  });
});

async function refreshDevices() {
  const list = await (await api("devices")).json();
  const physical = list.filter((device) => !device.virtual);
  dropdowns.get("camera").setOptions(
    physical.length
      ? physical.map((device) => ({
          label: `${device.name} · ${device.path}`,
          value: device.path,
        }))
      : [{ label: "No webcam detected", value: "" }],
  );
  if (state && physical.some((device) => device.path === state.settings.camera))
    dropdowns.get("camera").setValue(state.settings.camera);
  else if (physical.length) await patch({ camera: physical[0].path });
}

$("start").addEventListener("click", async () => {
  pending = true;
  uiError = null;
  render(state);
  try {
    await settingsQueue;
    const action = ["loading", "running"].includes(state.phase)
      ? "stop"
      : "start";
    render(await (await api(action, { method: "POST" })).json());
  } catch (error) {
    showError(error);
  } finally {
    pending = false;
    render(state);
  }
});
$("refresh").addEventListener("click", () => refreshDevices().catch(showError));
$("resolution").addEventListener("change", (event) => {
  const resolution = event.target.value;
  updateFrameRates(resolution, $("fps-select").value);
  // Submit the pair together so switching from 60 fps to 4K is always valid.
  patch({ resolution, fps: Number($("fps-select").value) });
});
for (const [id, key] of Object.entries({
  camera: "camera",
  "fps-select": "fps",
  model: "model",
  color: "color",
  "output-device": "output_device",
})) {
  $(id).addEventListener("change", (event) =>
    patch({
      [key]: key === "fps" ? Number(event.target.value) : event.target.value,
    }),
  );
}
document.querySelectorAll('input[name="quality"]').forEach((input) => {
  input.addEventListener("change", () => {
    if (input.checked) patch({ quality: input.value });
  });
});
for (const id of ["blur", "cleanup", "shrink"]) {
  $(id).addEventListener(
    "input",
    () =>
      ($(`${id}-value`).textContent =
        `${$(id).value}${id === "shrink" ? " px" : "%"}`),
  );
  $(id).addEventListener("change", () =>
    patch({ [id]: Number($(id).value) / (id === "cleanup" ? 100 : 1) }),
  );
}
document
  .querySelectorAll("[data-view]")
  .forEach((button) =>
    button.addEventListener("click", () =>
      patch({ preview: button.dataset.view }),
    ),
  );
document.querySelectorAll("[data-effect]").forEach((button) =>
  button.addEventListener("click", () => {
    selectPanel("background");
    backgroundPicker =
      button.dataset.effect === "image" && !state.has_background;
    if (backgroundPicker) {
      render(state);
      $("background-file").focus();
    } else patch({ effect: button.dataset.effect });
  }),
);
$("virtual-camera").addEventListener("change", (event) =>
  patch({ virtual_camera: event.target.checked }),
);
$("mirror").addEventListener("change", () =>
  $("preview-image").classList.toggle("mirrored", $("mirror").checked),
);

$("background-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  $("upload-help").textContent = "Loading background…";
  try {
    const data = new FormData();
    data.append("file", file);
    const next = await (
      await api("background", { method: "POST", body: data })
    ).json();
    backgroundPicker = false;
    uiError = null;
    render(next);
    $("upload-help").textContent = file.name;
  } catch (error) {
    showError(error);
    $("upload-help").textContent = "Upload failed. Choose another image.";
  }
  event.target.value = "";
});

function hideFrame() {
  if (previewSocket) {
    const socket = previewSocket;
    previewSocket = null;
    socket.close();
  }
  $("preview-image").hidden = true;
  $("preview-label").hidden = true;
  $("empty").hidden = false;
  $("preview-image").src = "/static/camera-off.svg";
  if (imageURL) {
    URL.revokeObjectURL(imageURL);
    imageURL = null;
  }
}

function connectPreview() {
  if (previewSocket || document.hidden || state?.phase !== "running") return;
  const socket = new WebSocket(
    `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/preview`,
  );
  previewSocket = socket;
  previewFrames = 0;
  previewPeriod = performance.now();
  const decodeFrame = async (event) => {
    if (previewSocket !== socket || state?.phase !== "running") return;
    // Fetch the next frame while this one decodes. Acknowledge only when a
    // decode starts, bounding the queue to one decoding frame and one waiting.
    if (socket.readyState === WebSocket.OPEN) socket.send("next");
    const url = URL.createObjectURL(event.data);
    try {
      const decoded = new Image();
      decoded.src = url;
      await decoded.decode();
      if (previewSocket !== socket || state?.phase !== "running") {
        URL.revokeObjectURL(url);
        return;
      }
      const previous = imageURL;
      imageURL = url;
      $("preview-image").src = url;
      $("preview-image").hidden = false;
      $("preview-label").hidden = false;
      $("empty").hidden = true;
      if (previous) URL.revokeObjectURL(previous);
      previewFrames++;
      const now = performance.now();
      if (now - previewPeriod >= 1000) {
        $("preview-fps").textContent = (
          (previewFrames * 1000) /
          (now - previewPeriod)
        ).toFixed(1);
        previewFrames = 0;
        previewPeriod = now;
      }
      $("preview-status").textContent =
        `Live preview: ${decoded.naturalWidth} × ${decoded.naturalHeight}. Virtual output: ${state.resolution}.`;
    } catch {
      URL.revokeObjectURL(url);
      socket.close();
    }
  };
  let decoding = Promise.resolve();
  socket.onmessage = (event) => {
    decoding = decoding.then(() => decodeFrame(event));
  };
  socket.onclose = () => {
    if (previewSocket === socket) {
      previewSocket = null;
      hideFrame();
      $("preview-fps").textContent = "—";
      $("preview-status").textContent = "Preview disconnected. Reconnecting…";
    }
  };
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) hideFrame();
  else connectPreview();
});

async function statusLoop() {
  try {
    const next = await (await api("status")).json();
    if (disconnected) uiError = null;
    disconnected = false;
    const first = !state;
    render(next, first);
    if (first) await refreshDevices();
  } catch (error) {
    disconnected = true;
    showError(
      new Error(
        "Disconnected from Offscene. Check that the local server is running.",
      ),
    );
    hideFrame();
    $("phase").textContent = "Disconnected";
    $("start").disabled = true;
    $("live-dot").classList.remove("live");
    $("fps").textContent =
      $("capture-fps").textContent =
      $("latency").textContent =
      $("preview-fps").textContent =
        "—";
  }
  setTimeout(statusLoop, 1000);
}

const setupPopover = $("setup-popover");
setupPopover.addEventListener("beforetoggle", (event) => {
  if (event.newState !== "open") return;
  const trigger = $("setup-help").getBoundingClientRect();
  const width = Math.min(380, innerWidth - 32);
  setupPopover.style.left = `${Math.max(16, Math.min(trigger.right - width, innerWidth - width - 16))}px`;
  setupPopover.style.bottom = `${Math.max(16, innerHeight - trigger.top + 8)}px`;
  setupPopover.style.maxHeight = `${Math.max(120, trigger.top - 24)}px`;
});
function closeSetupPopover() {
  if (setupPopover.matches(":popover-open")) setupPopover.hidePopover();
}
window.addEventListener("resize", closeSetupPopover);
document.addEventListener(
  "scroll",
  (event) => {
    if (event.target !== setupPopover) closeSetupPopover();
  },
  true,
);

async function init() {
  try {
    state = await (await api("status")).json();
    await refreshDevices();
    render(state, true);
  } catch (error) {
    showError(error);
  }
  statusLoop();
  connectPreview();
}
init();
