"""Loopback-only control panel. Frames never leave this process over the internet."""

import argparse
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import uvicorn
from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .camera_presets import PRESETS
from .engine import Engine, devices
from .scenes import UPLOAD_LIMIT, SceneConflict, decode_image

engine = Engine()
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app):
    yield
    engine.stop()


app = FastAPI(title="Offscene", lifespan=lifespan)
app.add_middleware(
    TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"]
)


@app.middleware("http")
async def local_control(request: Request, call_next):
    # A custom header prevents websites from driving a local webcam via forms or
    # cross-origin fetch. No CORS permission is granted. Also protect frame reads.
    if (
        request.url.path.startswith("/api/")
        and request.headers.get("x-offscene") != "1"
    ):
        return JSONResponse(
            {"detail": "Use the local Offscene control panel."}, status_code=403
        )
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
    )
    return response


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    return engine.status()


@app.get("/api/devices")
def cameras():
    return devices()


class CameraControlPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, StrictInt] = Field(default_factory=dict)
    exposure: Literal["auto", "motion", "manual", "preserve"] | None = None


@app.get("/api/camera-controls")
def camera_controls():
    try:
        return engine.camera_control_info()
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.patch("/api/camera-controls")
def camera_control_update(patch: CameraControlPatch):
    try:
        return engine.update_camera_controls(patch.values, patch.exposure)
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/camera-presets")
def camera_presets():
    return [
        {"id": key, "label": value["label"], "description": value["description"]}
        for key, value in PRESETS.items()
    ]


@app.post("/api/camera-presets/{name}")
def camera_preset_apply(name: str):
    try:
        return engine.undo_camera_preset() if name == "undo" else engine.apply_camera_preset(name)
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/camera-controls/reset")
def camera_control_reset():
    try:
        return engine.reset_camera_controls()
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.patch("/api/settings")
def settings(patch: dict):
    try:
        return engine.update(patch)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/start")
def start():
    try:
        return engine.start()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/stop")
def stop():
    return engine.stop()


@app.get("/api/frame")
def frame():
    with engine.lock:
        jpeg = engine.jpeg
    return (
        Response(jpeg, media_type="image/jpeg") if jpeg else Response(status_code=204)
    )


@app.websocket("/api/preview")
async def preview(socket: WebSocket):
    # WebSockets cannot set the API's custom header. Require exact same origin.
    if socket.headers.get("origin") != f"http://{socket.headers.get('host')}":
        await socket.close(code=1008)
        return
    await socket.accept()
    sequence = -1
    try:
        while True:
            current, jpeg = await asyncio.to_thread(engine.wait_preview, sequence)
            if not jpeg or current == sequence:
                if engine.status()["phase"] not in ("running", "loading"):
                    break
                await asyncio.sleep(0.05)
                continue
            sequence = current
            await socket.send_bytes(jpeg)
            # Keep at most one frame in flight; slow clients get the newest next.
            await asyncio.wait_for(socket.receive_text(), timeout=5)
    except (WebSocketDisconnect, TimeoutError):
        pass
    finally:
        try:
            await socket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass


@app.post("/api/background")
async def background(file: Annotated[UploadFile, File()]):
    data = await file.read(UPLOAD_LIMIT + 1)
    await file.close()
    if len(data) > UPLOAD_LIMIT:
        raise HTTPException(413, "Choose an image smaller than 12 MB.")
    try:
        decoded = await asyncio.to_thread(decode_image, data)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        return await asyncio.to_thread(engine.set_background, decoded)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class SceneName(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


def scene_action(action, *args):
    try:
        return action(*args)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except SceneConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/scenes")
def scenes():
    return engine.status()["scenes"]


@app.post("/api/scenes")
def scene_create(body: SceneName):
    return scene_action(engine.save_scene, body.name)


@app.put("/api/scenes/{scene_id}")
def scene_update(scene_id: str):
    return scene_action(engine.save_scene, None, scene_id)


@app.patch("/api/scenes/{scene_id}")
def scene_rename(scene_id: str, body: SceneName):
    return scene_action(engine.rename_scene, scene_id, body.name)


@app.delete("/api/scenes/{scene_id}")
def scene_delete(scene_id: str):
    return scene_action(engine.delete_scene, scene_id)


@app.post("/api/scenes/{scene_id}/apply")
def scene_apply(scene_id: str):
    return scene_action(engine.apply_scene, scene_id)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main():
    parser = argparse.ArgumentParser(
        description="Offscene — NVIDIA GPU webcam background removal"
    )
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    print(
        f"Open http://127.0.0.1:{args.port}. Camera stays off until you press Start camera."
    )
    # JPEG frames are already compressed. Deflating them again blocks the event
    # loop, especially for detailed full-resolution Original previews.
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        access_log=False,
        ws_per_message_deflate=False,
    )


if __name__ == "__main__":
    main()
