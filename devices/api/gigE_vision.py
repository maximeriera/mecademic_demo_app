import os
import logging
import argparse
import time
import signal
import numpy as np
from genicam import genapi
from genicam.gentl import TimeoutException
from harvesters.core import Callback, Harvester

from core.LogSetup import get_module_logger

# NOTE: OpenCV is only needed by the standalone live-view demo at the bottom of
# this file, not by GigECamera itself. It is imported there so that using the
# camera from the application does not require opencv-python to be installed.

# Module logger instead of logging.basicConfig(). The old basicConfig() call
# ran at import and reconfigured the *root* logger process-wide, so merely
# having a camera in config.yaml duplicated every application record onto
# stderr in a different format. Records from here now reach combined.log and
# stdout through the mecademic.* hierarchy like everything else.
logger = get_module_logger("device.gige.transport")


class CameraConnectionError(RuntimeError):
    """Raised when a camera connection attempt fails."""


class _IncompleteFrameCounter(Callback):
    """Counts the frames harvesters discards because packets went missing on the way."""

    def __init__(self, camera: "GigECamera"):
        self.camera = camera

    def emit(self, context=None) -> None:
        self.camera.incomplete_frames += 1


#: GenICam node interfaces, in the order a node is matched against them. The
#: first five are the value features the app reads and writes.
_NODE_TYPES = ("IBoolean", "IInteger", "IFloat", "IEnumeration", "IString", "ICommand", "ICategory")
_VALUE_TYPES = _NODE_TYPES[:5]
#: GenICam visibility levels, from fewest features to most ("Invisible" is never shown).
_VISIBILITIES = ("Beginner", "Expert", "Guru", "Invisible")


def _node_type(node) -> str:
    """``"IFloat"``, ``"IEnumeration"``... for a GenICam node."""
    for name in _NODE_TYPES:
        cls = getattr(genapi, name, None)
        if cls is not None and isinstance(node, cls):
            return name
    try:   # an untyped node: its principal interface says what it is
        interface = node.node.principal_interface_type
    except Exception:
        return type(node).__name__
    for name in _NODE_TYPES:
        if interface == getattr(genapi.EInterfaceType, f"intf{name}", None):
            return name
    return type(node).__name__


def _node_info(node, feature_name: str) -> dict:
    """``{display_name, tooltip, visibility}`` of a node, with fallbacks for nodes without them."""
    info = {"display_name": feature_name, "tooltip": "", "visibility": "Beginner"}
    inode = getattr(node, "node", None)
    if inode is None:
        return info
    try:
        info["display_name"] = inode.display_name or feature_name
    except Exception:
        pass
    try:
        info["tooltip"] = inode.tooltip or ""
    except Exception:
        pass
    try:
        visibility = inode.visibility
        info["visibility"] = next(v for v in _VISIBILITIES if visibility == getattr(genapi.EVisibility, v))
    except Exception:
        pass
    return info


def _access(node, *modes: str) -> bool:
    """True if the node's access mode is one of *modes* ("RW", "RO", "WO")."""
    try:
        mode = node.get_access_mode()
    except Exception:
        return True  # no access information: let the write itself decide
    return any(mode == getattr(genapi.EAccessMode, m) for m in modes)


def _stream_locked(node) -> bool:
    """True if the camera can lock this feature (``pIsLocked``, usually TLParamsLocked while streaming).

    That is what tells a feature locked by the stream, which ``set_feature``
    writes with the stream stopped, from one that is read-only for good.
    """
    try:
        inode = node.node
        if "pIsLocked" not in list(inode.property_names):
            return False
        prop = inode.get_property("pIsLocked")
    except Exception:
        return False
    value = prop[0] if isinstance(prop, (tuple, list)) and prop else prop
    return bool(str(value or "").strip())


def _coerce(feature_name: str, node, value):
    """*value* converted to what the node takes; ValueError if it cannot be."""
    kind = _node_type(node)
    is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "IBoolean" and isinstance(value, bool):
        return value
    if kind == "IInteger" and is_number and float(value).is_integer():
        return int(value)
    if kind == "IFloat" and is_number:
        return float(value)
    if kind in ("IEnumeration", "IString") and isinstance(value, str):
        return value
    if kind not in _VALUE_TYPES:
        return value  # an interface we do not know: hand the value over as is
    raise ValueError(f"feature '{feature_name}' ({kind}) cannot take {value!r}")


