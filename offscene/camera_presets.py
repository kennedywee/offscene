"""Local camera looks, expressed as offsets from each driver's defaults."""

IMAGE_CONTROLS = ("brightness", "contrast", "saturation", "sharpness", "gamma", "hue")
PRESETS = {
    "natural": {
        "label": "Natural",
        "description": "Default image settings for a neutral starting point.",
        "offsets": {},
    },
    "cinematic": {
        "label": "Cinematic",
        "description": "Deeper contrast, muted color, and softer detail.",
        "offsets": {
            "brightness": -0.03,
            "contrast": 0.10,
            "saturation": -0.10,
            "sharpness": -0.04,
            "gamma": -0.02,
        },
    },
    "apple": {
        "label": "Apple-inspired",
        "description": "Bright, restrained color and gentle contrast. An interpretation, not Apple's camera processing.",
        "offsets": {
            "brightness": 0.04,
            "contrast": -0.02,
            "saturation": 0.02,
            "sharpness": -0.02,
            "gamma": 0.02,
        },
    },
    "professional": {
        "label": "Professional",
        "description": "Balanced color with a small lift in brightness and definition.",
        "offsets": {"brightness": 0.02, "contrast": 0.04, "sharpness": 0.02},
    },
    "polished": {
        "label": "Polished",
        "description": "Brighter midtones, gentle contrast, and softer detail. No face retouching.",
        "offsets": {
            "brightness": 0.04,
            "contrast": -0.04,
            "saturation": 0.02,
            "sharpness": -0.08,
            "gamma": 0.03,
        },
    },
    "tiktok": {
        "label": "TikTok",
        "description": "Brighter color, stronger contrast, and crisp detail.",
        "offsets": {
            "brightness": 0.06,
            "contrast": 0.07,
            "saturation": 0.10,
            "sharpness": 0.04,
            "gamma": 0.02,
        },
    },
}


def preset_values(name, controls):
    if name not in PRESETS:
        raise ValueError("Choose an available camera look.")
    values = {}
    for key in IMAGE_CONTROLS:
        control = controls.get(key)
        if not control or control["readonly"] or control["inactive"]:
            continue
        low, high, step = control["min"], control["max"], control["step"]
        target = control["default"] + PRESETS[name]["offsets"].get(key, 0) * (
            high - low
        )
        steps = max(0, min((high - low) // step, round((target - low) / step)))
        values[key] = low + steps * step
    if not values:
        raise ValueError("This camera does not expose image controls for these looks.")
    return values
