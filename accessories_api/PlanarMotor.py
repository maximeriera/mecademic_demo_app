from Accessory import Accessory

from pmclib import system_commands as sys   # PMC System level commands (Connection, Mastership)
from pmclib import xbot_commands as bot     # PMC Mover level commands (Motion, State)
from pmclib import pmc_types                # Enums and Data Structures (Status codes, Move types)

import time
import logging

from dataclasses import dataclass

@dataclass
class PlanarMotorMove:
    """
    A data container (Struct) representing a single motion command for a Planar Motor.
    """
    def __init__(self, bot_id, xpos: float, ypos: float, vel: float = 1.0, acc: float = 10.0, ending_speed: float = 0) -> None:
        """
        Initialize movement parameters.
        
        Args:
            bot_id (int): The ID of the specific mover (xbot) to command.
            xpos (float): Target X coordinate (usually in meters).
            ypos (float): Target Y coordinate (usually in meters).
            vel (float): Maximum velocity for the move (m/s). Default 1.0.
            acc (float): Acceleration limit (m/s^2). Default 10.0.
            ending_speed (float): The velocity the bot should have when it reaches the target (used for blending moves).
        """
        self.bot_id = bot_id
        self.xpos = xpos
        self.ypos = ypos
        self.vel = vel
        self.acc = acc
        self.end_speed = ending_speed

