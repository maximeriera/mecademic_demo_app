from RobotController import RobotController

from TaskType import TaskType
from RobotState import RobotState

import time

ROBOT_IP = '192.168.0.100'

if __name__ == "__main__":
    print("--- Starting Robot Demo ---")
    
    # 1. Initialize the Controller
    controller = RobotController()
    controller.initialize()

    # 2. Run a Finite Task (HOME)
    print("\n--- Starting HOME Task ---")
    controller.start_task(TaskType.HOME)
    while controller.get_state() != RobotState.READY:
        time.sleep(0.5)
    
    # 3. Run an Infinite Task (PROD)
    print("\n--- Starting PROD Task (Infinite) ---")
    controller.start_task(TaskType.PROD)
    time.sleep(3) # Let it run for a few seconds
    
    # 4. Stop the Infinite Task
    print("\n--- Stopping PROD Task ---")
    controller.stop_current_task()
    while controller.get_state() != RobotState.READY:
        time.sleep(0.5)
        
    # 5. Run a Finite Task (HOME)
    print("\n--- Starting HOME Task ---")
    controller.start_task(TaskType.SHIPMENT)
    while controller.get_state() != RobotState.READY:
        time.sleep(0.5)    
        
    # 6. Cleanup
    print("\n--- Initiating Shutdown ---")
    controller.shutdown()