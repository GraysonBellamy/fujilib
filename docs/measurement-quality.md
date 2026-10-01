---
description: What a Fuji ZP-series analyzer's Modbus readings can and cannot be used for — resolution, response time, hold, validity, and the oxygen scope statement.
---

# Measurement quality

fujilib reports what the analyzer reports, with the analyzer's own status
beside every value. It does not make the values better than the analyzer
makes them, and some limits matter for scientific use. This page states them;
the evidence is in [design §2.5 and §2.11](design.md) and the
[protocol findings](protocol-findings.md).

!!! warning "Oxygen for calorimetry"
    **Modbus O2 is not validated for oxygen-consumption calorimetry.** It stays
    unvalidated until acceptance limits are set and a simultaneous comparison of
    Modbus O2 against the analyzer's analog output has been run across relevant
    O2 changes, ranges and response settings (design §2.11, §13.1 #15). Until
    then, take O2 for heat-release rate from a validated path, and use fujilib
    for the analyzer's status, validity and metadata.

## Resolution is the display's

A concentration over Modbus is the four-digit number on the panel, with its
decimal point in another register. Anything above 9.999 carries at most two
decimals.

- On the development unit **O2 arrives with two decimals: 0.01 vol%, or 100 ppm,
  per step**. Quantization alone is about 29 ppm RMS. For a 0.10-point O2
  depletion, two independently quantized readings can differ by up to 10 % of
  the depletion.
- CO arrives with three decimals (10 ppm per step) on its 0-1 vol% range.
- `Reading.raw_value` and `Reading.decimals` (the `chN_raw` and
  `chN_decimals` columns) give the exact integer and scale, so the steps are
  visible rather than mistaken for a noise-free signal.
- The A/D counts of `read_adc()` are raw service values, not a
  higher-resolution measurement.

## Time response happens inside the analyzer

Every value has passed through the analyzer's response-time filter before it
reaches a register, and an averaged channel through its average period as
well. On the development unit the filter is a moving average as long as the
response time (15 s as shipped): a value is the mean of the last N seconds,
so it lags a steady change by N/2 s, and a step is 90 % through 0.9 N s
after it reaches the detector. A response time of 1 s still averages over a
second. Only 0 switches the filter off: the A/D values then equal the
unsmoothed detector counts ([protocol findings](protocol-findings.md) §20).
The sample line adds its own delay, which no setting removes.

fujilib timestamps the Modbus read (`t_mono_ns`, `t_utc`: the midpoint of the
request and reply), not the gas. The settings are reported by
`read_metadata()` and written into `fuji-capture`'s `.meta.json`, so time
alignment can account for them; they are never subtracted from timestamps.

## A flat value may be a held value

During output hold, and on some calibration steps, the concentration
registers are frozen at their last value (ZPA manual §6.6). Over-range is not
flagged either: the register carries the computed number when the panel
shows `----`. So:

- use `Reading.state` / `chN_state` (`ok`, `hold`, `calibrating`,
  `auto_calibration`, `channel_error`, `analyzer_error`, `source_invalid`,
  `settling`, `unknown`), or `Reading.valid` / `chN_valid`, which is true only
  for `ok`;
- never read a flat Modbus value as evidence that a gas has stabilized;
- keep the raw values: validity flags them, it does not hide them.

`valid` is `None`, not `True`, when the status was not read
(`poll(detail=False)`); the recorder always reads it.

## The analyzer does not say it is warming up

After power-on the readings are far off for about a minute, and no status,
hold or error flag says so. On the development unit CO2 first read 220 % of
its range and came within 0.02 vol% of its settled value 64 s later
([protocol findings](protocol-findings.md) §15.5). Respect the warm-up time in
the analyzer's manual before trusting a reading; fujilib cannot know how long
the analyzer has been on when it opens the port.

What fujilib can see is its own connection. When the port fails (the adapter
unplugged, say) and the analyzer is reopened, by a recording's
`ReconnectPolicy` or by `reopen()`, the analyzer may have lost power too. So
for 90 s after such a reopen, every reading that would be `ok` has the state
`settling` and `valid` is false. The value is kept as read.
`Session.settling_until` says when the period ends, and
`open_device(..., settle_after_reopen_s=...)` sets its length (0 for none).

This does not cover an analyzer switched off and on while the port stays
open. The polls in between fail with a timeout, no reopen follows, and the
readings after it are `ok` as far as fujilib can tell.

## Labels must be asserted

Which gas a channel carries is only trustworthy when you assert it with
`channel_map` (`--gas` on the command line). The analyzer's type code only
suggests labels, and on the development unit it is out of date. Every reading
and row records `label_source`; a value that feeds a calculation should come
from a channel whose source is `asserted`.

## Calibration state is not in the readings

A reading does not say when its channel was last zeroed or spanned. On
firmware before 2.24 there is no calibration log at all, and the error log
records only failed calibrations, so calibration recency has to come from the
operator. The development unit's error log is full of calibration errors 5,
6 and 7, and its readings at capture (O2 20.29 vol%, CO2 −0.11 vol%) suggest a
calibration that is due.
