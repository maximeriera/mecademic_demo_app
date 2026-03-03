import socket
import re
import logging

TIMEOUT = 35

from enum import Enum

class EyePlusErrorCode(Enum):
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
    def __init__(self, logger:logging.Logger, ip_address: str, recipe:int, port: int = 7171):
        self.logger = logger
        
        self._ip_address = ip_address
        self._port = port
        self._connection: socket.socket | None = None
        self._connected = False
        
        self.termination = "\n"
        self.recipe = recipe
        
                
        self._connected = False
        self._faulted = True
        
        self._in_calib = False
        self._calib_pose = 0
        
    @property
    def connected(self):
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
        self.logger.info(f"Setting get_part timeout to {timeout} seconds.")
        command = "set_parameter timeout " + str(timeout)
        self.__send_raw__(command)
        response = self.__receive_raw__()
        return response
    
    def get_part_timeout(self):
        command = "get_parameter timeout"
        self.__send_raw__(command)
        response = self.__receive_raw__()
        return response
    
    def disconnect(self):
        self.logger.info("Disconnecting from Asyril Eye Plus...")
        if self._connection:
            self._connection.close()
            self._connection = None
            self._connected = False
            self.logger.info("Disconnected successfully.")
        else:
            self.logger.warning("No active connection to disconnect.")
            
    def stop_production(self):
        command = "stop production"
        self.__send_raw__(command)
        response = self.__receive_raw__()
        return response

    def start_production(self):
        command = "start production " + str(self.recipe)
        self.__send_raw__(command)
        response = self.__receive_raw__()
        return response

    def get_part(self):
        command = "get_part"
        self.__send_raw__(command)
        response = self.__receive_raw__()
        if response.startswith("200"):
            response_dict = self.extract_to_dict(response)
        else:
            response_dict = {response}
        return response_dict

    def force_take_image(self):
        command = "force_take_image"
        self.__send_raw__(command)
        response = self.__receive_raw__()
        return response

    def prepare_part(self):
        command = "prepare_part"
        self.__send_raw__(command)
        response = self.__receive_raw__()
        return response

    def can_take_image(self, value):
        command = "can_take_image " + str(value).lower()
        self.__send_raw__(command)
        response = self.__receive_raw__()
        return response

    def set_parameter(self, parameter):
        command = "set_parameter " + str(parameter)
        self.__send_raw__(command)
        response = self.__receive_raw__()
        return response
    
    def start_calibration(self, recipe:int = None):
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
        command = "stop handeye_calibration"
        self.__send_raw__(command)
        response = self.__receive_raw__()
        if response.startswith("200"):
            self._in_calib = False
            self._calib_pose = 0
    
    def calibrate(self):
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
                raise RuntimeError(f"Failed to save calibration: {response}")
        else:
            raise RuntimeError(f"Failed to calibrate: {response}")
        return response
    
    def take_calibration_image(self, x:float, y:float):
        if not self._calib_pose in [1, 4]:
            raise ValueError(f"Invalid calibration pose number: {self._calib_pose}. Must be 1, 2, 3, or 4.")
        command = "take_calibration_image " + str(self._calib_pose)
        self.__send_raw__(command)
        response = self.__receive_raw__()
        if not response.startswith("200"):
            raise RuntimeError(f"Failed to take calibration image: {response}")
        self._calib_pose += 1
        return response
    
    def set_calibration_pose(self, x:float, y:float):
        if not self._calib_pose in [1, 4]:
            raise ValueError(f"Invalid calibration pose number: {self._calib_pose}. Must be 1, 2, 3, or 4.")
        command = "set_calibration_point " + str(self._calib_pose) + " " + str(x) + " " + str(y)
        self.__send_raw__(command)
        response = self.__receive_raw__()
        if not response.startswith("200"):
            raise RuntimeError(f"Failed to set calibration pose: {response}")
        return response

    def __send_raw__(self, command):
        self.logger.info(f"Sending command: {command}")
        self._connection.send(
            bytes(f'{command}{self.termination}', encoding="ascii"))

    def __receive_raw__(self):
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
            return True
        elif response.startswith("201"):
            return True, response
        elif response.startswith("400"):
            self.logger.error(f"Bad request: {response} - {EyePlusErrorCode(int(response))}")
            return False
        else:
            self.logger.error(f"Received error response: {response} - {EyePlusErrorCode(int(response))}")
            return None

    @staticmethod
    def extract_status(response):
        status = int(response[:3])
        return status

    @staticmethod
    def extract_position(response: str):
        split_response = response.split(' ')
        x = float(split_response[1][2:])
        y = float(split_response[2][2:])
        rz = float(split_response[3][3:])
        return [x, y, rz]
    
    @staticmethod
    def extract_to_dict(data_str:str) -> dict:
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
