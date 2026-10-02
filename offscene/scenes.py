"""Saved scenes: versioned local metadata and per-scene background images."""

import io
import json
import logging
import os
import re
import secrets
import unicodedata

import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError

log = logging.getLogger(__name__)
VERSION = 1
UPLOAD_LIMIT = 12 * 1024 * 1024
# Stored PNGs are lossless copies of an already decoded, at most 3840 × 2160
# image, so they may exceed the upload limit. Bound them by raw pixel size.
ASSET_LIMIT = 3840 * 2160 * 3 + 1024 * 1024
FIELDS = (
    "camera", "resolution", "fps", "exposure", "zoom", "camera_preset", "model",
    "quality", "effect", "blur", "color", "cleanup", "shrink",
)
SCENE_ID = re.compile(r"[0-9a-f]{16}")
ASSET = re.compile(r"background-[0-9a-f]{8}\.png")


class SceneConflict(ValueError):
    """The request is valid but cannot run in the current state."""


def decode_image(data, formats=None):
    """Decode untrusted image bytes within the upload pixel limits, as BGR."""
    try:
        # Inspect dimensions before full decoding to bound decompression memory.
        with Image.open(io.BytesIO(data), formats=formats) as source:
            if source.width * source.height > 24_000_000:
                raise ValueError("Choose an image with at most 24 megapixels.")
            source.load()
            source.thumbnail((3840, 2160))
            return cv2.cvtColor(np.asarray(source.convert("RGB")), cv2.COLOR_RGB2BGR)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Choose a valid JPEG, PNG, or WebP image.") from exc


def scene_settings(settings):
    """Included settings, with only the selected camera's control profile."""
    values = settings.model_dump(include=set(FIELDS))
    values["camera_profile"] = dict(settings.camera_controls.get(settings.camera, {}))
    return values


def clean_name(name, scenes, exclude=None):
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        raise ValueError("Enter a scene name.")
    if len(name) > 60:
        raise ValueError("Use 60 characters or fewer.")
    if any(unicodedata.category(c) == "Cc" for c in name):
        raise ValueError("Scene names cannot contain control characters.")
    for scene in scenes:
        if scene["id"] != exclude and scene["name"].casefold() == name.casefold():
            raise ValueError(f"A scene named “{scene['name']}” already exists.")
    return name


def _parse(data):
    if (
        not isinstance(data, dict)
        or data.get("version") != VERSION
        or not isinstance(data.get("scenes"), list)
    ):
        raise ValueError(f"expected version {VERSION} scene metadata")
    scenes, ids = data["scenes"], set()
    for scene in scenes:
        if (
            not isinstance(scene, dict)
            or not isinstance(scene.get("id"), str)
            or not SCENE_ID.fullmatch(scene["id"])
            or scene["id"] in ids
            or not isinstance(scene.get("name"), str)
            or not 0 < len(scene["name"].strip()) <= 60
            or not isinstance(scene.get("settings"), dict)
            or not (
                scene.get("background") is None
                or isinstance(scene["background"], str)
                and ASSET.fullmatch(scene["background"])
            )
        ):
            raise ValueError("invalid scene record")
        ids.add(scene["id"])
    applied = data.get("applied")
    if applied is not None and (
        not isinstance(applied, dict)
        or applied.get("id") not in ids
        or not isinstance(applied.get("background_changed"), bool)
    ):
        raise ValueError("invalid applied scene")
    return scenes, applied


class SceneStore:
    """Scene metadata in scenes.json and assets under scenes/<scene-id>/.

    Callers serialize mutations. Only names recorded in valid metadata are
    deleted; user-entered names never become paths.
    """

    def __init__(self, root):
        self.path = root / "scenes.json"
        self.assets = root / "scenes"
        self.scenes = []
        self.applied = None
        self.error = None
        self.load()

    def load(self):
        try:
            self.scenes, self.applied = _parse(json.loads(self.path.read_text()))
            self.error = None
        except FileNotFoundError:
            self.scenes, self.applied, self.error = [], None, None
        except (OSError, ValueError) as exc:
            # Never overwrite unreadable metadata; block mutations instead.
            self.scenes, self.applied = [], None
            self.error = (
                f"Saved scenes are unavailable because {self.path} is unreadable "
                f"({exc}). Repair or move that file; camera controls still work."
            )

    def require(self):
        if self.error:
            self.load()
        if self.error:
            raise SceneConflict(self.error)
        return self

    def get(self, scene_id):
        for scene in self.scenes:
            if scene["id"] == scene_id:
                return scene
        raise LookupError("That scene no longer exists. Choose another scene.")

    def commit(self, scenes, applied):
        """Atomically replace scenes.json, then adopt the new state in memory."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        data = {"version": VERSION, "applied": applied, "scenes": scenes}
        try:
            with open(temporary, "w", encoding="utf-8") as file:
                json.dump(data, file, indent=2, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self.scenes, self.applied = scenes, applied

    def write_asset(self, scene_id, image):
        """Write and verify a new PNG; it is unreferenced until metadata commits."""
        ok, encoded = cv2.imencode(".png", image)
        if not ok:
            raise ValueError("Could not encode the background image.")
        directory = self.assets / scene_id
        directory.mkdir(parents=True, exist_ok=True)
        name = f"background-{secrets.token_hex(4)}.png"
        temporary = directory / f".{name}.tmp"
        try:
            with open(temporary, "wb") as file:
                file.write(encoded.tobytes())
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, directory / name)
            self.read_asset(scene_id, name)
        except BaseException:
            temporary.unlink(missing_ok=True)
            self.remove_asset(scene_id, name)
            raise
        return name

    def read_asset(self, scene_id, name):
        with open(self.assets / scene_id / name, "rb") as file:
            data = file.read(ASSET_LIMIT + 1)
        if len(data) > ASSET_LIMIT:
            raise ValueError("The scene background image is too large.")
        return decode_image(data, formats=("PNG",))

    def remove_asset(self, scene_id, name):
        if not name:
            return
        try:
            (self.assets / scene_id / name).unlink(missing_ok=True)
            (self.assets / scene_id).rmdir()
        except OSError:
            # A non-empty directory or an already removed file is harmless.
            pass
