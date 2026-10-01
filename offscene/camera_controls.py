"""Bounded V4L2 camera controls, with driver ranges and session restoration."""

import errno
import fcntl
import logging
import os
import struct
import threading

log = logging.getLogger(__name__)
# Standard control IDs from linux/v4l2-controls.h. No vendor/private writes.
CONTROLS = {
    "auto_exposure": (0x009A0901, "Exposure mode", "Exposure"),
    "exposure_time_absolute": (0x009A0902, "Shutter", "Exposure"),
    "gain": (0x00980913, "Gain", "Exposure"),
    "power_line_frequency": (0x00980918, "Anti-flicker", "Exposure"),
    "white_balance_automatic": (0x0098090C, "Auto white balance", "White balance"),
    "white_balance_temperature": (0x0098091A, "Temperature", "White balance"),
    "focus_automatic_continuous": (0x009A090C, "Autofocus", "Lens"),
    "focus_absolute": (0x009A090A, "Focus", "Lens"),
    "zoom_absolute": (0x009A090D, "Zoom", "Lens"),
    "brightness": (0x00980900, "Brightness", "Image"),
    "contrast": (0x00980901, "Contrast", "Image"),
    "saturation": (0x00980902, "Saturation", "Image"),
    "sharpness": (0x0098091B, "Sharpness", "Image"),
    "gamma": (0x00980910, "Gamma", "Image"),
    "hue": (0x00980903, "Hue", "Image"),
    "backlight_compensation": (0x0098091C, "Backlight compensation", "Image"),
}
AUTO = {
    "auto_exposure": ("exposure_time_absolute", 1),
    "white_balance_automatic": ("white_balance_temperature", 0),
    "focus_automatic_continuous": ("focus_absolute", 0),
}


