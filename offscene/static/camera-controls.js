// Hardware ranges and current values come from the selected V4L2 camera.
const cameraFields = new Map();
const cameraGroups = new Map();
let cameraControlData = {};
let cameraControlKey = "";
let cameraControlsLoading = false;
let cameraControlsBusy = false;
let cameraControlRevision = 0;
let cameraPresets = [];

async function loadCameraPresets() {
  try {
    cameraPresets = await (await api("camera-presets")).json();
    $("camera-presets").replaceChildren(...cameraPresets.map((preset) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = preset.label;
      button.dataset.cameraPreset = preset.id;
      const check = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      check.setAttribute("viewBox", "0 0 16 16");
      check.setAttribute("aria-hidden", "true");
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", "m3 8 3.5 3.5L13 4.5");
      check.append(path);
      button.append(check);
      button.addEventListener("click", () =>
        changeCameraControls({}, undefined, { preset: preset.id }),
      );
      return button;
    }));
    $("camera-look-help").replaceChildren(...cameraPresets.flatMap((preset) => {
      const name = document.createElement("dt");
      const description = document.createElement("dd");
      name.textContent = preset.label;
      description.textContent = preset.description;
      return [name, description];
    }));
    $("camera-preset-description").className = "sr-only";
    updateCameraControlState();
  } catch (error) {
    $("camera-preset-description").textContent = error.message;
    $("camera-preset-description").className = "help";
  }
}

function controlScale(name) {
  if (name === "digital_zoom") return 100;
  return name === "exposure_time_absolute" ? 10 : 1;
}

function changeCameraValue(name, value) {
  return name === "digital_zoom"
    ? patch({ zoom: value / 100 })
    : changeCameraControls({ [name]: value });
}

function drawCameraControls(data) {
  cameraGroups.set("Exposure", $("camera-exposure-group"));
  data.controls = {
    ...data.controls,
    digital_zoom: {
      label: "Digital zoom", group: "Lens", type: "range",
      min: 100, max: 200, step: 1, default: 100,
      value: Math.round((state.settings.zoom || 1) * 100),
    },
  };
  cameraControlData = data.controls;
  for (const [name, control] of Object.entries(data.controls)) {
    if (name === "auto_exposure" || name === "zoom_absolute") continue;
    if (!cameraGroups.has(control.group)) {
      const group = document.createElement("fieldset");
      group.className = "camera-group";
      const legend = document.createElement("legend");
      legend.textContent = control.group;
      group.append(legend);
      $("camera-controls").append(group);
      cameraGroups.set(control.group, group);
    }
    let field = cameraFields.get(name);
    if (!field) {
      const row = document.createElement("div");
      row.className = "camera-control";
      const id = `camera-${name}`;
      const label = document.createElement("label");
      label.id = `${id}-label`;
      label.htmlFor = id;
      label.textContent =
        control.label +
        (name === "exposure_time_absolute" ? " · ms" : "");
      row.append(label);
      let input, number, dropdown;
      if (control.type === "toggle") {
        row.classList.add("camera-toggle");
        const toggle = document.createElement("label");
        toggle.className = "switch";
        input = document.createElement("input");
        input.type = "checkbox";
        input.setAttribute("role", "switch");
        toggle.append(input, document.createElement("span"));
        row.append(toggle);
      } else if (control.type === "menu") {
        input = document.createElement("button");
        input.type = "button";
        input.className = "dropdown-trigger";
        input.setAttribute("role", "combobox");
        input.setAttribute("aria-haspopup", "listbox");
        input.setAttribute("aria-expanded", "false");
        input.setAttribute("aria-controls", `${id}-options`);
        input.innerHTML =
          '<span class="dropdown-value"></span><svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg>';
        const menu = document.createElement("div");
        menu.id = `${id}-options`;
        menu.className = "dropdown-menu";
        menu.setAttribute("role", "listbox");
        menu.setAttribute("aria-labelledby", label.id);
        menu.setAttribute("popover", "auto");
        row.append(input, menu);
      } else {
        row.classList.add("camera-range");
        input = document.createElement("input");
        input.type = "range";
        number = document.createElement("input");
        number.type = "number";
        number.setAttribute("aria-labelledby", label.id);
        number.addEventListener("change", () => {
          if (number.reportValidity() && number.value !== "")
            changeCameraValue(
              name, Math.round(Number(number.value) * controlScale(name)),
            );
        });
        input.addEventListener("input", () => {
          number.value = Number(input.value) / controlScale(name);
        });
        row.append(number, input);
        if (name === "exposure_time_absolute") row.append($("shutter-warning"));
      }
      input.id = id;
      input.setAttribute("aria-labelledby", label.id);
      input.addEventListener("change", () =>
        changeCameraValue(
          name,
          control.type === "toggle" ? Number(input.checked) : Number(input.value),
        ),
      );
      cameraGroups.get(control.group).append(row);
      if (control.type === "menu") {
        dropdown = new Dropdown(input);
        dropdowns.set(id, dropdown);
      }
      field = { row, input, number, dropdown };
      cameraFields.set(name, field);
    }
    const { input, number, dropdown } = field;
    if (dropdown) {
      const signature = JSON.stringify(control.options);
      if (field.options !== signature) {
        dropdown.setOptions(control.options);
        field.options = signature;
      }
      dropdown.setValue(control.value);
    } else if (control.type === "toggle")
      input.checked = Boolean(control.value);
    else {
      for (const [key, value] of Object.entries({
        min: control.min,
        max: control.max,
        step: control.step,
      })) {
        input[key] = value;
        number[key] = value / controlScale(name);
      }
      if (
        document.activeElement !== input &&
        document.activeElement !== number
      ) {
        input.value = control.value;
        number.value = control.value / controlScale(name);
      }
    }
  }
  updateCameraControlState();
}

