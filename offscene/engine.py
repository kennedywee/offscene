"""GPU matting, latest-frame capture, and optional V4L2 output."""

import hashlib
import logging
import os
import queue
import secrets
import threading
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import cv2
import torch
import torch.nn.functional as F
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    model_validator,
)

from .camera_controls import CONTROLS, CameraControls
from .camera_presets import preset_values
from .scenes import FIELDS, SceneConflict, SceneStore, clean_name, scene_settings

log = logging.getLogger(__name__)
CACHE = Path(
    os.environ.get("OFFSCENE_CACHE", Path(__file__).resolve().parent.parent / ".cache")
)
MODELS = {
    "mobilenetv3": "847a8b5139498afbf7abde9cc347b41030540f6498db593a0e3d9dde1eccdd96",
    "resnet50": "1273e58a7946296b148844a73b87e393d0ac8e5bce04af877ed443686e3c7c46",
}


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera: str = Field(default="/dev/video0", pattern=r"^/dev/video\d+$")
    resolution: Literal["1280x720", "1920x1080", "2560x1440", "3840x2160"] = "1920x1080"
    fps: Literal[30, 60] = 30
    exposure: Literal["auto", "motion", "manual", "preserve"] = "auto"
    zoom: float = Field(default=1.0, ge=1, le=2)
    camera_preset: Literal[
        "custom", "natural", "cinematic", "apple", "professional", "polished", "tiktok"
    ] = "custom"
    camera_controls: dict[str, dict[str, StrictInt]] = Field(default_factory=dict)
    model: Literal["mobilenetv3", "resnet50"] = "resnet50"
    quality: Literal["balanced", "detail"] = "balanced"
    effect: Literal["blur", "color", "image"] = "blur"
    color: str = Field(default="#182226", pattern=r"^#[0-9a-fA-F]{6}$")
    blur: int = Field(default=55, ge=10, le=100)
    cleanup: float = Field(default=0.08, ge=0, le=0.8)
    shrink: int = Field(default=1, ge=0, le=4)
    preview: Literal["output", "original", "matte"] = "output"
    virtual_camera: bool = False
    output_device: str = Field(default="/dev/video10", pattern=r"^/dev/video\d+$")

    @model_validator(mode="after")
    def validate_capture_rate(self):
        if self.resolution in ("2560x1440", "3840x2160") and self.fps > 30:
            raise ValueError("1440p and 4K support up to 30 fps. Select 30 fps first.")
        return self


def devices():
    result = []
    for path in sorted(Path("/sys/class/video4linux").glob("video*")):
        try:
            # Loopback cameras have no physical device parent. UVC metadata nodes
            # have index 1 and cannot provide images.
            virtual = not (path / "device").exists()
            if not virtual and (path / "index").read_text().strip() != "0":
                continue
            result.append(
                {
                    "path": f"/dev/{path.name}",
                    "name": (path / "name").read_text().strip(),
                    "virtual": virtual,
                }
            )
        except OSError:
            continue
    return result


def model_path(variant="resnet50"):
    name = f"rvm_{variant}_fp16.torchscript"
    checksum = MODELS[variant]
    path = CACHE / "models" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == checksum:
        return path
    temporary = path.with_suffix(".download")
    try:
        with (
            urllib.request.urlopen(
                f"https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/{name}",
                timeout=60,
            ) as source,
            temporary.open("wb") as target,
        ):
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != checksum:
            raise RuntimeError("Model checksum failed. Retry the download.")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