class CameraControls:
    def __init__(self, device):
        self.lock = threading.RLock()
        self.fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
        self.touched = set()
        try:
            self.original = {k: v["value"] for k, v in self.read().items()}
        except Exception:
            os.close(self.fd)
            raise

    def _ioctl(self, number, layout, *values):
        data = bytearray(struct.pack(layout, *values))
        # _IOWR('V', number, struct), the Linux V4L2 ABI.
        request = (3 << 30) | (len(data) << 16) | (ord("V") << 8) | number
        fcntl.ioctl(self.fd, request, data)
        return struct.unpack(layout, data)

    def read(self):
        with self.lock:
            if self.fd is None:
                raise ValueError("Camera controls are closed. Start the camera again.")
            result = {}
            for name, (cid, label, group) in CONTROLS.items():
                try:
                    q = self._ioctl(
                        36, "=II32siiiiIII", cid, 0, b"", 0, 0, 0, 0, 0, 0, 0
                    )
                    _, kind, _, minimum, maximum, step, default, flags, _, _ = q
                    if flags & 1 or kind not in (1, 2, 3):
                        continue
                    value = self._ioctl(27, "=Ii", cid, 0)[1]
                except OSError as exc:
                    if exc.errno in (errno.EINVAL, errno.ENOTTY):
                        continue
                    raise
                options = []
                if kind == 3:
                    for index in range(minimum, maximum + 1):
                        try:
                            menu = self._ioctl(37, "=II32sI", cid, index, b"", 0)
                            options.append(
                                {
                                    "value": index,
                                    "label": menu[2]
                                    .split(b"\0")[0]
                                    .decode("utf-8", "replace"),
                                }
                            )
                        except OSError as exc:
                            if exc.errno != errno.EINVAL:
                                raise
                result[name] = {
                    "label": label,
                    "group": group,
                    "type": {1: "range", 2: "toggle", 3: "menu"}[kind],
                    "min": minimum,
                    "max": maximum,
                    "step": max(1, step),
                    "default": default,
                    "value": value,
                    "inactive": bool(flags & 16),
                    "readonly": bool(flags & (2 | 4)),
                    "options": options,
                }
            return result

    def _write(self, name, value):
        control = self.read().get(name)
        if not control or control["readonly"] or control["inactive"]:
            raise ValueError(
                f"{CONTROLS.get(name, (None, name))[1]} is unavailable in the current camera mode."
            )
        if (
            type(value) is not int
            or not control["min"] <= value <= control["max"]
            or (value - control["min"]) % control["step"]
        ):
            raise ValueError(
                f"{control['label']} must be {control['min']}–{control['max']} in steps of {control['step']}."
            )
        if control["options"] and value not in [o["value"] for o in control["options"]]:
            raise ValueError(f"Unsupported {control['label']} option.")
        self.touched.add(name)
        if name in AUTO:
            self.touched.add(AUTO[name][0])
        self._ioctl(28, "=Ii", CONTROLS[name][0], value)
        actual = self._ioctl(27, "=Ii", CONTROLS[name][0], 0)[1]
        if actual != value:
            raise ValueError(
                f"Camera returned {actual} instead of {value} for {control['label']}."
            )

    def restore(self, values):
        # Unlock manual values first, then restore their original Auto modes.
        for mode, (dependent, manual) in AUTO.items():
            if mode in values or dependent in values:
                try:
                    self._write(mode, manual)
                except (OSError, ValueError):
                    log.warning("Could not unlock %s for restoration", mode)
        for name in [k for k in values if k not in AUTO] + [
            k for k in values if k in AUTO
        ]:
            try:
                self._write(name, values[name])
            except (OSError, ValueError):
                log.exception("Could not restore camera control %s", name)

    def apply(self, values, *, exposure=None, fps=30, persist=None):
        with self.lock:
            before = {k: v["value"] for k, v in self.read().items()}
            touched_before = self.touched.copy()
            try:
                if exposure and exposure != "preserve":
                    controls = self.read()
                    shutter = controls.get("exposure_time_absolute")
                    mode = controls.get("auto_exposure")
                    if not shutter or not mode:
                        raise ValueError(
                            "This webcam does not expose manual exposure controls."
                        )
                    limit = max(shutter["min"], min(shutter["max"], int(7500 / fps)))
                    limit -= (limit - shutter["min"]) % shutter["step"]
                    if exposure in ("manual", "motion") or shutter["value"] > limit:
                        self._write("auto_exposure", 1)
                        self._write(
                            "exposure_time_absolute",
                            values.get("exposure_time_absolute", limit)
                            if exposure == "manual"
                            else limit,
                        )
                    if exposure == "auto":
                        modes = [o["value"] for o in mode["options"]]
                        self._write("auto_exposure", 3 if 3 in modes else 0)
                # Auto toggles must precede their dependent sliders.
                for name in sorted(values, key=lambda k: k not in AUTO):
                    if (
                        name == "exposure_time_absolute"
                        and exposure is not None
                        and exposure != "manual"
                    ):
                        continue
                    control = self.read().get(name)
                    if not control:
                        raise ValueError(f"Unsupported camera control: {name}.")
                    if not control["inactive"]:
                        self._write(name, values[name])
                if persist:
                    persist()
                return self.read()
            except Exception:
                changed = self.touched - touched_before | set(values)
                if exposure:
                    changed |= {"auto_exposure", "exposure_time_absolute"}
                for mode, (dependent, _) in AUTO.items():
                    if mode in changed:
                        changed.add(dependent)
                    if dependent in changed:
                        changed.add(mode)
                self.restore({k: before[k] for k in changed if k in before})
                raise

    def reset(self, persist=None):
        """Restore driver defaults, unlocking manual controls before Auto modes."""
        with self.lock:
            controls = self.read()
            modes = {
                mode: manual
                for mode, (_, manual) in AUTO.items()
                if mode in controls
                and not controls[mode]["readonly"]
                and not controls[mode]["inactive"]
                and (
                    not controls[mode]["options"]
                    or manual in [o["value"] for o in controls[mode]["options"]]
                )
            }
            unlocked = {AUTO[mode][0] for mode in modes}
            defaults = {
                name: control["default"]
                for name, control in controls.items()
                if not control["readonly"]
                and (not control["inactive"] or name in unlocked)
            }
            before = {name: controls[name]["value"] for name in defaults}
            try:
                for mode, manual in modes.items():
                    self._write(mode, manual)
                for name in sorted(defaults, key=lambda key: key in AUTO):
                    self._write(name, defaults[name])
                result = self.read()
                if persist:
                    persist(defaults)
                return result
            except Exception:
                self.restore(before)
                raise

    def close(self):
        with self.lock:
            if self.fd is not None:
                try:
                    self.restore(
                        {
                            k: self.original[k]
                            for k in self.touched
                            if k in self.original
                        }
                    )
                finally:
                    os.close(self.fd)
                    self.fd = None
