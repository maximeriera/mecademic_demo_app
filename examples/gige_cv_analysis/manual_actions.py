"""Manual actions: the "Inspect once" button of the Manual tab (listed in config.yaml)."""

from __future__ import annotations


def inspect_once(devices, context=None):
    """Analyse the frame of the next trigger pulse and log what blob_analysis found.

    The analysis runs on every pulse, so ``analyze_once()`` waits for the next
    result. It raises TimeoutError if no pulse comes within the timeout: check
    the laser driver's sync output and the camera's trigger line.
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
