"""Devices module for Mecademic demo application."""

from .Device import Device

__all__ = [
	"AsyrilEyePlus",
	"BrainboxesEDDigital",
	"GigEVisionDevice",
	"MecaRobot",
	"PlanarMotor",
	"ArduinoBoard",
	"Device",
	"IoLogikE1212",
	"LMISensor",
	"ZaberAxis",
]


def __getattr__(name):
	if name == "BrainboxesEDDigital":
		from .BrainboxesED import BrainboxesEDDigital

		return BrainboxesEDDigital
	if name == "AsyrilEyePlus":
		from .Asyril import AsyrilEyePlus

		return AsyrilEyePlus
	if name == "MecaRobot":
		from .MecaRobot import MecaRobot

		return MecaRobot
	if name == "GigEVisionDevice":
		from .GigEVisionDevice import GigEVisionDevice

		return GigEVisionDevice
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

		return ZaberAxis
	raise AttributeError(f"module {__name__!r} has no attribute {name!r}")