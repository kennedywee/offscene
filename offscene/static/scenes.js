// Saved scenes: select for review, apply only while stopped, never auto-save.
let selectedScene = "";
let sceneBusy = false;
let sceneSignature = null;
let sceneStatusTimer = 0;
let dialogReturn = null;
let dialogSubmit = null;
let sceneMenuFocusLast = false;

const RESOLUTION_LABELS = {
  "1280x720": "720p",
  "1920x1080": "1080p",
  "2560x1440": "1440p",
  "3840x2160": "4K",
};
const MODEL_LABELS = { resnet50: "ResNet50", mobilenetv3: "MobileNetV3" };

function sceneItems() {
  return state?.scenes?.items || [];
}

function sceneById(id) {
  return sceneItems().find((scene) => scene.id === id);
}

function sceneSummary({ settings: s }) {
  const camera = cameraNames.has(s.camera)
    ? cameraNames.get(s.camera)
    : `${s.camera} · unavailable`;
  const background =
    s.effect === "blur"
      ? `Blur ${s.blur}%`
      : s.effect === "color"
        ? `Color ${String(s.color).toUpperCase()}`
        : "Image";
  return [
    camera,
    RESOLUTION_LABELS[s.resolution] || s.resolution,
    `${s.fps} fps requested`,
    background,
    MODEL_LABELS[s.model] || s.model,
  ].join(" · ");
}

function renderScenes(next) {
  const scenes = next.scenes;
  const items = scenes.items;
  if (!items.some((scene) => scene.id === selectedScene))
    selectedScene = items.some((scene) => scene.id === scenes.applied)
      ? scenes.applied
      : items[0]?.id || "";
  const dropdown = dropdowns.get("scene");
  const signature = JSON.stringify(items.map(({ id, name }) => [id, name]));
  if (signature !== sceneSignature) {
    sceneSignature = signature;
    dropdown.setOptions(
      items.length
        ? items.map(({ id, name }) => ({ value: id, label: name }))
        : [{ value: "", label: "No saved scenes" }],
    );
  }
  dropdown.setValue(selectedScene);
  const scene = sceneById(selectedScene);
  const active = ["loading", "running", "stopping"].includes(next.phase);
  const locked = Boolean(scenes.error);
  $("scene-apply").disabled = sceneBusy || locked || !scene || active;
  $("scene-save").disabled = sceneBusy || locked;
  $("scene-more").disabled = sceneBusy || locked || !scene;
  if ($("scene-more").disabled) closeSceneMenu();
  $("scene-hint").hidden = !active || !scene || locked;
  $("scene-summary").textContent = scene ? sceneSummary(scene) : "";
  const applied = sceneById(scenes.applied);
  $("scene-state").replaceChildren();
  if (applied) {
    const name = document.createElement("strong");
    name.textContent = applied.name;
    const chip = document.createElement("span");
    chip.className = "scene-chip";
    chip.textContent = scenes.modified ? "Modified" : "Applied";
    $("scene-state").append(name, chip);
  }
  $("scene-state").hidden = !applied;
  $("scene-error").textContent = scenes.error || "";
  $("scene-error").hidden = !scenes.error;
}

function announceScene(message) {
  clearTimeout(sceneStatusTimer);
  $("scene-status").textContent = message;
  sceneStatusTimer = setTimeout(() => {
    $("scene-status").textContent = "";
  }, 4000);
}

async function sceneRequest(path, options, message) {
  sceneBusy = true;
  renderScenes(state);
  try {
    await settingsQueue;
    const next = await (await api(path, options)).json();
    uiError = null;
    return next;
  } finally {
    sceneBusy = false;
    if (state) renderScenes(state);
  }
}

function sceneName(scene) {
  return `“${scene.name}”`;
}

// Native modal dialogs make the page inert; Tab also wraps inside the dialog.
function openDialog(dialog, returnFocus, onSubmit) {
  dialogReturn = returnFocus;
  dialogSubmit = onSubmit;
  dialog.querySelectorAll(".field-error").forEach((el) => {
    el.hidden = true;
    el.textContent = "";
  });
  dialog.showModal();
}

function closeDialog(dialog) {
  if (dialog.open) dialog.close();
}

function dialogError(dialog, message) {
  const error = dialog.querySelector(".field-error");
  error.textContent = message;
  error.hidden = false;
}

function openNameDialog({ title, submit, value, returnFocus, onSave }) {
  const dialog = $("scene-name-dialog");
  $("scene-name-title").textContent = title;
  $("scene-name-submit").textContent = submit;
  $("scene-name").value = value;
  openDialog(dialog, returnFocus, () => onSave($("scene-name").value));
  $("scene-name").focus();
  $("scene-name").select();
}

function openConfirmDialog({ title, copy, submit, returnFocus, onConfirm }) {
  $("scene-confirm-title").textContent = title;
  $("scene-confirm-copy").textContent = copy;
  $("scene-confirm-submit").textContent = submit;
  openDialog($("scene-confirm-dialog"), returnFocus, onConfirm);
  $("scene-confirm-dialog").querySelector("[data-dialog-cancel]").focus();
}

function sceneMenuItems() {
  return [...$("scene-menu").querySelectorAll('[role="menuitem"]')];
}

function openSceneMenu(last = false) {
  if ($("scene-more").disabled) return;
  sceneMenuFocusLast = last;
  $("scene-menu").showPopover();
}

function closeSceneMenu(restore = false) {
  const sceneMenu = $("scene-menu");
  if (sceneMenu.matches(":popover-open")) sceneMenu.hidePopover();
  if (restore) $("scene-more").focus();
}