class GigECamera:
    def __init__(self, cti_path: str, camera_index: int = 0):
        """
        Initializes the GigE Camera handler.
        
        :param cti_path: Absolute path to the camera vendor's GenTL GenICam (.cti) file.
        :param camera_index: The index of the camera to connect to if multiple exist.
        """
        if not os.path.exists(cti_path):
            raise FileNotFoundError(f"GenTL Producer not found at path: {cti_path}")
            
        self.cti_path = cti_path
        self.camera_index = camera_index
        
        self.harvester = None
        self.ia = None  # Image Acquirer object
        self._is_connected = False
        self._is_streaming = False
        #: GenICam PixelFormat of the last captured frame (e.g. "Mono12", "BayerRG8").
        self.last_pixel_format: str | None = None
        #: Frames discarded since connect() because packets were lost (UDP on GigE).
        self.incomplete_frames = 0

    @staticmethod
    def _safe_attr(obj, name: str, default="N/A"):
        """Return attribute value if available, otherwise a readable fallback."""
        return getattr(obj, name, default)

    def print_discovery_diagnostics(self):
        """Logs transport layer, interface, and device details discovered by Harvester."""
        if not self.harvester:
            return

        logger.info("========== GigE Discovery Diagnostics ==========")
        logger.info(f"CTI path: {self.cti_path}")

        interfaces = getattr(self.harvester, 'interface_info_list', None)
        if interfaces is None:
            logger.info("Interfaces discovered: unsupported by current Harvester version")
        else:
            logger.info(f"Interfaces discovered: {len(interfaces)}")
            for idx, interface in enumerate(interfaces):
                interface_id = self._safe_attr(interface, 'id_')
                display_name = self._safe_attr(interface, 'display_name')
                tl_type = self._safe_attr(interface, 'tl_type')
                logger.info(
                    f"  Interface[{idx}] id={interface_id}, display_name={display_name}, tl_type={tl_type}"
                )

        devices = self.harvester.device_info_list
        logger.info(f"Devices discovered: {len(devices)}")
        for idx, device in enumerate(devices):
            vendor = self._safe_attr(device, 'vendor')
            model = self._safe_attr(device, 'model')
            serial_number = self._safe_attr(device, 'serial_number')
            user_defined_name = self._safe_attr(device, 'user_defined_name')
            tl_type = self._safe_attr(device, 'tl_type')
            id_ = self._safe_attr(device, 'id_')
            logger.info(
                f"  Device[{idx}] vendor={vendor}, model={model}, serial={serial_number}, "
                f"name={user_defined_name}, tl_type={tl_type}, id={id_}"
            )

        logger.info("================================================")

    def connect(self):
        """Initializes the GenTL producer and hooks into the specified camera."""
        if self._is_connected:
            logger.warning("Camera already connected.")
            return

        try:
            logger.info("Initializing Harvester and loading GenTL driver...")
            self.harvester = Harvester()
            self.harvester.add_file(self.cti_path)
            self.harvester.update()
            self.print_discovery_diagnostics()

            if len(self.harvester.device_info_list) == 0:
                raise CameraConnectionError(
                    "No GigE Vision cameras detected on the network. Check connections and subnet."
                )

            if not (0 <= self.camera_index < len(self.harvester.device_info_list)):
                raise CameraConnectionError(
                    f"camera_index={self.camera_index} is out of range for "
                    f"{len(self.harvester.device_info_list)} discovered camera(s)."
                )

            logger.info(f"Found {len(self.harvester.device_info_list)} camera(s). Connecting to index {self.camera_index}...")
            if hasattr(self.harvester, 'create'):
                try:
                    self.ia = self.harvester.create(self.camera_index)
                except Exception as create_exc:
                    logger.warning(
                        f"harvester.create() failed ({create_exc}); falling back to create_image_acquirer()."
                    )
                    self.ia = self.harvester.create_image_acquirer(self.camera_index)
            else:
                self.ia = self.harvester.create_image_acquirer(self.camera_index)

            self.incomplete_frames = 0
            try:
                self.ia.add_callback(self.ia.Events.INCOMPLETE_BUFFER, _IncompleteFrameCounter(self))
            except Exception as exc:   # an older harvesters: the count just stays at 0
                logger.debug(f"Incomplete-frame counter unavailable: {exc}")

            selected_device = self.harvester.device_info_list[self.camera_index]
            connected_model = self._safe_attr(selected_device, 'model')
            connected_serial = self._safe_attr(selected_device, 'serial_number')
            self._is_connected = True
            logger.info(f"Connected to camera: model={connected_model}, serial={connected_serial}")
        except CameraConnectionError:
            self.disconnect()
            raise
        except Exception as exc:
            self.disconnect()
            raise CameraConnectionError(f"Failed to connect to GigE camera: {exc}") from exc

    def _node(self, feature_name: str):
        """The GenICam node of *feature_name*. KeyError if the camera has none."""
        if not self._is_connected:
            raise RuntimeError("Cannot access features. Camera is not connected.")
        try:
            # remote_device.node_map exposes the GenICam XML tree on the camera
            node = getattr(self.ia.remote_device.node_map, feature_name)
        except Exception:
            node = None
        if node is None:
            raise KeyError(f"camera has no feature '{feature_name}'")
        return node

    def set_feature(self, feature_name: str, value):
        """
        Directly adjusts GenICam nodes/features on the camera hardware.
        Example: camera.set_feature('ExposureTime', 20000.0)

        The value is converted to the node's type (a whole float for an
        integer node, any number for a float node). KeyError for an unknown
        feature, ValueError for a value the node refuses.
        """
        node = self._node(feature_name)
        value = _coerce(feature_name, node, value)
        try:
            node.value = value
        except Exception as e:
            raise ValueError(f"feature '{feature_name}' refused {value!r}: {e}") from e
        logger.info(f"Set GenICam node '{feature_name}' to {value}")

    def get_feature(self, feature_name: str):
        """Current value of a GenICam feature. KeyError if the camera has none."""
        return self._node(feature_name).value

    def discard_queued(self) -> int:
        """Drop the frames already waiting in the acquirer (a software trigger wants a fresh one)."""
        dropped = 0
        while self._is_streaming:
            buffer = self.ia.try_fetch(timeout=0.001)
            if buffer is None:
                break
            buffer.queue()
            dropped += 1
        return dropped

    def execute(self, command_name: str) -> None:
        """Run a GenICam command node, e.g. ``camera.execute("TriggerSoftware")``."""
        node = self._node(command_name)
        try:
            node.execute()
        except Exception as e:
            raise ValueError(f"command '{command_name}' failed: {e}") from e

    def is_writable(self, feature_name: str) -> bool:
        """True if the feature can be written now (many are locked while streaming)."""
        return _access(self._node(feature_name), "RW", "WO")

    def describe_feature(self, feature_name: str) -> dict:
        """``{name, value, type, writable, stream_locked, min, max, inc, unit, choices, display_name, tooltip, visibility}``.

        Entries the node does not have are None; ``choices`` lists an
        enumeration's values. ``stream_locked``: the camera may lock it while
        streaming, so ``writable`` False does not mean read-only for good.
        """
        node = self._node(feature_name)
        info = {"name": feature_name, "value": None, "type": _node_type(node),
                "writable": _access(node, "RW", "WO"), "stream_locked": _stream_locked(node),
                "min": None, "max": None, "inc": None, "unit": None, "choices": None,
                **_node_info(node, feature_name)}
        if _access(node, "RW", "RO"):
            try:
                info["value"] = node.value
            except Exception:
                pass
        for key in ("min", "max", "inc", "unit"):
            try:
                info[key] = getattr(node, key)
            except Exception:
                pass
        if info["type"] == "IEnumeration":
            try:
                info["choices"] = list(node.symbolics)
            except Exception:
                pass
        return info

    def feature_tree(self, visibility: str = "Beginner") -> list:
        """Every value feature up to *visibility*, by category: ``[{name, features: [describe...]}]``.

        Walks the camera's GenICam ``Root`` category. Nested categories become
        "Parent / Child" groups. Commands, registers, ports and features the
        camera does not implement or offer right now are left out. Reading
        every feature is one control-channel read each: fetch it on demand,
        never on a timer.
        """
        if not self._is_connected:
            raise RuntimeError("Cannot read features. Camera is not connected.")
        limit = _VISIBILITIES.index(visibility)   # ValueError for an unknown level
        groups: list = []

        def shown(child, name) -> bool:
            level = _node_info(child, name)["visibility"]
            return level != "Invisible" and _VISIBILITIES.index(level) <= limit

        def walk(category, path):
            features, subcategories = [], []
            for child in category.features:
                try:
                    name = child.node.name
                except Exception:
                    continue
                if not shown(child, name):
                    continue
                kind = _node_type(child)
                if kind == "ICategory":
                    subcategories.append((child, name))
                elif kind in _VALUE_TYPES and _access(child, "RW", "RO", "WO"):
                    try:
                        features.append(self.describe_feature(name))
                    except Exception as e:   # one odd node must not hide the others
                        logger.debug(f"Feature '{name}' left out of the tree: {e}")
            if features:
                groups.append({"name": " / ".join(path) or "Root", "features": features})
            for child, name in subcategories:
                walk(child if hasattr(child, "features") else self._node(name),
                     path + [_node_info(child, name)["display_name"]])

        walk(self.ia.remote_device.node_map.get_node("Root"), [])
        return groups

    def start_stream(self):
        """Allocates host buffers and tells the camera to begin transmitting data."""
        if not self._is_connected:
            raise RuntimeError("Connect to the camera before starting the stream.")
        if self._is_streaming:
            return

        logger.info("Starting image stream acquisition...")
        self.ia.start()
        self._is_streaming = True

    def capture_frame(self, timeout_sec: float = 2.0) -> np.ndarray:
        """
        Blocks until a frame is received, processes it into a NumPy array, and returns it.

        Raises the built-in TimeoutError when no frame arrives within
        *timeout_sec*: with a hardware trigger, that only means no pulse came.
        """
        if not self._is_streaming:
            raise RuntimeError("Cannot capture frame. Stream has not been started.")

        # fetch() blocks until a new image buffer arrives from the GigE network
        try:
            fetched = self.ia.fetch(timeout=timeout_sec)
        except TimeoutException:
            raise TimeoutError(f"no frame within {timeout_sec:g} s") from None
        with fetched as buffer:
            payload = buffer.payload
            component = payload.components[0]

            # Extract dimensions
            width = component.width
            height = component.height
            self.last_pixel_format = getattr(component, "data_format", None)

            # Reshape raw data buffer without copying memory (Zero-Copy):
            # H×W for mono and Bayer formats, H×W×C for RGB8/BGR8 and the like.
            channels = component.data.size // (width * height)
            shape = (height, width) if channels == 1 else (height, width, channels)
            raw_data = component.data.reshape(shape)

            # Return a hard copy so the buffer can be safely released back to the queue immediately
            return raw_data.copy()

    def stop_stream(self):
        """Stops transmission and frees up host memory buffers."""
        if self._is_streaming and self.ia:
            logger.info("Stopping image stream...")
            self.ia.stop()
            self._is_streaming = False

    def disconnect(self):
        """Gracefully disconnects hardware resources and tears down Harvester context."""
        self.stop_stream()
        
        if self.ia:
            logger.info("Destroying Image Acquirer context...")
            self.ia.destroy()
            self.ia = None
            
        if self.harvester:
            logger.info("Releasing Harvester engine...")
            self.harvester.reset()
            self.harvester = None
            
        self._is_connected = False
        logger.info("Camera disconnected cleanly.")

    # --- Python Context Manager Magic Methods ---
    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GigE Vision acquisition helper")
    parser.add_argument(
        "--cti-path",
        default="C:\\Program Files\\Teledyne\\Spinnaker\\cti64\\vs2015\\Spinnaker_GenTL_v140.cti",
        help="Absolute path to GenTL .cti driver file",
    )
    parser.add_argument(
        "--camera-index",
        type=int,
        default=0,
        help="Index of the discovered camera to connect to",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Run discovery diagnostics and exit without streaming",
    )
    parser.add_argument(
        "--frame-timeout-sec",
        type=float,
        default=1.0,
        help="Per-frame acquisition timeout in seconds",
    )
    parser.add_argument(
        "--max-consecutive-timeouts",
        type=int,
        default=20,
        help="Stop stream after this many consecutive timeout errors",
    )
    parser.add_argument(
        "--max-consecutive-errors",
        type=int,
        default=50,
        help="Stop stream after this many consecutive non-timeout frame errors",
    )
    parser.add_argument(
        "--verbose-harvesters",
        action="store_true",
        help="Enable verbose Harvester internal logs (includes stack traces from driver layer)",
    )
    parser.add_argument(
        "--auto-recover",
        action="store_true",
        help="Enable one automatic reconnect attempt after repeated stream errors/timeouts",
    )
    args = parser.parse_args()

    if not args.verbose_harvesters:
        logging.getLogger("harvesters").setLevel(logging.CRITICAL)
        logging.getLogger("harvesters.core").setLevel(logging.CRITICAL)

    interrupt_state = {"count": 0}

    def _handle_sigint(_signum, _frame):
        interrupt_state["count"] += 1
        if interrupt_state["count"] == 1:
            logger.info("Ctrl+C received. Stopping now...")
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handle_sigint)

    # Point this to your specific vendor's GenTL driver (.cti file)
    CTI_PATH = args.cti_path

    if args.diagnose:
        camera = GigECamera(cti_path=CTI_PATH, camera_index=args.camera_index)
        try:
            camera.connect()
        except CameraConnectionError as exc:
            logger.error(f"Diagnostic connect failed: {exc}")
        except Exception as exc:
            logger.exception(f"Unexpected diagnostic failure: {exc}")
        finally:
            camera.disconnect()
        raise SystemExit(0)

    # Only the live-view loop below needs OpenCV, so import it here: --diagnose
    # and the GigECamera class itself stay usable without opencv-python.
    import cv2

    # Using 'with' ensures cleanup happens automatically even if an error pops up inside the block!
    try:
        with GigECamera(cti_path=CTI_PATH, camera_index=args.camera_index) as camera:

            # 1. Tweak some hardware configurations
            # (Note: Feature names are case-sensitive and vary slightly by manufacturer GenICam XML definitions)
            try:
                camera.set_feature('PixelFormat', 'Mono8')
                camera.set_feature('ExposureTime', 30000.0)  # Exposure in microseconds (30ms)
            except Exception as e:
                print(f"Skipping custom configuration setup: {e}")

            # 2. Start streaming
            camera.start_stream()

            logger.info("Streaming continuously. Press 'q' or ESC to exit.")
            frame_count = 0
            fps_window_start = time.time()
            consecutive_timeouts = 0
            consecutive_errors = 0
            attempted_stream_recovery = False

            try:
                while True:
                    if interrupt_state["count"] > 0:
                        break

                    try:
                        # 1. Grab the raw 728x544 frame from the Genie Nano
                        raw_frame = camera.capture_frame(timeout_sec=args.frame_timeout_sec)
                        consecutive_timeouts = 0
                        consecutive_errors = 0
                    except Exception as e:
                        error_msg = str(e).lower()
                        is_timeout = "timeout" in error_msg or "timed out" in error_msg
                        is_stream_invalid = (
                            "invalid data stream handle" in error_msg
                            or "list index out of range" in error_msg
                        )
                        if is_timeout:
                            consecutive_timeouts += 1
                            if consecutive_timeouts in (1, 5) or consecutive_timeouts % 10 == 0:
                                logger.warning(
                                    f"Frame timeout {consecutive_timeouts}/{args.max_consecutive_timeouts}: {e}"
                                )

                            if consecutive_timeouts >= args.max_consecutive_timeouts:
                                if args.auto_recover and not attempted_stream_recovery:
                                    attempted_stream_recovery = True
                                    logger.warning(
                                        "Too many consecutive timeouts. Reconnecting camera once to recover."
                                    )
                                    if interrupt_state["count"] > 0:
                                        raise KeyboardInterrupt
                                    try:
                                        if interrupt_state["count"] > 0:
                                            raise KeyboardInterrupt
                                        camera.disconnect()
                                        if interrupt_state["count"] > 0:
                                            raise KeyboardInterrupt
                                        camera.connect()
                                        if interrupt_state["count"] > 0:
                                            raise KeyboardInterrupt
                                        camera.start_stream()
                                    except Exception as recover_exc:
                                        raise RuntimeError(
                                            "Stream recovery failed after timeout burst: "
                                            f"{recover_exc}"
                                        ) from recover_exc
                                    consecutive_timeouts = 0
                                    consecutive_errors = 0
                                    continue

                                raise RuntimeError(
                                    "Stream stalled after repeated timeouts. "
                                    "Check cable, network adapter settings, and camera trigger mode."
                                )
                        else:
                            consecutive_errors += 1
                            if consecutive_errors in (1, 5) or consecutive_errors % 50 == 0:
                                logger.warning(
                                    f"Frame error {consecutive_errors}/{args.max_consecutive_errors}: {e}"
                                )

                            # Certain errors indicate the data stream has become invalid and
                            # should trigger a reconnect quickly rather than spinning in place.
                            if is_stream_invalid and consecutive_errors >= 5:
                                consecutive_errors = args.max_consecutive_errors

                            if consecutive_errors >= args.max_consecutive_errors:
                                if args.auto_recover and not attempted_stream_recovery:
                                    attempted_stream_recovery = True
                                    logger.warning(
                                        "Too many consecutive frame errors. Reconnecting camera once to recover."
                                    )
                                    if interrupt_state["count"] > 0:
                                        raise KeyboardInterrupt
                                    try:
                                        if interrupt_state["count"] > 0:
                                            raise KeyboardInterrupt
                                        camera.disconnect()
                                        if interrupt_state["count"] > 0:
                                            raise KeyboardInterrupt
                                        camera.connect()
                                        if interrupt_state["count"] > 0:
                                            raise KeyboardInterrupt
                                        camera.start_stream()
                                        consecutive_errors = 0
                                        consecutive_timeouts = 0
                                        continue
                                    except Exception as recover_exc:
                                        raise RuntimeError(
                                            "Stream recovery failed after repeated frame errors: "
                                            f"{recover_exc}"
                                        ) from recover_exc

                                raise RuntimeError(
                                    "Stream stalled after repeated frame errors. "
                                    "Check pixel format, payload layout, and camera trigger mode."
                                )
                        time.sleep(0.01)
                        continue

                    # 2. Convert the Genie Nano's Bayer grid into a standard BGR color image
                    # Note: If colors look inverted/weird (e.g., faces look blue), switch
                    # cv2.COLOR_BayerRG2BGR to cv2.COLOR_BayerGR2BGR or cv2.COLOR_BayerBG2BGR
                    color_frame = cv2.cvtColor(raw_frame, cv2.COLOR_BayerRG2BGR)

                    # 3. View your live color feed
                    cv2.imshow("Teledyne Genie Nano Live", color_frame)

                    frame_count += 1
                    elapsed = time.time() - fps_window_start
                    if elapsed >= 1.0:
                        fps = frame_count / elapsed
                        logger.info(f"Live stream FPS: {fps:.1f}")
                        frame_count = 0
                        fps_window_start = time.time()

                    key = cv2.waitKey(1) & 0xFF
                    if key == ord('q') or key == 27:
                        break

            except KeyboardInterrupt:
                logger.info("Keyboard interrupt received. Stopping stream.")
            finally:
                cv2.destroyAllWindows()

            print("Finished continuous streaming loop.")
    except CameraConnectionError as exc:
        logger.error(f"Camera connection failed: {exc}")
        raise SystemExit(1)
    except KeyboardInterrupt:
        logger.info("Interrupted by user (Ctrl+C). Exiting cleanly.")
        raise SystemExit(0)
    except RuntimeError as exc:
        logger.error(f"Streaming stopped: {exc}")
        raise SystemExit(1)
    except Exception as exc:
        logger.error(f"Unexpected shutdown error: {exc}")
        raise SystemExit(1)
