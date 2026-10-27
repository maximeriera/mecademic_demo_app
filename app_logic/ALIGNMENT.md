# Fiber alignment: brief

Part 1 is for whoever presents the demo: what the robot does, why it is
hard, and how to read the screen. Part 2 is for whoever runs, tunes or
changes it. The code is in `fiber_alignment/algorithm.py` (the algorithm)
and `fiber_alignment/alignment.py` (moves, readings, safety, verdict).

---

## Part 1: for presenters

### What the robot does

The robot holds a fiber that carries infrared laser light (1557 nm, about
1 mW). Facing it, across a small gap, is a fixed fiber connected to a power
meter. Both fiber ends have collimating optics: the light crosses the gap as a
beam about half a millimetre wide. The robot moves the fiber it holds, sideways
and in angle, until as much light as possible goes into the fixed fiber.

The light is invisible and there is no camera. The robot only has one number
to go by: the power the meter reads, up to about a hundred times a second.

A perfect alignment reads about 93 % of the source power. The rest is
reflected at the two glass/air faces. On screen, that is −0.31 dB vs source.

### Why it is hard

- **Four settings at once.** The robot can shift the fiber sideways (X, Z) and
  tilt it (Rx, Rz). It never moves the fiber along its axis: the gap stays as
  taught, so the two ends can never touch.
- **Lopsided tolerances.** Sideways, the beam forgives a few hundred µm. In
  angle, 0.3° already loses two thirds of the light. More than about 0.7° off,
  nothing reaches the meter: it reads noise, and there is no slope to climb.
  So finding the light is mostly a matter of angle.
- **Angle and position are linked.** Tilting the fiber also moves the spot
  where its beam lands, by about 175 µm per degree for ends 10 mm apart. Each
  tilt has to be re-centred before it can be judged.
- **A flat top.** Near the best pose, being 20 µm or 0.05° off costs only 1 to
  3 %. The last phase finds the top from the shape of the power around it,
  not by feel.

### The four phases

The screen has one card per phase, left to right. The card of the running phase
is outlined in yellow.

| Phase | What the robot does | What the card shows |
|---|---|---|
| **Acquire**: find the light | It measures the meter's noise. If light already reaches the meter, it goes straight to Center (unless the full search is on: see below). Otherwise it spirals out in angle (up to 2°), then, still in the dark, sideways (up to 500 µm). It stops once it has passed the light, then goes back to the brightest point. | A 3D landscape of the power along the spiral, over Rx/Rz for the tilt spiral and over X/Z for the sideways one. The "light" mark on the colour bar is the level that counts as light. |
| **Center**: centre on the beam | It sweeps across the beam along X, then along Z, both ways. It fits a bell curve to each sweep and moves to its top, until the top stops moving. | The latest X and Z sweeps (green forward, blue backward), the fitted bell curve, and the chosen centre in yellow. |
| **Angle**: square the angle | For Rx, then Rz, it tries tilts stepping away from the current one while the power rises, re-centring at each tilt. A curve through the best tilts picks the angle. It repeats with finer steps while that still gains. | Top: the power against each tilt, with the fitted curve and the beam's "walk" in µm per degree. Bottom: the sideways re-centring sweeps. |
| **Peak**: the last few µm | It takes nine readings at rest on a small grid (±0.2 beam width, about ±50 µm). It fits a dome to them and steps to its top, until the steps are tiny. It ends on the best reading taken at rest. | The grid readings as a 3D landscape, the centre of each iteration on its floor, and the step each iteration took. |

When the start is tilted, Angle brings by far the largest gain (+6 to
+18 dB in simulation); when it is only off sideways, Center does. The whole
alignment takes about 17 to 25 s on the simulated cell. On the real cell, it
scales with how fast the meter and the robot answer.

### The top row

- **Power, live.** Green at or above the threshold (`power_threshold_mw`),
  red below.
- **Power over time.** In dB against the threshold: 0 is the threshold, and
  the best so far is drawn in green. Each phase has a band.
- **Alignment.** The verdict, the time spent against the time budget, and one
  line per phase with its gain in dB and its duration.

### The verdict

`✓ Aligned · 931.7 µW` or `✗ Below threshold · …`. The line under it gives
the threshold, the coupling against the source in dB, and the number of
readings. A fiber below the threshold is still put back in its slot and
counted as failed, and the cycle moves on to the next fiber. If the alignment
stops on an error (or ABORT), it shows `✗ Interrupted` with the message under
it.