document.addEventListener("DOMContentLoaded", () => {
  for (const dialog of document.querySelectorAll(".scene-dialog")) {
    dialog.addEventListener("cancel", (event) => {
      if (sceneBusy) event.preventDefault();
    });
    dialog.addEventListener("close", () => {
      dialog.querySelector("input")?.removeAttribute("aria-invalid");
      const target =
        dialogReturn && !dialogReturn.disabled ? dialogReturn : $("scene");
      dialogReturn = null;
      dialogSubmit = null;
      target.focus();
    });
    dialog.addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      const focusable = [
        ...dialog.querySelectorAll("button:not(:disabled), input:not(:disabled)"),
      ];
      const first = focusable[0];
      const last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
    dialog.querySelector("[data-dialog-cancel]").addEventListener("click", () => {
      if (!sceneBusy) closeDialog(dialog);
    });
    dialog.querySelector("form").addEventListener("submit", async (event) => {
      event.preventDefault();
      if (sceneBusy || !dialogSubmit) return;
      const buttons = dialog.querySelectorAll("button");
      buttons.forEach((button) => (button.disabled = true));
      try {
        await dialogSubmit();
        closeDialog(dialog);
      } catch (error) {
        dialogError(dialog, error.message);
        dialog.querySelector("input")?.setAttribute("aria-invalid", "true");
      } finally {
        buttons.forEach((button) => (button.disabled = false));
      }
    });
  }

  const sceneMenu = $("scene-menu");
  const items = sceneMenuItems();
  sceneMenu.addEventListener("beforetoggle", (event) => {
    if (event.newState !== "open") return;
    const rect = $("scene-more").getBoundingClientRect();
    const width = Math.min(180, innerWidth - 24);
    sceneMenu.style.width = `${width}px`;
    sceneMenu.style.left = `${Math.max(12, Math.min(rect.right - width, innerWidth - width - 12))}px`;
    sceneMenu.style.top = `${rect.bottom + 6}px`;
  });
  sceneMenu.addEventListener("toggle", () => {
    const open = sceneMenu.matches(":popover-open");
    $("scene-more").setAttribute("aria-expanded", String(open));
    if (open) (sceneMenuFocusLast ? items.at(-1) : items[0]).focus();
    sceneMenuFocusLast = false;
  });
  $("scene-more").addEventListener("keydown", (event) => {
    if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      openSceneMenu(event.key === "ArrowUp");
    }
  });
  sceneMenu.addEventListener("keydown", (event) => {
    const index = items.indexOf(document.activeElement);
    const focus = {
      ArrowDown: (index + 1) % items.length,
      ArrowUp: (index - 1 + items.length) % items.length,
      Home: 0,
      End: items.length - 1,
    }[event.key];
    if (focus !== undefined) {
      event.preventDefault();
      items[focus].focus();
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeSceneMenu(true);
    } else if (event.key === "Tab") closeSceneMenu();
  });
  window.addEventListener("resize", () => closeSceneMenu());

  for (const item of items) {
    item.addEventListener("click", () => {
      const scene = sceneById(selectedScene);
      closeSceneMenu();
      if (!scene) return;
      const returnFocus = $("scene-more");
      const id = encodeURIComponent(scene.id);
      if (item.dataset.sceneAction === "update")
        openConfirmDialog({
          title: `Update ${sceneName(scene)}?`,
          copy: `This replaces the saved settings in ${sceneName(scene)} with the current camera, background, and processing setup.`,
          submit: "Update scene",
          returnFocus,
          onConfirm: async () => {
            render(await sceneRequest(`scenes/${id}`, { method: "PUT" }));
            announceScene(`Updated ${sceneName(scene)}.`);
          },
        });
      else if (item.dataset.sceneAction === "rename")
        openNameDialog({
          title: "Rename scene",
          submit: "Rename",
          value: scene.name,
          returnFocus,
          onSave: async (name) => {
            const next = await sceneRequest(`scenes/${id}`, {
              method: "PATCH",
              body: JSON.stringify({ name }),
            });
            render(next);
            announceScene(`Renamed to ${sceneName(sceneById(scene.id))}.`);
          },
        });
      else
        openConfirmDialog({
          title: `Delete ${sceneName(scene)}?`,
          copy: "This removes the saved scene and its background image. Your current camera setup stays the same.",
          submit: "Delete scene",
          returnFocus: $("scene"),
          onConfirm: async () => {
            render(await sceneRequest(`scenes/${id}`, { method: "DELETE" }));
            announceScene(`Deleted ${sceneName(scene)}.`);
          },
        });
    });
  }

  $("scene").addEventListener("change", (event) => {
    selectedScene = event.target.value;
    renderScenes(state);
  });

  $("scene-save").addEventListener("click", () =>
    openNameDialog({
      title: "Save scene",
      submit: "Save",
      value: "",
      returnFocus: $("scene-save"),
      onSave: async (name) => {
        const next = await sceneRequest("scenes", {
          method: "POST",
          body: JSON.stringify({ name }),
        });
        selectedScene = next.scenes.applied;
        render(next);
        announceScene(`Saved ${sceneName(sceneById(selectedScene))}.`);
      },
    }),
  );

  $("scene-apply").addEventListener("click", async () => {
    const scene = sceneById(selectedScene);
    if (!scene) return;
    try {
      const next = await sceneRequest(
        `scenes/${encodeURIComponent(scene.id)}/apply`,
        { method: "POST" },
      );
      backgroundPicker = false;
      render(next, true);
      if (next.settings.effect === "image")
        $("upload-help").textContent = `Image from ${sceneName(scene)}`;
      announceScene(`Applied ${sceneName(scene)}. Start the camera when ready.`);
    } catch (error) {
      showError(error);
    }
    // The button is disabled during the request, which drops keyboard focus.
    ($("scene-apply").disabled ? $("scene") : $("scene-apply")).focus();
  });
});
