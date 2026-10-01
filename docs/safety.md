---
description: How fujilib guards every change it makes to a Fuji ZP-series analyzer — safety tiers, the checks before a write, read-back, operation commands, and what the checks cannot cover.
---

# Safety

fujilib can change a few settings of the analyzer and send it four documented
commands. Every change is guarded the same way: it must be asked for with
`confirm=True`, it is checked before anything is sent, it is sent once, and
what it did is read back. This page is the rule book; the reasons are in the
[design](design.md) (§5.4, §6).

## Safety tiers

Every operation has a tier, which follows its effect.

| Tier | Operations | Gate |
|---|---|---|
| `READ_ONLY` | every read | none |
| `STATEFUL` | `return_to_measurement()`, `start_blowback()` | `confirm=True` |
| `PERSISTENT` | response times, output hold, hold mode and hold values, a channel's range and range method | `confirm=True` |
| `DANGEROUS` | `start_auto_calibration()`, `start_auto_zero_calibration()`; the calibration gases and the calibration scope | `confirm=True`; on the command line also `--i-understand-this-is-destructive` |

Calibration is `DANGEROUS` because it overwrites the calibration and is right
only if the right gas is flowing. A calibration gas value is `DANGEROUS`
because the next calibration, manual or automatic, is computed from it.
`STATEFUL` needs `confirm=True` too, unlike in `sartoriuslib`.

## The checks before anything is sent

In this order, and a refusal at any of them sends nothing:

1. **The name.** A setting is named as in the [register map](registers.md);
   an unknown name, or one that is read-only, is refused
   (`FujiValidationError`).
2. **The analyzer is open**, and no connection failure has broken the session
   (`FujiConnectionError`).
3. **The tier.** Anything above `READ_ONLY` needs `confirm=True`, exactly
   `True` (`FujiConfirmationRequiredError`).
4. **The option.** Auto calibration and auto zero calibration need the
   auto-calibration option; blowback needs an analyzer that has it. The model
   must be known, so a command that needs an option is refused before
   `identify()` (`FujiCapabilityError`, see below).
5. **The value**: its type, an enum member or its name (never its number),
   a whole number within the limits, the unit of a calibration gas
   (`FujiValidationError`).

Then, holding the port so no other traffic interleaves, fujilib reads before
it writes:

6. **The status.** A write or a command is refused while an auto calibration
   or auto zero calibration runs, while a channel is being calibrated, while
   the front panel shows a menu or the maintenance or factory screen, or
   during a manual calibration at the panel (`FujiAnalyzerStateError`,
   nothing written). The operator may be changing the very setting, and a
   setting changed during a calibration can change its scope. Return to
   measurement is the exception: its purpose is to leave a menu, or a manual
   calibration's channel selection. It too is refused while a calibration is
   under way (see below).
7. **What the setting depends on.** A calibration gas is checked against its
   channel and range read just now: the range must exist on the channel, the
   unit must be the range's own, the value must fit its decimals exactly and
   lie within 1-105 % (span) or 0-100 % (zero) of its full scale. A range is
   selected only while the channel's range method is manual.

## What may be written

Only a reviewed subset of the register map: the calibration gases and
calibration scope, the five response times, output hold, hold mode and the
hold values, and each channel's range and range method (`manual` or `auto`).
These are documented, the development analyzer does not contradict them, they
are not options, and the development analyzer can test them all. The
[register map](registers.md) marks them.

Everything else is read-only, including settings the manuals document:

| Settings | Why they are not written |
|---|---|
| alarms | the target channel's encoding is undocumented, so a limit cannot be scaled safely |
| auto-calibration and auto-zero schedules and flow times | the start time's encoding is contradicted by the analyzer; an option |
| key lock | switched on, it also blocks the operator's forced stop of a calibration |
| moving averages, O2 correction, peak alarm | options; O2 correction is a password-protected setting on the panel |
| blowback, measurement point, reference gas | not features of the ZPA |
| interference coefficients, factory data | not documented; never written |

