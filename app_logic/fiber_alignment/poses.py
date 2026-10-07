"""Robot poses of the fiber alignment cell: the data to teach on the real robot.

Every pose is in the robot's world reference frame (WRF), in mm and degrees,
exactly as the Mecademic web portal shows it, and assumes the tool reference
frame ``TOOL_TRF`` below.

ALL VALUES ARE PLACEHOLDERS. Teach each pose on the robot, paste it here, then
set ``POSES_TAUGHT = True``. Until then every robot motion is refused (see
``cell.prepare_robot``), so made-up coordinates are never sent to the arm.

Geometry assumed by the process: fibers stand vertical (tool pointing down).
They are picked from above their slot, and the two fibers face each other along
the robot's Z axis, so the alignment searches in X/Y.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterator, Tuple

#: Flip to True once every pose below has been taught on the real cell.
POSES_TAUGHT = False


@dataclass(frozen=True)
class Pose:
    """A cartesian pose: position in mm, orientation (alpha, beta, gamma) in degrees."""

    x: float
    y: float
    z: float
    alpha: float
    beta: float
    gamma: float

    def __iter__(self) -> Iterator[float]:
        """Unpack as the six arguments of MovePose / MoveLin: ``robot.MovePose(*pose)``."""
        return iter((self.x, self.y, self.z, self.alpha, self.beta, self.gamma))

    def offset(self, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0) -> "Pose":
        """The same pose, translated in the WRF."""
        return replace(self, x=self.x + dx, y=self.y + dy, z=self.z + dz)


@dataclass(frozen=True)
class FiberSlot:
    """One holder of the fiber rack. ``number`` is what the operator sees (1..N)."""

    number: int
    #: The gripper closes here on pick and opens here on place, so a fiber
    #: always goes back exactly where it was taken from.
    pick: Pose

    @property
    def approach(self) -> Pose:
        """Clearance pose straight above ``pick``."""
        return self.pick.offset(dz=SLOT_APPROACH_DZ_MM)


# --- Tool --------------------------------------------------------------------

#: Tool reference frame (SetTrf), ideally with the TCP at the tip of the held
#: fiber. Teach every pose below with this same TRF.
TOOL_TRF = Pose(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)  # TODO teach

# --- Joint positions -----------------------------------------------------------

HOME_JOINTS: Tuple[float, ...] = (0.0, -20.0, 20.0, 0.0, 30.0, 0.0)  # TODO teach
SHIPMENT_JOINTS: Tuple[float, ...] = (0.0, -60.0, 60.0, 0.0, 90.0, 0.0)  # TODO teach

# --- Fiber rack ------------------------------------------------------------------

#: Height of each slot's approach pose above its pick pose.
SLOT_APPROACH_DZ_MM = 20.0

#: One entry per fiber holder. Add or remove entries to change N; numbers must
#: run 1..N in order.
FIBER_SLOTS: Tuple[FiberSlot, ...] = (
    FiberSlot(1, pick=Pose(170.0, -60.0, 40.0, 180.0, 0.0, 0.0)),  # TODO teach
    FiberSlot(2, pick=Pose(170.0, -40.0, 40.0, 180.0, 0.0, 0.0)),  # TODO teach
    FiberSlot(3, pick=Pose(170.0, -20.0, 40.0, 180.0, 0.0, 0.0)),  # TODO teach
    FiberSlot(4, pick=Pose(170.0, 0.0, 40.0, 180.0, 0.0, 0.0)),  # TODO teach
)

# --- Alignment station: the fixed fiber, connected to the power meter -------------

#: Where the search starts: the held fiber faces the fixed one with a small gap.
ALIGN_START = Pose(220.0, 60.0, 50.0, 180.0, 0.0, 0.0)  # TODO teach
#: On the fixed fiber's axis, backed off from ALIGN_START. The fiber enters and
#: leaves the station in a straight line (MoveLin) between these two poses, so
#: the tips never move sideways while they are close.
ALIGN_APPROACH = ALIGN_START.offset(dz=10.0)  # TODO teach, or keep as an offset


if [slot.number for slot in FIBER_SLOTS] != list(range(1, len(FIBER_SLOTS) + 1)):
    raise ValueError("poses.FIBER_SLOTS must be numbered 1..N, in order.")


def fiber_slot(number: int) -> FiberSlot:
    """The slot numbered ``number`` (1..N)."""
    if not 1 <= number <= len(FIBER_SLOTS):
        raise ValueError(
            f"Fiber slot {number} is not defined: poses.FIBER_SLOTS has slots 1..{len(FIBER_SLOTS)}."
        )
    return FIBER_SLOTS[number - 1]


def next_slot_number(number: int) -> int:
    """The slot after ``number``, wrapping from N back to 1."""
    return number % len(FIBER_SLOTS) + 1
