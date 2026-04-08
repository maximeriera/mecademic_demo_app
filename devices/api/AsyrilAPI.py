"""Low-level TCP/IP client for the Asyril Eye+ intelligent feeder.

Communicates with the Eye+ controller over a plain TCP socket using the
Asyril ASCII command protocol (newline-terminated). Every command returns
a three-digit status code followed by optional payload data.

Classes
-------
EyePlusErrorCode
    Enum mapping every known Eye+ status code to a human-readable name.
AsyrilEyePlusApi
    Socket client exposing production, calibration, and imaging commands.
"""

import socket
import re
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, Future

TIMEOUT = 35

from enum import Enum

class EyePlusErrorCode(Enum):
    """Eye+ ASCII protocol status codes.

    4xx codes indicate client errors (bad command, invalid state, etc.).
    5xx codes indicate server-side errors (hardware faults, timeouts, etc.).
    """
    # --- Client Error Codes ---
    RECEIVED_COMMAND_UNKNOWN = 401
    INVALID_ARGUMENT = 402
    SYSTEM_NOT_IN_PRODUCTION_STATE = 403
    PARAMETER_DOES_NOT_EXIST = 404
    GET_PART_COMMAND_ALREADY_ACTIVE = 405
    REQUESTED_TRANSITION_NOT_ALLOWED = 406
    RECIPE_IDENTIFIER_NOT_FOUND = 407
    RECIPE_NOT_READY = 408
    SYSTEM_NOT_IN_VALID_STATE = 409
    NO_VALID_LICENSE_FOUND = 410
    INTERNAL_CONCURRENT_CONNECTIONS_EXHAUSTED = 411
    PURGE_OPTION_NOT_ENABLED = 412
    SYSTEM_NOT_IN_PURGE_STATE = 413
    PURGE_COMMAND_ALREADY_ACTIVE = 414
    INVALID_DURATION_FOR_PURGE_COMMAND = 415
    NOT_ENOUGH_POINTS_FOR_HAND_EYE_CALIBRATION = 416
    REQUESTED_POINT_NOT_SET = 417
    INVALID_COMMAND_FOR_RECIPE_TYPE = 419
    ADVANCED_PURGE_DISABLED = 420
    MISSING_REFERENCE_IMAGE_FOR_ADVANCED_PURGE = 421
    PURGE_VIBRATIONS_NOT_FULLY_CONFIGURED = 422
    ASYFILL_NOT_CALIBRATED = 423
    CANNOT_GET_FILL_RATIO_ON_NON_ASYFILL_SMART_HOPPER = 424
    REQUIRES_MORE_RECENT_ASYFILL_FIRMWARE = 425
    ASYFILL_HAS_NOT_VIBRATED_ENOUGH = 426
    INVALID_COMMAND_FOR_RECIPE_TYPE_ALT = 427
    INVALID_ARGUMENT_FOR_PURGE_COMMAND = 428

    # --- Server Error Codes ---
    TIMEOUT_FINDING_VALID_PARTS = 501
    ASYCUBE_ALARM_RAISED = 502
    TIMEOUT_WAITING_ON_CAN_TAKE_IMAGE = 503
    GET_PART_INTERRUPTED = 510
    UNABLE_TO_CONNECT_TO_ASYCUBE = 511
    COMMUNICATION_ERROR_WITH_ASYCUBE = 512
    ASYCUBE_RETURNED_ERROR = 513
    ERROR_TURNING_BACKLIGHT_ON_OFF = 514
    ERROR_TURNING_FRONTLIGHT_ON_OFF = 515
    CAMERA_NOT_CONNECTED = 516
    PURGE_FLAP_TIMEOUT = 517
    PURGE_INTERRUPTED = 518
    NO_CALIBRATION_AVAILABLE = 519
    NO_PICK_POINT_MATCH = 520
    ALL_PARTS_COULD_NOT_BE_PURGED = 521
    COMMUNICATION_ERROR_WITH_ASYFILL = 522
    ASYFILL_NOT_CONNECTED = 523
    INTERNAL_ERROR_ASYFILL = 595
    INTERNAL_ERROR_PRODUCTION = 596
    INTERNAL_ERROR_VISION = 597
    INTERNAL_ERROR_FEEDER = 598
    INTERNAL_ERROR_SYSTEM = 599

