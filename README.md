# Offscene

GPU webcam background removal for Linux and NVIDIA GPUs. A local control panel provides background blur, solid-color replacement, image replacement, edge cleanup, and a V4L2 virtual camera for meeting apps and OBS.

The studio follows the approved [Offscene satin metal direction in Paper](https://app.paper.design/file/01M3TDHM1FNCEV9YEPAZK2J3WA/p-1-0): graphite surfaces, a silver switch wordmark, Public Sans, and restrained metal finishes on interactive controls. Background effects sit below the preview; the Adjustments panel has Background, Camera, and Processing tabs. Processing contains the matting model, analysis quality, and virtual-camera output controls. Capture, output, and preview rates remain separate measured values.

Offscene uses [Robust Video Matting](https://github.com/PeterL1n/RobustVideoMatting) with FP16 CUDA inference. Choose ResNet50 for higher quality or MobileNetV3 for lower GPU cost. It retains the model's recurrent state between frames. Matting, edge trimming, background blur, and compositing run on the GPU; webcam capture, JPEG decoding/preview encoding, and virtual-device delivery use the CPU. It is an independent project, not NVIDIA Broadcast, and equivalent quality has not been established. Hair, fast movement, objects, and difficult lighting can still produce artifacts.

## Run

Requirements: Linux, an NVIDIA GPU with a driver compatible with CUDA 12.8, a V4L2 webcam, and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run offscene
```

Open **http://127.0.0.1:8765**, select your webcam and effect, then click **Start camera**. Python 3.12 and the CUDA runtime dependencies are installed into the project's `.venv`; a system CUDA toolkit is unnecessary. The first dependency installation downloads several GB. The first start of each model downloads it from the official RVM release and verifies its SHA-256 hash (54 MB for ResNet50, 8 MB for MobileNetV3). Later runs work offline.

The camera starts only on request. **Stop camera** releases it and frees GPU model memory. Closing the control panel does **not** stop output, so calls continue while the panel is closed. Stop the camera in the panel or press Ctrl+C in the server terminal. Do not run multiple server instances against the same webcam. To change the HTTP port: `uv run offscene --port 8766`.

## Virtual camera setup

[pyvirtualcam](https://github.com/letmaik/pyvirtualcam) uses [v4l2loopback](https://github.com/v4l2loopback/v4l2loopback) on Linux. Install its DKMS package and the headers matching your **running** kernel. Offscene does not install kernel modules or ask for administrator credentials through the browser.

On Arch / Omarchy (the current machine already has matching kernel headers):

```sh
sudo pacman -S --needed v4l2loopback-dkms
sudo modprobe v4l2loopback video_nr=10 card_label=Offscene exclusive_caps=1
```

On Ubuntu / Debian:

```sh
sudo apt install v4l2loopback-dkms linux-headers-$(uname -r)
sudo modprobe v4l2loopback video_nr=10 card_label=Offscene exclusive_caps=1
```

Check that `/dev/video10` is free before choosing that number. If an existing loopback module serves another app, install your distribution's `v4l2loopback-ctl` utility and create an additional device with `sudo v4l2loopback-ctl add -n Offscene -x 1 /dev/video10` instead of unloading the module. Secure Boot systems may require signing/enrolling the DKMS module according to the distribution's instructions. If DKMS builds for a different installed kernel, boot that kernel or install headers for the running one.

Enable **Virtual camera** in Offscene, then select **Offscene** in Meet, Zoom, Discord, OBS, or another camera consumer. Start output before opening the consumer's camera list: `exclusive_caps=1` advertises capture capability once a producer attaches. Refresh the list or reopen the consumer if needed. A failed virtual-camera connection leaves the processed preview working and displays an error; toggle output off and on after fixing setup to retry. Run the `modprobe` command again after a reboot; the app does not change boot configuration.

Virtual webcams transmit opaque video, not an alpha channel. Use **Solid color** with `#00ff00` and OBS's Chroma Key filter for a transparent scene. **Original** and **Matte** only change the diagnostic preview; the virtual camera always receives the processed composite. **Mirror preview** is also preview-only and is off by default, so the Output preview has the same orientation as the virtual camera.

## Upgrade from Clearcam or a renamed directory

Stop the old server, open a terminal in the `offscene` directory, then run `uv sync` and `uv run offscene`. Sync removes the old project installation and installs the new command at the current path. If an activated shell still points to the old directory, run `deactivate` first. The Python package is now `offscene`, the API header is `X-Offscene: 1`, and the cache override is `OFFSCENE_CACHE`. Update scripts or environment settings that used the previous names. The default `.cache/` directory is unchanged, so settings and downloaded models survive the rename. Reload any open control-panel tabs.

If you moved the directory with an existing `.venv`, regenerate it to repair all dependency commands and activation scripts, which can retain absolute paths. This replaces only the disposable environment; `.cache/` is preserved:

```sh
uv venv --clear --python 3.12 --prompt offscene
uv sync --locked
uv run offscene
```

Loading an already-loaded kernel module does not change its camera label. If the old Clearcam device is the **only loopback device**, stop the camera and close all camera consumers, then recreate it:

```sh
sudo modprobe -r v4l2loopback && sudo modprobe v4l2loopback video_nr=10 card_label=Offscene exclusive_caps=1
cat /sys/devices/virtual/video4linux/video10/name
```

The final command should print `Offscene`. This reuses `/dev/video10`; no second device or kernel-module reinstall is needed. If the module is busy, close its consumers and retry; do not force removal. When other loopback devices exist, use the distribution's `v4l2loopback-ctl` utility to delete and recreate only the old device instead:

```sh
sudo v4l2loopback-ctl delete /dev/video10 && sudo v4l2loopback-ctl add -n Offscene -x 1 /dev/video10
```

If you previously added persistent configuration under `/etc/modprobe.d/` or `/etc/modules-load.d/`, update only the old app's entry and remove duplicate device definitions. This project does not create those files. The checked machine had no persistent loopback configuration. See the upstream [device-management instructions](https://github.com/v4l2loopback/v4l2loopback#dynamic-device-management) for shared-module setups.

## Quality and performance

- **Balanced** runs the matting network at a 512-pixel longest edge, then refines at the full camera resolution. **Detail** uses 768 pixels and more GPU time; it is not guaranteed to improve every shot.
- **Background suppression** rejects faint background regions. **Trim edge** erodes the matte by 0–4 output pixels to reduce narrow fringes. Start around 8–12% suppression and 1 px trim. Raising either too far removes hair and translucent foreground detail. Neither can reliably fix a whole object confidently mistaken for the subject.
- Select 720p or 1080p at 30/60 fps, or 1440p/4K at 30 fps. Stop the camera before changing the source, resolution, frame rate, or model. Camera image controls and exposure can be adjusted live. A webcam that cannot supply the requested resolution produces an explicit error instead of silently falling back to 720p. Higher resolutions require more GPU and USB bandwidth; achieved fps may be below the requested rate.
- Capture and preview encoding each keep only the newest frame. A separate encoder thread streams JPEG frames over a same-origin WebSocket. WebSocket deflate is disabled because the JPEGs are already compressed. The browser receives the next frame while decoding the current one, with at most one frame waiting to decode; it does not wait an extra display refresh before requesting a frame. Original preview encodes the captured BGR frame directly, avoiding an RGB copy and conversion back to BGR. Preview and virtual output both use the full selected resolution, including 2560 × 1440 and 3840 × 2160. The browser fits the image to the available space without reducing the streamed image resolution. Preview uses JPEG compression; it is not a lossless pixel reference. Hidden browser tabs suspend their preview connection. There is no 15 fps preview cap or per-frame HTTP polling.
- The panel separates **output fps**, **preview fps** (decoded by that browser), and **Processing ms** (preprocessing, inference, compositing, and transfer back to CPU). The status API also reports capture fps and frame age from OpenCV delivery through virtual send. These timings exclude sensor exposure and driver buffering and are not glass-to-glass latency.
- Use steady front lighting and enough separation from the background. Long exposure can reduce capture fps regardless of GPU speed.

**Camera looks:** Adjustments → Camera offers Natural, Cinematic, Apple-inspired, Professional, Polished, and TikTok. These local presets adjust supported brightness, contrast, saturation, sharpness, gamma, and hue controls relative to the camera’s reported defaults, respecting its range and step size. Exposure, white balance, focus, zoom, and background remain as configured. Results depend on the webcam and lighting; Apple-inspired does not reproduce Apple’s image processing, and Polished does not retouch faces. **Undo** restores the previous image settings for the latest look change in this session. A manual camera adjustment switches the label to Custom settings and clears Undo. The selected look and its values are saved; reset clears the selection.

Preset recipes live in `offscene/camera_presets.py`. Applying a recipe uses the same validated, transactional camera-control path as manual edits. A future LLM can select or propose settings through that path; this version has no LLM dependency, API calls, or image uploads.

**Digital zoom:** The Camera tab provides a 1×–2× center crop. It applies before GPU matting, keeps the selected output dimensions, and uses matching framing in Original, Output, Matte, and the virtual camera. Original-preview resizing runs on the separate encoder thread. Digital zoom enlarges a crop; it does not add sensor detail. This replaces the hardware Zoom slider because the EMEET Piko accepted 100–150 control values at 4K without changing the captured image.

**Reset camera:** In Adjustments → Camera, use **Reset** beside Look while the camera is running. It restores all supported writable exposure, white-balance, lens, and image controls to the defaults reported by that camera, including its automatic modes, and returns digital zoom to 1×. The hardware defaults replace only the selected camera’s saved control profile; resolution, processing, and backgrounds stay as configured. If a control write or saving fails, Offscene attempts to restore the previous hardware values and reports the error.

**Exposure:** Auto lets the webcam adjust to the light, first shortening a stale manual exposure that would prevent the requested frame rate. Auto may still reduce capture fps in dim light. Motion priority sets a fixed exposure of 25 ms at 30 fps or 12.5 ms at 60 fps; this improves motion and consistency but can darken the image. Add light or use Auto if needed. Manual unlocks the shutter slider and numeric input in milliseconds; the panel warns when the shutter exceeds the frame interval. Keep current exposure makes no exposure changes. Normal stop/error cleanup restores the original exposure mode and value; a forced process kill cannot run that cleanup.

**Camera controls:** Start the camera and open **Adjustments → Camera** for supported shutter, gain, white balance, focus, zoom, anti-flicker, brightness, contrast, saturation, sharpness, gamma, hue, and backlight controls. Sliders and numeric inputs use ranges reported by the driver; unsupported controls are omitted, and manual white balance/focus are disabled until their Auto switch is off. Camera-specific gain and focus values use driver units, not ISO or distance. Changes are saved per camera device in `.cache/settings.json`, reapplied on start, and the pre-session hardware values are restored on normal stop. Failed changes are rolled back. Controls use standard Linux V4L2 ioctls without shell commands or administrator access.

On the RTX 3060 Ti, a repeated 1080p frame measured about 11 ms with ResNet50 / Balanced and 17.5 ms with ResNet50 / Detail before adding edge trimming. These are processing benchmarks, not webcam frame rates. The original EMEET Piko manual setting of 97.4 ms limited capture to approximately 10.3 fps; changing application language cannot overcome that sensor limit. The larger model offers a modest quality improvement, not guaranteed Broadcast parity.

## Saved scenes

A scene stores a named camera setup: the physical camera device, resolution, requested frame rate, exposure mode, digital zoom, Look label, that camera’s saved control profile, matting model, analysis quality, background suppression, edge trim, background effect, blur amount, solid color, and a copy of the current image when **Image** is selected. Scenes do not include whether the camera is running, virtual-camera enablement or output device, preview mode, preview mirroring, or Look **Undo** history; applying a scene keeps those as they are. Automatic exposure, white balance, and focus are saved as configured modes, not as momentary camera readings.

Use the scene controls above the source selectors:

- **Save scene** stores the current setup under a new name. Names are trimmed, 1–60 characters, and unique ignoring case. Saving works while the camera is running and does not interrupt output.
- Selecting a scene only shows its summary: camera, resolution, requested fps, background, and model. The frame rate is the requested rate, not a measurement.
- **Apply scene** requires the camera to be stopped and never starts it. Offscene first validates the scene’s settings, that its camera is connected, and its background image; any failure leaves the current settings and background unchanged. Camera controls are written to the hardware through the normal validated path when you press **Start camera**. A scene never substitutes another camera: connect the saved one and refresh cameras.
- The applied scene’s name stays visible. **Modified** appears when included settings change, including a new background image; preview and output preferences do not count. Changes never overwrite a scene automatically.
- The **⋯** menu offers **Update scene** (replace the selected scene with the current setup, after confirmation), **Rename**, and **Delete** (after confirmation). Deleting a scene does not change the current setup.

Scenes live beside the settings in the cache directory (`.cache/`, or `OFFSCENE_CACHE`):

```text
.cache/
├── settings.json
├── scenes.json                 # versioned scene metadata and the last applied scene
├── scenes/
│   └── <scene-id>/
│       └── background-<revision>.png
└── models/
```

Scene IDs are generated; names are never used as paths. Metadata is written to a temporary file and atomically replaced. A new background image is written and decoded back before the metadata refers to it, and the replaced or deleted image is removed only after the metadata is saved, so a failed save or update leaves the previous scene usable. Each update writes a new `background-<revision>.png` for that reason. Stored images are lossless PNG copies of the uploaded image after the existing 12 MB, 24-megapixel, and 3840 × 2160 decoding limits.

After a restart, Offscene restores the last applied scene’s image when the saved settings still match that scene, and leaves the camera off. If that image is missing or unreadable, Offscene starts with Blur, reports which scene is affected, and does not show the scene as applied; use **Update scene** with a new image to repair it. If `scenes.json` itself is unreadable, Offscene reports the path, does not overwrite the file, and disables scene actions until it is repaired or moved; camera and background controls keep working.

## Verified on this machine

Saved scenes were checked on 2026-10-02 with the EMEET Piko / RTX 3060 Ti against a separate cache directory. Blur, solid-color, and image scenes were saved and applied while stopped, then started; processed 2560 × 1440 and 1280 × 720 frames matched each scene’s background, and a saved Cinematic look reached the hardware on start. Scenes and an image survived a server restart with the camera off. Saving while running continued output; applying while running was rejected in the UI and API. Modified, Update, Look Undo, naming limits (empty, duplicate, 60/61 characters), HTML-like names rendered as text, rename, cancel, and delete were exercised. A missing camera, a missing or corrupt image (including at startup), invalid scene settings, unreadable metadata, and simulated metadata/settings write failures each left the previous settings, scenes, and files unchanged. Keyboard selection, menu navigation, dialog focus trapping, Escape, and focus restoration were checked at 1920 × 1080 and 375 × 812 without horizontal overflow. The v4l2loopback module was not loaded during this check, so virtual-camera delivery of applied scenes was not re-verified.

Camera reset was verified from Manual and Auto exposure on 2026-10-01: all 16 supported controls matched their reported defaults, saved defaults survived a server restart, and a simulated save failure restored every previous hardware value. The digital zoom slider was exercised in the browser; live 4K feature matching measured 1.501× and 2.000× in Original preview, with corresponding zoom in the virtual output and aligned preview/output framing at 2×. Reset returned digital zoom to 1×.

The Original-preview update was checked on 2026-10-01 with the EMEET Piko / RTX 3060 Ti, ResNet50 / Detail, Auto exposure, blur 74, suppression 0.2, edge trim 2, a full-resolution browser preview, and a separate virtual-camera reader. At 4K, the three-minute consumer run averaged 29.143 fps; the browser averaged 28.573 fps over an overlapping 168-second window. At 1440p, the 60-second consumer run averaged 30.014 fps and the browser averaged 30.016 fps over 74 seconds. Both runs had no read errors or consecutive identical sampled frames. These measurements fix the large Original-preview/output gap but do not establish locked 4K30 under load. The direct BGR Original encoder and the RGB processed-preview encoder produced byte-identical JPEGs to the previous conversion path at 4K, retaining JPEG quality 90.

Full-resolution preview and live camera controls were checked on 2026-10-01. Separate 60-second runs with ResNet50 / Detail, Motion priority, blur, a visible full-resolution browser preview, and a virtual-camera reader delivered 29.966 fps at 3840 × 2160 and 30.015 fps at 2560 × 1440, without read errors or consecutive identical sampled frames. Browser image dimensions matched each output resolution. Live control readback, invalid-value rejection, Auto/manual dependencies, rollback, saved-profile reload, and hardware restoration passed. Desktop (1920 × 1080) and mobile (375 × 812) controls had no horizontal overflow; custom dropdown keyboard selection, shutter numeric entry, the long-exposure warning, and manual white balance were exercised.

The 2026-10-01 frame-transfer optimization was verified on the EMEET Piko / RTX 3060 Ti with two separate three-minute runs after a ten-second warmup. Both used ResNet50 / Detail, Motion priority, blur 74, suppression 0.2, edge trim 2, a visible 1080p browser preview, and a separate OpenCV reader of the full-resolution virtual camera:

| Resolution | Virtual-camera consumer | Browser preview average | Processing mean / p95 | Consumer frame interval p95 / maximum |
| --- | --- | --- | --- | --- |
| 3840 × 2160 | 30.015 fps | 30.00 fps | 27.57 / 31.4 ms | 39.86 / 48.92 ms |
| 2560 × 1440 | 30.015 fps | 30.03 fps | 18.89 / 20.0 ms | 37.21 / 43.49 ms |

Neither run produced a read error or consecutive identical sampled frames. A separate 45-second 4K run with Auto exposure delivered 30.019 fps to the virtual-camera reader. These measurements establish sustained 30 fps under the tested settings, not a guarantee under every GPU workload or lighting condition; Auto exposure can still reduce capture fps in dim light.

RGB pixels now enter a reusable pinned CPU buffer, transfer as bytes, and convert to FP16 on the GPU. The GPU also packs output pixels before download, avoiding full-frame CPU repacking in virtual output and preview. The model, analysis resolution, and image-processing operations are unchanged. Exact pixel comparisons against the previous implementation passed for both models across 720p–4K, blur/color/image backgrounds, and Output/Original/Matte previews; retained preview frames also stayed unchanged when the upload buffer was reused. Camera exposure and saved user settings were preserved.

The Offscene rebrand was checked on 2026-10-01: the old project installation and build artifacts were replaced, the virtual environment was regenerated for the renamed directory, and `/dev/video10` reported `Offscene`. Live 1080p preview, a separate ten-frame virtual-camera read, blur/color/image controls, image upload, Original/Matte preview, keyboard adjustment tabs, and stop/restart passed. Desktop (1920 × 1080), mobile (375 × 812), and landscape (812 × 375) had no horizontal overflow. The renamed API header was accepted; missing and old headers were rejected. Settings were restored and the camera stopped after verification. Python compilation, Ruff, JavaScript syntax checks, and wheel/source builds passed.

Live 720p and 1080p capture, blur/color/image replacement, background upload through the browser, matte preview, and stop/restart were exercised. After installing v4l2loopback, a separate OpenCV consumer read ten processed 720p frames from `/dev/video10`; switching the panel to Original preserved the processed virtual output. Missing-device errors kept the preview running. Desktop (1920 × 1080) and mobile (375 × 812) UI checks passed without horizontal overflow. Ruff, JavaScript syntax checks, Python compilation, and wheel/source builds passed. Specific meeting applications and NVIDIA Broadcast quality parity have not been tested.

After the preview update, short live runs with ResNet50, Motion priority, background blur, edge trimming, and virtual output enabled measured:

| Capture request | Analysis detail | Preview stream measured | Virtual-camera frame verified |
| --- | --- | --- | --- |
| 1080p / 30 fps | Detail | 27 fps | 1920 × 1080 |
| 1080p / 60 fps | Balanced | 44 fps | 1920 × 1080 |
| 1440p / 30 fps | Balanced | 15 fps | 2560 × 1440 |
| 4K / 30 fps | Balanced | 15 fps | 3840 × 2160 |

These are measurements from this EMEET Piko / RTX 3060 Ti setup, not guaranteed rates. Capture itself was approximately 15 fps in the higher-resolution runs. Preview remained 1080p while virtual output retained 1440p/4K. Each run verified exposure restoration and preview disconnection on stop. A foreign-origin WebSocket was rejected with HTTP 403. Image quality was inspected on a sample frame, without a ground-truth matte or Broadcast comparison.

A subsequent 1080p/60 investigation isolated two capture bottlenecks: the retained 97.4 ms exposure limited the raw camera to about 10 fps, and a single V4L2 buffer limited OpenCV capture to 43.4 fps even with a 12.5 ms exposure. Three driver buffers measured 59.3 fps with the same exposure, so capture now uses three buffers while retaining only the latest decoded frame for processing. With MobileNetV3 / Balanced, color replacement, no edge trim, Motion priority, and virtual output enabled, the updated pipeline measured roughly 56–59 fps after warmup. With the browser preview visible, a separate 1080p virtual-camera consumer measured 51.8 fps; the browser preview subsequently reported 54.1 fps. These short runs show the benefit on this machine, not a guarantee of sustained 60 fps. Frame-age telemetry excludes time spent in the driver buffers.

## Data and access

The server binds only to `127.0.0.1`; no video is uploaded to a cloud service. The UI has no external fonts, scripts, or analytics. HTTP API reads and camera controls require a custom request header, and preview WebSockets require an exact matching Origin. No cross-origin permissions are granted. This protects against drive-by websites, not other processes running as your user. Do not expose the server through a public proxy.

Settings, saved scenes, scene background images, and model files live in `.cache/` (override with `OFFSCENE_CACHE`); see [Saved scenes](#saved-scenes). Uploaded backgrounds otherwise live in memory: after a server restart, only an image saved in the last applied, unmodified scene is restored. Settings persist; camera activation does not. Background uploads are limited to 12 MB and 24 megapixels.

## Troubleshooting

- **CUDA unavailable:** check `nvidia-smi` and run `uv sync`. Offscene reports an error rather than silently falling back to CPU inference.
- **Cannot open webcam:** close apps holding the physical camera; check device permissions and click Refresh. Use the virtual camera in call apps while Offscene owns the physical one.
- **No frames / USB disconnect:** stop, reconnect, refresh, and start again. A stalled capture thread prevents a second capture from opening over it.
- **Low fps:** compare capture, output, and preview rates. For low capture fps, try Motion priority and more light. If processing is the limit, try Balanced or MobileNetV3. If only preview is slow, keep the browser visible and check its GPU acceleration. Refresh old control-panel tabs after upgrading.
- **Model download failure:** confirm GitHub access and retry Start. Incomplete downloads are discarded; a checksum mismatch prevents loading.

## License and credits

GPL-3.0; see [LICENSE](LICENSE). The separately downloaded pretrained model is from [PeterL1n/RobustVideoMatting v1.0.0](https://github.com/PeterL1n/RobustVideoMatting/releases/tag/v1.0.0), whose code and models are published under GPL-3.0. Preserve upstream licensing and attribution when redistributing. Other dependencies retain their respective licenses.

RVM: Shanchuan Lin, Linjie Yang, Imran Saleemi, and Soumyadip Sengupta, *Robust High-Resolution Video Matting with Temporal Guidance*, WACV 2022. [Paper and project](https://peterl1n.github.io/RobustVideoMatting/).

The interface bundles [Public Sans](https://github.com/uswds/public-sans), under the SIL Open Font License. The font and its license are served locally from `offscene/static/fonts/`.