Whatever the registry says, the Modbus client refuses any write outside a
frozen envelope of documented addresses, as the last step before the wire, so
a forged register or an address in a file cannot reach the analyzer. At the
key-simulation register (42001), which also reaches the factory menu, it lets
through only the six keys of a manual zero or span, UP, DOWN, ESC, ENT, ZERO
and SPAN: the value is checked as well as the address, so MODE and SIDE never
reach the panel.

Limits are the narrower of the two manuals' where they disagree: the MODBUS
manual defers setting ranges to the instruction manual. The response time is
the exception: it is 0-60 s, as the MODBUS manual gives it, since 0 switches
the analyzer's filter off, where the instruction manual gives 1-60 s
([protocol findings](protocol-findings.md) §20).

## A write, read back

A setting is written once with FC06, never retried, and then read back in a
scope shielded from the operation's deadline, within a deadline of its own:
what the port's timing allows two reads with every retry (3.2 s at the
defaults). So a write that used up its deadline, or whose reply was lost, is
still read back. A caller that cancels the call itself cancels it without a
read-back. What the read-back finds is the outcome, in a
[`WriteResult`](api/devices.md):

| Outcome | When | `write_parameter()` |
|---|---|---|
| verified | the register reads back as written, even if the write's own reply was lost | returns the result |
| mismatch | it reads back otherwise: the analyzer ignored the write, the panel changed it, or a write whose reply was lost never arrived | raises `FujiVerificationError` |
| unknown | the read-back failed too | raises `FujiWriteOutcomeUnknownError` |

An exception reply is a refusal: nothing was applied, and it is raised as the
`FujiModbusError` it is. A port that fails while a write waits for its reply,
or during the read after a write or a command, breaks the session, which must
then be reopened.

A channel's range reads back before the analyzer measures on it: on the
development analyzer the switch came some tens of milliseconds later. A range
write that reads back as written is therefore followed: the channel's current
range is read, within the same budget, until it shows the new range, and
`FujiVerificationError` is raised if it never does.

Retrying a write after an unknown outcome is the caller's decision. Read the
setting first.

**Key lock does not protect the settings.** It locks the front panel's keys,
not Modbus: on the development analyzer a setting written while key lock was
on was applied.

**A written setting is kept.** On the development analyzer a setting written
over Modbus survived switching the analyzer off and on, with no save step, so
every write goes to the analyzer's non-volatile memory. No manual says how
many writes that memory tolerates, so fujilib never writes periodically. A
session logs a warning when more than `write_warn_per_minute` settings (10 by
default, an argument of `open_device`) are written in a minute.

## Settings documents

`fuji-configure dump` writes every setting as a `fujilib-settings/1` document;
`Analyzer.diff_settings()` and `fuji-configure diff` compare one with the
analyzer; `Analyzer.apply_settings()` and `fuji-configure apply` write what
differs.

- The whole document is compared first. If anything in it is refused (an
  unknown name, an operation, a read-only setting that differs, a value that
  does not fit, a calibration gas without its unit), **nothing is written**.
- `max_tier` refuses a document whose writes go above it, on the comparison
  made when applying, so a document compared earlier cannot turn DANGEROUS
  unnoticed. `fuji-configure apply` passes `PERSISTENT` unless it is given
  `--i-understand-this-is-destructive`.
- A document from another analyzer (another serial number) is refused unless
  any analyzer is allowed.
- The writes go one at a time, each read back, in a fixed order: output hold
  before the hold settings when it is switched on, after them when it is
  switched off; a range method before its range; then the response times, the
  calibration scope and the calibration gases.
- **The first write that fails stops the rest.** The report lists what was
  written, what failed and what was not attempted. Nothing is rolled back;
  apply the document again once the cause is fixed, and the settings already
  written compare as unchanged.

## Operation commands

| Method | Command | Tier | Outcome |
|---|---|---|---|
| `return_to_measurement()` | 42002 | `STATEFUL` | `done` when the panel shows the measurement screen with no calibration flag set |
| `start_auto_calibration()` | 42003 | `DANGEROUS` | `started`, `ambiguous` (see below), or `sent` when the status after it cannot be read |
| `start_auto_zero_calibration()` | 42004 | `DANGEROUS` | as auto calibration |
| `start_blowback()` | 42005 | `STATEFUL` | `sent`: no register shows blowback |

