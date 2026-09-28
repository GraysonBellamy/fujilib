---
description: Architecture, design decisions, and phased implementation plan for fujilib, an async Python driver for Fuji Electric ZP-series NDIR gas analyzers.
---

# fujilib — Architecture & Design

> Async-first Python driver for **Fuji Electric ZP-series NDIR gas analyzers**
> (ZPA, and the ZPB / ZPG / ZPAJ / ZPG3E models that share its MODBUS map),
> built on `anyserial` + `anymodbus`.
>
> `fujilib` is a member of the `*lib` instrument-driver family (`alicatlib`,
> `sartoriuslib`, `watlowlib`, `servomexlib`, `nidaqlib`, `dtollib`). **Family harmony
> is defined at the boundary, not the core:** the entry point, the frozen models, the
> error hierarchy, the streaming / sinks / sync / CLI conventions, the tooling skeleton
> and the *unified device-library API* that `capa` consumes all match the siblings. The
> internals are shaped to this device.
>
> Status: **proposal, revised 2026-09-28.** Phase 0 (repository bootstrap), Phase 1
> (registry, codecs, models and the sample shape) and Phase 3 (transport, Modbus client,
> simulated analyzer and read procedures) are done. There is no facade yet.
>
> - **Where statements come from.** Statements about the device come from the three
>   manuals in `docs/manuals/` (§14) and are marked **[manual]**. The bench analyzer was
>   probed read-only on 2026-09-28; what it actually does is recorded in
>   [protocol-findings.md](protocol-findings.md) and marked **[bench]** here. A
>   **[bench]** statement is an observation of one unit on firmware 1.02. Where it
>   disagrees with the manual, the observation wins, but an explanation of *why* it
>   happened is only as good as the experiment behind it (§2.4 is the cautionary
>   example).
> - **How this revision was made.** It integrates two independent reviews of the first
>   draft, and a follow-up read-only bench session on the same day (the timing
>   re-measurement in §2.4 and the re-read of the scan's malformed replies in §2.3).
> - **Decisions.** Decisions the owner has confirmed are struck through in §13.1.
>   Recommendations still awaiting the owner are marked *(awaiting)* where they shape
>   the plan.
>
> Code comments should cite this document as "design §N".

---

## 1. What kind of device this is (and why it drives the design)

| Property of the ZP-series | Consequence for the design |
|---|---|
| **One wire protocol**: MODBUS RTU at a fixed 38400 8-N-1. RS-485 on ZPA/ZPB/ZPG, RS-232C on ZPAJ/ZPG3E | No `AUTO` ladder, no protocol sniffing, no baud sweep. One protocol client. `anymodbus` is a hard dependency, not an extra. |
| Only FC 03 / 04 / 06 / 10, **at most 64 words per message**, and FC06 covers a smaller holding range than FC10 | fujilib owns a region-aware block planner capped at 64. The allowed function codes are recorded **per register**. No coils or discretes: every status flag is a whole register. |
| Values are **scaled integers** whose decimal point and unit live in *other* registers | The registry stores a scaling *rule*, not a constant. A concentration is decoded as a (value, decimal point, unit) triple read in one transaction. |
| **Twelve display channels**: up to 5 measured components, then O2-corrected values, corrected averages and an O2 average | The core object is the channel and the central artifact is a `Frame`, as in `servomexlib`. Which gas each channel carries is **asserted by the caller**. The type code and live data only suggest it (§2.9). |
| A **large writable settings surface** (about 170 holding registers) plus five operation commands | There is a real parameter registry, as in `watlowlib`, with read coalescing. Writes are governed by an **immutable write policy**, not by the registry alone (§5.4). |
| Manual zero/span calibration is reachable **only by simulating front-panel keys**, and the same keys reach the factory menu | Key simulation is **not planned** (§6.5). Only the documented operation commands are exposed. |
| The front panel stays live; an operator can change ranges and settings at any time | Cached metadata must be refreshable, and scaled writes re-read their scaling immediately before writing. Host locks cannot serialize the operator. |
| Up to 31 stations on one RS-485 line | One `anymodbus.Bus` per port, owned by one `ModbusPort`. Multi-drop management comes after 0.1.0 (§7.4). |
| Firmware 2.24 added the calibration log and type-code digits 27–29; the firmware version itself is **not readable** over Modbus | Capabilities are probed, not assumed (`Availability`). Only a well-formed probe answered with illegal-address counts as "unsupported". |
| **[bench]** O2 is quantized to 0.01 vol% (100 ppm) over Modbus. The O2 cell is a Hummingbird Premus paramagnetic sensor (owner), probably an upgrade, whose performance spec is not yet in hand | The library's first value to `capa` is **status, validity and metadata**. Modbus O2 is not validated for oxygen-consumption calorimetry until the comparison in §2.11 is done. |

**Design thesis:** keep the family's outer shell and the unified API verbatim; build the
core from `servomexlib`'s *frame-centric read path* plus a `watlowlib`-style *parameter
registry*, joined by one session choke point, with an explicit write policy.

### Goals

- A channel-oriented read API (`poll()`, `read_channel()`, `identify()`, `snapshot()`,
  `read_metadata()`) that needs **two transactions per full poll**.
- **Validity and provenance on every acquired value.** Hold, calibration and error state
  (channel and analyzer) and the gas-label source travel with each reading into every
  row, and unknown validity stays unknown.
- A named-parameter read API over a registry that is the single source of truth for the
  register map. Writes arrive in 0.2.0, restricted to a reviewed subset.
- Conformance to the unified device-library API, exercised against a `capa` adapter spike
  *before* 0.1.0 freezes the sample shape (§7.8, §12).
- No hardware needed to develop or test: a byte-accurate simulated analyzer drives the
  full stack in CI.
- Writes that are hard to get wrong: `confirm=True`, pre-I/O validation, a frozen write
  envelope, read-back verification, explicit unknown outcomes, and no automatic retry of
  non-idempotent commands.

### Non-goals

- Changing the station number or serial settings (front panel only, not in the map).
- Writing factory-mode parameters (linearization, calibration coefficients). The bench
  unit exposes them to FC03 (§2.3). fujilib does not model them, will never write them,
  and does not offer key simulation, which could reach the factory menu (§6.5).
- Writing 00A4h–00ABh (interference compensation), whose encoding and purpose are
  inferred, not documented (§2.6).
- Firmware update. None of the three manuals describes one and no register relates to
  it. The service manual says only that the main board "is programmed according to the
  ordered specification" and that a replacement must be ordered by analyzer serial
  number (TN5A1191 §2.7), so it is a manufacturer service matter.
- Modbus TCP / ASCII. `anymodbus` has no TCP; the analyzer has neither.
- `pint` or `pydantic` dependencies (`units.to_pint()` returns strings; models are
  frozen slotted dataclasses).
- Presenting A/D counts as higher-resolution O2. They are raw service values (§6.6).

---

## 2. Device and protocol facts

Unless marked **[bench]**, §2 is **[manual]** (INZ-TN5A1190a-E unless noted).

### 2.1 Family and link

| Models | Interface | Topology |
|---|---|---|
| ZPA, ZPB, ZPG | RS-485, 2-wire half duplex, D-sub 9 (pin 1 SG, 2 RTxD+, 3 RTxD−) | 1:N, up to 31 stations, 500 m, 100 Ω termination at both ends |
| ZPAJ, ZPG3E | RS-232C, D-sub 9 (pin 2 RxD, 3 TxD, 5 SG) | 1:1 (or 1:31 behind an RS-232C↔RS-485 converter) |

Serial settings are fixed and cannot be changed: **38400 bps, 8 data bits, no parity,
1 stop bit, no flow control**. Station No. is 1–31, set in maintenance mode on the panel;
**0 disables communication**. No broadcast behaviour is documented.

### 2.2 Function codes and limits

| FC | Operation | Max words | Notes |
|---|---|---|---|
| 03h | Read holding (4xxxx) | 64 | user settings |
| 04h | Read input (3xxxx) | 64 | measurements, status, fixed settings, logs |
| 06h | Write single holding | 1 | user settings **0000h–009Dh only**, and operation commands |
| 10h | Write multiple holding | 64 | user settings 0000h–00ABh; quantity 1 is allowed |

Exception codes: 01 illegal function, 02 illegal data address, 03 illegal data value
(described as "too many words requested / outside the map"). A bad CRC, a wrong station
number or an inter-byte gap over 24 bit times produces **no response**.

**[bench]** The bench unit answers differently in two cases:

- FC01 and FC02 are answered with exception **02**, not 01.
- A read that *starts* outside the map is answered with 02. A read that starts inside
  a region and *crosses its end* is answered with **03**.

Function codes that were never sent (FC08 and others) remain unknown.

### 2.3 Valid address regions

Relative (0-based, on-the-wire) addresses. Register number = base + relative address,
base 40001 (holding) or 30001 (input).

| FC | Relative address | Register No. | Contents |
|---|---|---|---|
| 03h, 10h | 0000h–00ABh | 40001–40172 | user settings |
| 06h | 0000h–009Dh | 40001–40158 | user settings (the FC06 bound is lower than FC10's) |
| 06h | 07D0h–07D4h | 42001–42005 | operation commands (write-only) |
| 04h | 0000h–00C1h | 30001–30194 | measurement and status |
| 04h | 0425h–0469h | 31062–31130 | fixed settings: ranges, units, type and board code |
| 04h | 047Ah–047Ch | 31147–31149 | type code digits 27–29, **firmware ≥ 2.24** |
| 04h | 1000h–1707h | 34097–35896 | calibration log, **firmware ≥ 2.24** |

Unused addresses *inside* a region read as 0, so a block read may span gaps within a
region but must never cross a region boundary. **[bench]** Not all of them: the "do not
use" words 00B9h and 00BDh have read 6 and 16 (findings §4.3). The planner discards
bridged words, so nothing depends on their value.

**[bench]** The analyzer's readable map is wider than the manual's. On the bench unit
(firmware 1.02):

| FC | Readable | Beyond the manual |
|---|---|---|
| 04 | 0000h–00C1h, 03E8h–0479h | 03E8h–0424h holds a real-time clock and 21 A/D values; 046Ah–0479h |
| 03 | 0000h–00ABh, 03E8h–069Bh, 0BB8h–0C66h | two blocks of factory calibration and configuration data |

The original one-word scan recorded 43 addresses (23 FC04, 20 FC03) with malformed
replies rather than exception 02. A read-only re-read of exactly those addresses with
correct gaps returned exception 02 on 3 of 3 attempts each. The malformed replies were
link corruption caused by the timing defect in §2.4, not features of the map.

FC03 and FC04 address separate tables throughout. Consequences for the design:

- **Read regions** are a property of the `DeviceProfile`, refined per station by what
  `identify()` observes. The observation is stored in the session, never in the shared
  profile.
- **Write regions** are a separate, frozen constant that probing can never widen (§5.4).
  Being readable does not make a register writable.

### 2.4 Timing

| Quantity | Fuji requirement | Modbus spec at 38400 | `anymodbus` "auto" |
|---|---|---|---|
| Frame delimiter (slave side) | 24 bit times = 0.63 ms | t3.5 = 1.75 ms | — |
| Idle before a request | ≥ 48 bit times (1.25 ms); **2.5 ms needed, 5 ms recommended** | 1.75 ms | 1.75 ms |
| Gap between bytes of a request | < 24 bit times; ≤ 1 ms recommended | t1.5 = 0.75 ms | 0.75 ms |
| Slave turnaround | 1–30 ms | — | — |
| Retries | "3 times or more" recommended | — | 1 |

`anymodbus`'s automatic inter-frame gap is **shorter than Fuji requires**, so fujilib
sets it explicitly.

**[bench] What the analyzer needs.** The measurement below times each gap with a
busy-wait from the moment the previous reply was received, in randomized order. It is
250 trials per cell, FC04, one word, no retries (findings §6.3):

| Gap after the reply | normal → normal | normal → exception | exception → normal | exception → exception |
|---|---|---|---|---|
| 0 ms | 0 | 5 | 2 | 1 |
| 1 ms | 0 | 0 | 0 | 0 |
| 2 ms | 0 | 0 | 0 | 1 |
| 5 ms | 0 | 0 | 0 | 0 |

The analyzer needs a gap of **at most 1 ms after any reply, exception or not**. There is
no device-specific recovery after an exception. The single failure at 2 ms was isolated
and is read as background loss (about 1 in 3,000 requests), which read retries absorb.
An earlier fixed-order run of 500 requests per row agrees (findings §6.1). These are
host-side gaps. The FTDI latency timer delays the host's view of each reply, so the
analyzer saw at least these gaps, and the data cannot resolve requirements below about
1 ms.

**[bench] What `anymodbus` delivers.** With `anymodbus`'s own 5 ms idle and no other
wait, 0.2.0 collapsed the gap after an exception reply to about 0.1 ms and failed 2 of
500 trials there. 0.2.1 kept every gap at 5.2 ms or more and failed none of 1,000
(findings §6.3).

**Correction to the first draft.** The first draft required 20–30 ms after an exception
reply. That conclusion came from two defects in the measurement method, not from the
analyzer:

1. **`anymodbus` counts the gap from the wrong moment after an exception.** It only
   records "last I/O" after a successful reply (`bus.py:475`). After an exception or a
   timeout, the gap is counted from when the request was sent (`bus.py:439`). A
   configured 5 ms after an exception reply was therefore no gap at all. Fixed in
   `anymodbus` 0.2.1 (§4.7).
2. **Short sleeps on Windows are rounded up.** On the development machine, any
   `anyio.sleep` under about 16 ms takes about 16 ms, and 20 ms takes about 33 ms. The
   configured gaps in the original probe were not the gaps on the wire.

**Defaults** (`fujilib.config.DEFAULTS`, each with its evidence beside it):

- `inter_frame_idle = 5 ms`: the manual's recommendation, measured from the end of the
  last reply of any kind (§4.2). On Windows it becomes about 16 ms of real idle, which is
  harmless at 1 Hz.
- `request_timeout = 0.5 s`.
- `retries = 2` for reads, performed by fujilib's client (§4.5).
- `startup_settle = 50 ms`, once, before the first request on a newly opened port.
- `resync_window = 0.1 s` after an uncertain transaction (§4.2). A reply ends at most
  about 81 ms after its request: 30 ms turnaround, about 35 ms for a 64-word reply at
  38400 baud, and the FTDI adapter's 16 ms latency timer.
- There is **no** `exception_recovery` setting.
- Linux timing is untested.

Round trip is 12–27 ms for one word and 50–63 ms for 64 words through an FTDI adapter at
its default 16 ms latency timer. A two-block poll takes about 120 ms, a practical ceiling
of **7–8 Hz** for one station. The default polling rate is 1 Hz. On a multi-drop line the
budget is per port: 31 stations cannot each be polled at 1 Hz.

### 2.5 Data formats

| Kind | Encoding |
|---|---|
| Word | 16-bit, high byte first |
| Long word | two words, **low-order word first** (`WordOrder.LOW_HIGH`, `ByteOrder.BIG`) |
| Concentration | signed −9999…9999, no decimal point; divide by 10^dp, dp ∈ {0,1,2,3} |
| Unit code | 0 vol%, 1 ppm, 2 mg/m³, 3 g/m³ |
| Settings in concentration units | 0…9999, scaled by the dp of their **(channel, range)** from 31087–31096 |
| Time of day | hour and minute are **BCD** (`23h` = 23); day of week 0–6 = Sun–Sat |
| Type / board code | one character per register |
| Log "empty" marker | −1 (FFFFh) |

Over-range is **not** flagged: when the panel shows `----` the register still carries
the computed concentration. During output hold the concentration registers are held too
(ZPA manual §6.6), so the per-channel hold flag is part of data validity. For the same
reason a flat Modbus reading is never evidence that a gas has stabilized.

**[bench]** Confirmed: one ASCII character per register in the low byte; negative
concentrations are two's complement; long words are low word first.

**[bench] Resolution is the display's.** A concentration is the four-digit panel value,
so anything above 9.999 carries at most two decimals. On the bench unit O2 arrives as
`2029` with two decimals: **0.01 vol%, or 100 ppm, per step**. CO arrives with three
decimals (10 ppm per step). `Reading.raw_value` and `Reading.decimals` expose this
exactly so a consumer can see the quantization rather than mistake it for noise-free
data. What this means for calorimetry is in §2.11.

### 2.6 Register map summary

Holding registers (FC03/06/10). `c` = channel 1–5, `r` = range 1–2, `n` = alarm 1–5.

| Relative | Register | Contents |
|---|---|---|
| 0000–0013 | 40001–40020 | calibration gas: zero, span per (c, r); address `4(c−1) + 2(r−1)` |
| 0014–0018 | 40021–40025 | auto-calibration enable per channel |
| 0019–001D | 40026–40030 | manual zero mode per channel (0 each, 1 at once) |
| 001E–0022 | 40031–40035 | calibration range per channel (0 current range, 1 both) |
| 0023–0036 | 40036–40055 | alarm n limits: r1 high, r1 low, r2 high, r2 low |
| 0037–0040 | 40056–40065 | alarm n mode (0 H, 1 L, 2 H-or-L, 3 HH, 4 LL); alarm n on/off |
| 0041 | 40066 | alarm hysteresis, 0–20 %FS |
| 0042–0047 | 40067–40072 | auto-calibration start day / hour / minute, cycle, cycle unit, on/off |
| 0049 | 40074 | key lock |
| 004B–0053 | 40076–40084 | response time Ch1–4 (odd addresses), O2 meter (0–60 s) |
| 0054–005B | 40085–40092 | moving-average period 1–4 and unit |
| 005C, 005D | 40093, 40094 | output hold on/off; O2 correction reference value (1–19 %) |
| 005E–0061 | 40095–40098 | peak alarm: on/off, concentration, count, hysteresis |
| 0062–0068 | 40099–40105 | auto-zero schedule, on/off, gas flow time |
| 0069–0077 | 40106–40120 | per channel: range selection, range-switch method (manual/remote/auto), auto-cal range |
| 0078–0083 | 40121–40132 | alarm 1–6 target channel; alarm 6 limits, mode, on/off |
| 0084–008A | 40133–40139 | auto-calibration gas flow times 1–7 (60–900 s) |
| 008B–0090 | 40140–40145 | hold mode (last value / setting); hold set value per channel (%FS) |
| 0091–0098 | 40146–40153 | blowback schedule, duration, on/off, displacement time |
| 0099–009C | 40154–40157 | measurement-point switching |
| 009D | 40158 | O2 limit for correction (1–20 %) |
| 009E–00A3 | 40159–40164 | reference-gas switching and averaging (ZPB/ZPG); above the FC06 bound |
| 00A4–00A5 | 40165–40166 | interference compensation coefficient; the manual lists two separate words, "Ch1 range 1 / range 2", with no limits |
| 00A6–00AB | 40167–40172 | undocumented |

**[bench]** 00A4h–00ABh read as four low-word-first long words, each 1,000,000. The
reading "four interference coefficients" is an **inference**, not a documented fact, so
the whole block is **read-only** in fujilib.

**What building the register map found** (Phase 1; **[bench]** where marked):

- **[bench] Schedule start times may not be BCD.** The manual says the auto-calibration,
  auto-zero and blowback start hour and minute are BCD. All three start hours read
  `000Ch` on the bench unit, which is not BCD, while the clock at 03E8h is BCD. The hour
  is probably binary (12:00). These six registers are *contested*: kept raw and never
  written until the start time the panel shows settles it (§13.2 #26).
- **[bench] The alarm target channel's encoding is undocumented.** The manual gives 0–6
  with no meaning. The bench unit reads 0–4 for alarms 1–5 (channel − 1?) and 12 for
  alarm 6, outside the documented range. The registers are contested, so alarm limits
  stay raw until the encoding is known (§5.2, §13.2 #27).
- **Response time is per NDIR component, not per channel.** 40076–40082 belong to NDIR
  components 1–4, and O2 has its own slot (40084) whatever its channel. Which output
  each of the four moving-average "orders" (40085–40092) averages is not stated.
- **The two cycle-unit registers differ.** The schedules use 0 = hours, 1 = days; the
  moving-average and measurement-point periods use 0 = hours, 1 = minutes.
- **The manuals disagree on five limits** (peak-alarm concentration, O2 reference,
  response time, moving-average period, the schedule cycles). The registry takes the
  MODBUS manual's limits and records each conflict; each is resolved before any write
  (Phase 6).
- **Decoding is total.** An undocumented enum value is kept as a plain integer, and a
  concentration whose decimal point does not decode becomes a reading with no value and
  state `unknown`, rather than failing a whole read.

The manual labels the alarm registers "Ch1…Ch5", but the instruction manual describes
*alarms* 1–6, each with a **target channel** (40121–40126). fujilib models them as
alarms, not channels.

The "manual zero mode" (40026–40030) and "calibration range" (40031–40035) settings
widen a calibration beyond the channel and range it was started on. Any operation that
calibrates must report every channel and range it will affect (§6.2).

Input registers (FC04).

| Relative | Register | Contents |
|---|---|---|
| 0000–0023 | 30001–30036 | Ch1–12: concentration, decimal point, unit (3 words each) |
| 0024–002F | 30037–30048 | peak count; current range Ch1–5; alarm 1–5 state; peak-count alarm |
| 0030–003A | 30049–30059 | auto calibration running; zero / span calibration running per channel |
| 003B, 003C | 30060, 30061 | instrument error; calibration error |
| 003D–0082 | 30062–30131 | error log, 14 × (error No.−1, day, hour, minute, channel−1), newest first |
| 0083–00A4 | 30132–30165 | active errors: 1, 2, 3, 10; then errors 4–9 per channel |
| 00A5–00B3 | 30166–30180 | per channel: auto-zero running, auto-span running, **hold** |
| 00B4–00B6 | 30181–30183 | display state: screen, manual-calibration step, top channel |
| 00BC, 00BE | 30189, 30191 | manual-calibration cursor channel; alarm 6 state |
| 0425–0447 | 31062–31096 | per (c, r): number of ranges, unit, range value, decimal point |
| 0448–0469 | 31097–31130 | type code digits 1–26; board code digits 1–8 |
| 047A–047C | 31147–31149 | type code digits 27–29 |
| 1000–1707 | 34097–35896 | calibration log: 5 channels × 40 records × 9 words; channel c starts at `1000h + 360(c−1)` |

A calibration-log record is: channel, range-and-kind (0 Z1, 1 S1, 2 Z2, 3 S2), detector
count (long word), deviation (%FS × 10), month, day, hour, minute. Neither log carries a
year, and the error log carries no month, so both are modelled as **partial timestamps**,
never as `datetime`. The panel's own error-log screen shows year and month; the Modbus
copy does not.

**[bench]** Undocumented but readable, and modelled as *probed* registers
(`Availability`, never assumed):

| Relative (FC04) | Contents |
|---|---|
| 03E8–03EE | real-time clock, BCD: year, month, day, day of week, hour, minute, second |
| 03EF–0418 | the 21 A/D conversion values of the service manual's table, long words |

The "board" code (0462–0469) is the **serial number**.

### 2.7 Operation commands (FC06 only, write-only)

| Register | Value | Effect |
|---|---|---|
| 42001 | key code | simulate a key: 01 MODE, 02 SIDE, 04 UP, 08 DOWN, 10 ESC, 20 ENT, 40 ZERO, 80 SPAN |
| 42002 | 1 | force the display back to measurement mode |
| 42003 | 1 | run auto calibration once |
| 42004 | 1 | run auto zero calibration once |
| 42005 | 1 | run blowback once (option) |

There is **no register to stop** a running auto calibration, and no register that starts
a manual zero or span calibration; both exist only as key sequences on the panel. The
key register also reaches maintenance and factory mode (§6.5), so fujilib never writes
42001.

### 2.8 Error codes (ZPA manual §8, service manual §4)

| No. | Meaning | Scope | Contact |
|---|---|---|---|
| 1 | light source / sector motor fault | analyzer | instrument error |
| 2 | detector failure | analyzer | instrument error |
| 3 | A/D conversion fault | analyzer | instrument error |
| 4 | zero calibration outside allowable range | channel | calibration error |
| 5 | zero calibration amount over 50 %FS | channel | calibration error |
| 6 | span calibration outside allowable range | channel | calibration error |
| 7 | span calibration amount over 50 %FS | channel | calibration error |
| 8 | reading unstable during calibration | channel | calibration error |
| 9 | error 4–8 occurred during auto calibration | channel | calibration error |
| 10 | output cable / DIO circuit fault | analyzer | instrument error |

On errors 5 and 7 the panel offers "ENT: Force cal." (ZPA manual p. 77).

### 2.9 Channel layout and labels

Which quantity appears on which channel follows from three digits of the type code
(ZPA manual §5.3(3), §9.3): digit 6 (NDIR components), digit 7 (O2 source) and digit 21
(O2 correction outputs). For example code `V` / O2 fitted / `C` gives:

`Ch1 NOx · Ch2 SO2 · Ch3 CO2 · Ch4 CO · Ch5 O2 · Ch6–8 corrected NOx, SO2, CO ·
Ch9–11 corrected averages`

Digit 7 has six values in the current table. The O2 value can come from:

- no O2 at all;
- an external O2 analyzer, through the A/I connector as a 0–1 V DC signal;
- an external zirconia analyzer (ZFK7), through the same connector;
- a built-in galvanic fuel cell;
- a built-in paramagnetic cell, in two variants.

So an O2 channel does not by itself imply a built-in sensor.

**[bench] The type code is a hint, not an authority.** The bench unit reports
`ZPACBJY1MPFYYYYYY2DEYAYAY0`:

- Digit 6 (`J`, CO2 + CO) is right.
- Digit 7 is `Y` ("none"), yet channel 3 measures O2 with a built-in Hummingbird Premus
  paramagnetic cell. The owner believes the cell was an upgrade, which would explain
  the stale code.
- Digits 4, 25 and 26 are not in the current table at all.
- The unit carries revision code `1`; the manual documents revision `2`.

**Labelling rule.** A gas label that feeds a calculation must be asserted by the caller.
`identify()` works in this order:

1. **Caller assertion wins.** A `channel_map` passed to `open_device` or `identify`
   labels channels with `LabelSource.ASSERTED`. For the bench rig that is Ch1 CO2,
   Ch2 CO, Ch3 O2 *(awaiting owner confirmation)*.
2. **Presence.** A channel is present if it is asserted, or if its reading triple has
   been non-zero at least once in this session. An all-zero triple is a *legal zero
   reading* (0, dp 0, vol%), not proof of absence. It leaves an unasserted channel
   "not yet seen" and never removes a channel already established. The range-count
   register is not a presence test: unused channels report two ranges.
3. **Range, unit and decimals** come from the range registers.
4. **Suggested labels.** For unasserted present channels, `gas` is `Gas.UNKNOWN`, and
   `suggested_gas` comes from the type code where it decodes. Otherwise it comes from
   the layout rule (NDIR components first, then O2), with `LabelSource.INFERRED`. A
   suggestion never selects a scientific channel automatically.

Every `ChannelInfo`, `Reading` and row records its label source. This follows
`watlowlib`'s lesson from its unit register: an honest "unknown" beats a confident wrong
tag.

The code tables differ per model, so only the ZPA table ships initially; other models
expose the raw code until their manuals are added.

### 2.10 Golden frames

The manual's worked frames (the CRC example and the FC03, FC04, FC06 and FC10
exchanges, nine frames) were recomputed against CRC-16/MODBUS twice, independently,
and all match. They are `tests/fixtures/manual_frames.txt`, in the family arrow format.

```
> 01 03 00 04 00 02 85 CA                                  # read Ch2 r1 zero/span gas
< 01 03 04 00 00 03 E8 FA 8D                               #   0, 1000 -> 0.0 / 100.0 ppm
> 01 04 00 0C 00 03 70 08                                  # read Ch5 conc / dp / unit
< 01 04 06 04 B0 00 02 00 00 81 0D                         #   1200, dp 2, vol% -> 12.00 vol%
> 01 06 07 D0 00 40 88 B7                                  # ZERO key (response echoes)
> 01 10 00 23 00 04 08 13 88 00 0A 03 E8 00 0A E2 A6       # write alarm 1 limits
< 01 10 00 23 00 04 30 00
```

### 2.11 O2 measurement quality: what the library can and cannot claim

What is known:

- **The Modbus O2 value is quantized to 100 ppm** (§2.5). Quantization alone contributes
  about 29 ppm RMS (step/√12). For a 0.10-percentage-point depletion, two independently
  quantized endpoints can differ by up to 10 % of the depletion.
- **The ZPA's catalog performance may not describe this O2 cell.** The catalog gives
  repeatability ±0.5 % FS, linearity ±1 % FS and drift ±2 % FS per week (ZPA manual §9).
  The owner reports a Hummingbird Premus paramagnetic cell, probably an upgrade, so
  these figures may belong to a different O2 option or to the NDIR channels.
  Hummingbird's public pages give only the range (0–100 % O2). The variant and its
  numeric specification are still to be obtained (§13.4).
- **Resolution depends on the cell.** If the Premus is repeatable to well under 100 ppm,
  Modbus quantization is the binding limit and an analog path may do better. Whether it
  does depends on how the ZPA generates its analog output, which is unknown. If the cell
  is near the catalog figure, neither path is better than the cell.
- **The response-time setting shapes every O2 value.** 40084, "O2 meter response time",
  is 15 s on the bench unit. It is applied inside the analyzer and matters for
  calorimetry time alignment as much as resolution does.
- **The bench unit's calibration state is doubtful.** At capture, O2 read 20.29 vol%,
  CO2 −0.11 vol% and CO −0.009 vol%. The error log is full of zero and span calibration
  errors 5, 6 and 7 on all three channels.
- **capa already takes O2 from an analog input.** Its cone profile requires the oxygen
  channel group to be `ANALOG_IN`, and it wants analyzer serials, response times,
  calibration gases and zero/span recency
  (`capa/experiment/profiles/cone_calorimeter.py`).

Consequences for this plan:

1. 0.1.0 leads with **status, validity and metadata**:
   - `read_metadata()` (§7.2);
   - the validity flag (§8);
   - label provenance.
2. The documentation states that Modbus O2 is **not validated for oxygen-consumption
   calorimetry**. It stays unvalidated until the owner sets acceptance limits and a
   simultaneous Modbus-against-analog comparison has been run across relevant O2
   changes, ranges and response settings (Phase 4).
3. capa's zero/span-recency preflight cannot be answered from firmware 1.02: it has no
   calibration log, and the error log records only failed calibrations. On this unit the
   operator must supply it.

---

## 3. Layered architecture

```
anyserial                    raw async serial bytes
anymodbus                    RTU framing, CRC, bus lock, inter-frame timing, decoders
   │
   ▼
transport/     base.py       Transport Protocol + SerialSettings (default 38400 8-N-1)
               serial.py     SerialTransport over anyserial; exposes the real SerialPort
               fake.py       FakeTransport: scripted request -> reply bytes (arrow fixtures)
   │
   ▼
protocol/      base.py       ProtocolKind (MODBUS_RTU) + ProtocolClient Protocol
               modbus/
                 port.py     ModbusPort (internal): owns the one anymodbus.Bus for a port,
                             operation lock, attempt reports, quiet-window check
                 client.py   ModbusClient: block reads with retries and counters; the only
                             holder of an anymodbus.Slave; final write-envelope check
                 read_plan.py  region-aware coalescing planner, 64-word cap (reads only)
                 codec.py    scaled ints, int16 sign, BCD, long words, characters
                 errors.py   anymodbus exception -> FujiError mapping
   │
   ▼
registry/      registers.py  RegisterSpec + the full map, generated from stride patterns
               regions.py    read regions per function code (refinable per station)
               write_policy.py  WRITE_ENVELOPE (frozen) + OperationSpec table
               channels.py   ChannelId, ChannelRole, Gas, LabelSource
               units.py      Unit, coercion
               enums.py      AlarmMode, AlarmState, RangeMethod, ErrorCode, DisplayScreen, ...
               typecode.py   type-code decoder and the channel-layout table (ZPA)
   │
   ▼
devices/       profile.py    DeviceProfile (ZP_PROFILE): registry + regions + limits + identify
               capability.py Capability, SafetyTier, Availability
               models.py     Reading, Frame, ChannelStatus, AnalyzerStatus, DeviceInfo, logs, ...
               decode.py     pure decoders: register banks -> identity, frames, logs, metadata
               reads.py      read procedures: a plan, a client and a decoder; stateless
               session.py    THE choke point: gates, lock, deadlines, verify, caches, counters
               analyzer.py   Analyzer — the public facade
               metadata.py   AnalyzerMetadata: the read-only settings snapshot for consumers
               settings.py   typed settings groups + SettingsSnapshot writes      (Phase 6)
               operations.py auto-cal / auto-zero / blowback / return-to-measure (Phase 6)
               factory.py    async open_device(...)  <- THE entry point
               discovery.py  find_devices, DiscoveryResult, DiscoverySummary
               snapshot.py   DeviceSnapshot, FujiDeviceSnapshot
   │
   ▼
streaming/ sinks/ sync/ cli/              ported from the siblings (§7)
manager.py                                 after 0.1.0 (§7.4)
testing/    arrow.py (fixtures)  mock.py (MockAnalyzer, MockLine)  pair.py (wiring, bank)
errors.py  config.py  units.py  version.py  _logging.py  _lock.py  _deadline.py  py.typed
```

There is no `devices/panel.py`: key simulation is not planned (§6.5).

### What is deliberately different from the siblings

| Topic | Sibling practice | fujilib | Why |
|---|---|---|---|
| Command layer | `sartoriuslib` / `watlowlib`: `Command[Req, Resp]` with one variant per protocol | none; a small internal `OperationSpec` table covers the five commands | one protocol; a variant layer would have exactly one variant |
| Read path | `watlowlib`: one transaction per parameter | coalesced block reads | a poll touches about 120 registers |
| Stream handed to `anymodbus` | `servomexlib`: its own `ByteStream` wrapper | the **real `SerialPort`** | see §4.1 |
| Bus objects | `servomexlib`: one `Bus` per analyzer | one `Bus` per port | the bus lock then serializes multi-drop by construction |
| Read retries | performed inside `anymodbus` | performed and counted by `ModbusClient` | `recoverable_error_count` must be observable (§4.5) |
| `Sample` field names | `servomexlib`: `monotonic_ns` | `t_mono_ns`, `t_utc`, `t_midpoint_mono_ns` | unified API §C, which `capa` reads (§7.8) |
| `Sample` shape | `watlowlib` long; `sartoriuslib`/`alicatlib` one per poll | one per poll, carrying a `Frame` (wide) | analyzer status travels once with all channels; matches capa's `wide_row` path (§7.6) |
| Per-call `timeout` | `servomexlib`: accepted, then discarded | honoured as an operation deadline | §6.4 |
| Writes | `watlowlib`: request value echoed back | read-back verification | `anymodbus` does not compare write echoes |
| Access checks | `watlowlib`: a write to a read-only row reaches the wire | rejected before I/O, plus a frozen envelope check at the client | §5.4 |

---

## 4. Modbus layer

### 4.1 Binding to `anymodbus` — hand it the real port

`anymodbus.Bus` accepts any `anyio.abc.ByteStream`. Two behaviours are active **only when
the stream is literally an `anyserial.SerialPort`**: they sit behind an `isinstance` check
in `Bus._maybe_drain` / `_maybe_reset_input`.

1. `drain_after_send` waits until the bytes have actually left the UART before starting
   the response clock. This matters on half-duplex RS-485.
2. `reset_input_buffer_before_request` discards stale bytes after an error.

Baud-derived timing uses a separate typed-attribute lookup. A wrapper could forward the
baud rate, but it would still lose the two behaviours above.

`servomexlib` wraps the port in its own `ByteStream` and loses both. It had a reason
(three protocols share one port and `AUTO` must sniff raw bytes); fujilib does not. So the
fujilib `Transport` is a thin lifecycle object that **exposes** the stream instead of
**being** one:

```python
class Transport(Protocol):
    @property
    def label(self) -> str: ...
    @property
    def is_open(self) -> bool: ...
    @property
    def settings(self) -> SerialSettings: ...
    @property
    def stream(self) -> anyio.abc.ByteStream: ...  # what ModbusPort binds the Bus to
    async def aclose(self) -> None: ...
```

`SerialTransport.stream` is the `SerialPort`. The simulated analyzer uses
`anyserial.testing.serial_port_pair()`, whose ends are also real `SerialPort` objects, so
tests exercise the same code path as hardware. `FakeTransport.stream` is itself (a
scripted `ByteStream`) and is used only for byte-exact fixture replay. It therefore never
exercises drain or input reset; those are covered by the port-pair tests.

`open_device(port: str | Transport)` keeps the family signature. `SerialTransport.open`
opens a port under its canonical name: `anyserial.canonical_port_name()` of the name
with surrounding whitespace removed. On Windows that drops a `\\.\` or `\\?\` prefix and
upper-cases the rest, so `COM8`, `com8` and `\\.\COM8` are one port; elsewhere a symlink
resolves to its target. The canonical name labels the transport in errors and rows, and
is the key a manager will share ports by (§4.2). An empty name is refused before
anything is opened.

### 4.2 `ModbusPort` — one bus per serial port

```python
class ModbusPort:  # internal; not exported
    def __init__(
        self,
        transport: Transport,
        *,
        request_timeout: float = 0.5,
        inter_frame_idle: float = 0.005,
        startup_settle: float = 0.05,
    ) -> None: ...
    def client(self, address: int) -> ModbusClient: ...  # validates 1..31
    @property
    def lock(self) -> anyio.Lock: ...  # operation lock
    async def aclose(self) -> None: ...
```

`ModbusPort` never hands out a raw `anymodbus.Slave`. Only `ModbusClient` holds one
(§5.4).

**Two locks, with distinct jobs:**

- `anymodbus.Bus`'s internal lock serializes **transactions**.
- `ModbusPort.lock` serializes **operations**: sequences that must not interleave with
  another station's or task's traffic, such as write-then-verify or the two blocks of a
  poll. It is acquired through `maybe_acquire()` (`watlowlib`'s `_lock.py`) so the
  recorder can hold it across a batch without deadlocking. It keeps the application's
  own traffic contiguous. It cannot freeze the analyzer's updates or the operator at the
  panel.

**Ownership.** Each `ModbusPort` has exactly one owner:

- A standalone `open_device` on a port name.
- A caller-supplied `Transport`. Opening a second device on a `Transport` that already
  has a bus is refused.
- In a future manager, one ref-counted port per canonical name.

There is no process-global cache shared across event loops. A second open with
incompatible serial or timing settings is refused. A failed `identify()` during open
closes what it opened.

- **One port per transport.** A small registry, keyed by the transport's identity and
  holding its ports weakly, refuses a second open port on a transport. It is a guard, not
  a cache.
- **Closing waits for the operation in progress.** `ModbusPort.aclose()` takes the
  operation lock (shielded) before it releases the transport. So no request of the old
  port is still waiting for its reply when a new port starts sending on the same line.
- **A closed port or transport is refused before anything is sent.** The request fails
  with a definite `FujiConnectionError`, never a write of unknown outcome.

**Inter-frame timing.** The idle gap must be measured from the end of the last reply of
any kind: a normal reply, an exception, a CRC failure or a timeout. `anymodbus` 0.2.0
measured it from the request after anything but a normal reply (§2.4). 0.2.1 measures it
from the end of every transaction, including a cancelled one (§4.7 item 1), and fujilib
requires 0.2.1.

`anymodbus` waits out the gap, and the one-shot startup settle, *inside* the call.
Since 0.3.0 it reports every attempt to a transaction observer, including when the
request had been sent (after the gap, the write and the drain) and when the attempt
ended (§4.7 item 7). A `TransferTiming` is those two moments, so it never includes time
spent waiting for the line. A timestamp taken before the call would be early by the gap:
5 ms, or about 16 ms on Windows, moving `t_mono_ns` by up to 8 ms. With 0.2.1 fujilib
avoided that by waiting out the gap itself.

**Resynchronization: the quiet window.** A cancelled or timed-out transaction can leave a
reply in flight. FC03/04 replies carry no register address, so a late reply can be
accepted as the answer to the next read of the same length. Clearing the input buffer
before a request removes only the bytes already received.

So after any attempt whose outcome is uncertain (cancelled, timed out, damaged,
mismatched, or of the wrong length), the bus sends nothing until the window has passed
since the attempt ended. Meanwhile it reads and discards whatever arrives, and it also
waits for the line to be quiet for the inter-frame gap. This is `anymodbus`'s
`late_reply_window` (0.3.0, §4.7 item 10), which fujilib sets to `resync_window`. A
well-formed reply of the right length, or an exception reply, is certain and opens no
window.

- **0.1 s rather than `request_timeout`.** It covers the slowest documented reply:
  30 ms turnaround, 133 bytes at 38400 baud and 16 ms of adapter latency come to 84 ms
  (`anymodbus.estimate_late_reply_window`). One lost reply then costs a poll about
  0.6 s, not 1 s.
- **Deadlines inside the window are refused.** The port tracks the window from the
  observer's reports. A caller whose operation deadline ends inside it is refused before
  any I/O, with `FujiResyncRequiredError`.
- **The port assumes the worst.** The reports do not say whether an uncertain attempt's
  request reached the wire (§4.7 item 15), so the port assumes it did.
- A test shows the hazard is real: with the window at 0, a reply that arrives 50 ms after
  its read timed out is taken as the answer to the next read, at another address.
- **[bench]** On the analyzer (findings §10.3) the hazard showed up as a lost request
  rather than stale data. With the window at 0, a read sent while a cancelled read's reply
  was still on the half-duplex line went unanswered in 23 of 30 trials, costing a 0.5 s
  timeout and a retry. With the 0.1 s window, 30 of 30 succeeded at once. The run was
  repeated on `anymodbus`'s `late_reply_window` (findings §10.5): 25 of 30 lost without
  it, 0 of 30 with it.

### 4.3 Read planner

`read_plan.py` turns a set of `RegisterSpec`s into the fewest block reads such that:

- every block lies inside a single valid region (§2.3);
- no block exceeds **64 words**;
- a multi-word value is never split across blocks;
- gaps are bridged only inside a region and only up to `max_gap` words.

It is a pure function with property tests for each invariant. It is used **only for
reads**; the write path never coalesces or bridges (§6.3). The hot paths are precomputed
at import:

| Operation | Blocks (FC04 unless noted) | Transactions |
|---|---|---|
| `poll()` | `0000h+61` (readings, ranges, alarms, calibration flags, error summary) and `0083h+60` (active errors, hold flags, display state, alarm 6) | 2 |
| `identify()` | `0425h+35` (ranges), `0448h+34` (type code and serial), `0000h+36` (readings); probes: `03E8h+49` (clock and A/D; on failure, separately), `047Ah+3`, `1000h+9` | 3 + 3 probes |
| `read_metadata()` | FC03 `0000h+64`, `0040h+64`, `0080h+36`; FC04 `03E8h+7` (clock, if supported) | 4 |
| `read_settings()` | FC03 `0000h+64`, `0040h+64`, `0080h+44` | 3 |
| `read_ranges()` | `0425h+35` | 1 |
| `read_calibration_log(ch)` | 5 × 63 words + 1 × 45 words from `1000h + 360(ch−1)` (7 whole records per full block), then the newest record again | 6 + 1 |
| `read_error_log()` | `003Dh+60`, `0079h+10` (whole records), then `003Dh+5` again | 2 + 1 |

`poll()` does not read the clock. `read_clock()` and `read_metadata()` do, and each clock
value carries its own read time.

**Poll outcomes:**

- **Block 1 succeeds, block 2 fails:** the whole poll is an error. The raw block-1 words
  are kept in the error context.
- **`poll(detail=False)`** reads block 1 only. Its readings have `status=None` and
  `valid=None` (unknown), never a manufactured "healthy". The recorder always uses the
  full poll.

**Log reads.** A log read spans several transactions, so a new entry can arrive
mid-scan. The reader reads the newest record again after the scan, compares it with the
first record of the scan, and rereads once if it changed. A second change is not chased.
An entry identical to the newest one (the same error, channel and minute) cannot be
detected this way.

The procedures behind this table live in `devices/reads.py`: each one is a precomputed
plan, the client and a pure decoder, with no state. What a decoder needs beyond the words
(the established channels, the ranges, the current range) is passed in by the session,
which caches it.

### 4.4 Response checks

Since 0.3.0, `anymodbus` checks each reply against its request inside the attempt
(§4.7 item 3).

- **A mismatched reply** raises `UnexpectedResponseError`: a register read of another
  length, or a write whose echo differs. On a read it is retried, and it opens a quiet
  window (§4.2), because it is most likely a late reply to another request.
- **A bad argument** raises `ConfigurationError`, a `ModbusError`. The client still
  validates its arguments before calling `anymodbus`, so none should reach it; a bare
  `ValueError` would be a bug and is not translated.
- **A value that fails to decode** is a `FujiProtocolError`, never a configuration error.

### 4.5 Retries and idempotency

`anymodbus` retries, with `RetryPolicy(retries=read_retries)` (default 2). Its default
policy retries a timeout or a damaged or mismatched reply, and only for idempotent
function codes, so a write is never retried.

Every attempt is reported to the port's observer, and the client counts from the
reports:
- every failed attempt, by kind;
- the retries;
- `recoverable_error_count`: when a read succeeds after k failed attempts, it grows by k
  (unified API §J).

| Operation | When the reply is lost, damaged, mismatched or of the wrong length |
|---|---|
| Any read | retried by `anymodbus` up to `retries` (default 2), every attempt reported and counted. An exception reply is an answer and is never retried |
| Setting write | not retried. The session reads the register back and reports verified, mismatch or unknown (§6.4) |
| Operation command (42002–42005) | not retried. The state is re-read from the status registers, and an ambiguous outcome is reported as unknown |

### 4.6 Error mapping

Applied at one boundary (`protocol/modbus/errors.py`, called by the client), always
`raise ... from exc`. Order matters: `FrameTimeoutError` (a `TimeoutError`) and
`TransportError` are `OSError`s, so the Modbus classes are matched first.

| `anymodbus` | fujilib |
|---|---|
| `IllegalFunctionError` | `FujiModbusIllegalFunctionError` |
| `IllegalDataAddressError` | `FujiModbusIllegalDataAddressError` |
| `IllegalDataValueError` | `FujiModbusIllegalDataValueError` |
| other `ModbusExceptionResponse` | `FujiModbusError`, with the code in the context |
| `FrameTimeoutError` | `FujiModbusTimeoutError` |
| `CRCError`, `ChecksumError`, `FrameError` | `FujiFrameError` |
| `BusClosedError`, `ConnectionLostError` (including `TransportError`, a port that fails mid-transaction) | `FujiConnectionError` |
| `ConfigurationError` | `FujiConfigurationError` (see §4.4) |
| `UnexpectedResponseError`, `ProtocolError` | `FujiProtocolError` |
| other `ModbusError`, e.g. `ModbusUnsupportedFunctionError` | `FujiModbusError`. Since 0.3.0 `anymodbus` raises that one only on the send side, which fujilib never reaches. A reply carrying a function code the client never sends is read to the idle gap, and the CRC decides: `CRCError` if damaged, `UnexpectedResponseError` if intact (§4.7 item 12) |
| `OSError`, `anyio.BrokenResourceError`, `BusyResourceError`, `ClosedResourceError` | `FujiConnectionError`, for a failure outside a transaction; inside one, `anymodbus` translates them |

**Writes.** An exception reply to a write is a definite refusal: nothing was applied.
Every other failure after the request may have gone out makes the outcome unknown and
raises `FujiWriteOutcomeUnknownError`, with the kind of failure in its context:

- a lost, damaged or mismatched reply;
- a port that fails while the client waits for the reply;
- an operation deadline that expires. This holds also when the caller enforces the same
  `Deadline` around the call, in which case the outermost scope catches the cancellation
  first.

A closed port or transport is refused before anything is sent (§4.2), so it stays a
definite `FujiConnectionError`.

Expiry of an operation deadline (§6.4) is a `FujiTimeoutError` carrying the operation
name, the elapsed time, the port and the station. For a read plan, it also carries the
blocks already read, which covers a deadline that expires between the two blocks of a
poll.

### 4.7 Changes to make upstream in `anymodbus` and `anyserial`

Item 1 was a **prerequisite of Phase 3** (0.2.1). Items 2–12 were released in 0.3.0
(2026-09-28), except item 6, which upstream declined; fujilib dropped its workarounds for
them (§12, Phase 3). They also benefit `servomexlib` and `watlowlib`. Items 13 and 14,
in `anyserial`, were released in its 0.2.0 (2026-09-28); fujilib dropped its own
port-name helper and the hardware tests' trio expected failure.

1. ~~**Record the completion of every transaction.**~~ **Released in 0.2.1
   (2026-09-28).** `_last_io_monotonic` is now set in a `finally` at the end of every
   transaction and broadcast (reply, exception, checksum or framing error, timeout,
   cancellation), with regression tests in `tests/integration/test_inter_frame_gap.py`.
2. ~~Replace the `isinstance(stream, SerialPort)` checks with a capability check~~ —
   0.3.0: any stream's async `drain()` / `reset_input_buffer()` is used.
3. ~~Verify the register count of read responses, and compare write echoes~~ — 0.3.0:
   `UnexpectedResponseError`, checked inside each attempt.
4. ~~A public request hook and per-function quantity limits on `MockSlave`~~ — 0.3.0:
   `MockSlave.handle()`, `ServerException`, `QuantityLimits`.
5. ~~Raise a `ModbusError` subclass, not bare `ValueError`, for bad arguments~~ — 0.3.0:
   `ConfigurationError`.
6. ~~A retry callback~~ — declined: `RetryPolicy` is a frozen, shareable value. The
   transaction observer (item 7) serves.

Found while building Phase 3 (none blocks fujilib; each has a workaround in place):

7. ~~**A transaction observer**~~ — 0.3.0: `TransactionInfo` per attempt, with
   `sent_at` after the gap, the write and the drain. fujilib's timestamps and counters
   come from it (§4.2, §4.5).
8. ~~**Public server-side request decoding**~~ — 0.3.0: request decoders, `MockServer`.
   fujilib's `MockLine` is a `MockServer` (§10).
9. ~~**Translate every port failure**~~ — 0.3.0: `TransportError`; the input reset is
   inside the error handling.
10. ~~**Late replies after cancellation**~~ — 0.3.0: `TimingConfig.late_reply_window`
    replaces fujilib's own quiet window (§4.2).
11. ~~`FaultPlan` docstrings~~ — 0.3.0.
12. ~~**A reply with a function code the framer cannot frame**~~ — 0.3.0, done
    differently: the reply is read to the idle gap and the CRC decides between `CRCError`
    and `UnexpectedResponseError`; reads retry either.

And in `anyserial`:

13. ~~`normalise_com_path` turns `\\?\COM8` into `\\.\\\?\COM8`, and there is no public
    helper to canonicalize a port name.~~ **Released in 0.2.0 (2026-09-28).** A `\\?\`
    path is opened unchanged, and `canonical_port_name()` gives every spelling of a port
    one name. fujilib's own helper, `transport/ports.py`, is gone (§4.1).
14. ~~**[bench] Trio cannot read a real COM port on Windows.**~~ **Fixed in 0.2.0
    (2026-09-28).** An idle `receive()` under trio raised `SerialError` (WinError 1460)
    about 1 ms after it was called.
    - `anyserial` reads with the "wait-for-any" `COMMTIMEOUTS` policy, under which an
      overlapped read with no data completes with `STATUS_TIMEOUT`, a success status.
      asyncio's Proactor returns it as 0 bytes and `anyserial` reissues the read. Trio
      raised it, and `anyserial` treated it as a failed port. Its trio read path now
      treats it as the empty completion asyncio reports.
    - The simulator's port pair does not take this path, so CI cannot catch it. The
      hardware tests carried a strict expected failure for trio on Windows until the fix;
      they now pass on trio (findings §10.4).

And again in `anymodbus`, found adopting 0.3.0:

15. **Say whether an attempt's request reached the wire.** `TransactionInfo` has
    `sent_at` (after the drain) but not the bus's own `request_on_wire`. A cancelled
    attempt may have sent its request, or not; `anymodbus` opens its late-reply window
    only in the first case. fujilib cannot tell the two apart, so its quiet-window check
    assumes the worse (§4.2).

---

## 5. Registry

### 5.1 `RegisterSpec`

```python
@dataclass(frozen=True, slots=True)
class RegisterSpec:
    name: str  # "calibration_gas.ch2.range1.span"
    group: str  # "Calibration gas": its heading in docs/registers.md
    table: RegisterTable  # HOLDING | INPUT
    address: int  # 0-based relative address, exactly as on the wire
    dtype: DataType  # UINT16 | INT16 | UINT32_LH | BCD | BOOL | ENUM | CHAR
    access: Access  # READ | READ_WRITE   (commands are OperationSpecs, §5.4)
    read_functions: frozenset[int]  # e.g. {0x03}
    write_functions: frozenset[int]  # e.g. {0x06, 0x10}, {0x10}, or empty
    safety: SafetyTier
    evidence: Evidence  # DOCUMENTED | OBSERVED | INFERRED | CONTESTED
    manual_ref: str  # "TN5A1190a p.28": a PDF page
    doc: str
    count: int = 1  # 1; 2 for a long word; the width of a character field
    scaling: Scaling = NONE  # NONE | FIXED(n) | BY_RANGE | BY_ALARM_TARGET | INLINE
    unit: str | None = None  # a fixed unit ("s", "%FS"); a scaled value's comes with its decimals
    minimum: int | None = None  # raw limits
    maximum: int | None = None
    enum: type[IntEnum] | None = None
    channel: ChannelId | None = None
    range: int | None = None
    alarm: int | None = None
    requires: Capability = Capability.NONE  # option / model / firmware gate
    notes: str = ""  # where the manuals disagree or the bench unit contradicts them

    @property
    def register_number(self) -> int: ...  # 40001- or 30001-based, for humans and docs
```

The map is written in **Python**, not JSON. Most of it is generated from stride helpers
(`for c in 1..5, for r in 1..2`), so 343 registers, plus the error log and the
1,800-word calibration log, come from about a hundred declarations that read like the manual's tables.
`watlowlib` uses JSON because its map is a 1,500-row vendor spreadsheet; here a typed
table is smaller and checked by mypy.

Registers the bench shows but the manual does not document carry `evidence=OBSERVED` or
`INFERRED` and are never writable. Documented registers the
bench unit contradicts carry `CONTESTED` and are read-only too (§2.6). The two logs
are `LogSpec`s (fixed-width records in per-channel regions), not 1,600 separate
fields. `RegisterRegistry` indexes the map by name and by address.

**Page references** are PDF page numbers, which match the page markers of the text
extracts. The printed page number is 3 lower in TN5A1190a, 13 lower in TN2ZPAb and 8
lower in TN5A1191b.

**Eager validation at import**, failing loudly as `FujiConfigurationError`:

- no two specs overlap within a table;
- each spec lies inside a valid region for **each function code it lists**;
- every spec with write functions lies inside `WRITE_ENVELOPE` (§5.4);
- names are unique; enum and limit metadata are consistent.

**Generated documentation.** `docs/registers.md` is **generated** from the registry
(`scripts/gen_register_docs.py`) and checked in CI with `--check`, following
`alicatlib`'s codegen check. It lists address, functions, safety tier, scaling, limits,
option scope, evidence and manual reference, and it is the artifact that gets reviewed
against the manual. The generator ships in the sdist. CI never needs the git-ignored
manuals.

### 5.2 Scaling rules

| Rule | Used by | Decimal point and unit come from |
|---|---|---|
| `INLINE` | Ch1–12 concentration | the two registers that follow it, read in the same block |
| `BY_RANGE` | calibration gas values, range values | 31087–31096 and 31067–31076, keyed by (channel, range) |
| `BY_ALARM_TARGET` | alarm limits | the `BY_RANGE` registers of the alarm's target channel |
| `FIXED(n)` | calibration deviation (%FS × 10) | the constant |
| `NONE` | counts, switches, times | — |

Alarm limits are per *alarm*, so their scaling is resolved through the alarm's target
channel first. The target register's encoding is contested (§2.6), so alarm limits are
decoded raw until the caller supplies the target channels.

### 5.3 `DeviceProfile` — how the rest of the family is added

Following `watlowlib` 0.7.0, the device type is first class from day one:

```python
@dataclass(frozen=True, slots=True)
class DeviceProfile:
    name: str  # "zp"
    registry: RegisterRegistry
    regions: RegionMap  # documented read regions
    max_words_per_request: int  # 64
    max_address: int  # 31
    default_protocol: ProtocolKind
    default_serial: SerialSettings  # 38400 8-N-1
    identify: IdentifyStrategy
    typecode_decoders: Mapping[str, TypeCodeDecoder]  # only "ZPA" ships


ZP_PROFILE: DeviceProfile  # ZPA / ZPB / ZPG / ZPAJ / ZPG3E (TN5A1190)
DEVICE_PROFILES: tuple[DeviceProfile, ...] = (ZP_PROFILE,)
```

A profile is shared and frozen. Per-station observations, such as a wider readable map
or which capabilities were found, live in the session. Another Fuji analyzer family with
a different map becomes a new profile and registry. Nothing above `devices/profile.py`
changes, but a profile never widens `WRITE_ENVELOPE`.

### 5.4 Write policy

`registry/write_policy.py` holds the only definition of what fujilib may ever write:

```python
WRITE_ENVELOPE: Final = (  # frozen; not derived from the registry or probing
    WriteRange(fc=0x06, first=0x0000, last=0x009D),
    WriteRange(fc=0x10, first=0x0000, last=0x00A3),
    WriteRange(fc=0x06, first=0x07D1, last=0x07D4),  # operation commands 42002-42005
)
```

- **00A4h–00ABh are excluded:** inferred coefficients (§2.6).
- **07D0h (key simulation) is excluded** (§6.5).
- **009Eh–00A3h are gated to ZPB/ZPG** by a model `Capability`, and are written only
  with FC10.

**Operations are not registers.** The four commands are `OperationSpec`s, each with a
dedicated facade method, its own safety tier and its own post-conditions. They are
unreachable through `write_parameter`, `SettingsSnapshot` or `fuji-configure apply`.

**The guarantee.** No supported fujilib operation can construct a write outside
`WRITE_ENVELOPE`. Three things enforce it:

1. Public writes resolve a **name** to the canonical, immutable registry entry. A
   caller-built `RegisterSpec`, a custom registry and an address in a settings file are
   all refused.
2. The session checks table, exact start, exact width, allowed function, value domain,
   capability and effective safety tier.
3. `ModbusClient`'s two write methods re-check every request against `WRITE_ENVELOPE` as
   the last step before `anymodbus`. That check is independent of the registry.

A caller who opens `anymodbus` directly is outside this guarantee, and the documentation
says so.

---

## 6. Session and safety

### 6.1 Gate ladder

`devices/session.py` is the only path from the facade to the wire. Every call walks these
gates, in order, **before any byte is sent**:

1. **State.** The session is open, not broken, and not awaiting resynchronization.
2. **Safety tier.** Anything above `READ_ONLY` needs `confirm=True`, else
   `FujiConfirmationRequiredError`. Names and file contents cannot lower a tier.
3. **Access.** Resolve by name to the canonical spec or operation (§5.4). Writing a
   read-only register is refused.
4. **Capability.** Option, model and firmware requirements. An `UNSUPPORTED` entry in
   the availability cache short-circuits.
5. **Validation.** Types, names, enum members, channel and range existence, and finite
   values.

Then, under the operation lock:

1. refresh the scaling if the value is scaled;
2. encode, and check the raw limits;
3. check the write envelope in the client;
4. do the I/O;
5. decode and verify;
6. update availability and counters.

A scaled value's raw limits can only be checked after its scaling is refreshed. So
validation is split: everything that needs no I/O happens first, and the final raw range
check comes before the first write frame.

### 6.2 Safety tiers

`sartoriuslib`'s four tiers, as an `IntEnum`. The tier follows the **effect** of an
operation, not merely whether it persists:

| Tier | Operations |
|---|---|
| `READ_ONLY` | every read |
| `STATEFUL` | `return_to_measurement()`, `start_blowback()` |
| `PERSISTENT` | settings writes that change only configuration (alarms, ranges, averaging, hold mode, response time) |
| `DANGEROUS` | `start_auto_calibration()` and `start_auto_zero_calibration()`; enabling or changing auto-calibration and auto-zero schedules; changing calibration-gas values or the calibration-scope settings (40026–40035) |

Calibration is `DANGEROUS` because it overwrites the calibration coefficients and is only
correct if the right gas is flowing. Scheduling a calibration is equally dangerous,
because it causes one later. Every `DANGEROUS` operation reports the channels and ranges
it will affect, including the widening by the "at once" and "both" settings, before it
asks for confirmation. The CLI additionally requires `--i-understand-this-is-destructive`
for that tier, as in the siblings.

`PERSISTENT` is a conservative classification. That settings survive a power cycle
without an explicit save is still unverified (§13.2).

### 6.3 Write path (Phase 6)

```
resolve name -> validate -> [refresh (channel, range) decimal point + unit] -> encode
  -> check raw limits -> choose FC from write_functions (FC06 only within 0000h-009Dh;
     otherwise FC10, quantity 1 allowed) -> envelope check -> write -> read back -> compare
```

- A mismatch raises `FujiVerificationError` with expected and observed values in the
  context.
- **No coalescing across unrequested registers.** Every FC10 word corresponds to an
  explicitly requested setting. The read planner's gap bridging is never used.
- **Snapshot apply** preflights the whole input before the first write. It rejects
  unknown names and operation names, writes in a defined order, and on failure reports
  which writes completed.
- **Settings the panel requires to be switched off first** (alarm settings, the
  auto-calibration schedule) are written by switching off, then writing, then verifying.
  If anything fails, the automatic function is **left off**, and the partial state is
  reported with both the primary and the cleanup errors. It is never re-enabled
  unconditionally in a `finally` block.

The manual does not state a write-endurance figure. fujilib therefore never writes
periodically, and the recorder has no write path.

### 6.4 Timeouts and uncertain outcomes

- **Per transaction:** `request_timeout`, set when the port is opened.
- **Per call:** `timeout=` is an **operation deadline** for the whole operation. It
  starts before lock acquisition and covers queue time, retries, scaling reads and
  verification. The remaining budget is passed to nested steps rather than restarted.
  `None` (the default) means no outer deadline, so bus timing and retries govern.

**A timeout is not a rollback.** A write can be accepted by the analyzer while its reply
is lost. So once a write request has been sent:

- the read-back runs in a shielded scope with its own short, bounded deadline;
- the result carries `write_state`: `"verified"`, `"mismatch"` or `"unknown"`, plus
  whether transmission started and any observed value;
- an unknown outcome raises `FujiWriteOutcomeUnknownError` (§9);
- nothing is retried to find out.

An idle command-status flag after a command can mean "never started" or "already
finished"; fujilib reports that ambiguity instead of guessing.

`servomexlib` found that an outer deadline equal to one request timeout cancels
legitimate mid-sweep retries, and resolved it by ignoring the argument. fujilib keeps the
argument meaningful by defining it as a deadline for the operation.

### 6.5 Remote panel and manual calibration — not planned

Manual zero and span calibration exist only as front-panel key sequences through register
42001. fujilib does **not** implement key simulation, for four reasons:

1. **The same keys reach factory mode.** The service manual (TN5A1191b-E §3.1) enters
   factory mode through the panel with a password printed in the manual, and "14.
   Coefficient" there edits calibration coefficients. Key simulation would break the
   factory-data guarantee (§5.4).
2. **A single key can force a calibration.** On calibration errors 5 and 7, ENT means
   "force calibration" (§2.8).
3. **Gas stability can't be judged over Modbus.** During output hold the concentration
   registers are frozen (§2.5), so a flat Modbus value while gas flows proves nothing.
4. **The scope is wider than the call.** "At once" and "both" settings extend one
   calibration to other channels and ranges (§2.6).

Auto calibration and auto zero (42003/42004) cover remote calibration where the gas system
is plumbed for it. If a workflow ever needs manual calibration remotely, it needs a new
design and hardware prototype first. That design must have verified state transitions,
operator-controlled progression, reporting of all affected channels, and state-specific
cleanup. Returning the display to measurement is not proof that a calibration stopped.

### 6.6 Probed capabilities

Features that depend on firmware, on options, or on registers the manual does not
document are never assumed. `identify()` probes each one and records an `Availability` in
the session:

| Availability | Set when |
|---|---|
| `SUPPORTED` | the probe returns data that validates |
| `UNSUPPORTED` | a well-formed probe inside one documented or observed block is answered with exception 02 |
| `UNKNOWN` | timeout, CRC or framing failure, or an exception that could mean a malformed probe |
| `INVALID_DATA` | readable, but the content does not validate (for example a clock that is not a date) |

Only `UNSUPPORTED` short-circuits later calls, with `FujiCapabilityError` before any I/O.
`reprobe(capability)` clears the entry.

| Capability | Probe (FC04) | Bench unit | Gives |
|---|---|---|---|
| `CLOCK` | 03E8h, 7 words; BCD date and time | supported | `read_clock()`, `AnalyzerMetadata.clock` |
| `ADC_VALUES` | 03EFh, 42 words | supported | `read_adc()` |
| `TYPE_CODE_EXT` | 047Ah, 3 words | unsupported | type-code digits 27–29 |
| `CALIBRATION_LOG` | 1000h, 9 words | unsupported | `read_calibration_log()` |

`CLOCK` and `ADC_VALUES` are undocumented. They are exposed because they are useful and
reading them is harmless.

- They are validated on every read and documented as observed on one unit.
- `read_adc()` returns the raw tuple plus a table-order interpretation. It is a service
  diagnostic, not a calibrated or higher-resolution gas measurement.
- The analyzer clock is naive local time with a two-digit year. On the bench it ran
  minutes behind the host. It never replaces host acquisition timestamps.

### 6.7 Caches

| Cache | Filled by | Invalidated by |
|---|---|---|
| `DeviceInfo` (type code, serial, channel layout) | `identify()` | explicit `identify()` |
| Established channels | assertion; first non-zero triple | never within a session |
| Range metadata (count, unit, value, decimal point per (c, r)) | `identify()` | every scaled write; `refresh_ranges()`; a change in the current-range registers seen by `poll()` |
| Availability per capability | first probe | `reprobe()`; `UNKNOWN` is retried on next use |
| Last `Frame` | `poll()` | next `poll()` |

---

## 7. Public API

### 7.1 Entry point

```python
async def open_device(
    port: str | Transport,
    *,
    profile: DeviceProfile = ZP_PROFILE,
    protocol: ProtocolKind | None = None,  # None -> profile default (MODBUS_RTU)
    address: int = 1,  # station No., 1..31
    serial_settings: SerialSettings | None = None,  # None -> 38400 8-N-1
    timeout: float = 0.5,  # per-transaction request timeout
    identify: bool = True,
    channel_map: Mapping[ChannelId, Gas] | None = None,  # asserted labels (§2.9)
) -> Analyzer: ...
```

`protocol` is kept for boundary harmony and for the `protocol` column in sink schemas; it
has one valid value today.

### 7.2 The `Analyzer` facade

```python
async with await open_device(
    "COM8",
    address=1,
    channel_map={ChannelId.CH1: Gas.CO2, ChannelId.CH2: Gas.CO, ChannelId.CH3: Gas.O2},
) as anz:
    info = await anz.identify()  # type code, serial, channels, ranges, capabilities
    meta = await anz.read_metadata()  # response times, cal gases, hold mode, clock, ...
    frame = await anz.poll()  # all channels + analyzer status, 2 transactions
    o2 = frame.channel("CH3")  # Reading(value=20.29, unit=Unit.VOL_PERCENT,
    #         valid=True, label_source=ASSERTED, ...)
```

| Group | Methods | Phase |
|---|---|---|
| Reads | `poll(*, detail=True)`, `read_channel(ch)`, `status()`, `channel_status(ch)` | 4 |
| Identity and metadata | `identify()`, `snapshot()` (no I/O), `read_ranges()`, `refresh_ranges()`, `read_metadata()` | 4 |
| Diagnostics | `read_clock()`, `read_adc()`, `reprobe(capability)` | 4 |
| Logs | `read_error_log()`, `read_calibration_log(ch=None)` | 4 |
| Parameters (read) | `read_parameter(name)`, `read_parameters(names)`, `read_settings()` | 4 |
| Streaming | `poll_samples()` (satisfies `PollSource` via `PollSourceAdapter`) | 5 |
| Parameters (write) | `write_parameter(name, value, *, confirm=False)`; settings helpers such as `set_range`, `configure_alarm`, `set_hold`, `set_response_time` | 6 |
| Operations | `start_auto_calibration`, `start_auto_zero_calibration`, `start_blowback`, `return_to_measurement`, `calibration_status()`, `wait_for_calibration(timeout=...)` | 6 |

Every I/O method takes keyword-only `timeout: float | None = None`. Everything above
`READ_ONLY` takes `confirm: bool = False`. Channel arguments accept `ChannelId` or `str`.

### 7.3 Sync facade

```python
from fujilib.sync import Fuji

with Fuji.open("COM8", address=1) as anz:
    print(anz.poll())
```

`SyncAnalyzer` is hand-written one-liners over a `SyncPortal` (`anyio` blocking portal),
as in every sibling. It is guarded by `alicatlib`'s stronger parity test: parameter names,
kinds **and** defaults must match the async method, and every public async method must be
covered.

### 7.4 Manager — after 0.1.0

The bench rig has one analyzer on one port, and the recorder needs only
`PollSourceAdapter`. `FujiManager` is therefore deferred until a multi-drop or
multi-analyzer need appears *(awaiting)*. When built, it follows the `watlowlib`
contract:

- named analyzers;
- canonicalized, ref-counted ports, where stations on the same port share one
  `ModbusPort` and different ports poll concurrently;
- `ErrorPolicy.RAISE | RETURN` applied uniformly to `poll`, `poll_samples` and
  `execute_each`;
- `DeviceResult[T]`.

Cadence is budgeted per port (§2.4). One slow or absent station must not grow queues
without bound.

### 7.5 Discovery

```python
async def find_devices(
    *,
    ports: Sequence[str] | None = None,
    addresses: Sequence[int] = (1,),
    profiles: Sequence[DeviceProfile] = DEVICE_PROFILES,
    per_probe_timeout_s: float = 0.3,
) -> list[DiscoveryResult]: ...
```

Read-only. The probe is an FC04 read of type-code digits 1–3, which must decode to `Z`,
`P` and a model letter. A CRC-valid exception reply counts as "a Modbus device, but not a
ZP analyzer". Baud is fixed, so a full scan of one port is 31 probes. Discovery never
raises for a probe failure; it returns an `ok=False` row. `DiscoverySummary` aggregates a
scan, as in `sartoriuslib`.

### 7.6 Streaming, recording and sinks

The recorder follows the **`sartoriuslib` / `alicatlib`** contract, with one wide sample
per poll (§13.1 #13):

```python
class PollSource(Protocol):
    async def poll(
        self, names: Sequence[str] | None = None
    ) -> Mapping[str, DeviceResult[Frame]]: ...


@asynccontextmanager
async def record(
    source: PollSource,
    *,
    rate_hz: float,
    duration: float | None = None,
    names: Sequence[str] | None = None,
    overflow: OverflowPolicy = OverflowPolicy.BLOCK,
    buffer_size: int = 64,
) -> AsyncGenerator[Recording[Mapping[str, Sample]]]: ...
```

- **One `Sample` per analyzer per tick, carrying the whole `Frame`.** Analyzer-level
  status (instrument error, alarms, auto calibration) travels once with all channels,
  and capa's adapter can follow its `wide_row` path (`capa/devices/alicat.py`) with no
  per-tick workaround. `watlowlib`'s long samples need one in capa (`tick_first`).
- **A failed poll** produces one `Sample` with `frame=None` and `error` set, so gaps are
  recorded rather than dropped. It does not produce twelve invented readings.
- **`sample_to_row(sample, channels)`** flattens a sample to columns fixed after
  `identify()`:
  - the header: `device`, `address`, `protocol`, `t_mono_ns`, `t_utc`,
    `t_midpoint_mono_ns`, `requested_at`, `received_at`, `latency_s`;
  - per established channel: `chN_value`, `chN_raw`, `chN_decimals`, `chN_unit`,
    `chN_gas`, `chN_label_source`, `chN_state`, `chN_valid`, `chN_hold`,
    `chN_calibrating`, `chN_errors`;
  - analyzer-level: `instrument_error`, `calibration_error`, `analyzer_errors`,
    `alarm1` … `alarm6`, `auto_calibration_running`;
  - `error_type` and `error_message`, `None` on a successful poll.

  Every value is a scalar (`float`, `int`, `str`, `bool` or `None`), because capa's
  `SourceRecord.row` accepts nothing else. Datetimes are ISO strings, error codes are
  sorted and comma-joined (`""` when none), and alarm states are lower-case names (or
  the raw number when undocumented). An error row has the same keys, with `None` in every
  reading and analyzer column. `chN_state` is one token of the closed `ReadingState`
  vocabulary (§8), which capa stores as `ChannelSample.status`.

  The schema is fixed before the first row, including when the first row is an error:
  `row_columns(channels)` gives every column's type, so a sink never infers types from
  an error row. One column table backs `Reading.as_dict()`, `Frame.as_long_rows()` and
  `sample_to_row()`. An established channel never disappears, so `SchemaLock` holds.
- **Long rows are a helper, not the recorder's shape.** `Frame.as_long_rows()` produces
  one row per channel with the analyzer status repeated, for SQL unions with long-format
  siblings.
- **Batches and summary.** One tick is one batch, overflow drops whole batches, and the
  `AcquisitionSummary` counts polls, not rows.
- **Disconnects** end the recording with an error unless the caller supplies a reconnect
  policy. This is decided explicitly, not inherited from a copied recorder.
- **Sinks** for 0.1.0 are memory, CSV and Parquet. SQLite, JSONL and Postgres follow when
  someone needs them. All take `SchemaLock`.

Modbus is request/response only, so `StreamMode` has one member, `POLL`.

### 7.7 CLI

Plain `argparse`, each `main(argv=None) -> int`, each drivable with `--fixture`.

| Command | Purpose | Phase |
|---|---|---|
| `fuji-decode` | decode a hex frame or a register dump offline | 1 |
| `fuji-read` | one-shot poll, status, identity, metadata, logs | 4 |
| `fuji-discover` | scan ports and station numbers | 4 |
| `fuji-stream`, `fuji-capture` | live stream; record to a sink | 5 |
| `fuji-diag timing` | read-only link timing, busy-wait gaps measured from the reply | 5 |
| `fuji-configure` | `dump` (0.1.0) / `diff` / `apply` (0.2.0, `--confirm`, settings names only) | 5 / 6 |

The CLI uses the same session and write policy as the facade; it has no private write
path.

### 7.8 Unified device-library API conformance

The cross-library contract (`UNIFIED_API_HANDOFF.md`) is in none of the repositories. The
table below is the common subset recovered from `watlowlib`, `sartoriuslib` and
`alicatlib` code, their `test_unified_api.py` files, and watlowlib's CHANGELOG. Sections
D, L and N are referenced nowhere and remain unknown (§13.4). `servomexlib` diverges from
the contract in places; fujilib follows the contract.

| § | Requirement | fujilib |
|---|---|---|
| A | `open_device` is the canonical entry point; async context manager and `close()`; cleanup on failed open | §7.1 |
| B | `DiscoveryResult(ok, port, address, baudrate, protocol, device_info, error, elapsed_s)`; `DiscoverySummary` | §7.5 |
| C | `Sample` carries `t_mono_ns`, `t_utc`, `t_midpoint_mono_ns`, `requested_at`, `received_at`, `latency_s`. `t_mono_ns`/`t_utc` are the request/reply **midpoint** of the concentration block, and `requested_at`, `received_at` and `latency_s` are that block's, so `t_utc` is their midpoint | §8 |
| E | `DeviceResult.success()` / `.failure()`; `PollSourceAdapter(name, device)` over the `poll(names) -> Mapping` contract | §7.6 |
| F | typed `FujiTransientTransportError`: **deferred**, as in `watlowlib` and `alicatlib`; revisit if a cold-open race is observed | §9 |
| G | `ErrorContext.address` | §9 |
| H | `DeviceSnapshot` + `FujiDeviceSnapshot`; `snapshot()` is awaitable and does no I/O; fields `name`, `model`, `firmware` (None: not readable), `serial`, `connected`, `last_error`, `recoverable_error_count`, `captured_at` | §8 |
| I, M | `Recording` with `stream`, `summary`, `rate_hz`; mutable `AcquisitionSummary` | §7.6 |
| J | `Session.recoverable_error_count`: failed read attempts that a later attempt of the same read recovered (the client's `recoverable_error_count`) | §4.5 |
| K | `to_pint()` covering every `Unit` member, exported at top level and from `fujilib.units`; `vol%` and `ppm` differ by 10⁴, and `to_pint` never converts values. The strings are ones capa's unit registry parses: `vol%` → `percent`, `ppm`, `mg/m**3`, `g/m**3`, and `None` for an unknown unit | §8 |
| 6 | top-level exports: `open_device`, `find_devices`, `sample_to_row`, `PollSourceAdapter`, `Recording`, `DeviceResult`, `DiscoveryResult`, `DiscoverySummary`, `DeviceSnapshot`, `FujiDeviceSnapshot`, `to_pint` | `tests/unit/test_unified_api.py` |

`requested_at` and `latency_s` are populated on every polled sample (`servomexlib`
declares them but never sets them). `t_midpoint_mono_ns` is `None`: a configured
averaging period does not reveal the actual integration window. Response-time and
averaging settings are reported in `AnalyzerMetadata` instead of shifting timestamps.

**The capa adapter.** It will not be a thin wrapper. capa's adapters are 850–1,040 lines
each plus a 200–340-line simulator, and capa has no gas-analyzer adapter yet. The fuji
adapter follows `capa/devices/alicat.py`:

- a `wide_row` `SourceRecord` per tick;
- a new `FujiChannel(device, channel)` binding type;
- `ChannelSample.status` carrying validity;
- a simulator, descriptor, discovery and handshake hooks;
- an operator-declared expected gas per channel, checked against the asserted map. This
  is the pattern of the watlow adapter's `wire_temperature_unit`: a mismatch quarantines
  the channel with a `DeviceEvent`.

A spike of this adapter against `MockAnalyzer` is part of Phase 4 (§12), in a capa branch
authorized separately.

---

## 8. Data models

All `@dataclass(frozen=True, slots=True)`, except the recorder's mutable
`AcquisitionSummary` and `Recording`. `StrEnum` / `IntEnum` / `Flag` throughout.

```python
class ChannelId(StrEnum):      CH1 = "CH1" ... CH12 = "CH12"
class ChannelRole(StrEnum):    INSTANTANEOUS, O2_CORRECTED, O2_CORRECTED_AVERAGE, O2_AVERAGE, UNKNOWN
class Gas(StrEnum):            NO, NOX, SO2, CO2, CO, CH4, O2, UNKNOWN
class LabelSource(StrEnum):    ASSERTED, TYPE_CODE, INFERRED, UNKNOWN
class Unit(StrEnum):           VOL_PERCENT = "vol%"; PPM = "ppm"; MG_M3 = "mg/m3"; G_M3 = "g/m3"; UNKNOWN = "?"
class ReadingState(StrEnum):   OK, ANALYZER_ERROR, CHANNEL_ERROR, CALIBRATING, AUTO_CALIBRATION, HOLD, SOURCE_INVALID, UNKNOWN

@dataclass(frozen=True, slots=True)
class ChannelStatus:                        # measured channels 1-5 only
    range: int                              # 1 or 2
    zero_calibrating: bool
    span_calibrating: bool
    auto_zero_running: bool
    auto_span_running: bool
    hold: bool                              # value is frozen, not live
    errors: frozenset[ErrorCode]            # errors 4-9 for this channel

@dataclass(frozen=True, slots=True)
class Reading:
    channel: ChannelId
    gas: Gas                                # UNKNOWN unless asserted or decoded
    suggested_gas: Gas | None
    label_source: LabelSource
    role: ChannelRole
    value: float | None
    unit: Unit
    raw_value: int                          # the signed integer from the register
    decimals: int                           # 0..3, so the exact decimal can be rebuilt
    status: ChannelStatus | None            # None for derived channels and poll(detail=False)
    state: ReadingState                     # `valid` derives from it (validity rule below)
    protocol: ProtocolKind

@dataclass(frozen=True, slots=True)
class AnalyzerStatus:
    instrument_error: bool
    calibration_error: bool
    errors: frozenset[ErrorCode]            # analyzer-level: 1, 2, 3, 10
    alarms: tuple[AlarmState | int, ...]    # alarm 1..6; an undocumented value stays an int
    peak_count: int
    peak_alarm: bool
    auto_calibration_running: bool
    display: DisplayState | None

@dataclass(frozen=True, slots=True)
class TransferTiming:                       # one Modbus transaction
    requested_at: datetime                  # UTC, tz-aware
    received_at: datetime
    t_request_mono_ns: int
    t_reply_mono_ns: int

@dataclass(frozen=True, slots=True)
class Frame:
    readings: tuple[Reading, ...]           # established channels only
    analyzer: AnalyzerStatus | None         # None when poll(detail=False)
    protocol: ProtocolKind
    readings_timing: TransferTiming         # block 1: every concentration
    status_timing: TransferTiming | None    # block 2
    raw: bytes                              # every block of the poll, concatenated
    def channel(self, cid: ChannelId | str) -> Reading: ...
    def as_long_rows(self, *, device: str, address: int) -> list[dict[str, object]]: ...

@dataclass(frozen=True, slots=True)
class RangeInfo:
    channel: ChannelId; count: int; units: tuple[Unit, ...]
    full_scale: tuple[float, ...]; decimals: tuple[int, ...]

@dataclass(frozen=True, slots=True)
class ChannelInfo:
    channel: ChannelId
    gas: Gas
    suggested_gas: Gas | None
    role: ChannelRole
    label_source: LabelSource
    derived_from: ChannelId | None = None   # source of a corrected value or average

@dataclass(frozen=True, slots=True)
class DeviceInfo:
    model: str                              # "ZPA"
    type_code: TypeCode                     # raw string + whatever decodes
    serial_number: str                      # the manual's "board" code, e.g. "N8A0259T"
    channels: tuple[ChannelInfo, ...]
    ranges: tuple[RangeInfo, ...]
    capabilities: Capability
    availability: Mapping[Capability, Availability]
    protocol: ProtocolKind
    address: int
    serial_settings: SerialSettings         # the settings actually in use
    health: DeviceHealth                    # OK | PARTIAL | FAILED
    firmware: str | None = None             # not readable: always None

@dataclass(frozen=True, slots=True)
class AnalyzerMetadata:                     # read_metadata(); what capa's snapshot carries
    serial_number: str
    ranges: tuple[RangeInfo, ...]
    current_range: Mapping[ChannelId, int]
    response_time_s: Mapping[ChannelId, int]          # where labels say which channel is O2 / NDIR
    response_time_ndir_s: tuple[int, ...]             # 40076-40082: NDIR components 1-4
    response_time_o2_s: int                           # 40084
    moving_average: tuple[AveragePeriod, ...]         # 40085-40092: 'orders' 1-4
    calibration_gas: Mapping[tuple[ChannelId, int], tuple[float | None, float | None]]  # zero, span per range
    calibration_scope: Mapping[ChannelId, CalibrationScope]               # 40026-40035
    hold_mode: HoldMode
    output_hold: bool
    auto_calibration: AutoCalibrationSchedule
    auto_zero: AutoZeroSchedule
    clock: datetime | None                  # analyzer clock, naive; None if unavailable
    clock_read_at: datetime | None          # host UTC time of that read
    captured_at: datetime

@dataclass(frozen=True, slots=True)
class AdcValues:                            # service manual "A/D data"; observed interpretation
    inputs: tuple[int, ...]                 # No. 0-4: NDIR Ch1-4 and the O2 sensor input
    temperatures: tuple[int, ...]           # No. 5-9
    resistances: tuple[int, ...]            # No. 10-13 and 17-20
    pressure: int                           # No. 14
    reference_voltage: int                  # No. 15
    ground: int                             # No. 16
    raw: tuple[int, ...]                    # all 21 counts, in table order
    received_at: datetime
    t_mono_ns: int

@dataclass(frozen=True, slots=True)
class PartialTimestamp:                     # logs carry no year (and errors no month)
    month: int | None; day: int; hour: int; minute: int
    def resolve(self, clock: datetime, *, max_age: timedelta) -> datetime | None: ...
    #   the unique matching date within max_age before clock; None if none or ambiguous

@dataclass(frozen=True, slots=True)
class ErrorLogEntry:       code: ErrorCode; channel: ChannelId | None; at: PartialTimestamp
@dataclass(frozen=True, slots=True)
class CalibrationLogEntry: channel: ChannelId; range: int; kind: CalibrationKind
                           detector_count: int; deviation_percent_fs: float; at: PartialTimestamp

@dataclass(frozen=True, slots=True)
class Sample:                               # one per analyzer per tick, unified API §C
    device: str
    address: int
    frame: Frame | None                     # None when error is set
    protocol: ProtocolKind
    t_mono_ns: int                          # midpoint of the concentration block
    t_utc: datetime                         # same instant, wall clock
    requested_at: datetime
    received_at: datetime
    latency_s: float
    t_midpoint_mono_ns: int | None = None
    metadata: Mapping[str, str] = ...
    error: FujiError | None = None
```

**Validity rule.** `Reading.state` is one token of `ReadingState`, and `Reading.valid`
derives from it: `True` for `ok`, `None` for `unknown`, `False` otherwise. The state is:

- `unknown` when the status block was not read, or the concentration's decimal point does
  not decode;
- otherwise the first of these that holds: `analyzer_error` (instrument error, or error
  1, 2, 3 or 10), `channel_error` (an error 4–9 on the channel), `calibrating` (zero or
  span, manual or automatic, on the channel), `auto_calibration` (auto calibration
  running), `hold`;
- `source_invalid` for a derived channel (O2-corrected or an average) whose source
  channel or O2 channel is not `ok`. When its source is not known (an unlabelled channel
  above 5), any invalid measured channel makes it invalid;
- `ok` otherwise.

Raw values are always kept; validity flags them, it does not hide them.

There is one row flattener, `sample_to_row()`; `Reading.as_dict()` delegates to the same
column definitions so the two cannot disagree (they do in `servomexlib`).

---

## 9. Error hierarchy

One root, and a frozen `ErrorContext` with these fields:

- `command_name`, `protocol`, `port`, `address`, `channel`;
- `register_address`, `function_code`, `request`, `response`;
- `elapsed_s`, `extra`.

`ErrorContext` has `.merged()`, and there is `FujiError.with_context()`.

```
FujiError
├── FujiConfigurationError
│   ├── FujiValidationError
│   └── FujiConfirmationRequiredError
├── FujiTransportError
│   ├── FujiTimeoutError                    also: operation deadline expired
│   │   └── FujiWriteOutcomeUnknownError    write sent, outcome not established (§6.4)
│   ├── FujiConnectionError
│   └── FujiResyncRequiredError             port awaiting resynchronization (§4.2)
├── FujiProtocolError
│   ├── FujiFrameError
│   ├── FujiDecodeError                     a register value that does not decode
│   ├── FujiVerificationError               read-back did not match the write
│   ├── FujiProtocolUnsupportedError
│   └── FujiModbusError
│       ├── FujiModbusIllegalFunctionError      (also FujiProtocolUnsupportedError)
│       ├── FujiModbusIllegalDataAddressError   (also FujiProtocolUnsupportedError)
│       ├── FujiModbusIllegalDataValueError
│       └── FujiModbusTimeoutError              (also FujiTimeoutError)
├── FujiCapabilityError
│   └── FujiFirmwareError
└── FujiSinkError
    ├── FujiSinkDependencyError             (also FujiConfigurationError)
    ├── FujiSinkSchemaError
    └── FujiSinkWriteError
```

Multiple inheritance uses a single root and marker mix-ins with no competing `__init__`,
covered by a construct / `with_context` round-trip test. Each class documents whether it
is retryable. The unified API's typed transient error (§F) is deferred (§7.8).

---

## 10. Testing strategy

**Simulated analyzer.** `fujilib.testing` puts `MockAnalyzer` stations on a `MockLine`,
over an `anyserial.testing.serial_port_pair()`. Both ends of the pair are real
`SerialPort` objects, so the real framer, CRC, drain, input reset and timing code run.
Each station is fujilib's own (§13.1 #26): the register model, the exception profiles,
the write list and the per-request faults. The line is `anymodbus`'s `MockServer`
(0.3.0), with a small `MockSlave` adapter per station.

- **One line, one reader.** `MockServer` reads each request frame once. It drops a frame
  with a bad CRC or for an absent station, so the analyzer stays silent as the manual
  says, and routes the rest to stations by number.
- **Two exception profiles.** Both follow the manual (TN5A1190a p.13): a request starting
  where its function code cannot be used answers 02; one whose count runs past the
  existing registers, or is over 64 words, answers 03. The bench unit agrees on every
  point but one: FC01/02 answer 02 there (`BENCH_1_02`), not 01 (`DOCUMENTED`).
  Function codes never sent to hardware answer as the manual says in both.
- **Region map** of §2.3 and the 64-word cap. The bench's readable FC04 block
  03E8h–0479h replaces the documented 0425h–0469h by default; a firmware-2.24 variant
  adds 047Ah and the calibration log.
- **Writes against an independent list.** The simulator accepts only the documented
  writes minus 00A4h–00ABh and 07D0h. The list is written out in `testing/mock.py`, not
  imported from `WRITE_ENVELOPE`, so a widened envelope fails the tests. Any other write
  raises `MockWriteViolation`, which fails the test. Operation commands are recorded;
  their effects are not simulated yet.
- **Reply faults per request:** drop, delay (a late reply), bad CRC, wrong word count,
  wrong function code, garbage, or an exception. Each fault applies once or every time, to
  every request or to the ones a predicate selects.
  - A write whose reply is lost or damaged is still applied, as on the analyzer. A write
    answered with an injected exception is not.
  - A delayed reply holds up the line, as a slow analyzer does on a half-duplex line.
    That is what lets the late-reply hazard of §4.2 be reproduced.
- **A record of every request**, with its `(fc, address, count)`, arrival time and reply
  time, so the inter-frame gap can be measured at the device end.
- **Test controls** set registers by registry name, and an `on_request` hook changes state
  between two requests, e.g. a hold that starts between the two blocks of a poll.

It is loaded from a `MockAnalyzerConfig` (station, profile, register banks, regions).
`DEFAULT_ZPA_BANK` is a **sanitized** bank derived from the bench capture: the documented
read blocks and the clock/A/D observations, with the factory calibration blocks omitted
(§13.1 #11). It is package data, `fujilib/testing/zpa_bench_documented.json`, so it works
from an installed wheel (§13.1 #31). It is made by `scripts/sanitize_capture.py` from the
coherent block capture (findings §4.3).

**Test layers**

| Layer | What it asserts |
|---|---|
| Golden frames | the manual's frames decode and encode byte-exactly |
| Registry | no overlaps; functions inside regions; writable specs inside the envelope; generated docs current |
| Codec (hypothesis) | scaling, sign, BCD and long-word round trips |
| Planner (hypothesis) | never over 64 words, never across a region, covers every requested register; calibration-log blocks end exactly at each channel's last record |
| Wire behaviour | exact transaction lists, e.g. `poll()` == `[(4, 0x0000, 61), (4, 0x0083, 60)]`; the whole bench bank read through client and simulator decodes exactly as the bank itself does |
| Timing | the gap, measured at the simulator, runs from the end of normal, exception, CRC-failed, timed-out and cancelled transactions, and between the client's own retries; a request is timed after the gap |
| Resync | a late reply to a timed-out or cancelled read is never accepted as the answer to a new same-length read at another address, and without the quiet window it is |
| Simulator | both exception profiles checked through plain `anymodbus`, and property-tested against a model of the region rules; forbidden writes fail the test |
| Private API | no fujilib module uses a private `anymodbus` or `anyserial` name |
| Gates | a refused operation leaves the mock's request log **empty**; preflight refusals send no write frame |
| Write policy | every public path (facade, CLI, snapshot file, forged spec, operation name in a settings file) is refused outside the envelope; FC10 spans never include unrequested words |
| Uncertain outcomes | lost write acknowledgement, cancellation after transmission, failed cleanup, a setting changed between write and verify |
| Validity | hold or instrument error arriving between the two poll blocks; block-2 failure; derived-channel validity; `detail=False` gives `valid=None` |
| Labels and presence | a populated channel reading an all-zero triple stays present; contradictory type codes; asserted map wins |
| Recording | error-first recording keeps a fixed schema; overflow drops whole batches; poll-rate counts polls; cancellation while a sink blocks |
| Faults | `FaultPlan` CRC corruption and dropped responses: reads retry and are counted, writes do not retry |
| Multi-drop | several stations behind the dispatcher; one absent station does not stall others unboundedly |
| Unified API | `test_unified_api.py`, import symmetry, sync parity |
| CLI | `main([...])` in-process with `--fixture` |

Backends: asyncio, asyncio+uvloop, trio (AnyIO pytest plugin). Settings:
`filterwarnings = ["error"]`, `xfail_strict`, `--strict-markers`.

**Hardware tests** live in `tests/hardware/`, doubly gated (deselected by `addopts`, and
skipped unless the environment variable is `1`). Each marker needs its own authorization
from the owner before it is ever run:

| Marker | Variable | Covers |
|---|---|---|
| `hardware` | `FUJILIB_ENABLE_HARDWARE_TESTS` | reads, identify, metadata, logs, discovery, link timing |
| `hardware_stateful` | `FUJILIB_ENABLE_STATEFUL_TESTS` | settings writes with restore, blowback, return-to-measurement |
| `hardware_destructive` | `FUJILIB_ENABLE_DESTRUCTIVE_TESTS` | auto calibration and auto zero, with calibration gas |

Bench configuration comes from `FUJILIB_HARDWARE_PORT` and `FUJILIB_HARDWARE_ADDRESS`.
`docs/hardware-test-day.md` is the written procedure. Timing probes must busy-wait,
measure gaps from the reply, randomize gap order and keep individual outcomes. On this
development machine serial probing runs through Bash → Python, as recorded for
`servomexlib`.

The simulator validates library integration. It does not validate USB timing, UART
drain on real hardware, or analyzer semantics it was programmed to assume.

---

## 11. Build, tooling and repository scaffolding

The family skeleton is copied, not reinvented. The newest and most evolved copy of each
file is used.

| File | Source | After copying |
|---|---|---|
| `.editorconfig`, `.python-version`, `.github/dependabot.yml`, `LICENSE` | `servomexlib` | none |
| `.gitattributes` | `servomexlib` | add `*.pdf binary` |
| `.github/workflows/ci.yml` | `servomexlib` | bump `actions/checkout` to `watlowlib`'s pin; add a core-only import job (no extras) |
| `.github/workflows/docs.yml`, `release.yml` | `servomexlib` | package name, PyPI URL; make publishing depend on the same revision having passed lint, type, test and docs |
| PR and issue templates | **`watlowlib`** | reword for the analyzer |
| `.gitignore`, `.pre-commit-config.yaml` | `servomexlib` | package name; codespell word list |
| `pyproject.toml` | `servomexlib` | rename. Make `anymodbus>=0.2.1,<0.3` a **core** dependency and remove both `modbus` and `modbus-ascii` extras. Declare only CLI scripts that exist. Exclude `docs/manuals/` and raw captures from the sdist |
| `zensical.toml` | `servomexlib` | names, URLs, nav limited to pages that exist |
| `CONTRIBUTING.md`, `SECURITY.md` | **`watlowlib`** structure | rewritten for fujilib |
| `version.py`, `tests/conftest.py` | `servomexlib` | rename; remove the continuous-mode fixtures |
| `uv.lock` | — | generated with `uv lock` (CI sets `UV_FROZEN=1`) |

**Do not copy from `servomexlib`:** its PR template, issue templates and
`CONTRIBUTING.md` still contain `sartoriuslib` text (xBPI / SBI, `Balance`, a `commands/`
package), and its `SECURITY.md` has Servomex-specific wording.

- **Dependencies.** Core: `anyio>=4.14`, `anyserial>=0.2.0,<0.3` (0.2.0 carries §4.7
  items 13 and 14), `anymodbus>=0.3,<0.4` (0.3.0 carries §4.7 items 2–12 and needs
  `anyio` 4.14). Extras:
  `docs` now, `parquet` with the Parquet sink (others as sinks are added).
  Python ≥ 3.13.
- **Release process.** Update `CHANGELOG.md`, make an annotated tag `vX.Y.Z`, publish a
  GitHub Release. `release.yml` then publishes to PyPI by trusted publishing with
  attestations, from the `pypi` environment, which accepts only `v*` tags. It first runs
  the whole of `ci.yml` (reused through `workflow_call`) and a docs build, so only a
  revision that passes both is published. Its artifact is named `release-dist`, because
  the reused CI workflow already uploads `dist` in the same run and artifact names must
  be unique per run. The name `fujilib` was still unclaimed on PyPI at Phase 0
  (2026-09-28).
- **Manuals.** The three PDFs live in `docs/manuals/`, which is git-ignored (done
  2026-09-28, following the family precedent). Left directly in `docs/` they would have
  been published to GitHub Pages, included in the sdist, and rejected by the pre-commit
  large-file limit. A plain-text extract of each manual sits beside its PDF so the
  contents can be searched; page markers in the extracts match the PDF page numbers.
  - Some spans in the manuals use embedded subset fonts with no Unicode mapping, so a
    plain extraction yields glyph IDs: the ZPA manual's "+29 shift". Regenerate the
    extracts with `scripts/extract_manuals.py`, which repairs those glyphs from
    evidence in each PDF: mappings learned from correctly mapped spans of the same font,
    per-font glyph offsets, and a few glyphs identified from the surrounding words.
    Anything it cannot identify becomes U+FFFD, and its report lists what remains: 4
    characters in the ZPA manual and about 900 in the service manual's screen-shot fonts,
    whose subsets renumber their glyphs.
  - zensical has no option to exclude files, so a **local** docs build copies
    `docs/manuals/` into `site/`. Pages is deployed only from CI, where the manuals do not
    exist; never deploy a local build.
- **Evidence.** Future captures record exact probe arguments, timeout, idle and retry
  settings, package versions, individual failures and file hashes. They distinguish a
  coherent block capture from a bank assembled one word at a time over minutes.

---

## 12. Implementation plan

Each phase ends with CI green on the full matrix. Software exits and hardware exits are
separate gates. Effort figures are rough working days, judged after two reviews; they
assume the scope below, not the first draft's.

### Decisions still open

The capture policy (§13.1 #11) and the sample shape (#13) are settled. The remaining
items marked *(awaiting)* in §13.1 are each needed before the phase that uses them:

- the asserted channel map, before Phase 4;
- the 0.1.0 acceptance criteria for O2, before the Phase 4 O2 comparison.

### Phase 0 — Repository bootstrap (**done 2026-09-28**)

- ~~Move the manuals to `docs/manuals/` (git-ignored)~~, with `.gitignore` and text
  extracts; ~~`scripts/extract_manuals.py`~~ (§11).
- ~~`git init`; create the GitHub repository **empty**~~ (avoids `servomexlib`'s two
  root commits); ~~configure Pages (source: Actions), the `github-pages` and `pypi`
  environments, and the PyPI pending trusted publisher~~.
- ~~Copy the scaffolding per §11; write `README.md`~~.
- ~~Minimal package: `__init__.py`, `version.py`, `errors.py`, `_logging.py`,
  `py.typed`~~. `errors.py` already holds the whole §9 hierarchy.

Differences from the plan above:

- `anymodbus>=0.2,<0.3` instead of `>=0.2.1`, because 0.2.1 was not released yet. The
  floor was raised to `>=0.2.1` once it was, the same day.
- The raw bench capture is git-ignored and excluded from the sdist (§13.1 #11).
- The Phase 2 probe scripts were reformatted to pass lint. Their syntax trees are
  unchanged, so they behave exactly as before.

*Exit:* lint, type check, six-cell test matrix, build and docs build are green.

### Prerequisite — `anymodbus` 0.2.1 (**done 2026-09-28**)

- ~~Record the completion of every transaction (§4.7 item 1), with a regression test
  for exception, CRC and timeout paths.~~
- ~~Then raise fujilib's dependency floor to `anymodbus>=0.2.1`.~~

*Exit:* released to PyPI, before Phase 3 begins. Met: 0.2.1 was published on
2026-09-28.

### Phase 1 — Registry, codecs, models, contract exercise (**done 2026-09-28**)

- ~~`registry/`: units, enums, channels, regions, write policy, the complete register
  map, the ZPA type-code decoder and channel-layout table~~.
- ~~`protocol/modbus/codec.py` and `read_plan.py`~~.
- ~~`devices/models.py`, `devices/capability.py`~~.
- ~~`scripts/gen_register_docs.py`, generated `docs/registers.md`, CI check~~.
- ~~`fuji-decode`~~.
- ~~**Contract exercise:** a synthetic three-channel `Frame` → `Sample` →
  `sample_to_row()` → the intended capa `SourceRecord` and `ChannelSample` mapping, as a
  test~~ (`tests/unit/test_contract_capa.py`). It fixes units, timestamps, validity
  columns, error rows and the snapshot shape before the facade is built.

*Tests:* golden frames; hypothesis round trips; registry and envelope validation; planner
invariants; one test per row of the channel-layout table (45 rows).

Differences from the plan above (decisions §13.1 #19–#25):

- Added `devices/decode.py`, pure decoders from register banks to every model, so
  `fuji-decode --dump` decodes a whole register bank and the client in Phase 3 only
  moves words.
- Built early, because the models and the contract test need them: `ProtocolKind`,
  `SerialSettings`, `Sample`, `sample_to_row()` with `row_columns()` and `ColumnSpec`,
  `DeviceSnapshot` and `FujiDeviceSnapshot`, and the arrow-fixture parser in
  `testing.py`. The sanitized bench bank is committed (§10).
- New in the registry: `Evidence.CONTESTED`, the `BY_ALARM_TARGET` scaling rule,
  `RangeIndex`, and `LogSpec` for the two logs (§2.6, §5.1, §5.2).
- `Reading` carries a `ReadingState` (§8), and rows gain `chN_state`, `alarm1` … `alarm6`,
  `address`, `protocol`, `error_type` and `error_message` (§7.6).
- Poll block 1 is `0000h+61` (§4.3).

*Exit:* every documented register is in the registry with a manual reference, and
`docs/registers.md` has been reviewed against the manual. The software part is met:
lint, both type checkers and 748 tests at 100 % coverage pass locally (Windows, Python
3.13). **The owner's review of `docs/registers.md` against the manual is outstanding.**

### Phase 2 — Bench probe (read-only part **done 2026-09-28**)

Standalone scripts using `anymodbus` directly. All traffic goes through `ReadOnlyStation`
in `scripts/_probe_common.py`, which exposes the four read function codes and nothing
else.

- `probe_connect.py` — find the station; passive listen; framing check.
- `probe_map.py` — dump the documented regions and the clock and A/D block in block
  reads; boundaries, the 64-word cap, unsupported function codes; decoded summary.
- `probe_scan.py` — read every address one word at a time to find the real map.
- `probe_link.py` — failure rate and latency against inter-frame gap. Its original sweep
  was confounded (§2.4); ~~give it a busy-wait mode that measures from the reply~~ — done
  as `--mode pairs` (findings §6.3).

*Output:* [protocol-findings.md](protocol-findings.md),
`tests/fixtures/captures/zpa_bench_20260928.json` (one word at a time) and
`tests/fixtures/captures/zpa_bench_block_20260928.json` (block reads, coherent), both
kept local (§13.1 #11). ~~The measured defaults go into `config.py` in Phase 3, each with
its measurement recorded beside it~~ — done: `fujilib.config` (§2.4).

*Remaining, read-only:*

- ~~after `anymodbus` 0.2.1, re-run the link probe with randomized gap order, all four
  normal/exception pairings and individual outcomes~~ — done 2026-09-28 (findings §6.3);
- ~~a block-read capture for coherent fixtures~~ — done 2026-09-28 (findings §4.3). It
  matches the register capture everywhere except the live words.

*Remaining, needing more:* the items in §13.2 that need a write, a power cycle, analog
wiring or calibration gas.

### Phase 3 — Transport, Modbus client and simulated analyzer (**done 2026-09-28**)

- ~~`transport/`, `protocol/base.py`, `protocol/modbus/{port,client,errors}.py`, including
  port ownership, client-side retries and counters, timing from the end of every
  transaction, and post-cancellation resynchronization~~.
- ~~`testing.py`: `MockAnalyzer` with both exception profiles, the multi-drop dispatcher,
  `mock_analyzer_pair()`, fixture loaders~~.
- ~~Client reads: frame, channel, status, identity, ranges, metadata, both logs, generic
  coalesced register reads~~.

*Tests:* wire behaviour, response-length checks, error mapping, retry counting, timing,
resync, multi-drop.

Differences from the plan above (decisions §13.1 #26–#31):

- **The simulator is fujilib's own**, not built on `MockSlave`: a `MockLine` dispatcher
  and a `MockAnalyzer` register model (#26, §10). `fujilib.testing` became a package:
  `arrow`, `mock` and `pair`.
- **The read procedures are a new module**, `devices/reads.py`, between the client and
  the session (#27, §4.3). `read_channel()` is `read_frame(...).channel(ch)` and belongs to
  the facade.
- **The client's FC06 and FC10 primitives exist**, with the envelope check last, but no
  public path reaches them (#28). The simulator records operation commands and does not
  act on them.
- **Resynchronization is a quiet window** of 0.1 s after any uncertain transaction, rather
  than listening for `request_timeout` under the lock (#29, §4.2).
- **Reads are also retried** after a framing error, an unexpected reply or a wrong word
  count (#30, §4.5).
- **The bench bank is package data** of `fujilib.testing` (#31).
- **Added along the way:**
  - `config.py` with the measured defaults (§2.4);
  - `_deadline.py` for operation deadlines (§6.4), and `_lock.py`;
  - `transport/ports.py`, and `RANGES_PLAN`;
  - the client waits out the inter-frame gap itself, so request timestamps are honest
    (§4.2);
  - the log reads check the newest record after the scan (§4.3);
  - `DOCUMENTED` and `BENCH_1_02` differ only on FC01/02, because the manual's own text
    for exception 03 covers a read that crosses a region end (§10).
- **Upstream findings** are §4.7 items 7–14.

*Exit:* the read path is fully covered without hardware. Locally (Windows, Python 3.13
and 3.14), lint, both type checkers, the docs build and 1233 tests at 100 % coverage
pass, on asyncio and trio. The uvloop backend and the Linux and macOS cells run only in
CI, which needs `phase-1` and this work pushed.

*Hardware check* (read-only, 2026-09-28, findings §10), all on the bench analyzer:

- every read procedure, and the eight hardware tests of
  `tests/hardware/test_hardware_client.py`, pass under asyncio;
- 300 polls ran back to back at 7.78 Hz with no failure;
- request timestamps come after the inter-frame gap;
- the quiet window turns a lost request (23 of 30 trials without it) into a clean read.

On Windows, trio cannot read a real COM port with `anyserial` 0.1.2 (§4.7 item 14).

*Adopting `anymodbus` 0.3.0* (2026-09-28). The release carried §4.7 items 2–12, so
fujilib dropped the workarounds they replace:

- its word-count check (§4.4);
- its own inter-frame gap wait, startup settle and end-of-transaction stamp; timestamps
  come from the transaction observer (§4.2);
- its quiet window, now `late_reply_window` (§4.2);
- its retry loop; the counters come from the observer (§4.5);
- the `ValueError` and unsupported-function-code mappings (§4.6);
- the simulator's frame reader and dispatcher; the line is `MockServer` (§10).

The port keeps the pre-send check against the quiet window.
`FailureKind.WORD_COUNT` is gone: a reply of the wrong length is now `UNEXPECTED`.

Checked without hardware (1,242 tests, 100 % coverage) and again on the analyzer
(findings §10.5).

*Adopting `anyserial` 0.2.0* (2026-09-28). The release carried §4.7 items 13 and 14:

- `transport/ports.py` is gone; `SerialTransport.open` names a port with
  `anyserial.canonical_port_name()` and still refuses an empty name (§4.1);
- the hardware tests lost their strict expected failure for trio on Windows.

Checked without hardware (1,247 tests, 100 % coverage) and on the analyzer, where the
eight hardware tests pass under asyncio and trio (findings §10.4).

### Phase 4 — Session, facade, discovery, sync, capa spike (4–5 days, plus a hardware session)

- `devices/{session,analyzer,factory,discovery,snapshot,profile,metadata}.py`.
- `sync/` with the parity test; `fuji-read`, `fuji-discover`, `fuji-configure dump`.
- `tests/hardware/test_hardware_reads.py`, `docs/hardware-test-day.md`, quickstarts.
- **capa adapter spike** against `MockAnalyzer`, in a capa branch (1–2 days, separately
  authorized): the `FujiChannel` binding, the `wide_row` record, `ChannelSample` status
  and expected-gas assertion.
- **O2 comparison, if the analog output is wired:** simultaneous Modbus and analog O2
  across relevant O2 changes, ranges, hold and response settings. Record the Premus
  variant and its specification.

*Exit:*

- the read-only API passes against the bench analyzer;
- the unified-API tests are green;
- the capa spike consumes real rows without adapter-side reshaping.

### Phase 5 — Streaming, sinks, CLI (5–6 days software; soak separate)

- Port `streaming/` (the `sartoriuslib`-shaped recorder), `sinks/` (memory, CSV,
  Parquet) and their sync wrappers.
- `fuji-stream`, `fuji-capture`, `fuji-diag timing`.
- Guides (including the O2 scope statement of §2.11), API reference stubs, one example.

*Software exit:* all of the above green without hardware.
*Hardware exit:* a 24-hour recording at 1 Hz on the bench that checks:

- expected tick and row counts;
- status and provenance retention;
- bounded memory;
- error accounting;
- clean shutdown;
- readable output.

A controlled disconnect/reconnect is tested separately.

**Release 0.1.0** — read-only monitoring, metadata and acquisition.

### Phase 6 — Settings and operation commands (5–7 days, stateful hardware)

- Session write path: name resolution, validation, scaling refresh, function selection,
  envelope check, read-back verification, uncertain-outcome reporting.
- `devices/settings.py` for a reviewed subset of fully specified settings, and
  `SettingsSnapshot` diff/apply with preflight.
- `devices/operations.py`: auto calibration, auto zero, blowback, return to measurement,
  `calibration_status()`, `wait_for_calibration()`, each reporting its affected channels
  and ranges.
- `fuji-configure diff/apply`; `docs/safety.md`.

*Tests:* the write-policy and uncertain-outcome suites; write-and-restore hardware tests;
persistence across a power cycle. Each hardware session is authorized separately.
**Release 0.2.0.**

### Phase 7 — Remote panel and manual calibration — not planned

See §6.5. Revisit only if a real workflow needs remote manual calibration. The work would
then start with a design and a hardware prototype, and be estimated after that.

### Phase 8 — Family and downstream (as hardware, manuals and need allow)

- The full capa adapter and simulator (`capa/devices/fuji.py`, in the `capa` repository),
  building on the Phase 4 spike.
- `FujiManager` and multi-drop, when needed.
- Further sinks (SQLite, JSONL, Postgres), when needed.
- Validate ZPB / ZPG / ZPAJ / ZPG3E; add their type-code tables; exercise the RS-232C
  path.
- The remaining upstream `anymodbus` item (§4.7 item 15).

### Sequencing

```
decisions ─► Phase 0 ─► Phase 1 ─► Phase 3 ─► Phase 4 ─► Phase 5 ─► 0.1.0 ─► Phase 6 ─► 0.2.0
                            ▲         ▲           ▲
anymodbus 0.2.1 ────────────┼─────────┘           │
Phase 2 (bench) ────────────┴─────────────────────┘   (findings feed registry, defaults, O2 scope)
```

Total to 0.1.0 is about 18–23 working days (3.5–4.5 engineer-weeks) plus the hardware
session and the soak, and another 1–1.5 weeks for 0.2.0. Re-estimate after the first
complete read-and-record slice.

---

## 13. Open items

### 13.1 Decisions for the owner

| # | Decision | Recommendation |
|---|---|---|
| 1 | ~~`Sample` and timestamp names: unified API (`t_mono_ns`, …) or `servomexlib`'s (`monotonic_ns`)~~ | **RESOLVED 2026-09-28: unified API.** It is what `capa` reads from `watlowlib`, `sartoriuslib` and `alicatlib` |
| 2 | Scope of the first release | Read-only monitoring, metadata and acquisition (0.1.0); a reviewed subset of writes in 0.2.0 |
| 3 | Key simulation and manual calibration | **Not planned** (§6.5) |
| 4 | ~~Where the manuals live~~ | **RESOLVED 2026-09-28:** `docs/manuals/`, git-ignored |
| 5 | Names: `Analyzer`, `FujiManager`, `FujiError`, `Fuji.open`, `fuji-*` | As listed |
| 6 | Keep the `protocol=` argument although it has one value | Keep, for harmony and sink schemas |
| 7 | Search-console override and `robots.txt` (`sartoriuslib` and `watlowlib` have them, `servomexlib` does not) | Owner's call |
| 8 | Docs palette, README badges, LICENSE holder spelling, CHANGELOG separator | Purple; none; "GraysonBellamy"; hyphen with ISO date |
| 9 | Coverage floor (no sibling enforces one) | Owner's call |
| 10 | ~~Caller-asserted serial number~~ | **MOOT 2026-09-28:** the serial is readable. The register block the manual calls "board" holds `N8A0259T`, matching the nameplate |
| 11 | ~~The register capture contains this analyzer's serial number and factory calibration tables~~ | **RESOLVED 2026-09-28:** the serial number may appear in the public repository and docs. The raw capture stays local (git-ignored, excluded from the sdist). A sanitized subset without the factory calibration blocks is committed for `DEFAULT_ZPA_BANK` (since Phase 1; package data of `fujilib.testing` since Phase 3, #31), taken from the coherent block capture (findings §4.3) |
| 12 | ~~Expose the undocumented real-time clock and A/D values (§2.6)~~ | **RESOLVED 2026-09-28: yes**, as probed capabilities (§6.6) |
| 13 | ~~Sample shape: one per poll carrying a `Frame` (wide), or one per channel (long)~~ | **RESOLVED 2026-09-28: wide** (§7.6). All channel values come from one read, and capa's `wide_row` path needs no per-tick workaround. `Frame.as_long_rows()` serves consumers that want long rows |
| 14 | Asserted channel map for the bench rig | Ch1 CO2, Ch2 CO, Ch3 O2; inferred labels never bind scientific channels |
| 15 | O2 acceptance for calorimetry (minimum depletion, response time, uncertainty; any standard that applies) | Needed before Modbus O2 is described as fit for purpose (§2.11) |
| 16 | ~~Fix `anymodbus` and release 0.2.1 before Phase 3~~ | **RESOLVED 2026-09-28:** released; fujilib requires `anymodbus>=0.2.1` |
| 17 | Defer `FujiManager` until after 0.1.0 | Yes, unless capa needs multi-analyzer management sooner |
| 18 | Sinks for 0.1.0 | Memory, CSV, Parquet |
| 19 | Pure decoders and `fuji-decode --dump` in Phase 1 | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed |
| 20 | `Reading.state`, a closed validity vocabulary, and a `chN_state` column (§8) | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed |
| 21 | Row encoding: `alarm1` … `alarm6`, comma-joined error codes, `address`, `protocol`, `error_type`, `error_message` (§7.6) | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed |
| 22 | `requested_at` / `received_at` are the concentration block's, so `t_utc` is their midpoint (§7.8 C) | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed |
| 23 | `manual_ref` gives PDF page numbers (§5.1) | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed |
| 24 | Commit the sanitized bench bank in Phase 1 (§10) | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed; taken from the coherent block capture (#11) |
| 25 | Poll block 1 is `0000h+61` (§4.3) | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed |
| 26 | The simulator: build on `anymodbus.testing.MockSlave`, or a fujilib-owned line dispatcher and register model (§10) | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: fujilib's own, on `anymodbus`'s public CRC and ADU helpers only. `MockSlave.serve()` owns its stream, so stations cannot share a line. Since `anymodbus` 0.3.0 the line is its `MockServer`; the stations stay fujilib's |
| 27 | Where the read procedures live, between the client (words) and the session (gates, caches) | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: `devices/reads.py`, stateless, over the `ProtocolClient` contract |
| 28 | Build the client's FC06/FC10 primitives, with the envelope check, before any public write path | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: yes. The simulator's write list is written out independently of `WRITE_ENVELOPE` |
| 29 | Resynchronization after an uncertain transaction (§4.2) | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: a quiet window of 0.1 s from the end of the transaction, relying on `anymodbus`'s input reset, instead of listening for `request_timeout` under the lock. Since `anymodbus` 0.3.0 it is that library's `late_reply_window`, which also reads and discards the late bytes |
| 30 | Which read failures are retried (§4.5) | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: a timeout, a bad CRC, a malformed frame, an unexpected reply and a wrong word count; never an exception reply. Since `anymodbus` 0.3.0 this is its default retry policy, and it does the retrying |
| 31 | Where the bench bank lives | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: package data of `fujilib.testing`, so `DEFAULT_ZPA_BANK` works from an installed wheel |

### 13.2 Hardware verification

Answered by the read-only probes of 2026-09-28. Details and data are in
[protocol-findings.md](protocol-findings.md).

| # | Question | Result |
|---|---|---|
| 1 | Inter-frame gap and latency | **≤ 1 ms after any reply, normal or exception**, measured from the reply by busy-wait, in all four normal/exception pairings in randomized order. The first draft's "20 ms after an exception" was a measurement artifact (§2.4). 12–27 ms per word read, 50–63 ms per 64 words |
| 24 | The link re-run after `anymodbus` 0.2.1 | Done (findings §6.3). Confirms item 1, including normal → exception. `anymodbus` 0.2.1 keeps its 5 ms idle after every reply on the analyzer (minimum 5.2 ms, 0 of 1,000 failed); 0.2.0 collapsed it to about 0.1 ms after an exception |
| 2 | Reply to unsupported function codes | FC01 and FC02 answer exception **02**, not 01. FC08 not sent |
| 3 | 65 words; region boundaries | exception 03; a block crossing a region end is also exception 03; a read starting outside the map is 02 |
| 5 | Registers 40165–40172 | four low-word-first long words of 1,000,000; *interpreted* as interference compensation coefficients (inferred) |
| 6 | Type and serial encoding | ASCII, one character per register, low byte |
| 7 | Populated channels | Ch1 CO2, Ch2 CO, Ch3 O2; unused channels return an all-zero triple (which a populated channel can also return at zero) |
| 9 | Negative concentrations | two's complement |
| 10 | Long-word order | low word first (confirmed on the coefficients and A/D values; the calibration log itself is absent) |
| 16 | O2 resolution | **two decimals: 0.01 vol% per step.** Comparison with the analog output still to do |
| 17 | Type code | `ZPACBJY1MPFYYYYYY2DEYAYAY0`; does not fully match the current code table (§2.9) |
| 19 | Map beyond 2000h | Sampled every 256 addresses and every multiple of 1000; nothing found |
| 20 | The scan's 43 malformed replies | all re-read as exception 02 (3 of 3 each); link artifacts |
| 28 | fujilib's client, read procedures and quiet window on the analyzer | Done 2026-09-28 (findings §10). Every read procedure works; 300 polls at 7.78 Hz with no failure; with no quiet window a read after a cancelled one was lost in 23 of 30 trials, with the window never; stale data was never accepted |

Still open:

4. FC06 against 009Eh–00A3h. *Not needed: fujilib uses FC10 there (§6.3).*
8. Whether an O2-average channel exists, and at which index. *Needs a unit with the O2
   correction option.*
11. Calibration-log depth. *Needs firmware 2.24 or later.*
12. Whether key lock (40074) blocks Modbus writes or commands. *Phase 6.*
13. Whether "Ch n" alarm registers are indexed by alarm number, as assumed in §2.6.
    *Phase 6, or a unit with the alarm option.*
14. Whether settings survive a power cycle without an explicit save. *Phase 6.*
15. ~~Whether `COM10` and above need the `\\.\` prefix~~ — `anyserial` already
    normalizes it; test canonical port identity instead (§4.1).
18. Whether the undocumented holding blocks accept writes. **Will not be tested.**
21. Modbus O2 against the analog output, simultaneously. *Phase 4, needs analog wiring.*
22. The Premus variant and specification. *Owner, from Hummingbird or Fuji.*
23. The analyzer's current calibration state (§2.11). *Owner.*
25. Linux timing. *If the rig runs Linux.*
26. The encoding of the schedule start hour and minute (§2.6). *Owner: read the
    auto-calibration start time the panel shows; no Modbus traffic is needed.*
27. The encoding of the alarm target channel (§2.6). *Phase 6, or a unit with the alarm
    option.*
29. ~~Trio with a real Windows COM port (§4.7 item 14)~~ — fixed in `anyserial` 0.2.0;
    the hardware tests pass on trio on the bench (findings §10.4). *Linux and macOS
    untested.*

### 13.3 The bench analyzer

| Field | Value | Source |
|---|---|---|
| Nameplate type | `ZPA3` | owner |
| Nameplate components | CO2 0–10 %, CO 0–1 %, O2P 0–10 % | owner |
| O2 cell | Hummingbird Premus paramagnetic, probably an upgrade; variant and specification not yet known | owner |
| Serial No. | N8A0259 (`N8A0259T` in the registers) | owner; bench |
| Manufactured | 2018-02 | owner |
| Interface | RS-485 | owner |
| Adapter, port | DTECH USB–RS-485 (FTDI FT232R, default 16 ms latency timer), `COM8` | bench |
| Station No. | 1 | bench |
| Type code | `ZPACBJY1MPFYYYYYY2DEYAYAY0` | bench |
| Program version | **1.02** (shown on the display at power-on) | owner |
| Channels | Ch1 CO2 0–10.00 vol%, Ch2 CO 0–1.000 vol%, Ch3 O2 0–21.00 / 0–25.00 vol% | bench |
| Response time | 15 s on every channel | bench |
| Calibration state | doubtful: O2 20.29 vol%, CO2 −0.11 vol% at capture; error log full of calibration errors 5, 6, 7 | bench |

The predictions made from the nameplate held: the channel layout, ranges that differ
from the nameplate, and firmware older than 2.24. What "ZPA3" denotes is still unknown;
it is not in the manuals and the registers report `ZPA`.

**Firmware.** Version 1.02 is older than 2.24, which costs this unit two things over
Modbus: the calibration log and type-code digits 27–29. Neither is needed for
measurement, status, settings or commands. No update procedure is documented (§1,
non-goals), so the library is designed to work fully on this firmware rather than to
depend on an upgrade.

**Every [bench] statement in this document comes from this one unit on version 1.02.**
The manual was revised for 2.24, a major version later. The documented map was confirmed
here, but the undocumented regions and the replies to unsupported requests may differ on
other firmware and on ZPB / ZPG. They are treated as properties to probe or to configure
per profile, never as constants of the family.

### 13.4 Questions only the owner can answer

1. **The unified-API spec.** Is `UNIFIED_API_HANDOFF.md` recoverable anywhere? Sections
   D, L and N are unaccounted for.
2. **The Premus details.** Which Premus variant is fitted? Can its datasheet (range,
   repeatability, noise, drift, T90) be obtained from Hummingbird or Fuji? Was it a
   factory option or a retrofit?
3. **O2 for heat-release rate.** Does it come from the ZPA or from another analyzer? Is
   the ZPA's analog output wired to the NI DAQ, and at what resolution?
4. **The capture conditions.** Was the analyzer sampling ambient air when O2 read
   20.29 vol%? When was it last successfully zeroed and spanned?
5. **Automated calibration.** Is it needed, and is the gas system plumbed for auto
   calibration?
6. **Deployment.** Will the rig run fujilib on Windows or Linux? May the FTDI latency
   timer be lowered from 16 ms?
7. **Firmware.** Is a firmware upgrade through Fuji service plausible? It is the only
   route to the calibration log that capa's zero/span preflight wants.
8. **Other models.** Are other ZP models, or older Fuji analyzers, in scope for a first
   release?

---

## 14. Source documents and references

Manuals (in `docs/manuals/`, git-ignored; each has a `.txt` extract beside it):

| File | Document | Used for |
|---|---|---|
| `TN5A1190a-E_*.pdf` | INZ-TN5A1190a-E, *MODBUS communication* for ZPA / ZPB / ZPG / ZPAJ / ZPG3E (rev. Oct 2021) | the protocol authority: §2.1–§2.7, §2.10 |
| `TN2ZPAb-E_*.pdf` | INZ-TN2ZPAd-E, *Instruction Manual, NDIR Infrared Gas Analyzer, Type ZPA* (4th ed., June 2025) | channel layout and digit-7 options §2.9, error codes §2.8, calibration and hold semantics, specifications §2.11, external O2 input |
| `TN5A1191b-E_*.pdf` | TN5A1191-E, *Service Manual* ZPA / ZPB / ZPG (2nd ed., July 2018) | error criteria, factory-mode access and coefficient menu (§6.5), A/D table, model differences |

Bench evidence:

| File | Contents |
|---|---|
| [protocol-findings.md](protocol-findings.md) | what the bench analyzer does on the wire, 2026-09-28, including the corrected timing measurement |
| `tests/fixtures/captures/zpa_bench_20260928.json` | every readable register, FC03 and FC04, 0000h–1FFFh, assembled one word at a time; kept local, not in the repository (§13.1 #11) |
| `scripts/probe_*.py` | the read-only probes that produced both |

External:

| Source | Used for |
|---|---|
| Hummingbird Sensing, [Paracube Premus-Alpha](https://hummingbirdsensing.com/sensors/sensor/paracube-premus-alpha/) | O2 cell range (0–100 %); no numeric performance published there |

Sibling libraries and what each contributes:

| Library | Role for fujilib |
|---|---|
| `servomexlib` | closest analog (gas analyzer on `anymodbus`): frame-centric facade, gate ladder, read planner, `MockSlave`-based fake, probe scripts, newest tooling skeleton |
| `watlowlib` | parameter registry, `DeviceProfile`, manager concurrency contract, snapshot fields, PR and issue templates |
| `sartoriuslib` | four-tier `SafetyTier`, the recorder / `PollSource` contract and error samples, `DiscoverySummary`, CLI conventions |
| `alicatlib` | strict sync parity test, generated-artifact CI check, the wide-sample pattern capa's adapter follows |
| `anymodbus` ≥ 0.3 | Modbus engine: gap, reply checks, retries, late-reply window and transaction observer; its `MockServer` is the simulator's line (§4.7) |
| `anyserial` ≥ 0.2.0 | serial transport, canonical port names and test port pair; 0.2.0 reads a Windows COM port under trio (§4.7) |
| `capa` | downstream consumer; its adapters, `SourceRecord` shapes and cone profile define what the library must provide |
