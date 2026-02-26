import socket
import re

from .Accessory import Accessory

class AsyrilEyePlus(Accessory):
    
    def __init__(self, ipaddress: str, recipe:int, port: int = 7171):
        self.ipaddress = ipaddress
        self.port = port

        self.termination = "\n"
        self.recipe = recipe
        
        self.__connection = None
        self.faulted = False
        
        self._in_calib = False
        self._calib_pose = 0
        
    def __del__(self):
        self.shutdown()
        
    def initialize(self):
        try:
            self.connect()
            self.start_production()
        except Exception as e:
            self.faulted = True
            print(f"Failed to initialize: {e}")

    def shutdown(self):
        try:
            self.stop_production()
            self.disconnect()
        except Exception as e:
            self.faulted = True
            print(f"Failed to shutdown: {e}")
    
    def isFaulted(self):
        return self.faulted

    def connect(self):
        try:
            self.__connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.__connection.connect((self.ipaddress, self.port))
        except Exception as e:
            self.faulted = True
            raise ConnectionError(f"Failed to connect: {e}")

    def disconnect(self):
        if self.__connection:
            self.__connection.close()
            self.__connection = None

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
        response_dict = self.extract_to_dict(response)  
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
    
    def take_calibration_image(self):
        raise NotImplementedError("Calibration method not implemented yet.")
    
    def set_calibration_pose(self, n:int, x:float, y:float):
        raise NotImplementedError("Calibration method not implemented yet.")

    def __send_raw__(self, command):
        self.__connection.send(
            bytes(f'{command}{self.termination}', encoding="ascii"))

    def __receive_raw__(self):
        response = self.__connection.recv(4096).decode("ascii")
        return response

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