A command is sent once and never retried. The analyzer's reply means it was
accepted, not that it finished, so the status is read after it. A calibration
that the analyzer acknowledged but that does not show as running is
`ambiguous`: it never started, or it already ended; fujilib reports that
rather than guessing. A command whose reply was lost is established from the
status where the status can say, and raises `FujiWriteOutcomeUnknownError`
where it cannot.

**Before starting a calibration**, `plan_auto_calibration()` and
`plan_auto_zero_calibration()` say, without changing anything, which channels
and ranges it will calibrate, against which gases, whether the outputs will be
held, and how long it should take. The start methods read the same plan and
return it with their result. Things to know:

- Auto calibration and auto zero calibration act on every channel enabled for
  auto calibration, on its auto-calibration range, and on both ranges where
  the calibration range is "both". The "at once" setting does not widen them.
- The analyzer has no calibration valves of its own: auto calibration drives
  external gas valves through the DIO option's contacts. **Without that
  option and the gases plumbed, it would calibrate on whatever gas is at the
  inlet.** fujilib therefore refuses the command unless the type code lists
  the option or `open_device(options=Capability.AUTO_CALIBRATION |
  Capability.AUTO_ZERO)` asserts it. A type code that lists the option says the
  contacts exist, not that the gases are plumbed, and its option table is
  reconstructed from the manual; asserting the option is the caller's statement
  that they are.
- It is refused while the analyzer reports an instrument error, since the
  calibration would be computed from a faulty reading.
- **Nothing stops a calibration over Modbus.** The front panel can force-stop
  one, but not while key lock is on.
