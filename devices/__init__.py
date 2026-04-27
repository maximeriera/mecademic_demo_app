"""Devices module for Mecademic demo application."""

from .Device import Device
# from .PlanarMotor import PlanarMotor
from .ArduinoBoard import ArduinoBoard
from .IoLogikE1212 import IoLogikE1212
from .LMISensor import LMISensor

__all__ = [
	"AsyrilEyePlus",
	"MecaRobot",
	"PlanarMotor",
	"ArduinoBoard",
	"Device",
	"IoLogikE1212",
	"LMISensor",
	"ZaberAxis",
]


def __getattr__(name):
	if name == "AsyrilEyePlus":
		from .Asyril import AsyrilEyePlus

		return AsyrilEyePlus
	if name == "MecaRobot":
		from .MecaRobot import MecaRobot

		return MecaRobot
	if name == "PlanarMotor":
		from .PlanarMotor import PlanarMotor

		return PlanarMotor
	if name == "ArduinoBoard":
		from .ArduinoBoard import ArduinoBoard

		return ArduinoBoard
	if name == "IoLogikE1212":
		from .IoLogikE1212 import IoLogikE1212

		return IoLogikE1212
	if name == "LMISensor":
		from .LMISensor import LMISensor

		return LMISensor
	if name == "ZaberAxis":
		from .OLD_ZaberAxis import ZaberAxis

__all__ = ["AsyrilEyePlus", "MecaRobot", "ArduinoBoard", "Device", "IoLogikE1212", "LMISensor"]