### Showing the whole search

When light already reaches the meter at the start, Acquire skips the
spirals, so the audience never sees the search. To show it, set
`acquire_full_search` to true in the Variables tab while the cell is idle.

Every alignment then runs the tilt spiral out to its full 2°, then the
sideways spiral out to its full 500 µm around the best tilt found, even with
light at the start. Each spiral ends on its brightest point. The Acquire card
reads "full search · …". It costs a few seconds per fiber; set it back to
false for production.

---

## Part 2: technical details

### Frame and axes

Every position is an offset from the alignment start, the pose `align_start`
leaves the fiber in. Offsets are in the start's tool frame (`TOOL_TRF`, whose
TCP must be the fiber tip):

- X and Z: lateral, in µm.
- Y: along the fiber, never moved.
- Rx and Rz: tilts about the tool axes, in degrees.

`io.local(pose)` and `io.pose_at(...)` convert between robot poses and these
offsets. The rows published on `align_sample` use them too.

### The phases in detail

**Acquire** (`acquire`, `spiral`)

- **Noise.** 20 readings at the start give the noise floor (their median)
  and its spread σ. Light is a reading of at least `signal_detect_uw`, and at
  least median + 8σ.
- **Light at the start** (median ≥ `signal_detect_uw`). Straight to Center,
  with no baseline under the fits (unless `acquire_full_search`, below).
- **Tilt spiral.** An Archimedean spiral in Rx/Rz around the start tilt, out
  to `acquire_radius_deg`:
  - pitch 1.5 angle widths, with a reading every third of the pitch along
    the path;
  - angular speed = reading spacing / measured time per reading;
  - queued in chunks of 400 moves, with blending.

  It stops once the power has fallen under half of the best reading since
  light showed. The fiber then goes to the best reading, which is read again
  at rest.
- **Lateral spiral.** If still dark: the same in X/Z, out to
  `acquire_radius_um`, with a pitch of 1.5 lateral widths.
- **Still dark.** `AlignmentNoSignal`.
- **`acquire_full_search`.** Both spirals run to their full radius whatever
  the start reads, with no stop past the light. Each ends on its best
  reading, and the lateral spiral runs around the best tilt.

**Center** (`center`, `cross_scan`)

- **Iterations.** Up to 3, each an X scan then a Z scan. It stops when the
  centre moves less than max(0.5 µm, 0.05 width).
- **A scan.** It spans ±1.5 widths around the current estimate, with a
  reading every width/10 (at least 0.25 µm). Each scan runs both ways, with
  1 sample per reading and the meter at a fixed range (1.2 × source).
- **The fit.** Each direction is fitted with a Gaussian over the noise floor:
  log(power − floor) is a parabola, fitted with weights.
  - The centre is the mean of the two directions.
  - `lag` is half their difference: what remains of the reading/pose delay.
- **Window rules.**
  - A centre within 15 % of the window's ends, or past them: the window moves
    towards it by at most half its span, and scans again (3 times at most).
  - No light in the window: it doubles around the same middle (3 times at
    most).
  - If no window catches the centre, the fiber goes to the best reading. It
    never goes to an extrapolated centre.
- **The width.** Updated only from a centre caught inside a window, by ×3 or
  ÷3 at most per fit.