- Readings during a calibration are marked `calibrating`, and held outputs
  `hold`, in every poll and recorded row. `wait_for_calibration(timeout=...)`
  polls the status until nothing is calibrating, leaving the port free
  between reads. Its result is `failed` when any calibration error is active
  at the end, even one left from an earlier calibration; `new_errors` lists
  those that appeared since the status read before the command (pass the
  command result's `before` as `since`).

The development analyzer has no auto-calibration option, so auto calibration
and auto zero calibration have been exercised only on the simulator.

## Manual calibration at the front panel

A manual zero or span is made at the panel: ZERO or SPAN, the cursor to the
channel, ENT to select it, and ENT again once the reading has settled on the
gas. fujilib can watch one made at the panel, reading only, or drive one from
the host with the same keys (below). Watching, it records what happened:

- `plan_manual_calibration(channel, "zero")` says, without changing anything,
  which channels and ranges a zero or span of `channel` would calibrate, and
  against which gases. A zero of a channel set to "at once" zeroes every channel
  so set, and a channel set to "both" is calibrated on both ranges.
- `wait_for_manual_calibration(timeout=...)` returns a `ManualCalibrationEvent`
  when one ends: the channels, whether it completed, failed or was cancelled (or
  that the reads cannot tell), the readings before and after, and the
  deviation from the calibration gas.

Settings writes and commands are refused while a manual calibration is under
way at the panel, as they are during any calibration. **Return to measurement
does not cancel a manual calibration.** On the development analyzer, sent while
the panel waited for the zero gas, it brought the display back to the
measurement screen but left the channel's calibration flag set. Nothing at the
panel showed it, and the flag stayed set until the operator entered that
channel's wait step again and pressed ESC (protocol findings §18.4). So:

- `return_to_measurement()` is refused while any calibration flag is set, with
  nothing sent (`FujiAnalyzerStateError`). It still closes a menu, and a manual
  calibration's channel selection, where no flag is set yet.
- It is `done` only when the panel shows the measurement screen with no flag
  set. A flag set after it, because an operator began a calibration at the
  panel meanwhile, raises `FujiVerificationError`.
- Cancel a manual calibration with ESC on its wait step.

## A manual zero or span from the host

`manual_calibration(plan, gas=..., confirm=True)` (and `fuji-calibrate`)
presses the calibration keys over Modbus while the operator switches the gas
valves. A key written over Modbus acts as the same key at the panel, and key
lock stops it (protocol findings §18). It sends only ZERO, SPAN, UP, DOWN, ENT
and ESC, never the keys that open the menus, and each only where it belongs:

- **Before the first key** it refuses, with nothing sent, unless the panel is
  on the measurement screen with no calibration or hold flag set, key lock and
  output hold are off, no instrument error is active, the plan reads the same
  as when it was made, and no channel would be calibrated on both ranges.
- **The gas named must be the calibration-gas setting** of every range the
  calibration touches, in its unit: the analyzer calibrates against the
  setting, not against what is flowing. Change the setting first if it differs.
- **Each key is confirmed** by what the panel shows next: the step, the cursor
  and the flags. A key the panel does not take stops the run. Keep your hands
  off the panel meanwhile; a key pressed there stops the run too.
- **It waits for the gas.** The reading of every channel calibrated must stay
  within 0.5 %FS for at least 30 s (or twice the response time), near the gas
  named and read at least every 5 s, before the key that calibrates.
- **The key that calibrates is `DANGEROUS`** and needs its own
  `confirm=True` (`--i-understand-this-is-destructive` on the command line).
  Everything above is checked again, with a fresh read of the panel last, in
  the same operation as that key: the wait step and its flags, no hold flag,
  each reading in the unit of its range, the gas still steady, no instrument
  error. A key the panel does not take ends the run, so a swallowed calibrating
  key is never followed by another. On the error display it sends ESC, never
  ENT, which would force the calibration.
- **However the run ends**, an error or Ctrl-C included, the panel is returned
  to measurement for the step it is on: ESC on channel selection, the wait step
  and the error display, a wait while a calibration runs, and return to
  measurement on any other screen. It then checks the flags, not only the
  screen.

If a calibration flag is still set on the measurement screen afterwards, the
run raises and names the channel: the analyzer still counts it as being
calibrated. Recover at the panel: ZERO or SPAN, the channel, ENT once (the wait
step), then ESC.

While a run holds the panel, the same session refuses setting writes,
commands and another run. A recording on the same analyzer goes on, its rows
marked `calibrating`.

**Not on the bench yet.** The run has been exercised on the simulator, whose
panel follows what the bench analyzer showed. A remote zero and span on the
bench analyzer, with the owner at the gases, is the next step (design §13.2
#43).

## On the command line

`fuji-configure apply` needs `--confirm`; without it the command stops before
the port is opened (exit 2). When a write would be `DANGEROUS`, it also needs
`--i-understand-this-is-destructive`, and without it prints the plan and exits
2 before writing anything. `--dry-run` compares and stops. The command ends
with `status:` and exits 1 unless the status is `ok` or `dry_run`. There is no
command for the operation commands; they are for programs.

`fuji-calibrate` needs `--confirm` for the keys and
`--i-understand-this-is-destructive` for the calibration, both before the port
is opened; `--plan` only says what a zero or span would calibrate. It asks
before the key that calibrates unless `--auto`.

## Hardware tests

| Marker | Enabled by | What it does |
|---|---|---|
| `hardware` | `FUJILIB_ENABLE_HARDWARE_TESTS=1` | reads only |
| `hardware_stateful` | `FUJILIB_ENABLE_STATEFUL_TESTS=1` | writes settings and restores them; return to measurement |
| `hardware_destructive` | `FUJILIB_ENABLE_DESTRUCTIVE_TESTS=1` | auto calibration and auto zero with calibration gas; none written yet |

Each also needs `FUJILIB_HARDWARE_PORT`, and each session needs the owner's
authorization. The procedure is in [Hardware test day](hardware-test-day.md).

## What the checks cannot cover

- **The operator at the front panel**, who can change a setting between the
  read before a write and the write itself. The read-back catches it.
- **`anymodbus` or another program used directly** on the same port: the
  guarantees are fujilib's, not the line's.
- **A value outside a setting's range, sent by another program.** The
  analyzer stores it as sent: the development analyzer took 61 s as a
  response time, outside fujilib's 0-60 s, without refusing or clamping
  it. fujilib's own limits are the only guard, and fujilib never sends
  such a value.
- **The gas at the inlet** during a calibration.

## See also

- [Register map](registers.md): every register, its tier and its limits.
- [Commands](cli.md): `fuji-configure diff` and `apply`.
- [Design](design.md) §5.4 (write policy), §6 (session and safety).
