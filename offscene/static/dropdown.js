// Select-only comboboxes. The popover top layer keeps menus outside scroll clips.
class Dropdown {
  constructor(trigger) {
    this.trigger = trigger;
    this.menu = document.getElementById(trigger.getAttribute("aria-controls"));
    this.label = trigger.querySelector(".dropdown-value");
    this.search = "";
    this.searchTime = 0;
    this.prepareOptions();
    trigger.addEventListener("click", () =>
      this.open ? this.close() : this.show(),
    );
    trigger.addEventListener("keydown", (event) => this.onKey(event));
    this.menu.addEventListener("toggle", () => {
      trigger.setAttribute("aria-expanded", String(this.open));
      if (!this.open) trigger.removeAttribute("aria-activedescendant");
    });
    this.menu.addEventListener("pointerdown", (event) =>
      event.preventDefault(),
    );
    this.menu.addEventListener("click", (event) => {
      const option = event.target.closest('[role="option"]');
      if (option) this.choose(this.options.indexOf(option));
    });
    document.addEventListener("focusin", (event) => {
      if (event.target !== trigger && !this.menu.contains(event.target))
        this.close();
    });
    window.addEventListener("resize", () => this.close());
    document.addEventListener(
      "scroll",
      (event) => {
        if (this.open && event.target !== this.menu) this.close();
      },
      true,
    );
  }

  get open() {
    return this.menu.matches(":popover-open");
  }
  get options() {
    return [...this.menu.querySelectorAll('[role="option"]')];
  }

  prepareOptions() {
    this.options.forEach((option, index) => {
      option.id = `${this.trigger.id}-option-${index}`;
      if (!option.querySelector(".option-check")) {
        const icon = document.createElementNS(
          "http://www.w3.org/2000/svg",
          "svg",
        );
        icon.setAttribute("class", "option-check");
        icon.setAttribute("viewBox", "0 0 16 16");
        icon.setAttribute("aria-hidden", "true");
        const path = document.createElementNS(
          "http://www.w3.org/2000/svg",
          "path",
        );
        path.setAttribute("d", "m3 8 3.5 3.5L13 4.5");
        icon.append(path);
        option.append(icon);
      }
    });
  }

  setOptions(options) {
    this.close();
    this.menu.replaceChildren(
      ...options.map(({ value, label }) => {
        const option = document.createElement("div");
        option.setAttribute("role", "option");
        option.dataset.value = value;
        option.textContent = label;
        return option;
      }),
    );
    this.prepareOptions();
    this.setValue(this.trigger.value);
  }

  setValue(value) {
    const options = this.options;
    const selected =
      options.find((option) => option.dataset.value === String(value)) ||
      options[0];
    this.trigger.value = selected?.dataset.value || "";
    this.label.textContent = selected?.textContent || "No options available";
    options.forEach((option) =>
      option.setAttribute("aria-selected", String(option === selected)),
    );
  }

  show() {
    if (this.trigger.disabled || !this.options.length) return;
    const rect = this.trigger.getBoundingClientRect();
    const below = window.innerHeight - rect.bottom - 12;
    const above = rect.top - 12;
    const upward =
      below < Math.min(this.menu.scrollHeight || 240, 240) && above > below;
    this.menu.style.width = `${Math.min(Math.max(rect.width, 220), window.innerWidth - 24)}px`;
    this.menu.style.maxHeight = `${Math.max(44, Math.min(280, upward ? above : below))}px`;
    this.menu.style.left = `${Math.max(12, Math.min(rect.left, window.innerWidth - parseFloat(this.menu.style.width) - 12))}px`;
    this.menu.style.top = upward ? "auto" : `${rect.bottom + 6}px`;
    this.menu.style.bottom = upward
      ? `${window.innerHeight - rect.top + 6}px`
      : "auto";
    this.menu.showPopover();
    this.trigger.setAttribute("aria-expanded", "true");
    this.search = "";
    this.highlight(
      Math.max(
        0,
        this.options.findIndex(
          (option) => option.dataset.value === this.trigger.value,
        ),
      ),
    );
  }

  close() {
    if (this.open) this.menu.hidePopover();
    this.trigger.setAttribute("aria-expanded", "false");
    this.trigger.removeAttribute("aria-activedescendant");
  }

  highlight(index) {
    this.active = index;
    this.options.forEach((option, i) =>
      option.classList.toggle("active", i === index),
    );
    const option = this.options[index];
    if (option) {
      this.trigger.setAttribute("aria-activedescendant", option.id);
      option.scrollIntoView({ block: "nearest" });
    }
  }

  choose(index) {
    if (this.trigger.disabled) return this.close();
    const option = this.options[index];
    if (!option || !option.dataset.value) return;
    this.setValue(option.dataset.value);
    this.close();
    this.trigger.focus({ preventScroll: true });
    this.trigger.dispatchEvent(new Event("change", { bubbles: true }));
  }

  onKey(event) {
    if (
      ["ArrowDown", "ArrowUp", "Home", "End", "Enter", " "].includes(event.key)
    ) {
      event.preventDefault();
      const wasOpen = this.open;
      if (!wasOpen) this.show();
      if (event.key === "Home") this.highlight(0);
      else if (event.key === "End") this.highlight(this.options.length - 1);
      else if (wasOpen && ["Enter", " "].includes(event.key))
        this.choose(this.active);
      else if (wasOpen)
        this.highlight(
          Math.max(
            0,
            Math.min(
              this.options.length - 1,
              this.active + (event.key === "ArrowDown" ? 1 : -1),
            ),
          ),
        );
    } else if (event.key === "Escape") {
      if (this.open) {
        event.preventDefault();
        this.close();
      }
    } else if (event.key === "Tab") {
      this.close();
    } else if (
      event.key.length === 1 &&
      !event.ctrlKey &&
      !event.metaKey &&
      !event.altKey
    ) {
      event.preventDefault();
      if (!this.open) this.show();
      const now = performance.now();
      this.search =
        (now - this.searchTime > 700 ? "" : this.search) +
        event.key.toLowerCase();
      this.searchTime = now;
      const index = this.options.findIndex((option) =>
        option.textContent.trim().toLowerCase().startsWith(this.search),
      );
      if (index >= 0) this.highlight(index);
    }
  }
}

const dropdowns = new Map(
  [...document.querySelectorAll(".dropdown-trigger")].map((trigger) => [
    trigger.id,
    new Dropdown(trigger),
  ]),
);