class LatestCamera:
    """Keep only the newest frame so a slow GPU cannot accumulate latency."""

    def __init__(self, settings):
        self.settings = settings
        self.frames = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._capture, daemon=True, name="webcam-capture"
        )
        self.error = None
        self.warning = None
        self.fps = 0.0
        self.controls = None
        self.thread.start()

    def _capture(self):
        cap = cv2.VideoCapture(self.settings.camera, cv2.CAP_V4L2)
        try:
            if not cap.isOpened():
                raise RuntimeError(
                    "Cannot open the webcam. Close other apps using it and check device permissions."
                )
            width, height = map(int, self.settings.resolution.split("x"))
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            cap.set(cv2.CAP_PROP_FPS, self.settings.fps)
            # Let the driver capture while OpenCV decodes the previous MJPEG.
            # A single driver buffer stalls capture; our queue still keeps only
            # the newest decoded frame for processing.
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 3)
            actual = (
                int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            )
            if actual != (width, height):
                raise RuntimeError(
                    f"Webcam returned {actual[0]}×{actual[1]} instead of {width}×{height}. Select a supported resolution."
                )
            self.controls = CameraControls(self.settings.camera)
            # This camera applies exposure reliably only after streaming starts.
            ok, _ = cap.read()
            if not ok:
                raise RuntimeError("Webcam could not start streaming.")
            try:
                self.controls.apply(
                    self.settings.camera_controls.get(self.settings.camera, {}),
                    exposure=self.settings.exposure,
                    fps=self.settings.fps,
                )
            except (ValueError, OSError) as exc:
                self.warning = f"Camera settings could not be applied: {exc}"
            period_start, count = time.perf_counter(), 0
            while not self.stop_event.is_set():
                ok, frame = cap.read()
                if not ok:
                    raise RuntimeError(
                        "Webcam stopped delivering frames. Check the USB connection."
                    )
                item = (time.perf_counter(), frame)
                count += 1
                if item[0] - period_start >= 1:
                    self.fps = round(count / (item[0] - period_start), 1)
                    period_start, count = item[0], 0
                try:
                    self.frames.get_nowait()
                except queue.Empty:
                    pass
                self.frames.put_nowait(item)
        except (cv2.error, RuntimeError, OSError) as exc:
            self.error = str(exc)
        finally:
            try:
                if self.controls:
                    self.controls.close()
            finally:
                cap.release()

    def read(self):
        try:
            return self.frames.get(timeout=2)
        except queue.Empty:
            raise RuntimeError(
                self.error or "No webcam frames received within two seconds."
            ) from None

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=3)


def zoom_bounds(width, height, zoom):
    cropped_width, cropped_height = round(width / zoom), round(height / zoom)
    return (
        (width - cropped_width) // 2,
        (height - cropped_height) // 2,
        cropped_width,
        cropped_height,
    )


class PreviewEncoder:
    """Encode the latest frame independently; JPEG work never stalls GPU output."""

    def __init__(self, publish):
        self.publish = publish
        self.frames = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run, daemon=True, name="preview-encoder"
        )
        self.thread.start()

    def submit(self, frame, *, bgr=False, zoom=1.0):
        try:
            self.frames.get_nowait()
        except queue.Empty:
            pass
        self.frames.put_nowait((frame, bgr, zoom))

    def _run(self):
        while not self.stop_event.is_set():
            try:
                frame, bgr, zoom = self.frames.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if zoom != 1:
                    height, width = frame.shape[:2]
                    x, y, w, h = zoom_bounds(width, height, zoom)
                    frame = cv2.resize(frame[y : y + h, x : x + w], (width, height))
                ok, encoded = cv2.imencode(
                    ".jpg",
                    frame if bgr else cv2.cvtColor(frame, cv2.COLOR_RGB2BGR),
                    [cv2.IMWRITE_JPEG_QUALITY, 90],
                )
                if ok and not self.stop_event.is_set():
                    self.publish(encoded.tobytes())
            except cv2.error:
                log.exception("Preview encoding failed")

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=2)


