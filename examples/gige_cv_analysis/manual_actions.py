"""Manual actions: the "Inspect once" button of the Manual tab (listed in config.yaml)."""

from __future__ import annotations


def inspect_once(devices, context=None):
    """Analyse the next software-triggered frame and log what blob_analysis found.

    ``analyze_once()`` waits for the next result while the analysis triggers
    the camera at its rate_hz, and triggers and analyses one frame itself when
    it is stopped.
    """
    vision = devices["vision"]
    if context is None:
        return vision.analyze_once()
    with context.step("Inspect"):
        result = vision.analyze_once(timeout=5.0)
    data = result["data"]
    if data.get("found"):
        vision.logger.info(f"Inspect once: blob at x={data['x_px']} y={data['y_px']}, "
                           f"{data['area_px']} px ({result['analysis_ms']} ms).")
    else:
        vision.logger.info(f"Inspect once: no blob (threshold {data.get('threshold')}).")
    return result


def get_manual_actions():
    return {"inspect_once": inspect_once}
