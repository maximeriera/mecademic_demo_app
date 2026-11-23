"""Helpers shared by this workspace's task modules (prod.py, home.py, manual_actions.py...).

Robot-side programs
-------------------
The cell's motion lives in programs saved on each robot controller. Python
starts a program and waits for the checkpoint the program sets on its last
line (``SetCheckpoint(<n>)``):

    run_program(robot, "pick_feeder", done_checkpoint=11)

Why a checkpoint and not just ``WaitIdle()``: the app drives the robots in
mecademicpy's asynchronous mode, where ``StartProgram`` only *sends* the
command and returns at once. ``WaitIdle()`` right after it can return before
the program has queued its first move. A checkpoint the program sets itself
arrives only when the program got there.

ABORT: the app's abort sends ``ClearMotion()`` to every robot, which makes a
pending checkpoint wait and ``WaitIdle()`` raise ``InterruptException``. So
these helpers never need to check for ABORT themselves.

Simulation
----------
On a ``type: "simulated"`` device every ``.api`` call returns ``None``. The
helpers tolerate that, so the same task code runs on hardware and in the
simulated smoke test (``check_workspace.py`` in the new-workspace skill).
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Iterable, List

#: Seconds to wait for a program's end checkpoint unless the caller says otherwise.
DEFAULT_PROGRAM_TIMEOUT_S = 60.0


class _SimulatedCheckpoint:
    """What ``ExpectExternalCheckpoint`` gives back on a simulated robot: nothing to wait for."""

    def wait(self, timeout: float | None = None) -> None:
        return None


def expect_checkpoint(robot, n: int):
    """Arm a wait for checkpoint *n*. Call it BEFORE starting the program that sets it.

    Armed after ``StartProgram``, a short program can reach its checkpoint
    first and the wait never ends.
    """
    event = robot.api.ExpectExternalCheckpoint(n)
    return _SimulatedCheckpoint() if event is None else event


class ProgramRun:
    """A program started on one robot, to wait for later (see :func:`start_program`)."""

    def __init__(self, robot, program: str, done_checkpoint: int, done):
        self.robot = robot
        self.program = program
        self.done_checkpoint = done_checkpoint
        self._done = done

    def wait(self, timeout: float = DEFAULT_PROGRAM_TIMEOUT_S, wait_idle: bool = True) -> None:
        """Block until the program set its end checkpoint (then, by default, until the robot is idle).

        Raises mecademicpy's ``TimeoutException`` after *timeout* seconds, and
        ``InterruptException`` on ABORT.
        """
        self._done.wait(timeout=timeout)
        if wait_idle:
            self.robot.api.WaitIdle(timeout=timeout)
        self.robot.logger.info(f"[{self.robot.device_id}] program '{self.program}' done "
                               f"(checkpoint {self.done_checkpoint}).")


def start_program(robot, program: str, done_checkpoint: int) -> ProgramRun:
    """Start *program* on *robot* without waiting; ``.wait()`` the returned run later.

    Start programs on several robots, then wait for each, to run them at the
    same time from a single thread:

        a = start_program(left, "pick", 11)
        b = start_program(right, "place", 21)
        a.wait(); b.wait()
    """
    done = expect_checkpoint(robot, done_checkpoint)
    robot.logger.info(f"[{robot.device_id}] StartProgram('{program}'), "
                      f"end checkpoint {done_checkpoint}.")
    robot.api.StartProgram(program)
    return ProgramRun(robot, program, done_checkpoint, done)


def run_program(robot, program: str, done_checkpoint: int,
                timeout: float = DEFAULT_PROGRAM_TIMEOUT_S, wait_idle: bool = True) -> None:
    """Start *program* on *robot* and block until it set *done_checkpoint*."""
    start_program(robot, program, done_checkpoint).wait(timeout=timeout, wait_idle=wait_idle)


def wait_all(runs: Iterable[ProgramRun], timeout: float = DEFAULT_PROGRAM_TIMEOUT_S) -> None:
    """Wait for several :class:`ProgramRun` in turn (each gets the full *timeout*)."""
    for run in runs:
        run.wait(timeout=timeout)


def in_parallel(*branches: Callable[[], Any]) -> List[Any]:
    """Run each callable in its own thread, wait for all, re-raise the first error.

    Only for branches that need Python logic between moves (ask the feeder,
    then move, then ask again...). Plain "start two programs and wait" needs
    no threads: use :func:`start_program` twice.

    Do not open ``context.step()`` inside a branch: the step tracker has one
    stack for the task, so steps from two threads would nest into each other.
    Wrap the whole parallel block in one step in the calling thread instead.
    """
    results: List[Any] = [None] * len(branches)
    errors: List[BaseException | None] = [None] * len(branches)

    def runner(index: int, branch: Callable[[], Any]) -> None:
        try:
            results[index] = branch()
        except BaseException as error:   # re-raised in the task thread below
            errors[index] = error

    threads = [threading.Thread(target=runner, args=(i, b), name=f"branch-{i}", daemon=True)
               for i, b in enumerate(branches)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for error in errors:
        if error is not None:
            raise error
    return results


def or_simulated(value: Any, stand_in: Any) -> Any:
    """*value*, or *stand_in* when it is ``None``.

    A real driver returns a value for the calls this is meant for (a feeder's
    part pose, an input state...). A ``type: "simulated"`` device returns
    ``None`` for every call, so the stand-in lets the cycle run in the
    simulated smoke test:

        part = or_simulated(feeder.api.get_part(), SIMULATED_PART)
    """
    return stand_in if value is None else value
