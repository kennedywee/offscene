# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Product Purpose

Local GPU webcam background removal, with a browser control panel and a virtual camera for other applications. The user wants quality comparable to NVIDIA Broadcast; parity has not been established.

## Capabilities and Constraints

The existing implementation uses Python, CUDA, Robust Video Matting, and V4L2 on Linux. It supports blur, solid color, image replacement, matte refinement, exposure control, and separate preview and output. Achieved frame rate depends on camera exposure, capture, processing, and delivery. Do not present requested frame rate as measured output.

## Brand Commitments

Offscene is the approved name, with the satin metal direction selected on 2026-10-01. The implementation reference is the [Offscene Paper file](https://app.paper.design/file/01M3TDHM1FNCEV9YEPAZK2J3WA/p-1-0), specifically “Studio · satin metal / background” and “Satin metal · identity & control system.” Earlier naming and color explorations are historical references.

Use Public Sans, graphite canvas (#151515), flat panels (#222222), neutral controls (#2C2C2C), text (#EEEEEE), and muted text (#ADADAD). The metal palette is highlight #F4F5F3, silver #D1D6D3, shade #B8BFBC, and ink #171717. Use the flat silver switch wordmark at UI sizes. Apply metal finishes only to the primary action, enabled toggle tracks, and slider thumbs; keep panels and text flat. The logo remains independent of camera state. Reserve green and red for status. Preserve visible keyboard focus and sufficient text and control contrast.

The studio prioritizes the preview, source controls, a horizontal background selector, and a right-hand adjustments panel with Background, Camera, and Processing tabs. Keep all current camera capabilities and distinguish requested frame rate from achieved capture, output, and preview rates. The command, Python package, API header, and cache environment variable use the Offscene namespace. Existing local settings and models remain in `.cache/`.

Trademark, package, and domain availability remain unverified; the name has known prior use as a game title. These limitations do not override the user's approved branding direction.

## Users

The primary audience is Linux enthusiasts who want precise camera control, confirmed by the user. Prioritize explicit device routing, achieved frame rates, exposure controls, and understandable processing choices.

## Operating Context

Select the physical camera, adjust the background, start processing, then select the virtual camera in the receiving application. Processing is local. Stopping releases the camera; closing the control panel does not stop output.

## Evidence on Hand

README.md records implementation capabilities and measured performance. NVIDIA Broadcast quality equivalence, universal application compatibility, pricing, and cross-platform support are unverified.