class AsyrilEyePlusApi:
    """TCP client for the Asyril Eye+ feeder.

    Wraps the Eye+ ASCII command protocol, providing methods for connection
    management, production control, part detection, imaging, and hand-eye
    calibration.

    Parameters
    ----------
    logger : logging.Logger
        Logger instance for this device.
    ip_address : str
        IP address of the Eye+ controller.
    recipe : int
        Default recipe ID to use for production and calibration.
    port : int, optional
        TCP port of the Eye+ command interface (default ``7171``).

    Attributes
    ----------
    recipe : int
        Active recipe ID.
    connected : bool
        ``True`` if the TCP socket is currently open and responsive.
    """

    def __init__(self, logger:logging.Logger, ip_address: str, recipe:int, port: int = 7171):
        self.logger = logger
        
        self._ip_address = ip_address
        self._port = port
        self._connection: socket.socket | None = None
        self._connected = False
        
        self.termination = "\n"
        self.recipe = recipe
        
                
        self._connected = False
        self._faulted = False
        
        self._in_calib = False
        self._calib_pose = 0

        # Async support: single-worker executor to serialise background recv calls
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asyril_async")
        self._pending_future: Future | None = None
        
    @property
    def connected(self):
        """Check whether the TCP connection to the Eye+ is still alive.

        Uses a non-consuming ``MSG_PEEK`` recv to detect a closed socket
        without disturbing any pending data.

        Returns
        -------
        bool
        """
        try:
            # Peek at 1 byte of data without removing it from the buffer
            data = self._connection.recv(1, socket.MSG_PEEK)
            if len(data) == 0:
                self.logger.warning("Connection closed by peer.")
                self._connected = False
        except BlockingIOError:
            pass  # No data available, but connection is still alive
        except ConnectionResetError:
            self.logger.warning("Connection reset by peer.")
            self._connected = False
        return self._connected
    
    def connect(self):
        """Open a TCP connection to the Eye+ controller.

        Raises
        ------
        ConnectionError
            If the socket cannot reach the device within the timeout.
        """
        try:
            self.logger.info(f"Attempting to connect to Asyril Eye Plus at {self._ip_address}:{self._port}")
            self._connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._connection.settimeout(TIMEOUT)
            self._connection.connect((self._ip_address, self._port))
            self._connected = True
        except Exception as e:
            self.logger.error(f"Failed to connect to Asyril Eye Plus at {self._ip_address}:{self._port}: {e}")
            self._connected = False
            raise ConnectionError(f"Failed to connect: {e}")

    def set_part_timeout(self, timeout:float = 30.0):
        """Set the maximum time (in seconds) the Eye+ waits to find a valid part.

        Parameters
        ----------
        timeout : float
            Timeout in seconds (default ``30.0``).

        Returns
        -------
        str
            Raw response from the device.
        """
        self.logger.info(f"Setting get_part timeout to {timeout} seconds.")
        command = "set_parameter timeout " + str(timeout)
        self.__send_raw__(command)
        response = self.__receive_raw__()
        return response
    
    def get_part_timeout(self):
        """Query the current ``get_part`` timeout value from the device.

        Returns
        -------
        str
            Raw response containing the timeout value.
        """
        command = "get_parameter timeout"
        self.__send_raw__(command)
        response = self.__receive_raw__()
        return response
    
    def disconnect(self):
        """Close the TCP connection and shut down the async executor."""
        self.logger.info("Disconnecting from Asyril Eye Plus...")
        self._executor.shutdown(wait=False, cancel_futures=True)
        if self._connection:
            self._connection.close()
            self._connection = None
            self._connected = False
            self.logger.info("Disconnected successfully.")
        else:
            self.logger.warning("No active connection to disconnect.")
            
    def stop_production(self):
        """Transition the Eye+ out of the production state.

        Returns
        -------
        str
            Raw response from the device.
        """
        command = "stop production"
        self.__send_raw__(command)
        response = self.__receive_raw__()
        return response

    def start_production(self):
        """Start the production state with the configured recipe.

        Automatically calls :meth:`reset_state` first to ensure the device
        is in the ``ready`` state before starting production.

        Returns
        -------
        str
            Raw response from the device.
        """
        self.reset_state()
        command = "start production " + str(self.recipe)
        self.__send_raw__(command)
        response = self.__receive_raw__()
        return response

    def get_part(self):
        """Blocking get_part. Waits until the device finds a part and returns the pose dict."""
        command = "get_part"
        self.__send_raw__(command)
        response = self.__receive_raw__()
        response_dict = {}
        if response.startswith("200"):
            response_dict = self.extract_to_dict(response)
        else:
            response_dict['resp'] = {response}
        return response_dict

    def get_part_async(self) -> Future:
        """Non-blocking get_part. Sends the get_part command immediately and returns a
        ``concurrent.futures.Future`` that resolves to the same dict as :meth:`get_part`.

        The device starts searching as soon as this method returns, so the caller is free
        to do other work (e.g. move the robot to the pick position) and only block when
        the result is actually needed::

            future = api.get_part_async()   # device starts searching
            robot.MoveJoints(...)            # motion happens in parallel
            pose = future.result(timeout=35) # block only when pose is required

        Raises:
            RuntimeError: if a previous async call is still pending.

        Note:
            Do not call other AsyrilAPI methods while the future is pending — the
            underlying socket is not safe for concurrent use.
        """
        if self._pending_future is not None and not self._pending_future.done():
            raise RuntimeError(
                "A get_part_async call is already in progress. "
                "Await the previous future before starting a new one."
            )

        self.logger.info("Sending asynchronous get_part command...")
        self.__send_raw__("get_part")

        def _wait_for_response() -> dict:
            try:
                response = self.__receive_raw__()
            except TimeoutError:
                self.logger.error("get_part_async timed out waiting for a part.")
                return {"resp": 501, "error": "Timeout finding valid parts"}
            except Exception as e:
                self.logger.error(f"get_part_async failed: {e}")
                self._faulted = True
                raise

            if response.startswith("200"):
                return self.extract_to_dict(response)
            else:
                code = int(response[:3]) if response and response[:3].isdigit() else 0
                self.logger.warning(f"get_part_async received non-200 response: {response.strip()}")
                return {"resp": code, "raw": response.strip()}

        self._pending_future = self._executor.submit(_wait_for_response)
        return self._pending_future

    def force_take_image(self):
        """Force the Eye+ to capture an image immediately, regardless of the
        ``can_take_image`` flag.

        Returns
        -------
        str
            Raw response from the device.
        """
        command = "force_take_image"
        self.__send_raw__(command)
        response = self.__receive_raw__()
        return response

    def prepare_part(self):
        """Ask the Eye+ to prepare (flip/vibrate) parts on the Asycube
        platform so they are in a pickable orientation.

        Returns
        -------
        str
            Raw response from the device.
        """
        command = "prepare_part"
        self.__send_raw__(command)
        response = self.__receive_raw__()
        return response

    def can_take_image(self, value):
        """Tell the Eye+ whether it is safe to take an image.

        The robot should call this with ``True`` once it has cleared the
        camera's field of view and ``False`` while it is in the way.

        Parameters
        ----------
        value : bool
            ``True`` to allow imaging, ``False`` to block it.

        Returns
        -------
        str
            Raw response from the device.
        """
        command = "can_take_image " + str(value).lower()
        self.__send_raw__(command)
        response = self.__receive_raw__()
        return response

    def set_parameter(self, parameter):
        """Set an arbitrary Eye+ parameter.

        Parameters
        ----------
        parameter : str
            Parameter string in the form ``"<name> <value>"``.

        Returns
        -------
        str
            Raw response from the device.
        """
        command = "set_parameter " + str(parameter)
        self.__send_raw__(command)
        response = self.__receive_raw__()
        return response
    
    def start_calibration(self, recipe:int = None):
        """Enter the hand-eye calibration state.

        Resets the device to ``ready``, then starts the calibration workflow
        for the given recipe. After this call, use :meth:`take_calibration_image`
        and :meth:`set_calibration_pose` to capture the required poses, then
        call :meth:`calibrate` to compute and save the result.

        Parameters
        ----------
        recipe : int, optional
            Recipe ID. Defaults to :attr:`recipe` if not provided.

        Returns
        -------
        str
            Raw response from the device.
        """
        self.reset_state()
        if recipe is None:
            recipe = self.recipe
        command = "start handeye_calibration " + str(recipe)
        self.__send_raw__(command)
        response = self.__receive_raw__()
        if response.startswith("200"):
            self._in_calib = True
            self._calib_pose = 1
        return response
    
    def stop_calibration(self):
        """Exit the hand-eye calibration state and reset internal pose counter."""
        command = "stop handeye_calibration"
        self.__send_raw__(command)
        response = self.__receive_raw__()
        if response.startswith("200"):
            self._in_calib = False
            self._calib_pose = 0
    
    def calibrate(self):
        """Compute and save the hand-eye calibration from the captured poses.

        Sends ``calibrate`` followed by ``save_calibration``. If either step
        fails, the calibration state is stopped and a ``RuntimeError`` is raised.

        Returns
        -------
        str
            Raw response from the ``save_calibration`` command.

        Raises
        ------
        RuntimeError
            If the calibration computation or save fails.
        """
        command = "calibrate"
        self.__send_raw__(command)
        response = self.__receive_raw__()
        if response.startswith("200"):
            command = "save_calibration"
            self.__send_raw__(command)
            response = self.__receive_raw__()
            if response.startswith("200"):
                self.stop_calibration()
            else:
                self.stop_calibration()
                raise RuntimeError(f"Failed to save calibration: {response}")
        else:
            self.stop_calibration()
            raise RuntimeError(f"Failed to calibrate: {response}")
        return response
    
    def take_calibration_image(self):
        """Capture a calibration image at the current pose index.

        The pose index auto-increments after each successful capture
        (1 → 2 → 3 → 4). Must be called after :meth:`start_calibration`.

        Returns
        -------
        str
            Raw response from the device.

        Raises
        ------
        ValueError
            If the internal pose counter is out of the valid range [1, 4].
        RuntimeError
            If the device returns a non-200 response.
        """
        if not self._calib_pose in [1, 4]:
            raise ValueError(f"Invalid calibration pose number: {self._calib_pose}. Must be 1, 2, 3, or 4.")
        command = "take_calibration_image " + str(self._calib_pose)
        self.__send_raw__(command)
        response = self.__receive_raw__()
        if not response.startswith("200"):
            self.stop_calibration()
            raise RuntimeError(f"Failed to take calibration image: {response}")
        self._calib_pose += 1
        return response
    
    def reset_state(self):
        """Return the Eye+ to the ``ready`` state.

        Queries the current state and, if it is not already ``ready``,
        sends the appropriate ``stop`` command to transition back.

        Returns
        -------
        str or None
            Raw response from the ``stop`` command, or ``None`` if already ready.

        Raises
        ------
        RuntimeError
            If the state query or the stop command fails.
        """
        command = "get_parameter state"
        self.__send_raw__(command)
        response = self.__receive_raw__()
        state = response[4:-1]
        self.logger.info(f"Current state before reset: {state}")
        if response.startswith("200"):
            if state != "ready":
                command = "stop " + state
                self.__send_raw__(command)
                response = self.__receive_raw__()
                if not response.startswith("200"):  
                    raise RuntimeError(f"Failed to take calibration image: {response}")
                return response
        else:
            raise RuntimeError(f"Failed to get state for reset: {response}")
    
    def set_calibration_pose(self, x:float, y:float):
        """Register the robot TCP position for the current calibration pose.

        Parameters
        ----------
        x : float
            X coordinate of the robot TCP in the robot frame.
        y : float
            Y coordinate of the robot TCP in the robot frame.

        Returns
        -------
        str
            Raw response from the device.

        Raises
        ------
        ValueError
            If the internal pose counter is out of the valid range [1, 4].
        RuntimeError
            If the device returns a non-200 response.
        """
        if not self._calib_pose in [1, 4]:
            raise ValueError(f"Invalid calibration pose number: {self._calib_pose}. Must be 1, 2, 3, or 4.")
        command = "set_calibration_point " + str(self._calib_pose) + " " + str(x) + " " + str(y)
        self.__send_raw__(command)
        response = self.__receive_raw__()
        if not response.startswith("200"):
            self.stop_calibration()
            raise RuntimeError(f"Failed to set calibration pose: {response}")
        return response

    def __send_raw__(self, command):
        """Send a raw ASCII command over the socket (adds termination character)."""
        self.logger.info(f"Sending command: {command}")
        self._connection.send(
            bytes(f'{command}{self.termination}', encoding="ascii"))

    def __receive_raw__(self):
        """Block until a response is received on the socket.

        Returns
        -------
        str
            Decoded ASCII response.

        Raises
        ------
        TimeoutError
            If the socket times out before data arrives.
        """
        try:
            response = self._connection.recv(4096).decode("ascii")
            self.logger.info(f"Received response: {response[:-1]}")
        except socket.timeout:
            self.logger.error("Socket timed out while waiting for response.")
            response = ""
            self._faulted = True
            raise TimeoutError("Socket timed out while waiting for response.")
        return response
    
    def __handle_response__(self, response:str):
        if response.startswith("200"):
            return True, response
        elif response.startswith("201"):
            return True, response
        elif response.startswith("400"):
            self.logger.error(f"Bad request: {response} - {EyePlusErrorCode(int(response))}")
            return False, response
        else:
            self.logger.error(f"Received error response: {response} - {EyePlusErrorCode(int(response))}")
            return None, None

    @staticmethod
    def extract_status(response):
        """Extract the three-digit status code from a raw response string."""
        status = int(response[:3])
        return status

    @staticmethod
    def extract_position(response: str):
        """Parse a response string into ``[x, y, rz]`` coordinates.

        Expects the format: ``"200 x=<val> y=<val> rz=<val>"``.

        Returns
        -------
        list[float]
            ``[x, y, rz]``
        """
        split_response = response.split(' ')
        x = float(split_response[1][2:])
        y = float(split_response[2][2:])
        rz = float(split_response[3][3:])
        return [x, y, rz]
    
    @staticmethod
    def extract_to_dict(data_str:str) -> dict:
        """Parse a key-value response string into a dictionary.

        Extracts all ``key=value`` pairs from the response. If the response
        starts with a numeric status code, it is stored under the ``'resp'`` key.

        Example: ``"200 x=1.5 y=2.3 rz=0.0"`` → ``{'resp': 200, 'x': 1.5, 'y': 2.3, 'rz': 0.0}``

        Parameters
        ----------
        data_str : str
            Raw response string from the Eye+.

        Returns
        -------
        dict
            Parsed key-value pairs with float values.
        """
        # Regex pattern to find keys and their numeric values
        # It looks for word characters (a-z), an equals sign, and then a number
        pattern = r'([a-zA-Z]+)=([-+]?\d*\.\d+|\d+)'

        # Find all matches
        matches = re.findall(pattern, data_str)

        # Create the dictionary and convert values to floats
        result = {key: float(val) for key, val in matches}

        # If you also need the '200' at the start (often an ID or Status code)
        # We can grab the first word/number in the string
        first_element = data_str.split()[0]
        if first_element.isdigit():
            result['resp'] = int(first_element)

        return result

def example_usage():
    api = AsyrilEyePlusApi(logger=logging.getLogger("AsyrilEyePlusApiExample"), ip_address="192.168.0.50", recipe=3276)
    try:
        api.connect()
        api.reset_state()
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        api.stop_production()
        api.disconnect()
        
if __name__ == "__main__":
    example_usage()