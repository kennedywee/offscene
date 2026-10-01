"""GPU matting, latest-frame capture, and optional V4L2 output."""

import hashlib
import logging
import os
import queue
import threading
import time
import urllib.request
from pathlib import Path
from typing import Literal

import cv2
import torch
import torch.nn.functional as F
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .camera_controls import CONTROLS, CameraControls

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

    def submit(self, frame):
        try:
            self.frames.get_nowait()
        except queue.Empty:
            pass
        self.frames.put_nowait(frame)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                frame = self.frames.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                ok, encoded = cv2.imencode(
                    ".jpg",
                    cv2.cvtColor(frame, cv2.COLOR_RGB2BGR),
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
        shape = (height, width, ratio)
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
        elif settings.preview == "original":
            preview = rgb.copy()
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
        # Uploaded images live only in memory; never silently start a camera.
        if self.settings.effect == "image":
            self.settings.effect = "blur"
        self.thread = None
        self.capture = None
        self.stop_event = threading.Event()
        self.jpeg = None
        self.jpeg_sequence = 0
        self.background = None
        self.background_version = 0
        self.state = {
            "phase": "stopped",
            "error": None,
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
        with self.lock:
            return {
                **self.state,
                "settings": self.settings.model_dump(),
                "has_background": self.background is not None,
            }

    def update(self, patch):
        if "camera_controls" in patch:
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
            self._save_settings(new)
            self.settings = new
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

    def update_camera_controls(self, values, exposure=None):
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
            profile.update(values)
            new = self.settings.model_copy(
                update={"camera_controls": profiles, "exposure": mode}
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
            self.capture.warning = None
            return {"device": new.camera, "controls": result, "state": self.status()}

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