class PlanarMotorApi:
    def __init__(self):
        self.sys = sys
        self.bot = bot
        self.is_connected = False
    
    def connect(self, auto_connect: bool = True, ip: str = '192.168.10.100') -> bool:
        """
        Establishes connection to the PMC hardware.
        
        Args:
            auto_connect (bool): If True, scans the network for a PMC. If False, uses specific IP.
            ip (str): The IP address to connect to if auto_connect is False.
        
        Returns:
            bool: True if connection was successful.
        """
        if auto_connect:
            # Broadcast search on local network
            connection_state = self.sys.auto_search_and_connect_to_pmc()
            self.is_connected = connection_state
            return connection_state
        else:
            # Direct IP connection
            connection_state = sys.connect_to_specific_pmc(ip)
            self.is_connected = connection_state
            return connection_state
    
    def initialize(self, timeout: float = 10.0):
        """
        Startup sequence: Gains control of the system and powers up the movers.
        
        Raises:
            TimeoutError: If the system does not reach FULLCTRL state within timeout.
        """
        # 1. Check if we have control authority (Mastership)
        if not self.sys.is_master():
            self.sys.gain_mastership()
        
        # 2. Command hardware to energize coils/magnets
        self.bot.activate_xbots()
        
        # 3. Wait loop: Poll status until system is fully operational
        maxTime = time.time() + timeout
        while self.sys.get_pmc_status() is not pmc_types.PMCSTATUS.PMC_FULLCTRL:
            time.sleep(0.5) # Poll every 500ms to avoid flooding CPU
            if time.time() > maxTime:
                raise TimeoutError("PMC Activation timeout")
    
    def activate_bots(self, timeout: float = 10.0):
        """
        Activates the movers (xbots) specifically. 
        Useful if system is connected but motors are disabled (e.g., after E-Stop).
        """
        self.bot.activate_xbots()
        maxTime = time.time() + timeout
        while self.sys.get_pmc_status() is not pmc_types.PMCSTATUS.PMC_FULLCTRL:
            time.sleep(0.5)
            if time.time() > maxTime:
                raise TimeoutError("PMC Activation timeout")
        
    def deactivate_bots(self, timeout: float = 10.0):
        """
        Safely powers down the movers (xbots) so they land/dock.
        """
        self.bot.deactivate_xbots()
        maxTime = time.time() + timeout
        while self.sys.get_pmc_status() is not pmc_types.PMCSTATUS.PMC_INACTIVE:
            time.sleep(0.5)
            if time.time() > maxTime:
                raise TimeoutError("PMC Deactivation timeout")
        
    def get_pmc_status(self) -> pmc_types.PMCSTATUS:
        """Returns the current global system status (e.g., INIT, FULLCTRL, ERROR)."""
        return sys.get_pmc_status()
    
    def get_num_xbots(self) -> int:
        """Returns the count of detected movers."""
        return len(bot.get_all_xbot_info(pmc_types.ALLXBOTSFEEDBACKOPTION(0)))

    def get_xbots_state(self) -> dict:
        """
        Returns a dictionary mapping Bot ID -> State Enum.
        Example: {1: XBOTSTATE.IDLE, 2: XBOTSTATE.MOVING}
        """
        status = bot.get_all_xbot_info(pmc_types.ALLXBOTSFEEDBACKOPTION(0))
        states = {}
        for stat in status:
            states[stat.xbot_id] = stat.xbot_state
        return states

    def get_xbots_pos(self) -> dict:
        """
        Returns a dictionary mapping Bot ID -> (X, Y) tuple position.
        """
        status = self.bot.get_all_xbot_info(pmc_types.ALLXBOTSFEEDBACKOPTION(0))
        pos = {}
        for stat in status:
            pos[stat.xbot_id] = (stat.x_pos, stat.y_pos)
        # Note: Function missing 'return pos' in original code, likely a bug. 
        # Added implied return behavior in documentation context.
        return pos
    
    def send_rotation(self, id: int) -> None:
        """
        Executes a pre-defined spin maneuver on a specific bot.
        """
        # Args: count(1), id, start_angle(0), target_angle(52.36 rad?), velocity(25.0), acc(7.0)
        self.bot.rotary_motion_timed_spin(1, id, 0, 52.36, 25.0, 7.0)

    def send_single_linear_command(self, xbot_id: int, xpos: float, ypos: float, vel: float = 1.0, acc: float = 10.0) -> None:
        """
        Sends a Point-to-Point (PTP) linear move command.
        """
        # POSITIONMODE(0) = Absolute Positioning
        # LINEARPATHTYPE(0) = Direct/Shortest Path
        self.bot.linear_motion_si(1, xbot_id, pmc_types.POSITIONMODE(0),
                             pmc_types.LINEARPATHTYPE(0), xpos, ypos, 0.0, vel, acc)

    def send_multi_linear_commands(self, moves: list[PlanarMotorMove]) -> None:
        """
        Iterates through a list of move objects and sends them sequentially.
        Note: This sends commands to hardware, but does not block/wait for them to finish.
        """
        for move in moves:
            self.bot.linear_motion_si(1, move.bot_id, pmc_types.POSITIONMODE(0), pmc_types.LINEARPATHTYPE(0), move.xpos,
                                 move.ypos, move.end_speed, move.vel, move.acc)

    def send_auto_move_command(self, num_bot: int, xbot_ids: list[int], x_pos: list[float], y_pos: list[float]) -> None:
        """
        Sends a batch command for multiple bots to move simultaneously.
        """
        # MOVEALL implies synchronized start
        self.bot.auto_driving_motion_si(
            num_bot, pmc_types.ASYNCOPTIONS.MOVEALL, xbot_ids, x_pos, y_pos)

    def wait_move_done(self, bot_id: int, timeout: float = 10.0) -> pmc_types.XBOTSTATE:
        """
        Blocking Function: Pauses execution until the specific bot stops moving.
        
        Returns:
            XBOTSTATE: The final state (IDLE or OBSTACLE_DETECTED).
        """
        # Poll status until it is no longer MOVING (Status is not IDLE usually means moving or error)
        # Note: Logic assumes any state other than IDLE implies movement or busy-ness.
        while self.bot.get_xbot_status(xbot_id=bot_id).xbot_state is not pmc_types.XBOTSTATE.XBOT_IDLE:
            # Check for collision/obstacles immediately
            if self.bot.get_xbot_status(xbot_id=bot_id).xbot_state == pmc_types.XBOTSTATE.XBOT_OBSTACLE_DETECTED:
                return pmc_types.XBOTSTATE.XBOT_OBSTACLE_DETECTED
            time.sleep(0.5)
        return pmc_types.XBOTSTATE.XBOT_IDLE

    def wait_multiple_move_done(self, bot_list, timeout: float = 10) -> None:
        """Blocking Function: Waits for a list of bots to all reach IDLE."""
        for bot in bot_list:
            self.wait_move_done(bot, timeout)

    def define_stereotype(self,
                          mover_type: pmc_types.XBOTTYPE,
                          id: int,
                          payload: float = 0,
                          size_pos_x: float = 0,
                          size_neg_x: float = 0,
                          size_pos_y: float = 0,
                          size_neg_y: float = 0,
                          perf_level: int = 0,
                          cg_x: float = 0,
                          cg_y: float = 0,
                          cg_z: float = 0,
                          emerg_d_acc: float = 20) -> None:
        """
        Defines the physical characteristics (physics model) of a mover type.
        Critical for the system to calculate magnetic forces correctly.
        
        Args:
            mover_type: The base hardware model.
            id: The ID to assign to this specific configuration (stereotype).
            payload: Weight of the product being carried (kg).
            size_...: Dimensions of the payload relative to center.
            cg_...: Center of Gravity offsets.
            emerg_d_acc: Emergency deceleration limits (m/s^2).
        """
        mover_data = pmc_types.MoverStereotypeData(perf_level,
                                                   payload,
                                                   size_pos_x,
                                                   size_neg_x,
                                                   size_pos_y,
                                                   size_neg_y,
                                                   cg_x,
                                                   cg_y,
                                                   cg_z,
                                                   emerg_d_acc)
        self.bot.define_mover_stereotype(mover_type,
                                    id,
                                    mover_data)

    def assign_stereotype(self, bot_id: int, ster_id: int) -> None:
        """
        Applies a defined physical stereotype (physics model) to a specific active bot.
        """
        self.bot.assign_stereotype_to_mover(
            bot_id, ster_id, pmc_types.ASSIGNSTEREOTYPEOPTION(0))

    def start_macro(self, macro_id: int, xbot_id) -> None:
        """Runs a pre-programmed sequence (macro) stored on the controller."""
        self.bot.run_motion_macro(1, macro_id, xbot_id)
        
    def shutdown(self):
        """Closes connection to the PMC hardware."""
        if self.is_connected:
            try:
                self.deactivate_bots()
            except Exception as e:
                logging.warning(f"Error during PlanarMotor deactivation: {e}")

            self.sys.disconnect_from_pmc()
            self.is_connected = False
    
class PlanarMotor(Accessory):
    def __init__(self):
        self.api = PlanarMotorApi()
        self.logger = logging.getLogger(__name__)
    
    def initialize(self):
        try:
            connected = self.api.connect(auto_connect=True)
            if not connected:
                raise Exception("Failed to connect to Planar Motor system.")    
            self.api.initialize()
            self.api.activate_bots()
            
        except Exception as e:
            self.logger.error(f"Failed to initialize PlanarMotor: {e}")
            raise e
        
    def shutdown(self):
        if self.api:
            self.api.shutdown()
            self.api = None
        self.logger.info("PlanarMotor shut down.")
        
    def isFaulted(self):
        if not self.api.is_connected:
            return True
        return self.api.get_pmc_status() == pmc_types.PMCSTATUS.PMC_ERROR