- **No light along a line.** `AlignmentNoSignal` ("Lost the light while
  centring along x").

**Angle** (`angle`, `tilt_search`)

- **Rounds.** Rx then Rz, repeated while a round gains at least 0.1 dB, 3
  rounds at most.
- **Tilt steps.** From the current tilt, steps of 0.5 angle width (halved
  each round), growing ×1.5 while the power rises. It tries one direction,
  then the other if the first did not help. At most 10 tilts per search.
- **At each tilt.** The fiber is re-centred along the lateral axis that tilt
  walks the beam on: Rx walks it along Z, Rz along X. The re-centring starts
  where the walk measured so far predicts the beam. The power after
  re-centring is the tilt's power. A tilt with no light counts as no power,
  and ends that direction.
- **The tilt kept.** A parabola in log power through the best tilt and its
  two neighbours gives the tilt, kept between those two neighbours. It also
  gives a new angle width.
- **The walk.** The slope of the re-centred position against the tilt
  (µm/°) is published as "walk". It is in effect the distance from the
  rotation centre to where the beam lands: ~175 µm/° for ends 10 mm apart.
- **Then.** Both lateral axes are re-centred.

**Peak** (`peak`)

- **The pattern.** A 3×3 grid of readings at rest (10 samples averaged
  each), of radius 0.2 × the mean lateral width (`PEAK_RADIUS_WIDTHS`): about
  ±48 µm on the cell's ends. Its edge points read ~8 % under its centre,
  plenty for the fit.
- **The step.** A quadratic fit of log power gives a Newton step to the top
  (a gradient step if the fit has no maximum), clipped to the radius.
- **When it stops.** On a step under max(0.5 µm, 0.01 width), a gain under
  0.05 dB, or after 6 iterations. The fiber then goes to the centre, or to
  the best reading at rest if that one is better.

**The verdict** (`run_alignment`)

- A fresh reading in auto range, where the algorithm left the fiber. If it is
  more than 0.2 dB under the best reading at rest, the fiber slowly goes back
  there first.
- Passed if the reading is at least `power_threshold_mw`.
- The result is published on `align_result`, with everything the page needs
  to redraw the run after a reload.

### Params (`context.yaml`, Variables tab)

| Param | Default | What it sets |
|---|---|---|
| `power_threshold_mw` | 0.9 | Pass or fail. Set it 0.5–1 dB under the peak measured on the setup. |
| `source_power_mw` | 1.0 | The source's output. The scans' fixed meter range is 1.2 × this; the verdict shows dB against it. |
| `lateral_width_um` | 200 | Starting guess of the lateral 1/e² radius. Cross scans span ±1.5 widths with a reading every width/10, the lateral spiral's pitch is 1.5 widths, and the Peak pattern spans ±0.2 width. Re-measured by every fit. |
| `angle_width_deg` | 0.3 | Starting guess of the tilt 1/e half-width. The tilt spiral's pitch is 1.5 widths, and the first tilt step 0.5 width. Re-measured by the tilt parabolas. |
| `acquire_radius_deg` | 2.0 | How far the tilt spiral goes. |
| `acquire_radius_um` | 500 | How far the lateral spiral goes. |
| `signal_detect_uw` | 1.0 | Lowest power counted as light, raised to noise + 8σ. |
| `acquire_full_search` | false | Run both spirals in full, even with light at the start (demo). |
| `align_range_um` | 2000 | Window: how far X, Y, Z may go from the start. It must leave 100 µm beyond `acquire_radius_um`. |
| `align_range_deg` | 8.0 | Window: how far the three angles may turn. It must leave 0.5° beyond `acquire_radius_deg`. |
| `align_timeout_s` | 300 | After this, back to the best reading at rest, and judged there. |
| `align_settle_s` | 0.05 | Pause after each point-to-point move. |

The finer constants (spans, densities, steps, stop criteria) are at the top
of `algorithm.py`, each with its reason.

How far the searches reach, with the cell's optics (240 µm, 0.3°) and 1 µW
to detect:
- light shows within about ±0.75° of the best tilt (at the best position),
  and within about ±440 µm of the best position (at the best tilt);
- so the tilt spiral finds a start up to about 2.7° off in tilt, and the
  lateral spiral one up to about 0.9 mm off sideways.

### Readings, poses and timing

- **Robot queries.** Every pose is read with `GetRtCartPos()`. On the cell,
  `GetRtTargetCartPos` times out ("timeout waiting for interruptable
  event"), and no pose is ever asked while the robot's motion is paused.
- **A scan.** The moves are queued while the motion is paused, with a
  checkpoint after the last one, then the motion resumes. Readings go on
  until the checkpoint is reached; each is one meter query followed by one
  pose query.
- **Where a reading was taken.** A reading's pose is interpolated at the
  middle of the meter's reading, between the poses read just before and
  just after it. Readings taken on the move therefore land where they were
  taken.
- **Speeds.** Scan speeds are the wanted spacing divided by the measured
  time per reading (a moving average, plus a 2 ms pause). A slower meter or
  network gives slower scans, not sparser readings.
- **What remains of the delay** (6.8 µm on the cell on 2026-10-27) is
  cancelled by scanning both ways. It shows as `lag` on the Center card.
- **The meter.**
  - 1 sample per reading while scanning, 10 at rest.
  - A fixed range during cross scans: 1.2 × source, ×10 on an overrange, up
    to 1 W. Auto range in the spirals, in the Peak phase and for the verdict.
  - The meter's settings are restored after the run.

### Safety

- **The window.** Every pose is checked against `align_range_um` /
  `align_range_deg` before it is sent. One outside is refused, and the
  alignment faults. The spirals' radii are checked against the window before
  anything moves.
- **No move along the fiber.** Tool Y is never commanded, so the gap stays
  as taught. The simulator fails any run where the ends touch.
- **Timeout.** After `align_timeout_s`, the fiber goes back slowly to the
  best reading at rest and is judged there.
- **ABORT or a device fault.** The robot's motion is cleared at once, and the
  view shows "Interrupted".
- **A stalled scan.** No robot motion for 3 s during a scan ends the run,
  with the robot's state in the message.

### Failure messages

| Message | Meaning | What to do |
|---|---|---|
| `No light above … tilting ±2° nor moving ±500 µm around the start: check the laser, the fibers and the align_start pose.` | Neither spiral saw light. | Check the laser is on and both fibers are connected. Check `align_start` lands within ~2° and ~0.5 mm of the best pose. Or raise the radii, within the window. |
| `Lost the light while centring along x: …` | The light vanished during Center. | The laser, the fibers, or the fiber slipping in the gripper. |
| `acquire_radius_um (…) needs 100 µm of room inside align_range_um (…)` (or `acquire_radius_deg`) | A spiral would leave the window. Refused before anything moves. | Lower the radius, or widen the window. |
| `X … is … mm from the start: outside the ±… mm window` | The algorithm asked for a pose beyond the window. | Widen the window, or start closer to the best pose. |
| `The power meter overranges at … W: check source_power_mw and the meter.` | More light than the meter's top range. | Check `source_power_mw` and the meter. |
| `The robot dropped the scan …`, `The robot is not executing the scan …`, `The robot did not complete a move …` | The robot is in error, paused or not activated. The message gives its state. | Clear the robot's error, then Clear Faults. |
| `✗ Below threshold` (not an error) | The alignment ran, but the best power is under the threshold. | Compare the threshold with the peak measured on the setup. Check that the connectors are clean. |
| `timed out` on the verdict line (not an error) | The time budget ran out; the fiber was judged at its best reading. | See the tuning below, or raise `align_timeout_s`. |

### Tuning on the cell

1. **Widths.** Run one alignment. Read the 1/e² width fitted on the Center
   card's profiles, and the tilt width from the Angle card's parabolas. Set
   `lateral_width_um` and `angle_width_deg` close to them.
2. **Walk.** The "walk" under the tilt plots should match the distance
   between the fiber ends. If it is much larger, the TCP is not at the fiber
   tip: tilts swing the tip around. Teach `TOOL_TRF` again.
3. **Lag.** The Center card's `lag` shows how late readings land along a
   scan. Two-way scans cancel it, but a large value means slow readings.
4. **Threshold.** Note the peak power of a good alignment, then set
   `power_threshold_mw` 0.5–1 dB under it.
5. **Cold starts.** Run 5 alignments from no light (tilted ~1° from the
   Manual tab) and 5 from a few µW. Note their time and final power.

### Testing without the cell

`tests/fiber_alignment/sim.py` simulates the cell: a robot running the moves
and answering pose queries as the cell does, and a meter reading what the ends
couple at the robot's pose (late and smeared on the move), on a simulated
clock. From the repository root:

```
pip install -r requirements-dev.txt
python -m pytest tests -q
```

The tests cover:
- the cell's run of 2026-10-27, a few µW at the start, and no light (tilt
  or lateral search);
- the full search;
- single-mode fibers, with a TCP off the tip;
- the dark;
- that what the alignment publishes is what the view plots.

To watch a simulated run in the real page:

```
python tools/view_replay.py                                   # http://127.0.0.1:8765/view/
python tools/view_replay.py --full-search --offset 50 0 0.45 0  # the whole search, light at the start
python tools/view_replay.py --step & node tools/view_check.mjs  # browser check, screenshots in test-results/view/
```

`--offset X Z RX RZ` puts the best pose that far from the start (µm, µm, °,
°). `--dark` runs with no light, and `--optics smf` uses bare single-mode
fibers.
