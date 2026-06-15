import os
import logging
import argparse
import time
import signal
import numpy as np
import cv2
from harvesters.core import Harvester

# Setup basic logging to see connection steps
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


class CameraConnectionError(RuntimeError):
    """Raised when a camera connection attempt fails."""

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

    @staticmethod
    def _safe_attr(obj, name: str, default="N/A"):
        """Return attribute value if available, otherwise a readable fallback."""
        return getattr(obj, name, default)

    def print_discovery_diagnostics(self):
        """Logs transport layer, interface, and device details discovered by Harvester."""
        if not self.harvester:
            return

        logging.info("========== GigE Discovery Diagnostics ==========")
        logging.info(f"CTI path: {self.cti_path}")

        interfaces = getattr(self.harvester, 'interface_info_list', None)
        if interfaces is None:
            logging.info("Interfaces discovered: unsupported by current Harvester version")
        else:
            logging.info(f"Interfaces discovered: {len(interfaces)}")
            for idx, interface in enumerate(interfaces):
                interface_id = self._safe_attr(interface, 'id_')
                display_name = self._safe_attr(interface, 'display_name')
                tl_type = self._safe_attr(interface, 'tl_type')
                logging.info(
                    f"  Interface[{idx}] id={interface_id}, display_name={display_name}, tl_type={tl_type}"
                )

        devices = self.harvester.device_info_list
        logging.info(f"Devices discovered: {len(devices)}")
        for idx, device in enumerate(devices):
            vendor = self._safe_attr(device, 'vendor')
            model = self._safe_attr(device, 'model')
            serial_number = self._safe_attr(device, 'serial_number')
            user_defined_name = self._safe_attr(device, 'user_defined_name')
            tl_type = self._safe_attr(device, 'tl_type')
            id_ = self._safe_attr(device, 'id_')
            logging.info(
                f"  Device[{idx}] vendor={vendor}, model={model}, serial={serial_number}, "
                f"name={user_defined_name}, tl_type={tl_type}, id={id_}"
            )

        logging.info("================================================")

    def connect(self):
        """Initializes the GenTL producer and hooks into the specified camera."""
        if self._is_connected:
            logging.warning("Camera already connected.")
            return

        try:
            logging.info("Initializing Harvester and loading GenTL driver...")
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

            logging.info(f"Found {len(self.harvester.device_info_list)} camera(s). Connecting to index {self.camera_index}...")
            if hasattr(self.harvester, 'create'):
                try:
                    self.ia = self.harvester.create(self.camera_index)
                except Exception as create_exc:
                    logging.warning(
                        f"harvester.create() failed ({create_exc}); falling back to create_image_acquirer()."
                    )
                    self.ia = self.harvester.create_image_acquirer(self.camera_index)
            else:
                self.ia = self.harvester.create_image_acquirer(self.camera_index)

            selected_device = self.harvester.device_info_list[self.camera_index]
            connected_model = self._safe_attr(selected_device, 'model')
            connected_serial = self._safe_attr(selected_device, 'serial_number')
            self._is_connected = True
            logging.info(f"Connected to camera: model={connected_model}, serial={connected_serial}")
        except CameraConnectionError:
            self.disconnect()
            raise
        except Exception as exc:
            self.disconnect()
            raise CameraConnectionError(f"Failed to connect to GigE camera: {exc}") from exc

    def set_feature(self, feature_name: str, value):
        """
        Directly adjusts GenICam nodes/features on the camera hardware.
        Example: camera.set_feature('ExposureTime', 20000.0)
        """
        if not self._is_connected:
            raise RuntimeError("Cannot set features. Camera is not connected.")
        try:
            # remote_device.node_map exposes the GenICam XML tree on the camera
            node = getattr(self.ia.remote_device.node_map, feature_name)
            node.value = value
            logging.info(f"Set GenICam node '{feature_name}' to {value}")
        except AttributeError:
            logging.error(f"GenICam node '{feature_name}' does not exist on this camera.")
            raise

    def start_stream(self):
        """Allocates host buffers and tells the camera to begin transmitting data."""
        if not self._is_connected:
            raise RuntimeError("Connect to the camera before starting the stream.")
        if self._is_streaming:
            return

        logging.info("Starting image stream acquisition...")
        self.ia.start()
        self._is_streaming = True

    def capture_frame(self, timeout_sec: float = 2.0) -> np.ndarray:
        """
        Blocks until a frame is received, processes it into a NumPy array, and returns it.
        """
        if not self._is_streaming:
            raise RuntimeError("Cannot capture frame. Stream has not been started.")

        # fetch() blocks until a new image buffer arrives from the GigE network
        with self.ia.fetch(timeout=timeout_sec) as buffer:
            payload = buffer.payload
            component = payload.components[0]
            
            # Extract dimensions
            width = component.width
            height = component.height
            
            # Reshape raw data buffer without copying memory (Zero-Copy)
            raw_data = component.data.reshape(height, width)
            
            # Return a hard copy so the buffer can be safely released back to the queue immediately
            return raw_data.copy()

    def stop_stream(self):
        """Stops transmission and frees up host memory buffers."""
        if self._is_streaming and self.ia:
            logging.info("Stopping image stream...")
            self.ia.stop()
            self._is_streaming = False

    def disconnect(self):
        """Gracefully disconnects hardware resources and tears down Harvester context."""
        self.stop_stream()
        
        if self.ia:
            logging.info("Destroying Image Acquirer context...")
            self.ia.destroy()
            self.ia = None
            
        if self.harvester:
            logging.info("Releasing Harvester engine...")
            self.harvester.reset()
            self.harvester = None
            
        self._is_connected = False
        logging.info("Camera disconnected cleanly.")

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
            logging.info("Ctrl+C received. Stopping now...")
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handle_sigint)

    # Point this to your specific vendor's GenTL driver (.cti file)
    CTI_PATH = args.cti_path

    if args.diagnose:
        camera = GigECamera(cti_path=CTI_PATH, camera_index=args.camera_index)
        try:
            camera.connect()
        except CameraConnectionError as exc:
            logging.error(f"Diagnostic connect failed: {exc}")
        except Exception as exc:
            logging.exception(f"Unexpected diagnostic failure: {exc}")
        finally:
            camera.disconnect()
        raise SystemExit(0)
    
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

            logging.info("Streaming continuously. Press 'q' or ESC to exit.")
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
                                logging.warning(
                                    f"Frame timeout {consecutive_timeouts}/{args.max_consecutive_timeouts}: {e}"
                                )

                            if consecutive_timeouts >= args.max_consecutive_timeouts:
                                if args.auto_recover and not attempted_stream_recovery:
                                    attempted_stream_recovery = True
                                    logging.warning(
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
                                logging.warning(
                                    f"Frame error {consecutive_errors}/{args.max_consecutive_errors}: {e}"
                                )

                            # Certain errors indicate the data stream has become invalid and
                            # should trigger a reconnect quickly rather than spinning in place.
                            if is_stream_invalid and consecutive_errors >= 5:
                                consecutive_errors = args.max_consecutive_errors

                            if consecutive_errors >= args.max_consecutive_errors:
                                if args.auto_recover and not attempted_stream_recovery:
                                    attempted_stream_recovery = True
                                    logging.warning(
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
                        logging.info(f"Live stream FPS: {fps:.1f}")
                        frame_count = 0
                        fps_window_start = time.time()

                    key = cv2.waitKey(1) & 0xFF
                    if key == ord('q') or key == 27:
                        break

            except KeyboardInterrupt:
                logging.info("Keyboard interrupt received. Stopping stream.")
            finally:
                cv2.destroyAllWindows()

            print("Finished continuous streaming loop.")
    except CameraConnectionError as exc:
        logging.error(f"Camera connection failed: {exc}")
        raise SystemExit(1)
    except KeyboardInterrupt:
        logging.info("Interrupted by user (Ctrl+C). Exiting cleanly.")
        raise SystemExit(0)
    except RuntimeError as exc:
        logging.error(f"Streaming stopped: {exc}")
        raise SystemExit(1)
    except Exception as exc:
        logging.error(f"Unexpected shutdown error: {exc}")
        raise SystemExit(1)