function updateCameraControlState() {
  const running = state?.phase === "running";
  document.querySelectorAll("[data-camera-preset]").forEach((button) => {
    button.disabled = !running || cameraControlsBusy ||
      !Object.values(cameraControlData).some((c) => c.group === "Image" && !c.readonly && !c.inactive);
    button.setAttribute("aria-pressed", String(button.dataset.cameraPreset === state?.settings.camera_preset));
  });
  $("camera-preset-undo").hidden = !state?.can_undo_camera_preset;
  $("camera-preset-undo").disabled = !running || cameraControlsBusy;
  $("camera-custom").hidden = state?.settings.camera_preset !== "custom";
  $("camera-presets").setAttribute("aria-busy", String(cameraControlsBusy));
  if (cameraPresets.length && !cameraControlsBusy) {
    const selected = cameraPresets.find((p) => p.id === state?.settings.camera_preset);
    $("camera-preset-description").textContent = selected
      ? `${selected.label} selected.`
      : "Custom settings.";
  }
  $("camera-reset").disabled =
    !running || cameraControlsBusy ||
    !Object.values(cameraControlData).some((control) => !control.readonly);
  for (const [name, field] of cameraFields) {
    const control = cameraControlData[name];
    field.row.hidden = !control;
    const disabled =
      !running ||
      cameraControlsBusy ||
      !control ||
      control.readonly ||
      control.inactive ||
      (name === "exposure_time_absolute" &&
        state.settings.exposure !== "manual");
    field.input.disabled = Boolean(disabled);
    if (field.number) field.number.disabled = Boolean(disabled);
    field.row.classList.toggle("unavailable", Boolean(disabled));
  }
  for (const group of cameraGroups.values())
    group.hidden = ![...group.querySelectorAll(".camera-control")].some(
      (row) => !row.hidden,
    );
  const shutter = cameraControlData.exposure_time_absolute?.value;
  $("shutter-warning").hidden = !(
    running &&
    state.settings.exposure === "manual" &&
    shutter > 10000 / state.settings.fps
  );
  $("shutter-warning").textContent =
    `This shutter is longer than one frame (${(1000 / (state?.settings.fps || 30)).toFixed(1)} ms). Capture may fall below the selected frame rate.`;
  $("exposure").disabled =
    cameraControlsBusy || ["loading", "stopping"].includes(state?.phase);
}

async function loadCameraControls() {
  if (cameraControlsLoading || cameraControlsBusy || !state) return;
  cameraControlsLoading = true;
  const device = state.settings.camera;
  const revision = cameraControlRevision;
  try {
    const data = await (await api("camera-controls")).json();
    if (
      data.device !== state.settings.camera ||
      device !== data.device ||
      revision !== cameraControlRevision
    )
      return;
    drawCameraControls(data);
    $("camera-controls-status").textContent = Object.keys(data.controls).length
      ? state.phase === "running"
        ? ""
        : "Start camera to adjust."
      : "This webcam does not expose adjustable camera controls.";
    $("camera-controls-status").hidden = !$("camera-controls-status").textContent;
  } catch (error) {
    cameraControlData = {};
    updateCameraControlState();
    $("camera-controls-status").textContent = error.message;
    $("camera-controls-status").hidden = false;
  } finally {
    cameraControlsLoading = false;
  }
}

function syncCameraControls(next) {
  updateCameraControlState();
  const key = `${next.settings.camera}:${next.phase}`;
  if (key !== cameraControlKey) {
    cameraControlKey = key;
    loadCameraControls();
  }
}

function changeCameraControls(values, exposure, { reset = false, preset = null } = {}) {
  cameraControlRevision++;
  cameraControlsBusy = true;
  updateCameraControlState();
  if (reset) $("camera-reset").textContent = "Resetting…";
  if (preset) $("camera-preset-description").textContent = preset === "undo" ? "Restoring previous look…" : "Applying camera look…";
  settingsQueue = settingsQueue.then(async () => {
    try {
      const data = await (
        await api(preset ? `camera-presets/${preset}` : reset ? "camera-controls/reset" : "camera-controls", {
          method: reset || preset ? "POST" : "PATCH",
          ...(reset || preset ? {} : {
            body: JSON.stringify({ values, ...(exposure ? { exposure } : {}) }),
          }),
        })
      ).json();
      uiError = null;
      render(data.state);
      dropdowns.get("exposure").setValue(data.state.settings.exposure);
      drawCameraControls(data);
    } catch (error) {
      showError(error);
      dropdowns.get("exposure").setValue(state.settings.exposure);
    } finally {
      cameraControlsBusy = false;
      $("camera-reset").textContent = "Reset";
      await loadCameraControls();
      updateCameraControlState();
    }
  });
  return settingsQueue;
}

document.addEventListener("DOMContentLoaded", () => {
  loadCameraPresets();
  $("camera-preset-undo").addEventListener("click", () =>
    changeCameraControls({}, undefined, { preset: "undo" }),
  );
  $("camera-reset").addEventListener("click", () =>
    changeCameraControls({}, undefined, { reset: true }),
  );
  $("exposure").addEventListener("change", (event) => {
    if (state?.phase === "running")
      changeCameraControls({}, event.target.value);
    else patch({ exposure: event.target.value });
  });
  $("refresh").addEventListener("click", () => {
    loadCameraControls();
    if (!cameraPresets.length) loadCameraPresets();
  });
  setInterval(() => {
    if (!document.hidden && !$("camera-panel").hidden) loadCameraControls();
  }, 2000);
});
