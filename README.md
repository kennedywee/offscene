# Offscene

GPU webcam background removal for Linux and NVIDIA GPUs. A local control panel provides background blur, solid-color replacement, image replacement, edge cleanup, and a V4L2 virtual camera for meeting apps and OBS.

The studio follows the approved [Offscene satin metal direction in Paper](https://app.paper.design/file/01M3TDHM1FNCEV9YEPAZK2J3WA/p-1-0): graphite surfaces, a silver switch wordmark, Public Sans, and restrained metal finishes on interactive controls. Background effects sit below the preview; the Adjustments panel separates background and camera settings. Capture, output, and preview rates remain separate measured values.

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

Virtual webcams transmit opaque video, not an alpha channel. Use **Solid color** with `#00ff00` and OBS's Chroma Key filter for a transparent scene. **Original** and **Matte** only change the diagnostic preview; the virtual camera always receives the processed composite. **Mirror preview** is also preview-only.

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
- Select 720p or 1080p at 30/60 fps, or 1440p/4K at 30 fps. Stop the camera before changing capture settings or the model. A webcam that cannot supply the requested resolution produces an explicit error instead of silently falling back to 720p. Higher resolutions require more GPU and USB bandwidth; achieved fps may be below the requested rate.
- Capture and preview encoding each keep only the newest frame. A separate encoder thread streams JPEG frames over a same-origin WebSocket, with one frame in flight per client. Preview is up to 1920 × 1080 at the achieved processing rate; virtual output retains the full capture resolution. Hidden browser tabs suspend their preview connection. There is no 15 fps preview cap or per-frame HTTP polling.
- The panel separates **output fps**, **preview fps** (decoded by that browser), and **Processing ms** (preprocessing, inference, compositing, and transfer back to CPU). The status API also reports capture fps and frame age from OpenCV delivery through virtual send. These timings exclude sensor exposure and driver buffering and are not glass-to-glass latency.
- Use steady front lighting and enough separation from the background. Long exposure can reduce capture fps regardless of GPU speed.

**Exposure:** Auto lets the webcam adjust to the light, first shortening a stale manual exposure that would prevent the requested frame rate. Auto may still reduce capture fps in dim light. Motion priority sets a fixed exposure of 25 ms at 30 fps or 12.5 ms at 60 fps; this improves motion and consistency but can darken the image. Add light or use Auto if needed. Keep camera settings makes no exposure changes. Normal stop/error cleanup restores the original exposure mode and value; a forced process kill cannot run that cleanup.

On the RTX 3060 Ti, a repeated 1080p frame measured about 11 ms with ResNet50 / Balanced and 17.5 ms with ResNet50 / Detail before adding edge trimming. These are processing benchmarks, not webcam frame rates. The original EMEET Piko manual setting of 97.4 ms limited capture to approximately 10.3 fps; changing application language cannot overcome that sensor limit. The larger model offers a modest quality improvement, not guaranteed Broadcast parity.

## Verified on this machine

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

Settings and model files live in `.cache/` (override with `OFFSCENE_CACHE`). Uploaded backgrounds live in memory, so upload them again after a server restart. Settings persist; camera activation does not. Background uploads are limited to 12 MB and 24 megapixels.

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