class Matting:
    def __init__(self, variant="resnet50"):
        if not torch.cuda.is_available():
            raise RuntimeError(
                "CUDA is unavailable. Install an NVIDIA driver and the CUDA PyTorch build (uv sync)."
            )
        torch.set_num_threads(2)
        self.model = torch.jit.freeze(
            torch.jit.load(str(model_path(variant)), map_location="cuda").eval()
        )
        self.states = [None] * 4
        self.shape = None
        self.background_key = None
        self.background = None
        self.upload = None

    @torch.inference_mode()
    def process(self, bgr, settings, background=None, background_version=0):
        height, width = bgr.shape[:2]
        ratio = min(
            1.0, (768 if settings.quality == "detail" else 512) / max(width, height)
        )
        shape = (height, width, ratio, settings.zoom)
        if shape != self.shape:
            self.states = [None] * 4
            self.shape = shape
        if self.upload is None or tuple(self.upload.shape) != bgr.shape:
            self.upload = torch.empty(bgr.shape, dtype=torch.uint8, pin_memory=True)
        rgb = self.upload.numpy()
        cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB, dst=rgb)
        src = (
            self.upload
            # Upload bytes first. The old blocking device/dtype conversion
            # converted to FP16 on the CPU and doubled the transfer size.
            .to(device="cuda", non_blocking=True)
            .to(dtype=torch.float16)
            .permute(2, 0, 1)
            .unsqueeze(0)
            / 255
        )
        if settings.zoom != 1:
            x, y, w, h = zoom_bounds(width, height, settings.zoom)
            src = F.interpolate(
                src[:, :, y : y + h, x : x + w],
                size=(height, width),
                mode="bilinear",
                align_corners=False,
            )
        foreground, alpha, *self.states = self.model(src, *self.states, ratio)
        alpha = ((alpha - settings.cleanup) / (1 - settings.cleanup)).clamp(0, 1)
        if settings.shrink:
            # Erode the soft matte to remove narrow background fringes.
            radius = settings.shrink
            alpha = -F.max_pool2d(-alpha, 2 * radius + 1, stride=1, padding=radius)
        if settings.effect == "blur":
            # Three box passes at reduced resolution approximate a Gaussian blur.
            small = F.interpolate(
                src,
                size=(max(1, height // 8), max(1, width // 8)),
                mode="bilinear",
                align_corners=False,
            )
            kernel = 2 * int(settings.blur / 10) + 1
            for _ in range(3):
                small = F.avg_pool2d(
                    F.pad(small, (kernel // 2,) * 4, mode="replicate"), kernel, stride=1
                )
            replacement = F.interpolate(
                small, size=(height, width), mode="bilinear", align_corners=False
            )
        else:
            key = (settings.effect, settings.color, width, height, background_version)
            if key != self.background_key:
                if settings.effect == "image":
                    if background is None:
                        raise RuntimeError(
                            "Upload a background image before selecting Image."
                        )
                    bh, bw = background.shape[:2]
                    scale = max(width / bw, height / bh)
                    resized = cv2.resize(
                        background,
                        (max(width, round(bw * scale)), max(height, round(bh * scale))),
                    )
                    y, x = (
                        (resized.shape[0] - height) // 2,
                        (resized.shape[1] - width) // 2,
                    )
                    pixels = cv2.cvtColor(
                        resized[y : y + height, x : x + width], cv2.COLOR_BGR2RGB
                    ).copy()
                    self.background = (
                        torch.from_numpy(pixels)
                        .to(device="cuda")
                        .to(dtype=torch.float16)
                        .permute(2, 0, 1)
                        .unsqueeze(0)
                        / 255
                    )
                else:
                    color = [
                        int(settings.color[i : i + 2], 16) / 255 for i in (1, 3, 5)
                    ]
                    self.background = torch.tensor(
                        color, device="cuda", dtype=torch.float16
                    ).view(1, 3, 1, 1)
                self.background_key = key
            replacement = self.background
        composite = foreground * alpha + replacement * (1 - alpha)
        # Pack RGB pixels on the GPU. A strided CPU image makes both the
        # virtual-camera sender and OpenCV repack the entire 4K frame.
        output = (
            (composite[0].permute(1, 2, 0).clamp(0, 1) * 255)
            .byte()
            .contiguous()
            .cpu()
            .numpy()
        )
        # The blocking output copy also completes the upload before the next
        # call reuses its pinned buffer. Returned frames must own their pixels
        # because preview encoding runs on another thread.
        if settings.preview == "matte":
            preview = (
                (alpha[0].expand(3, -1, -1).permute(1, 2, 0) * 255)
                .byte()
                .contiguous()
                .cpu()
                .numpy()
            )
        else:
            preview = output
        return output, preview


class Engine:
    def __init__(self):
        self.lock = threading.RLock()
        self.preview_ready = threading.Condition(self.lock)
        self.settings = Settings()
        self.settings_path = CACHE / "settings.json"
        if self.settings_path.exists():
            try:
                self.settings = Settings.model_validate_json(
                    self.settings_path.read_text()
                )
            except (ValueError, OSError):
                log.warning("Ignoring unreadable saved settings")
        self.thread = None
        self.capture = None
        self.camera_preset_undo = None
        self.stop_event = threading.Event()
        self.jpeg = None
        self.jpeg_sequence = 0
        self.background = None
        self.background_version = 0
        # Scene mutations hold scene_lock first, then the engine lock briefly.
        self.scene_lock = threading.Lock()
        self.scenes = SceneStore(CACHE)
        self.state = {
            "phase": "stopped",
            "error": self._restore_scene_background(),
            "warning": None,
            "fps": 0,
            "capture_fps": 0,
            "capture_warning": None,
            "processing_ms": 0,
            "frame_age_ms": 0,
            "frames": 0,
            "resolution": None,
            "virtual_device": None,
            "gpu": None,
        }

    def publish_preview(self, jpeg):
        with self.preview_ready:
            if not self.stop_event.is_set():
                self.jpeg = jpeg
                self.jpeg_sequence += 1
                self.preview_ready.notify_all()

    def wait_preview(self, sequence):
        with self.preview_ready:
            self.preview_ready.wait_for(
                lambda: self.jpeg_sequence != sequence or self.stop_event.is_set(),
                timeout=1,
            )
            return self.jpeg_sequence, self.jpeg

    def status(self):
        # Pick up repaired scene metadata without blocking on a mutation.
        if self.scenes.error and self.scene_lock.acquire(blocking=False):
            try:
                self.scenes.load()
            finally:
                self.scene_lock.release()
        with self.lock:
            return {
                **self.state,
                "settings": self.settings.model_dump(),
                "has_background": self.background is not None,
                "can_undo_camera_preset": self.camera_preset_undo is not None,
                "scenes": self._scene_status(),
            }

    def _restore_scene_background(self):
        """Restore the last applied scene's image only if it is still current.

        Uploaded images live only in memory. Never start a camera here.
        """
        current = self.settings
        applied = self.scenes.applied
        # Only an interrupted apply leaves "previous" on disk: the new scene is
        # recorded while settings.json still holds the previous scene's settings.
        if (
            applied
            and applied.get("previous")
            and not self._scene_matches(self.scenes.get(applied["id"]), current)
            and self._scene_matches(
                self.scenes.get(applied["previous"]["id"]), current
            )
        ):
            applied = self.scenes.applied = applied["previous"]
        if current.effect != "image":
            return None
        self.settings = current.model_copy(update={"effect": "blur"})
        if not applied or applied["background_changed"]:
            return None
        scene = self.scenes.get(applied["id"])
        if not scene["background"] or not self._scene_matches(scene, current):
            return None
        try:
            self.background = self.scenes.read_asset(scene["id"], scene["background"])
        except (OSError, ValueError):
            log.exception("Could not restore scene background")
            # Do not present the image scene as active after falling back.
            self.scenes.applied = None
            return (
                f"The background image for scene “{scene['name']}” is missing or "
                "unreadable, so Offscene started with Blur. Choose an image, then "
                "use Update scene to save it again."
            )
        self.settings = current
        self.background_version += 1
        return None

    def _scene_target(self, scene, base):
        """Validate a scene as complete Settings, keeping excluded preferences."""
        values = dict(scene["settings"])
        name = scene["name"]
        if set(values) != {*FIELDS, "camera_profile"}:
            raise ValueError(f"Scene “{name}” has missing or unknown settings.")
        profile = values.pop("camera_profile")
        if not isinstance(profile, dict) or any(k not in CONTROLS for k in profile):
            raise ValueError(f"Scene “{name}” has invalid camera controls.")
        try:
            new = Settings.model_validate(
                {**base.model_dump(), **values, "camera_controls": {}}
            )
            # Replace only this camera's profile; others stay as configured.
            profiles = {k: dict(v) for k, v in base.camera_controls.items()}
            profiles[new.camera] = profile
            return Settings.model_validate(
                {**new.model_dump(), "camera_controls": profiles}
            )
        except ValidationError as exc:
            error = exc.errors()[0]
            field = ".".join(str(part) for part in error["loc"]) or "settings"
            raise ValueError(
                f"Scene “{name}” has an invalid {field}: {error['msg']}."
            ) from None

    def _scene_matches(self, scene, settings):
        try:
            target = self._scene_target(scene, settings)
        except ValueError:
            return False
        return scene_settings(target) == scene_settings(settings)

    def _scene_status(self):
        applied = self.scenes.applied
        modified = False
        if applied:
            modified = applied["background_changed"] or not self._scene_matches(
                self.scenes.get(applied["id"]), self.settings
            )
        return {
            "items": [
                {
                    "id": scene["id"],
                    "name": scene["name"],
                    "settings": {
                        k: v
                        for k, v in scene["settings"].items()
                        if k != "camera_profile"
                    },
                    "has_background": scene["background"] is not None,
                }
                for scene in self.scenes.scenes
            ],
            "applied": applied["id"] if applied else None,
            "modified": modified,
            "error": self.scenes.error,
        }

    def _camera_active(self):
        return bool(
            (self.thread and self.thread.is_alive())
            or (self.capture and self.capture.thread.is_alive())
        )

    def save_scene(self, name=None, scene_id=None):
        """Save the current setup as a new scene, or replace scene_id's setup."""
        with self.scene_lock:
            with self.lock:
                store = self.scenes.require()
                previous = store.get(scene_id) if scene_id else None
                if not previous:
                    name = clean_name(name, store.scenes)
                settings = self.settings.model_copy(deep=True)
                background = self.background
            # Encode outside the engine lock so saving never stalls live output.
            new_id = scene_id or secrets.token_hex(8)
            asset = None
            if settings.effect == "image":
                if background is None:
                    raise ValueError("Choose a background image before saving.")
                try:
                    asset = store.write_asset(new_id, background)
                except OSError as exc:
                    raise ValueError(
                        f"Could not save the scene background: {exc.strerror or exc}."
                    ) from exc
            now = datetime.now(UTC).isoformat(timespec="seconds")
            record = {
                "id": new_id,
                "name": previous["name"] if previous else name,
                "created_at": previous.get("created_at", now) if previous else now,
                "updated_at": now,
                "settings": scene_settings(settings),
                "background": asset,
            }
            try:
                with self.lock:
                    scenes = (
                        [record if s["id"] == scene_id else s for s in store.scenes]
                        if previous
                        else [*store.scenes, record]
                    )
                    # The current setup now matches this scene unless the image
                    # changed while it was being written.
                    changed = asset is not None and self.background is not background
                    store.commit(scenes, {"id": new_id, "background_changed": changed})
            except OSError as exc:
                store.remove_asset(new_id, asset)
                raise ValueError(
                    f"Could not save scenes: {exc.strerror or exc}. The previous "
                    "scenes are unchanged."
                ) from exc
            if previous and previous["background"] != asset:
                store.remove_asset(new_id, previous["background"])
        return self.status()

    def rename_scene(self, scene_id, name):
        with self.scene_lock, self.lock:
            store = self.scenes.require()
            scene = store.get(scene_id)
            name = clean_name(name, store.scenes, exclude=scene_id)
            try:
                store.commit(
                    [{**s, "name": name} if s is scene else s for s in store.scenes],
                    store.applied,
                )
            except OSError as exc:
                raise ValueError(
                    f"Could not rename the scene: {exc.strerror or exc}."
                ) from exc
        return self.status()

    def delete_scene(self, scene_id):
        """Remove a scene and its image. Current settings stay unchanged."""
        with self.scene_lock, self.lock:
            store = self.scenes.require()
            scene = store.get(scene_id)
            applied = store.applied
            if applied and applied["id"] == scene_id:
                applied = None
            elif applied and (applied.get("previous") or {}).get("id") == scene_id:
                applied = {**applied, "previous": None}
            try:
                store.commit([s for s in store.scenes if s is not scene], applied)
            except OSError as exc:
                raise ValueError(
                    f"Could not delete the scene: {exc.strerror or exc}."
                ) from exc
            store.remove_asset(scene_id, scene["background"])
        return self.status()

    def apply_scene(self, scene_id):
        """Validate everything, then commit settings. Never starts the camera."""
        with self.scene_lock, self.lock:
            store = self.scenes.require()
            scene = store.get(scene_id)
            if self._camera_active():
                raise SceneConflict("Stop the camera to apply a scene.")
            new = self._scene_target(scene, self.settings)
            if new.camera == new.output_device:
                raise ValueError(
                    f"Scene “{scene['name']}” uses the virtual-camera output device "
                    "as its camera. Change the output device first."
                )
            if not any(d["path"] == new.camera and not d["virtual"] for d in devices()):
                raise SceneConflict(
                    "Scene camera is unavailable. Connect it and refresh cameras."
                )
            background = None
            if new.effect == "image":
                try:
                    if not scene["background"]:
                        raise ValueError("no image recorded")
                    background = store.read_asset(scene_id, scene["background"])
                except (OSError, ValueError) as exc:
                    raise ValueError(
                        f"The background image for scene “{scene['name']}” is "
                        "missing or unreadable. Current settings are unchanged."
                    ) from exc
            # Commit the scene marker first and settings last. Until the apply
            # completes, the marker keeps the previous scene so a restart after
            # an interruption or failed rollback can restore the previous scene.
            applied = store.applied
            previous = (
                {"id": applied["id"], "background_changed": applied["background_changed"]}
                if applied
                else None
            )
            try:
                store.commit(
                    store.scenes,
                    {"id": scene_id, "background_changed": False, "previous": previous},
                )
            except OSError as exc:
                raise ValueError(
                    f"Could not save scenes: {exc.strerror or exc}. Current "
                    "settings are unchanged."
                ) from exc
            try:
                self._save_settings(new)
            except OSError as exc:
                try:
                    store.commit(store.scenes, applied)
                except OSError:
                    log.exception("Could not restore the applied scene marker")
                    store.applied = applied
                raise ValueError(
                    f"Could not save settings: {exc.strerror or exc}. Current "
                    "settings are unchanged."
                ) from exc
            # The apply completed; drop the recovery reference so later edits
            # that happen to match the previous scene are not mistaken for an
            # interrupted apply. The next marker write persists this if needed.
            completed = {"id": scene_id, "background_changed": False}
            try:
                store.commit(store.scenes, completed)
            except OSError:
                log.exception("Could not finalize the applied scene marker")
                store.applied = completed
            self.settings = new
            if background is not None:
                self.background = background
                self.background_version += 1
            self.camera_preset_undo = None
            if self.state["phase"] != "error":
                # A startup background error no longer describes these settings.
                self.state["error"] = None
        return self.status()

    def set_background(self, image):
        """Use an uploaded image and select Image, or change nothing on failure."""
        with self.scene_lock, self.lock:
            previous = self.background, self.background_version
            self.background = image
            self.background_version += 1
            try:
                result = self.update({"effect": "image"})
            except OSError as exc:
                self.background, self.background_version = previous
                raise ValueError(
                    f"Could not save settings: {exc.strerror or exc}. The previous "
                    "background is unchanged."
                ) from exc
            except Exception:
                self.background, self.background_version = previous
                raise
            # The new image differs from any applied image scene's image.
            store, applied = self.scenes, self.scenes.applied
            if (
                applied
                and not applied["background_changed"]
                and store.get(applied["id"])["settings"].get("effect") == "image"
            ):
                changed = {"id": applied["id"], "background_changed": True}
                try:
                    store.commit(store.scenes, changed)
                except OSError:
                    log.exception("Could not record the background change")
                    store.applied = changed
                result = self.status()
            return result

    def update(self, patch):
        if "camera_controls" in patch or "camera_preset" in patch:
            raise ValueError("Use the camera-controls API to adjust the webcam.")
        with self.lock:
            new = Settings.model_validate({**self.settings.model_dump(), **patch})
            if self.thread and self.thread.is_alive():
                for field in (
                    "camera",
                    "resolution",
                    "fps",
                    "output_device",
                    "model",
                    "exposure",
                ):
                    if getattr(new, field) != getattr(self.settings, field):
                        raise ValueError(
                            "Stop the camera before changing the capture or output device settings."
                        )
            if new.camera == new.output_device:
                raise ValueError("Input and output cameras must be different devices.")
            if new.effect == "image" and self.background is None:
                raise ValueError("Upload a background image first.")
            camera_changed = new.camera != self.settings.camera
            preset_invalidated = camera_changed or new.exposure != self.settings.exposure
            if preset_invalidated:
                new.camera_preset = "custom"
            self._save_settings(new)
            self.settings = new
            if preset_invalidated:
                self.camera_preset_undo = None
        return self.status()

    def _save_settings(self, settings):
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.settings_path.with_suffix(".tmp")
        temporary.write_text(settings.model_dump_json(indent=2))
        temporary.replace(self.settings_path)

    def camera_control_info(self):
        with self.lock:
            device = self.settings.camera
            if (
                self.capture
                and self.capture.controls
                and self.capture.thread.is_alive()
            ):
                return {"device": device, "controls": self.capture.controls.read()}
            if not any(d["path"] == device and not d["virtual"] for d in devices()):
                raise ValueError("Select an available physical camera.")
            controls = CameraControls(device)
            try:
                return {"device": device, "controls": controls.read()}
            finally:
                controls.close()

    def update_camera_controls(self, values, exposure=None, *, preset="custom"):
        with self.lock:
            if (
                self.state["phase"] != "running"
                or not self.capture
                or not self.capture.controls
            ):
                raise ValueError("Start the camera to adjust its controls.")
            controls = self.capture.controls
            available = controls.read()
            mode = exposure or self.settings.exposure
            if "auto_exposure" in values or any(k not in CONTROLS for k in values):
                raise ValueError("Unsupported camera control.")
            for key in values:
                if key not in available:
                    raise ValueError(f"This camera does not support {key}.")
                if key == "exposure_time_absolute" and mode != "manual":
                    raise ValueError(
                        "Select Manual exposure before adjusting the shutter."
                    )
                # Manual sliders are only writable when their Auto mode is off.
                dependencies = {
                    "white_balance_temperature": "white_balance_automatic",
                    "focus_absolute": "focus_automatic_continuous",
                }
                auto = dependencies.get(key)
                if auto and values.get(auto, available.get(auto, {}).get("value", 0)):
                    raise ValueError(f"Turn off {available[auto]['label']} first.")
                unlocking = (key == "exposure_time_absolute" and mode == "manual") or (
                    auto and values.get(auto) == 0
                )
                if available[key]["readonly"] or (
                    available[key]["inactive"] and not unlocking
                ):
                    raise ValueError(
                        f"{available[key]['label']} is unavailable in the current camera mode."
                    )
            profiles = {k: dict(v) for k, v in self.settings.camera_controls.items()}
            profile = profiles.setdefault(self.settings.camera, {})
            if exposure:
                profile.pop("auto_exposure", None)
            profile.update(values)
            new = self.settings.model_copy(
                update={"camera_controls": profiles, "exposure": mode, "camera_preset": preset}
            )
            applied = dict(profile)
            if mode != "manual":
                applied.pop("exposure_time_absolute", None)
            result = controls.apply(
                applied,
                exposure=mode if exposure else None,
                fps=new.fps,
                persist=lambda: self._save_settings(new),
            )
            self.settings = new
            self.camera_preset_undo = None
            self.capture.warning = None
            return {"device": new.camera, "controls": result, "state": self.status()}

    def apply_camera_preset(self, name):
        with self.lock:
            available = self.camera_control_info()["controls"]
            values = preset_values(name, available)
            previous = {
                "values": {key: available[key]["value"] for key in values},
                "preset": self.settings.camera_preset,
            }
            result = self.update_camera_controls(values, preset=name)
            self.camera_preset_undo = previous
            result["state"] = self.status()
            return result

    def undo_camera_preset(self):
        with self.lock:
            if self.camera_preset_undo is None:
                raise ValueError("There is no camera look to undo.")
            previous = self.camera_preset_undo
            return self.update_camera_controls(previous["values"], preset=previous["preset"])

    def reset_camera_controls(self):
        with self.lock:
            if (
                self.state["phase"] != "running"
                or not self.capture
                or not self.capture.controls
            ):
                raise ValueError("Start the camera to reset its controls.")

            def persist(defaults):
                profile = dict(defaults)
                exposure = {0: "auto", 1: "manual", 3: "auto"}.get(
                    profile.get("auto_exposure"), "preserve"
                )
                if exposure != "preserve":
                    profile.pop("auto_exposure", None)
                profiles = dict(self.settings.camera_controls)
                profiles[self.settings.camera] = profile
                new = self.settings.model_copy(
                    update={
                        "camera_controls": profiles, "exposure": exposure, "zoom": 1.0,
                        "camera_preset": "custom",
                    }
                )
                self._save_settings(new)
                self.settings = new
                self.camera_preset_undo = None

            result = self.capture.controls.reset(persist=persist)
            self.capture.warning = None
            return {
                "device": self.settings.camera,
                "controls": result,
                "state": self.status(),
            }

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return self.status()
            if self.capture and self.capture.thread.is_alive():
                raise ValueError(
                    "Previous webcam capture is still closing. Reconnect the camera if it is stuck."
                )
            if not any(
                d["path"] == self.settings.camera and not d["virtual"]
                for d in devices()
            ):
                raise ValueError(
                    "Selected webcam is unavailable. Refresh devices and select a physical camera."
                )
            self.stop_event.clear()
            self.jpeg = None
            self.state.update(
                phase="loading",
                error=None,
                warning=None,
                frames=0,
                fps=0,
                processing_ms=0,
                frame_age_ms=0,
                capture_fps=0,
                capture_warning=None,
            )
            self.thread = threading.Thread(
                target=self._run, daemon=True, name="gpu-matting"
            )
            self.thread.start()
        return self.status()

    def stop(self):
        self.stop_event.set()
        with self.lock:
            if self.thread and self.thread.is_alive():
                self.state["phase"] = "stopping"
            self.jpeg = None
            self.preview_ready.notify_all()
        if self.thread:
            self.thread.join(timeout=5)
        return self.status()

    def _run(self):
        virtual = None
        attempted_virtual = False
        matting = None
        encoder = None
        try:
            with self.lock:
                settings = self.settings.model_copy()
            matting = Matting(settings.model)
            with self.lock:
                self.state["gpu"] = torch.cuda.get_device_name()
                settings = self.settings.model_copy()
            if self.stop_event.is_set():
                return
            self.capture = LatestCamera(settings)
            encoder = PreviewEncoder(self.publish_preview)
            period_start, count = time.perf_counter(), 0
            while not self.stop_event.is_set():
                captured_at, frame = self.capture.read()
                started = time.perf_counter()
                with self.lock:
                    settings = self.settings.model_copy()
                    background, version = self.background, self.background_version
                output, preview = matting.process(frame, settings, background, version)
                processed = time.perf_counter()
                if settings.virtual_camera and not attempted_virtual:
                    attempted_virtual = True
                    try:
                        import pyvirtualcam

                        virtual = pyvirtualcam.Camera(
                            width=output.shape[1],
                            height=output.shape[0],
                            fps=settings.fps,
                            device=settings.output_device,
                            fmt=pyvirtualcam.PixelFormat.RGB,
                        )
                        with self.lock:
                            self.state.update(
                                virtual_device=virtual.device, warning=None
                            )
                    except (RuntimeError, OSError, ValueError) as exc:
                        with self.lock:
                            self.state["warning"] = (
                                f"Virtual camera unavailable: {exc}. See README setup, then toggle output off and on to retry."
                            )
                if not settings.virtual_camera:
                    attempted_virtual = False
                    if virtual:
                        virtual.close()
                        virtual = None
                    with self.lock:
                        self.state.update(virtual_device=None, warning=None)
                if virtual:
                    try:
                        virtual.send(output)
                    except (RuntimeError, OSError, ValueError) as exc:
                        virtual.close()
                        virtual = None
                        with self.lock:
                            self.state.update(
                                virtual_device=None,
                                warning=f"Virtual camera disconnected: {exc}",
                            )
                now = time.perf_counter()
                if settings.preview == "original":
                    # Capture owns this BGR array; encoding it directly avoids
                    # copying the upload buffer and converting RGB back to BGR.
                    encoder.submit(frame, bgr=True, zoom=settings.zoom)
                else:
                    encoder.submit(preview)
                count += 1
                with self.lock:
                    self.state.update(
                        phase="stopping" if self.stop_event.is_set() else "running",
                        frames=self.state["frames"] + 1,
                        processing_ms=round((processed - started) * 1000, 1),
                        frame_age_ms=round((now - captured_at) * 1000, 1),
                        resolution=f"{output.shape[1]}×{output.shape[0]}",
                        capture_fps=self.capture.fps,
                        capture_warning=self.capture.warning,
                    )
                    if now - period_start >= 1:
                        self.state["fps"] = round(count / (now - period_start), 1)
                        count, period_start = 0, now
        except Exception as exc:
            log.exception("Camera pipeline failed")
            with self.lock:
                self.state.update(phase="error", error=str(exc))
        finally:
            if encoder:
                encoder.close()
            if self.capture:
                self.capture.close()
            if virtual:
                virtual.close()
            del matting
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            with self.lock:
                self.jpeg = None
                self.state.update(virtual_device=None, fps=0, capture_fps=0)
                if self.state["phase"] != "error":
                    self.state["phase"] = "stopped"
                self.preview_ready.notify_all()
