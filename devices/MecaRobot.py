from .Device import Device

import logging
from mecademicpy.robot import Robot as mdr

class MecaRobot(Device):
    def __init__(self, ip_address, name=None):
        if name is None:
            name = f"MecaRobot_{ip_address}"
        super().__init__(device_id=name)
        self._ip_address = ip_address
        self._connected = False
        self._ready = False
        self._faulted = False
        self._api = mdr()  # Initialize the robot's API interface

    @property
    def connected(self):
        self._connected = self._api.IsConnected()
        return self._connected
    
    @property
    def ready(self):
        self._ready = False
        if self.connected:
            self._ready = self._api.IsAllowedToMove()
        return self._ready
    
    @property
    def faulted(self):
        self._faulted = True
        if self.connected:
            self._faulted = self._api.GetStatusRobot().error_status
        return self._faulted
    
    @property
    def api(self):
        return self._api
    
    @property
    def info(self):
        if self.connected:
            info = self._api.GetRobotInfo()
            return {
                "model": info.model,
                "serial_number": info.serial,
                "firmware_version": info.version.get_str(),
                "ip_address": self._ip_address
            }
        else:
            return {
                "model": None,
                "serial_number": None,
                "firmware_version": None,
                "ip_address": self._ip_address
            } 

    def initialize(self):
        # Implement connection logic to the robot here
        self.logger.info(f"Attempting to connect to MecaRobot at {self._ip_address}")
        try:
            self._api.Connect(self._ip_address, disconnect_on_exception=False)
        except Exception as e:
            self.logger.error(f"Failed to connect to MecaRobot at {self._ip_address}: {e}")
            raise ConnectionError(f"Failed to connect: {e}")
        try:
            self._api.ClearMotion()
            self._api.ResumeMotion()
            self._api.ResetError()
            self._api.ActivateRobot()
            self._api.WaitActivated()
            if self._api.GetStatusRobot().error_status:
                raise Exception(f"{self._api.GetStatusRobot().error_code}")
            self._api.ActivateAndHome()
            self._api.WaitIdle()
        except Exception as e:
            self.logger.error(f"Error during MecaRobot initialization sequence: {e}")
            raise e
        
    def deactivate(self):
        self.logger.info("Deactivating MecaRobot.")
        self._api.DeactivateRobot()
        self._api.WaitDeactivated()
    
    def shutdown(self):
        self.logger.info(f"Shutting down MecaRobot at {self._ip_address}")
        self._api.ClearMotion()
        self._api.Disconnect()
        self._api.WaitDisconnected()

    def clear_fault(self):
        self.logger.info("Clearing faults on MecaRobot.")
        self._api.ResetError()
        self._api.ClearMotion()
        self._api.ResumeMotion()

    def abort(self):
        self.logger.warning(f"[{self.device_id}] Aborting: clearing motion queue.")
        try:
            self._api.ClearMotion()
            self._api.ResumeMotion()
        except Exception as e:
            self.logger.error(f"[{self.device_id}] Error during abort: {e}")
        
def example_usage():
    robot = MecaRobot("192.168.0.100")
    robot.initialize()
    
    print(f"Connected: {robot.connected}")
    print(f"Ready: {robot.ready}")
    print(f"Faulted: {robot.faulted}")
    
    robot._api.WaitHomed()
    
    print(f"Connected: {robot.connected}")
    print(f"Ready: {robot.ready}")
    print(f"Faulted: {robot.faulted}")
    
    robot.deactivate()
    robot.shutdown()
    
if __name__ == "__main__":
    example_usage()
    