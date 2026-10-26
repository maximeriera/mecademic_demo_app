"""Fiber alignment demo: the process shared by prod.py and manual_actions.py.

    poses.py      robot program names, tool frame, home/shipment joints
    cell.py       device names, ProcessError, robot setup, run_program()
    gripper.py    SmarAct gripper: open/close moves of its MCS2 jaw channel
    process.py    the five steps, plus run_cycle()
    alignment.py  the contract with the alignment algorithm: 3D moves, scans, readings
    algorithm.py  the colleague's alignment algorithm, ported
    frames.py     his frame math
"""
