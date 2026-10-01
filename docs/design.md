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
> Status: **proposal, revised 2026-10-01.** Phase 0 (repository bootstrap), Phase 1
> (registry, codecs, models and the sample shape), Phase 3 (transport, Modbus client,
> simulated analyzer and read procedures), Phase 4 (session, facade, discovery, sync
> and the read-only commands) and Phase 5 (recorder, sinks and the recording
> commands) are done; Phase 5's unplug test and 12-hour bench recording passed.
> Phase 6 (settings writes, settings documents and the operation commands) is done on
> the simulator and on the bench analyzer, and goes into 0.1.0 too (§13.1 #59). Phase 7
> (the front panel) was reopened by the owner on 2026-09-29 (§13.1 #71). Its first part, watching manual calibrations
> made at the panel, is done on the simulator and on the bench analyzer, and goes into
> 0.1.0. The key prototype ran on the bench on 2026-09-30 (findings §18). Its third
> part, a manual zero or span driven from the host with the calibration keys, is done on
> the simulator and on the bench analyzer (findings §19), and goes into 0.1.0 as well
> (§13.1 #95). **0.1.0 was released on 2026-09-30** (§12). Phase 8, the capa adapter,
> was begun on 2026-10-01 (§12; §13.1 #97–#106).
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
| Manual zero/span calibration is reachable **only by simulating front-panel keys**, and the same keys reach the factory menu | A manual calibration made at the panel is **watched** from the status registers, and one can be **driven** from the host with the calibration keys only, never MODE or SIDE; the write envelope checks the key's value (§5.4, §6.5, Phase 7). |
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
  register map, and writes restricted to a reviewed subset of it (§5.4).
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
  unit exposes some of them to FC03 (the linearization tables and the "other
  parameters"), but not the calibration coefficients (§2.3, §2.6). fujilib does not
  model them and will never write them. The keys it may simulate are the calibration keys only, never MODE or SIDE, so
  no key sequence it sends can open a menu (§6.5).
- Reading what the panel shows. Modbus reports which screen is shown, never its text,
  so a panel-only screen (the maintenance-mode calibration log, say) cannot be read by
  navigating to it (§13.1 #72).
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

- Eight read-only diagnostic and identification function codes were tried: FC07,
  FC08 (sub-function 0000h), FC0B, FC0C, FC11 (report server ID), FC14, FC18, and
  FC2B/0Eh (read device identification). All were answered with exception **01**
  (findings §15.1). No identification is available beyond the registers, and the
  program version is not readable at all.

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
| 04 | 0000h–00C1h, 03E8h–0479h | 03E8h–0424h holds a real-time clock and 21 A/D values; 046Ah–0471h the unsmoothed CO2 and CO detector counts; 0472h–0479h |
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
| Time of day | hour and minute are **BCD** (`23h` = 23) says the manual; the bench unit contradicts it for the schedule start times (§2.6); day of week 0–6 = Sun–Sat |
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
  written until the start time a panel shows settles it (§13.2 #26). The bench
  unit's panel cannot: without the option it has no auto-calibration menu.
- **[bench] The alarm target channel's encoding is undocumented.** The manual gives 0–6
  with no meaning. The bench unit reads 0–4 for alarms 1–5 (channel − 1?) and 12 for
  alarm 6, outside the documented range. The registers are contested, so alarm limits
  stay raw until the encoding is known (§5.2, §13.2 #27).
- **Response time is per NDIR component, not per channel.** 40076–40082 belong to NDIR
  components 1–4, and O2 has its own slot (40084) whatever its channel. Which output
  each of the four moving-average "orders" (40085–40092) averages is not stated.
- **The two cycle-unit registers differ.** The schedules use 0 = hours, 1 = days; the
  moving-average and measurement-point periods use 0 = hours, 1 = minutes.
- **The manuals disagree on seven limits** (peak-alarm concentration, O2 reference,
  response time, moving-average period, the schedule cycles, and, found with the
  write path, the alarm limits, which the ZPA manual gives as 0–100 %FS with high
  above low by more than the hysteresis, and the calibration gases, whose span it
  gives as 1–105 %FS). The MODBUS manual itself defers setting ranges to the
  instruction manual (TN5A1190a p.28). So a read-only register keeps the MODBUS
  manual's limits, and a writable one takes the narrower of the two: a span gas is
  written as 1–105 %FS. The response time is the exception. The MODBUS manual gives
  0–60 s and the instruction manual 1–60 s; **[bench]** 0 switches the filter off
  (findings §20), so it is written as 0–60 s (§13.1 #107). Each conflict is recorded
  in the register's notes.
- **The ZPA has no calibration valves of its own** (TN5A1191b p.10). Auto calibration
  and auto zero calibration drive external zero and span gas valves through the
  contacts of the DIO option (type-code digit 22; ZPA manual p.29). The bench unit's
  digit 22 is `A`, the fault contact only. Without the option and the gases
  plumbed, a calibration would most likely be computed from whatever gas is at the
  inlet (an inference: no manual says the command is refused).
- **Decoding is total.** An undocumented enum value is kept as a plain integer, and a
  concentration whose decimal point does not decode becomes a reading with no value and
  state `unknown`, rather than failing a whole read.

The manual labels the alarm registers "Ch1…Ch5", but the instruction manual describes
*alarms* 1–6, each with a **target channel** (40121–40126). fujilib models them as
alarms, not channels.

The "calibration range" setting (40031–40035) widens a calibration, manual or
automatic, to both ranges of its channel (ZPA manual p.45). The "manual zero mode"
(40026–40030) widens only a manual zero started at the panel: auto calibration and
auto zero calibration zero every enabled channel together whatever it says (ZPA
manual p.47). Which channels an automatic calibration touches is the list enabled
for auto calibration (40021–40025), on each channel's auto-calibration range
(40116–40120); the same list and ranges drive auto zero calibration (ZPA manual
p.59). Any operation that calibrates reports every channel and range it will affect
(§6.2).

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
| 00B4–00B6 | 30181–30183 | display state: screen, manual-calibration step (on the measurement screen; **[bench]** a menu's page number elsewhere), top channel |
| 00B9 | 30186 | "do not use"; **[bench]** the last manual calibration: 0 once a channel is selected, 4 while it runs, 6 when it has finished |
| 00BC, 00BE | 30189, 30191 | manual-calibration cursor channel; alarm 6 state |
| 00BD | 30190 | not listed; **[bench]** the key being pressed at the panel, in 42001's key codes; a key written to 42001 does not show (findings §18.1) |
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

**[bench]** Readable but not modelled (findings §15):

- **046A–0471 (FC04):** the CO2 and CO detector counts before smoothing, each twice.
  They are what the maintenance "Sensor Input" screen shows; the A/D values above are
  a smoothed copy. 0472h–0478h read 1 now and then, for no known reason.
- **The two FC03 factory blocks:** these hold no zero or span coefficients. The
  coefficients the factory screen shows appear in no readable word, so a calibration
  can be recorded only as it happens (§6.5). For O2, the counts recorded then are
  enough: the span coefficient is 800 × span gas / (span count − zero count) in A/D
  No. 4's counts (findings §17.3).
  - 0C2D–0C34 are the factory menu's "other parameters". On the bench unit **range
    limit is off**, so readings are not held at 110 %FS, and zero limit is off, which
    hides negative values on the display only.
- **The "do not use" words 00B7, 00B8, 00BA, 00BB and 00BF–00C1:** 0 on every screen so
  far.

The "board" code (0462–0469) is the **serial number**.

**[bench] A manual calibration at the panel** (findings §14):

- The step register (30182) follows it: 4 or 7 while a channel is selected, 5 or 8
  while the gas settles, 6 or 9 while it runs, then 0. The screen register (30181)
  stays 0, measurement, throughout.
- On any other screen, 30182 numbers the menu's pages, and some of those numbers are
  step values: 5 while the factory password is entered, and 4 and 8 in factory mode
  (findings §15.2). So fujilib reads it as a step only on the measurement screen (#80).
- The per-channel zero and span flags (30050–30059) are set from the wait step to the
  end, so they cover manual calibration as well as automatic.
- 00B9h and 00BDh (table above) follow it too. They are modelled as observed
  registers (`display.calibration_result`, `display.key`), read with every poll.
- It leaves **no readable trace**: no holding word changes, including the two
  factory blocks. The raw A/D count of the channel's detector does not change
  either, so it records what the detector saw when the calibration ran.

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
panel's forced stop of an auto calibration or auto zero calibration works only while
key lock is off (ZPA manual p.55–57, p.62). The key register also reaches maintenance
and factory mode (§6.5), so fujilib writes it only with the six calibration keys, and
only from the front-panel driver of a manual zero or span (`devices/keys.py`).

**[bench]** A key written to 42001 acts as the same key at the panel, within a tenth
of a second, unless key lock is on, which swallows it (findings §18). 42002 returns
the display to measurement from a manual calibration's wait step, but leaves the
channel's calibration flag set (findings §18.4), so `return_to_measurement()` is
refused while a calibration flag is set (§13.1 #83).

A command's reply confirms that the analyzer accepted it, not that it finished
(TN5A1190a p.11, p.17). Input 30049 is one flag for auto calibration and auto zero
calibration alike, and a channel measures on its auto-calibration range for the
duration of one. No register shows blowback running, and blowback is in neither the
ZPA manual nor its code table.

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
   calibration log over Modbus (only on the panel), and the error log records only
   failed calibrations. On this unit
   fujilib records each zero and span it sees as it happens (Phase 7). One made while
   nothing is connected, the operator must still supply.

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
               encode.py     a caller's value -> the word a setting write sends
               writes.py     write procedure: one FC06 write, read back; outcomes
               session.py    THE choke point: gates, lock, deadlines, verify, caches, counters
               analyzer.py   Analyzer — the public facade
               settings.py   settings documents: diff and apply
               operations.py auto-cal / auto-zero / blowback / return-to-measure; plans, status
               panel.py      manual calibration at the front panel: plans, watching, events, records
               keys.py       front-panel keys over Modbus: a manual zero or span driven from the host
               steadiness.py whether a calibration gas has settled (pure)
               factory.py    async open_device(...)  <- THE entry point
               discovery.py  find_devices, DiscoveryResult, DiscoverySummary
               snapshot.py   DeviceSnapshot, FujiDeviceSnapshot
   │
   ▼
streaming/  sample.py poll_source.py recorder.py      samples, poll sources, record()
sinks/      base.py (rows, SchemaLock, pipe) memory.py csv.py parquet.py
sync/       analyzer.py discovery.py recording.py sinks.py portal.py
cli/        decode read discover configure stream capture diag calibrate
manager.py                                 after 0.1.0 (§7.4)
testing/    arrow.py (fixtures)  mock.py (MockAnalyzer, MockLine)  pair.py (wiring, bank)
errors.py  config.py  units.py  version.py  _logging.py  _lock.py  _deadline.py  py.typed
```

`devices/panel.py` watches manual calibrations made at the front panel and records
them. `devices/keys.py` drives one from the host, and is the only module that writes the
key register (§6.5); it records the run with the same tracker.

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
| `identify()` | `0425h+35` (ranges), `0448h+34` (type code and serial), `0000h+42` (readings and current ranges); probes: `03E8h+49` (clock and A/D; on failure, separately), `047Ah+3`, `1000h+9` | 3 + 3 probes |
| `read_metadata()` | FC03 `0000h+64`, `0040h+64`, `0080h+36`; FC04 `0025h+5` (current ranges), `03E8h+7` (clock, if supported) | 5 |
| `read_settings()` | FC03 `0000h+64`, `0040h+64`, `0080h+44` | 3 |
| `read_ranges()` | `0425h+35` | 1 |
| `read_calibration_log(ch)` | 5 × 63 words + 1 × 45 words from `1000h + 360(ch−1)` (7 whole records per full block), then the newest record again | 6 + 1 |
| `read_error_log()` | `003Dh+60`, `0079h+10` (whole records), then `003Dh+5` again | 2 + 1 |
| discovery, per station | `0448h+3` (type-code digits 1–3), then `identify()` on a hit | 1 (+ 6) |

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
    WriteRange(fc=0x06, first=0x07D0, last=0x07D0, values=CALIBRATION_KEYS),  # 42001
    WriteRange(fc=0x06, first=0x07D1, last=0x07D4),  # operation commands 42002-42005
)
```

- **00A4h–00ABh are excluded:** inferred coefficients (§2.6).
- **07D0h (key simulation) takes only the six calibration keys**, UP, DOWN, ESC, ENT,
  ZERO and SPAN (`CALIBRATION_KEYS`, written out apart from `KeyCode`). The envelope
  checks the *value* there as well as the address: an address alone no longer passes,
  and MODE, SIDE, 0 or two keys at once are refused before the wire. The simulator's
  own write list checks the same six independently (§10).
- **009Eh–00A3h belong to ZPB/ZPG**, and FC10 is the only function that reaches them.

**The reviewed subset.** Inside the envelope, a register is writable only when its
declaration gives it a write tier. That is 52 registers: the calibration gases and
calibration scope, the five response times, output hold, hold mode and the five hold
values, and each channel's range and range method. They are documented, the bench unit
does not contradict them, they are not options, and the bench analyzer can test them
all. Every one is a single word inside FC06's reach, so a setting write is FC06.
Everything else is read-only (`docs/registers.md` gives each group's reason; the
[safety page](safety.md) summarizes them): the alarms (target encoding contested), the
automatic schedules and flow times (start time contested; an option), key lock (it
blocks the panel's forced stop), the averaging, O2-correction and peak-alarm options,
and the blowback, measurement-point and reference-gas settings of other models.
Widening the subset is a registry change the owner reviews in `docs/registers.md`.

**Operations are not registers.** The four commands are `OperationSpec`s, each with a
dedicated facade method, its own safety tier and its own post-conditions. They are
unreachable through `write_parameter`, `SettingsSnapshot` or `fuji-configure apply`.

**The guarantee.** No supported fujilib operation can construct a write outside
`WRITE_ENVELOPE`. Three things enforce it:

1. Public writes resolve a **name** to the canonical, immutable registry entry. A
   caller-built `RegisterSpec`, a custom registry and an address in a settings file are
   all refused: a setting is written only if its name and address are in
   `REVIEWED_SETTINGS`, a frozen list in `write_policy.py` written out apart from the
   registry, and no registry that marks anything else writable passes validation.
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

1. **Access.** Resolve by name to the canonical spec or operation (§5.4). An unknown
   name, or writing a read-only register, is refused (`FujiValidationError`). The tier
   of a setting is known only once its name resolves, so this comes first.
2. **State.** The session is open, not broken, and not awaiting resynchronization.
3. **Safety tier.** Anything above `READ_ONLY` needs `confirm=True`, exactly `True`,
   else `FujiConfirmationRequiredError`. Names and file contents cannot lower a tier.
4. **Capability.** Probed and firmware capabilities: an `UNSUPPORTED` entry in the
   availability cache short-circuits. Options and model features
   (`OPTION_CAPABILITIES`): an operation that needs one is refused unless the type
   code lists it or the caller asserted it with `open_device(options=...)`, and never
   on a model whose manual describes no such option (`MODEL_OPTIONS`). The model must
   be known, so such an operation is refused before `identify()`. Options never gate
   reads: an option's registers read whether it is fitted or not.
5. **Validation.** Types, enum members by name (never by number), whole numbers
   within limits, the unit of a scaled value, finite values (`devices/encode.py`).

Then, under the operation lock, for a setting write:

1. read the status, and refuse while a calibration runs, a channel is being
   calibrated, the front panel is in a menu, or a manual calibration is in progress
   at the panel (`FujiAnalyzerStateError`, nothing written);
2. read the range tables for a scaled value or a range selection, and check that the
   range exists on the channel (the tables list two ranges for every channel; its
   range count says which exist);
3. read the setting and what it depends on (a range selection needs the method to be
   manual);
4. encode, and check the raw and percent-of-full-scale limits;
5. check the write envelope in the client, and write once;
6. read back and compare (§6.3, §6.4);
7. mark the range tables stale after a range or scaled write, and count the write for
   the write-rate warning.

A scaled value's raw limits can only be checked after its scaling is refreshed. So
validation is split: everything that needs no I/O happens first, and the final raw range
check comes before the first write frame. A refusal at any gate before step 5 of the
second list sends no write.

### 6.2 Safety tiers

`sartoriuslib`'s four tiers, as an `IntEnum`. The tier follows the **effect** of an
operation, not merely whether it persists. Unlike `sartoriuslib`, fujilib requires
`confirm=True` for `STATEFUL` too, as `servomexlib` does.

| Tier | Operations |
|---|---|
| `READ_ONLY` | every read |
| `STATEFUL` | `return_to_measurement()`, `start_blowback()`; the keys of a remote manual calibration that open channel selection, move the cursor, select the channel and cancel (`manual_calibration()`) |
| `PERSISTENT` | settings writes that change only configuration: response times, output hold, hold mode and hold values, a channel's range and range method |
| `DANGEROUS` | `start_auto_calibration()` and `start_auto_zero_calibration()`; the ENT that starts a manual calibration (`RemoteCalibration.calibrate()`); changing calibration-gas values or the calibration-scope settings (40026–40035) |

Calibration is `DANGEROUS` because it overwrites the calibration coefficients and is only
correct if the right gas is flowing. A calibration-gas value is equally dangerous, because
the next calibration, manual or automatic, is computed from it. The automatic schedules,
which would also be `DANGEROUS`, are read-only (§5.4). Every calibration reports the
channels and ranges it will affect, including the widening by the "both" setting, in its
plan (`plan_auto_calibration()`), which the start methods read again and return. The CLI
additionally requires `--i-understand-this-is-destructive` for that tier; in the
siblings the flag exists only on their diagnostic tools.

`PERSISTENT` is the right classification: on the bench unit a setting written over
Modbus survived a power cycle with no save step (§13.2 #14, findings §13.5).

### 6.3 Write path

```
resolve name -> gates (§6.1) -> validate -> [status] -> [range tables] -> [setting and
  what it depends on] -> encode -> check raw and %FS limits -> envelope check
  -> FC06 write, once -> read back (shielded, own deadline) -> compare
  -> [a selected range, verified: read the current range until it follows]
```

- **FC06 always.** Every writable setting is one word inside FC06's reach (§5.4), so
  a setting write never coalesces and never bridges: each write is exactly the
  setting requested. The read planner's gap bridging is never used for writes.
- **The outcome is what the read-back finds** (`devices/writes.py`): verified,
  mismatch (`FujiVerificationError`, with the requested, previous and observed words
  in the context) or unknown (`FujiWriteOutcomeUnknownError`; §6.4).
- **A range write returns once the channel measures on it** (#70). The analyzer's
  current range can follow a verified range write some tens of milliseconds later
  (findings §13.2), so the session reads it, shielded and within the read-back
  budget, until it shows the range written. If it never does, or cannot be read,
  the write raises `FujiVerificationError`: the setting is written, but not in
  effect.
- **Settings documents** (`devices/settings.py`) are compared with the analyzer as a
  whole before the first write. Unknown names, operation names, input registers,
  read-only settings that differ, values that do not fit and a document from another
  analyzer are refused, and then nothing is written. The writes go in a defined
  order: output hold before the hold settings when it is switched on, after them
  when it is switched off; a range method before its range; then the response times,
  the calibration scope and the calibration gases. The first write that fails stops
  the rest; the report lists what completed, what failed and what was not attempted.
  Nothing is rolled back.
- **Settings the panel requires to be switched off first** (alarms, the automatic
  schedules) are not in the writable subset. When they join it, they are written by
  switching off, writing and verifying; if anything fails, the automatic function is
  **left off**, and the partial state is reported with both the primary and the
  cleanup errors. It is never re-enabled unconditionally in a `finally` block.

A written setting is kept through a power cycle without a save step (findings §13.5),
so every write goes to non-volatile memory, and the manual does not state a
write-endurance figure. fujilib therefore never writes periodically, the recorder
has no write path, and a session logs a warning when setting writes exceed
`write_warn_per_minute` (10 by default) in a rolling minute, as `alicatlib`'s
EEPROM-wear guard does.

### 6.4 Timeouts and uncertain outcomes

- **Per transaction:** `request_timeout`, set when the port is opened.
- **Per call:** `timeout=` is an **operation deadline** for the whole operation. It
  starts before lock acquisition and covers queue time, retries, scaling reads and
  verification. The remaining budget is passed to nested steps rather than restarted.
  `None` (the default) means no outer deadline, so bus timing and retries govern.

**A timeout is not a rollback.** A write can be accepted by the analyzer while its reply
is lost. So once a write request has been sent:

- the read-back runs in a shielded scope with its own bounded deadline, what the
  port's timing allows two block reads with every retry and a late-reply window
  (3.2 s at the defaults), so it happens even when the write used up the
  operation's deadline. A caller that cancels the call itself (not a deadline)
  cancels it without a read-back; the late-reply window protects the next request;
- the read-back decides: the register as written is `verified`, whether or not the
  write's own reply arrived; anything else is a `mismatch` (a write whose reply was
  lost and that reads back as before did not arrive); a failed read-back leaves it
  `unknown`;
- the result (`WriteResult`) carries the state, whether the write was acknowledged,
  and the requested, previous and observed values;
- an unknown outcome raises `FujiWriteOutcomeUnknownError` (§9), a mismatch
  `FujiVerificationError`;
- nothing is retried to find out, and the write itself is never retried;
- a port that fails while a write waits for its reply, or during its read-back,
  breaks the session, as any connection failure does.

An idle command-status flag after a command can mean "never started" or "already
finished"; fujilib reports that ambiguity (`CommandOutcome.AMBIGUOUS`) instead of
guessing. A command whose reply was lost is `started` when the status shows the
calibration running, `done` when it shows the measurement screen, and otherwise an
unknown outcome.

`servomexlib` found that an outer deadline equal to one request timeout cancels
legitimate mid-sweep retries, and resolved it by ignoring the argument. fujilib keeps the
argument meaningful by defining it as a deadline for the operation.

### 6.5 Remote panel and manual calibration

Manual zero and span calibration exist only as front-panel key sequences through register
42001. At the panel, each is ZERO or SPAN, the cursor to the channel, ENT to select it
(the wait step, while the gas settles) and ENT again to calibrate. The first design ruled
key simulation out. The owner reopened it on 2026-09-29 (§13.1 #71):

- capa's cone profile records zero and span times and checks them before a test;
- firmware 1.02 has no calibration log over Modbus to supply them (§2.11); its panel
  keeps one, which only the display shows (findings §15.6);
- remote zero and span from the host are wanted, with the operator switching the gas
  valves by hand and naming the gas.

The work is staged (§12, Phase 7). **Watching** manual calibrations made at the panel
needs no key (7A). The step, the flags, 00B9h and 00BDh show the whole sequence
(§2.6, findings §14). **The key prototype** (7B) showed on the bench that a key written
to 42001 acts as the same key at the panel (findings §18). **Driving** a calibration
(7C, `devices/keys.py`) answers the first design's four reasons as follows:

1. **The same keys reach factory mode.** Maintenance mode is behind MODE, a menu and a
   password entered with SIDE; its default is 0000, and it can change the station
   number. Factory mode is behind a second password, printed in the service manual
   (TN5A1191b-E §3.1). fujilib sends only ZERO, SPAN, UP, DOWN, ENT and ESC, never
   MODE or SIDE, and the client checks the *value* of every write to 42001, not only
   its address (§5.4). By the manual, no sequence of those keys opens a menu.
2. **A single key can force a calibration.** On errors 5 and 7, ENT forces the
   calibration (§2.8). ENT is sent only on the channel-selection and wait steps, and
   on the wait step only by `calibrate()`. On the error display fujilib sends only ESC.
3. **Gas stability can't be judged over Modbus while output hold is on.** The
   concentrations are held during a calibration then (ZPA p.64). With hold off they are
   live (findings §14), and a steadiness rule (§13.1 #78, `devices/steadiness.py`)
   watches them. Whether the A/D counts stay live under hold is still open (§13.2
   #38), so a remote calibration is refused while output hold is on (#86). The
   operator names the gas; it must be the calibration-gas setting of every range the
   calibration touches (#89), and the reading must settle within a tolerance of it.
4. **The scope is wider than the call.** "At once" zeroes every channel set to it
   together, and "both" calibrates both ranges (§2.6). `plan_manual_calibration()`
   reports every channel and range. The plan is read again before the first key and
   again before the calibrating key, and the run is refused if it has changed. A plan
   that widens any channel to both ranges is refused until the bench has shown what
   "both" does (#88).

**The run.** `Analyzer.manual_calibration(plan, gas=..., confirm=True)` is an async
context manager (`RemoteCalibration`; a blocking twin in `fujilib.sync`):

```python
plan = await anz.plan_manual_calibration("CH3", "span")
gas = CalibrationGas(20.95, "vol%", label="20.95 % O2 in N2")
async with anz.manual_calibration(plan, gas=gas, confirm=True, adc=True) as run:
    await run.wait_steady()  # the operator switches the valves; fujilib watches
    event = await run.calibrate(confirm=True)  # DANGEROUS: the ENT that calibrates
```

- **Entering it** refuses, with nothing sent, unless the panel is on the measurement
  screen with no step, no calibration or hold flag is set, key lock (#85) and output
  hold (#86) are off, no instrument error is active, the plan reads the same again,
  no channel is widened to both ranges (#88), and the gas named is each channel's
  calibration-gas setting (#89). It then presses ZERO or SPAN, moves the cursor and
  selects the channel, each key `STATEFUL`: the analyzer is on the wait step.
- **`wait_steady()`** reads the panel every `interval` (0.5 s), with the A/D block
  when `adc`, until every channel calibrated is steady on its gas (#78), or times out.
  The port is free between reads, so a recording goes on, its rows marked
  `calibrating`. A panel that leaves the wait step (a key at the panel, a 42002 from
  elsewhere) stops the run.
- **`calibrate(confirm=True)`** checks everything again in the same operation as the
  key: the settings and the plan, then the panel once more, so that it is the last
  thing read: the wait step with the plan's flags, no hold flag, each reading in its
  range's unit, the gas still steady with this read, no instrument error. Only then
  does it send the ENT, which is `DANGEROUS`, and it follows the calibration to its
  end. On the error display it sends ESC, never ENT. `cancel()` leaves the wait step
  with ESC.
- **One run per session** (#91). While one holds the panel, the same session refuses
  setting writes and commands, and another run.

**How keys are sent** (#92). Every key is one locked operation:
1. read the screen, the step, the cursor and the flags, and refuse unless the step
   allows the key (`key_refusal()`);
2. write the key once, never retried;
3. read until the step, the cursor and the flags show that it took, for at most
   `key_timeout` (2 s), shielded; any screen but measurement is unexpected.

On the bench the change was there by the first read after the reply, and a flag at
most one read later (findings §18.1). 00BDh shows only keys pressed at the panel, so it
cannot confirm fujilib's own keys. A key not seen taken, whether acknowledged or not,
stops the run, and the run then sends no key but the cleanup's: a calibrating ENT the
panel swallowed is never followed by another. A key whose reply was lost is settled by
the reads. A key is recorded before it is written, so the cleanup reads the panel after
it however its write and reads end, cancellation included; only a definite refusal (an
exception reply) takes it off the record. Keys go only on the plan's own steps: DOWN
and the selecting ENT on its kind's channel selection, the calibrating ENT on its wait
step.

- **Key lock** swallows a key written over Modbus, although the analyzer acknowledges
  it, and the analyzer then answers nothing for about two seconds (findings §18.6).
  fujilib reads key lock (40074) before the first key and before the calibrating key,
  and refuses while it is on (#85).
- **The cursor** wraps round at both ends. ZERO or SPAN may open it where it was left,
  or on Ch1: that happened after a 42002 and after a long pause (findings §18.2).
  Channels zeroed "at once" share a position, which reads as its first channel when
  reached going down and its last going up. fujilib moves the cursor with DOWN only,
  one confirmed key at a time, until it reads the planned channel, or the first of the
  "at once" channels; if it comes round to a channel already passed, the panel does
  not offer the channel and the run stops. ENT then needs the cursor on it.
- **The calibrating key** is followed for up to `run_timeout` (30 s): the analyzer did
  not answer for about a second while it stored a zero (findings §14.3).

Anything unexpected stops the run, and the cleanup depends on the step it stopped on:

| Step | Cleanup |
|---|---|
| channel selection | ESC, which returns to measurement |
| wait | ESC, which returns to measurement and clears the flags; never 42002, which returns the display but leaves the flag set (findings §18.4) |
| running | wait for the analyzer to finish |
| error display | ESC |
| any other screen | 42002 only |

Cleanup runs however the block is left, cancellation included, shielded and within its
own deadline (`cleanup_timeout`, 30 s), as the read-back does; its keys and 42002 wait
for the port within it too. Each cleanup key is sent once, and not again on a step
where it was just sent; a key whose own read failed before it was written may be tried
again. The result is kept however the cleanup ends, and an error leaving the block
carries a note when the panel was not left clean. It checks that the flags have
cleared as well as the screen: returning the display to measurement is not proof that
a calibration stopped (findings §18.4). A flag still set on the measurement screen
raises `FujiAnalyzerStateError`, names the channel and gives the recovery the bench
showed: enter that channel's wait step at the panel and press ESC. fujilib leaves that
recovery to the operator (#84).

**Records.** A calibration, watched or driven, is recorded as a `ManualCalibrationEvent`
(§8):
- the channels and ranges;
- the outcome, and how it was decided;
- the readings before and after, and the deviation from the calibration gas;
- the raw A/D values at the last read before it ran.

That is what firmware 2.24's calibration log keeps: a detector count and a deviation
(§13.1 #75). The A/D counts are no finer than the reading (findings §14.4), so they are
kept as evidence, never as a measurement. A driven run's `RemoteCalibrationResult` adds
the plan, the gas named, the steadiness verdict and rule, every read on the wait step,
the keys and the cleanup. Both are written as a `fujilib-calibration/1` document
(`calibration_record()`, `RemoteCalibrationResult.as_record()`, #90), from which capa's
`AnalyzerCalibration` zero and span times can be taken.

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
| Established channels | assertion; first non-zero triple, in the poll that shows it | never within a session |
| Range metadata (count, unit, value, decimal point per (c, r)) | `identify()`, `read_ranges()` | every scaled write; `read_ranges()`; a change in the current-range registers seen by `poll()` or `status()` (read again on next need) |
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
| Identity and metadata | `identify(channel_map=...)`, `snapshot(name=...)` (no I/O), `read_ranges()`, `read_metadata()` | 4 |
| Diagnostics | `read_clock()`, `read_adc()`, `reprobe(capability)` | 4 |
| Logs | `read_error_log()`, `read_calibration_log(ch=None)` | 4 |
| Parameters (read) | `read_parameter(name)`, `read_parameters(names)`, `read_settings()`, each with `alarm_targets=` (§5.2) | 4 |
| Streaming | `PollSourceAdapter(name, device)`, `DeviceResult` | 4 |
| Recording | `record()` over a `PollSource`; `reopen()` after a connection failure | 5 |
| Parameters (write) | `write_parameter(name, value, *, unit=None, confirm=False)`; `set_response_time`, `set_output_hold`, `set_hold_mode`, `set_hold_value`, `set_range`, `set_range_method`, `set_calibration_gas`; `diff_settings`, `apply_settings` | 6 |
| Operations | `start_auto_calibration`, `start_auto_zero_calibration`, `start_blowback`, `return_to_measurement`, `plan_auto_calibration()`, `plan_auto_zero_calibration()`, `calibration_status()`, `wait_for_calibration(timeout=...)` | 6 |
| Front panel | `plan_manual_calibration(channel, kind)`, `wait_for_manual_calibration(timeout=...)`; `ManualCalibrationTracker` over recorded frames; `manual_calibration(plan, gas=..., confirm=...)`, a `RemoteCalibration` with `wait_steady()`, `read()`, `calibrate(confirm=...)` and `cancel()` | 7 |

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
    identify: bool = True,
    max_concurrency: int = 8,
) -> list[DiscoveryResult]: ...
```

Read-only. The probe is an FC04 read of type-code digits 1–3, which must decode to `Z`,
`P` and a model letter. A CRC-valid exception reply counts as "a Modbus device, but not a
ZP analyzer". Baud is fixed, so a full scan of one port is 31 probes. Discovery never
raises for a probe failure; it returns an `ok=False` row.

- **Probes are not retried** (`read_retries=0`), and an analyzer found is identified in
  full unless `identify=False`. An absent station costs the probe timeout plus the
  0.1 s quiet window, so a full sweep takes about 12 s per port.
- **Ports are scanned in parallel**, up to `max_concurrency` (as in `alicatlib`); the
  stations of one port one at a time, on one bus. Results come back in the order given.
- **`DiscoveryResult`** has the unified fields (§7.8 B) and a `model`, set from the probe
  even without identification. When identification of a station that answered fails,
  the row is `ok=True` with `device_info=None` and the error.
- **`DiscoverySummary`**, from `summarize_discovery()`, is one row per port, as in
  `sartoriuslib`: the stations found, how many were probed, and the first error when
  nothing was found.
- **Every port scanned receives the probe frames**, including ports of other
  instruments, so `fuji-discover` scans every host port only with `--all-ports`.
- **Each port is scanned once**: two spellings of one port (`COM8`, `com8`) name the
  same canonical port (§4.1).

### 7.6 Streaming, recording and sinks

The recorder follows the **`sartoriuslib` / `alicatlib`** contract, with one wide sample
per poll (§13.1 #13). A survey of the four sibling recorders found defects they share;
fujilib's recorder is built to avoid them (§13.1 #45–#56).

```python
class PollSource(Protocol):  # runtime-checkable
    async def poll(
        self, names: Sequence[str] | None = None
    ) -> Mapping[str, DeviceResult[Frame]]: ...
    def layout(self, names: Sequence[str] | None = None) -> Mapping[str, SourceLayout]: ...
    async def reconnect(self, name: str) -> None: ...


@asynccontextmanager
async def record(
    source: PollSource,
    *,
    rate_hz: float,
    duration: float | None = None,
    names: Sequence[str] | None = None,
    overflow: OverflowPolicy = OverflowPolicy.BLOCK,
    buffer_size: int = 64,
    reconnect: ReconnectPolicy | None = None,
) -> AsyncGenerator[Recording[Mapping[str, Sample]]]: ...
```

- **One `Sample` per analyzer per tick, carrying the whole `Frame`.** Analyzer-level
  status (instrument error, alarms, auto calibration) travels once with all channels,
  and capa's adapter can follow its `wide_row` path (`capa/devices/alicat.py`) with no
  per-tick workaround. `watlowlib`'s long samples need one in capa (`tick_first`).
- **Rows keep their columns.** The recorder reads each analyzer's `SourceLayout`
  (station, protocol, established channels, whether it can be reopened) once, when it
  starts, and every sample it makes carries those channels (`Sample.channels`).
  `sample_to_row(sample)` uses them by default. capa calls it without a channel list and
  its own sink raises on a column added after the first flush, so a failed poll's row
  must have the same keys as any other (#45). A channel established later is left out
  of the recording's rows (#34), and an analyzer with no established channel is refused
  before the first poll.
- **A failed poll** produces one `Sample` with `frame=None` and `error` set, timed
  around the poll (including any wait for the port), so gaps are recorded rather than
  dropped. It does not produce twelve invented readings. Every name is in every batch.
- **Schedule.** Tick *k* is due `k / rate_hz` after the first. A poll that overruns
  whole slots skips them and counts them late, never catching up in a burst; slots past
  the end of a finite run are not counted. `max_drift_ms` is how late a poll *started*
  (the siblings include the poll's own round trip). The schedule runs on an injectable
  clock, so tests assert exact counts (#53).
- **Overflow.** `BLOCK` (the consumer sets the pace; ticks missed meanwhile are late),
  `DROP_NEWEST` and `DROP_OLDEST`, all implemented. A batch is dropped whole and counted
  in `samples_dropped`, not as late (#49).
- **The summary counts polls.** `AcquisitionSummary` has the siblings' `started_at`,
  `finished_at`, `samples_emitted`, `samples_late` and `max_drift_ms`, plus
  `target_total_samples`, `samples_dropped`, `error_samples`, `disconnects` and
  `reconnects`. There are no latency percentiles: every row has `latency_s`, and
  percentiles would grow with a 24-hour run (#48). `finished_at` is set however the
  recording stops, and batches still unread then count as dropped, so
  `samples_emitted` is exactly what the consumer received. For a run that ends normally,
  `samples_emitted + samples_dropped + samples_late == target_total_samples`.
- **Disconnects** end the recording: the tick's error sample is delivered, the stream
  ends, and leaving the `async with` block raises the `FujiConnectionError`, which
  `disconnects` counts. A
  `ReconnectPolicy` rides the outage out instead: every tick is an error sample, and
  the source's `reconnect()` (for `PollSourceAdapter`, `Analyzer.reopen()`) is tried on
  a back-off schedule (0.5, 1, 2, 5, 10, then 30 s). A reopen opens the port by name
  again and identifies the analyzer, which must have the same serial number and type
  code. Connection, timeout and framing failures of an attempt are retried; any other
  failure, such as another analyzer or an analyzer that was closed, ends the
  recording. A transport the caller supplied
  cannot be reopened, so the policy is refused for it before the first poll (#47).
- **One exception, not a group.** An exception group of one from the recorder's task
  group is raised as its member.
- **`sample_to_row(sample, channels=None)`** flattens a sample to columns fixed by its
  channels:
  - the header: `device`, `address`, `protocol`, `t_mono_ns`, `t_utc`,
    `t_midpoint_mono_ns`, `requested_at`, `received_at`, `latency_s`;
  - per channel: `chN_value`, `chN_raw`, `chN_decimals`, `chN_unit`,
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
- **Long rows are a helper, not the recorder's shape.** `Frame.as_long_rows()` produces
  one row per channel with the analyzer status repeated, for SQL unions with long-format
  siblings.
- **Sinks** for 0.1.0 are memory, CSV and Parquet (#18); SQLite, JSONL and Postgres
  follow when someone needs them. They share `BaseSink` (open once, write, close once;
  closing completes under cancellation).
  - **Columns are fixed before the first row** by a `SchemaLock`, from
    `row_columns(channels)`: at `open()` when the sink is given its channels, otherwise
    from the first batch's samples. Types never come from values, so an error-first
    recording still types `ch3_value` as a float. A sample with a channel the columns
    lack raises `FujiSinkSchemaError` rather than being written without it (#50).
  - **File I/O runs in a worker thread**, shielded, so a write that has started is
    finished and a cancelled writer never leaves half a batch.
  - **CSV** quotes text and not numbers, so `None` (an empty field) and empty text
    (`""`, e.g. `chN_errors` with no errors) stay apart. It is flushed after every write.
  - **Parquet** needs the `parquet` extra (`pyarrow`), imported when the sink opens.
    Rows are gathered into row groups of 1,000: `pipe()` writes about once a second,
    and a row group per write would make a day at 1 Hz 86,400 groups whose metadata
    `pyarrow` holds in memory until the file closes. The rows waiting for their group
    are kept as rows and made into one Arrow table per row group; an Arrow table per
    write grew the process's memory by 1.5 MB an hour at 1 Hz (#87). zstd by default,
    key-value metadata with `fujilib.version` and the caller's. A Parquet file is
    readable only once closed; closing runs on cancellation and Ctrl-C, and writes the
    footer even when the last rows cannot be written, but a killed process leaves a
    file without its footer, so CSV is the safer format unattended.
    `scripts/recover_parquet.py` recovers the complete row groups of such a file (#57).
- **`pipe(recording, sink, batch_size=64, flush_interval=1.0)`** writes batches in
  groups, at the latest `flush_interval` after the first of a group arrived, even while
  the stream is idle. When it stops, by the stream ending, an error or cancellation, it
  takes the batches already waiting and writes what it holds (for up to 30 s,
  shielded) unless the sink itself failed, so a file has a row for every batch the
  summary counts, after Ctrl-C too. It returns its own counts; the recording's are in
  `recording.summary` (#51).
- **Blocking code** has the same recorder: `fujilib.sync.record()` yields a
  `SyncRecording` iterated with a plain `for`, `pipe()` runs on the recording's portal,
  a blocking `PollSourceAdapter` brings its analyzer's portal, and the `Sync*Sink`
  classes wrap the async sinks.
- **No batch lock.** One analyzer's poll is one session operation, which already holds
  the port's lock across both blocks (§4.2). A manager polling several stations on one
  port would hold it per port group (§7.4).

### 7.7 CLI

Plain `argparse`, each `main(argv=None) -> int`, each drivable with `--fixture`.

| Command | Purpose | Phase |
|---|---|---|
| `fuji-decode` | decode a hex frame or a register dump offline | 1 |
| `fuji-read` | one-shot identity, poll, status, metadata, ranges, logs, clock, A/D, snapshot (`--include`, `--all`) | 4 |
| `fuji-discover` | scan named ports (or `--all-ports`) and station numbers | 4 |
| `fuji-stream` | print each poll at a fixed rate: text, CSV or JSON lines | 5 |
| `fuji-capture` | record to CSV or Parquet, with a `fujilib-capture/1` metadata document beside it | 5 |
| `fuji-diag timing` | read-only link timing, busy-wait gaps measured from the reply | 5 |
| `fuji-configure` | `dump` / `diff` / `apply` (`apply`: `--confirm`, `--i-understand-this-is-destructive` for a DANGEROUS write, `--dry-run`, settings names only) | 4 / 6 |
| `fuji-calibrate` | a manual zero or span from the host, the operator at the gas valves (`--plan`; `--confirm` and `--i-understand-this-is-destructive`; `--auto`) | 7 |

The CLI uses the same session and write policy as the facade; it has no private write
path.

- **Exit codes** are 0 on success, 1 for a library error, 2 for bad arguments, and 2 when
  `fuji-discover` finds nothing (as `servomex-discover` and `sarto-discover` do). Ctrl-C
  stops `fuji-stream` and `fuji-capture` cleanly, with 0: an open-ended recording is
  meant to end that way (#52). On Windows, Ctrl-Break does the same through the event
  loop's Ctrl-C handler. A window opened by a process that ignores Ctrl-C passes that
  on to everything started in it, and Ctrl-Break cannot be ignored that way (#57).
- **`--fixture`** for `fuji-read` and `fuji-configure` is a register bank (the JSON
  `fuji-decode --dump` reads), or `bench` for the bundled bench bank, answered by the
  simulated analyzer through `open_device`. A live read takes a dozen transactions, which
  an arrow script could not reasonably hold. `fuji-discover` has no `--fixture`: it opens
  ports by name.
- **`fuji-configure dump`** writes a document of format `fujilib-settings/1`: the
  analyzer's identity, then every holding register by name (never by address) with its
  decoded value, raw value, unit, access, safety tier and evidence. `diff` and `apply`
  read it (or a document naming only some settings): `diff` says what would be
  written or refused; `apply` needs `--confirm`, and `--i-understand-this-is-destructive`
  for a DANGEROUS write, writes nothing if anything is refused, and ends with a
  `status:` line, exiting 1 unless it is `ok` or `dry_run` (§6.3).
- **`fuji-capture`** identifies the analyzer, reads its metadata, and records with
  `pipe()` to the file `--out` names; the format follows the extension. Beside it,
  `<out>.meta.json` (format `fujilib-capture/1`) holds the identity, ranges and metadata
  (response times, calibration gases, hold mode, clock), the arguments and package
  versions. It is written when the recording starts, every minute while it runs with
  the counters so far, and when it ends, with how it ended (`finished`, `stopped` or
  `failed` with the error) and the recording's and the session's counters; each write
  replaces the file whole and stamps `updated_at`. The progress line is written from a
  worker thread, so a console that stops taking output holds up the line and not the
  recording (#57). A Parquet file carries the starting document in its metadata.
  Existing files are not replaced without `--force`, and a missing `pyarrow` is reported
  before the port is opened (#52).
- **`fuji-calibrate`** plans a zero or span of `--channel` (`--kind zero|span`) and,
  with `--confirm` and `--i-understand-this-is-destructive`, drives it (§6.5): it
  presses the keys, tells the operator to switch the inlet to the gas named
  (`--gas-value`, `--gas-unit`, `--gas-label`), prints the steadiness as it settles,
  asks before the key that calibrates unless `--auto`, and waits again if the gas
  moves before the key. It writes a `fujilib-calibration/1` record (`--out`) and ends
  with a `status:` line; 0 when the calibration completed, 1 otherwise (#93). With
  `--fixture` the named gas flows into the simulated inlet when the wait step opens.
- **`fuji-diag timing`** is `scripts/probe_link.py --mode pairs` on fujilib's own port and
  client: no library idle, no retries, busy-waited gaps, a random order (`--seed`), every
  trial kept (`fujilib-diag-timing/1`), and only FC04 reads (#54).

### 7.8 Unified device-library API conformance

The cross-library contract (`UNIFIED_API_HANDOFF.md`) is in none of the repositories. The
table below is the common subset recovered from `watlowlib`, `sartoriuslib` and
`alicatlib` code, their `test_unified_api.py` files, and watlowlib's CHANGELOG. Sections
D, L and N are referenced nowhere and remain unknown (§13.4). `servomexlib` diverges from
the contract in places; fujilib follows the contract.

| § | Requirement | fujilib |
|---|---|---|
| A | `open_device` is the canonical entry point; async context manager and `close()`; cleanup on failed open | §7.1; closes only what it opened (`alicatlib`'s rule) |
| B | `DiscoveryResult(ok, port, address, baudrate, protocol, device_info, error, elapsed_s)`; `DiscoverySummary` | §7.5 |
| C | `Sample` carries `t_mono_ns`, `t_utc`, `t_midpoint_mono_ns`, `requested_at`, `received_at`, `latency_s`. `t_mono_ns`/`t_utc` are the request/reply **midpoint** of the concentration block, and `requested_at`, `received_at` and `latency_s` are that block's, so `t_utc` is their midpoint | §8 |
| E | `DeviceResult.success()` / `.failure()`; `PollSourceAdapter(name, device)` over the `poll(names) -> Mapping` contract | §7.6 |
| F | typed `FujiTransientTransportError`: **deferred**, as in `watlowlib` and `alicatlib`; revisit if a cold-open race is observed | §9 |
| G | `ErrorContext.address` | §9 |
| H | `DeviceSnapshot` + `FujiDeviceSnapshot`; `snapshot()` is awaitable and does no I/O; fields `name`, `model`, `firmware` (None: not readable), `serial`, `connected`, `last_error`, `recoverable_error_count`, `captured_at` | §8 |
| I, M | `Recording` with `stream`, `summary`, `rate_hz`; mutable `AcquisitionSummary` | §7.6 |
| J | `Session.recoverable_error_count`: failed read attempts that a later attempt of the same read recovered (the client's `recoverable_error_count`) | §4.5; `Analyzer.session` |
| K | `to_pint()` covering every `Unit` member, exported at top level and from `fujilib.units`; `vol%` and `ppm` differ by 10⁴, and `to_pint` never converts values. The strings are ones capa's unit registry parses: `vol%` → `percent`, `ppm`, `mg/m**3`, `g/m**3`, and `None` for an unknown unit | §8 |
| 6 | top-level exports: `open_device`, `find_devices`, `sample_to_row`, `PollSourceAdapter`, `Recording`, `DeviceResult`, `DiscoveryResult`, `DiscoverySummary`, `DeviceSnapshot`, `FujiDeviceSnapshot`, `to_pint` | `tests/unit/test_unified_api.py` |

`requested_at` and `latency_s` are populated on every polled sample (`servomexlib`
declares them but never sets them). `t_midpoint_mono_ns` is `None`: a configured
averaging period does not reveal the actual integration window. Response-time and
averaging settings are reported in `AnalyzerMetadata` instead of shifting timestamps.

**The capa adapter.** It is not a thin wrapper. capa's adapters are 850–1,040 lines
each plus a 200–340-line simulator, and capa had no gas-analyzer adapter. The fuji
adapter is built into capa as `capa/devices/fuji.py` (§13.1 #97) and follows
`capa/devices/alicat.py`:

- a `wide_row` `SourceRecord` per tick, its row from `sample_to_row()`. A failed poll
  yields the record and no channel samples, as in capa's sartorius adapter;
- a new binding, `FujiChannel(device, channel, field)`, and a new channel kind for a gas
  concentration (#99). `field` is `value`, the concentration, or `valid`, 1.0 or 0.0
  from `Reading.valid`, so that alarms and procedures can watch validity (#106);
- `ChannelSample.status` carrying the reading's `ReadingState`;
- the channel map asserted in the device's parameters, as `open_device(channel_map=...)`
  takes it. A reading whose unit is not the unit its capa channel declares quarantines
  the channel with a `DeviceEvent`: the pattern of the watlow adapter's
  `wire_temperature_unit`. A type code that suggests another gas than the asserted one
  only warns, since the type code is a hint (§2.9);
- settings writes and a guarded manual zero or span from capa's manual control panel
  (#98, #104);
- a hand-written simulator on the builders of `fujilib.testing.frames` (#100, #103), a
  descriptor, and discovery and handshake hooks.

The work is Phase 8 (§12).

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
class ReadingState(StrEnum):   OK, ANALYZER_ERROR, CHANNEL_ERROR, CALIBRATING, AUTO_CALIBRATION, HOLD, SOURCE_INVALID, SETTLING, UNKNOWN

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
class ManualCalibrationEvent:               # a zero or span made at the panel (§6.5)
    kind: ManualCalibrationKind             # ZERO, SPAN
    outcome: ManualCalibrationOutcome       # COMPLETED, FAILED, CANCELLED, AMBIGUOUS
    channels: tuple[ChannelId, ...]         # from the zero or span flags, else the cursor
    ranges: Mapping[ChannelId, int]         # each channel's current range
    started_at: datetime                    # ZERO or SPAN first seen
    selected_at: datetime | None            # the wait step first seen
    ran_after: datetime | None              # it ran between these two reads
    ran_before: datetime | None
    ended_at: datetime                      # back on measurement
    before: Mapping[ChannelId, float | None]    # last reading on the wait step
    after: Mapping[ChannelId, float | None]     # first reading back on measurement
    adc_before: AdcValues | None            # the raw A/D values with ``before``
    new_errors: Mapping[ChannelId, frozenset[ErrorCode]]
    evidence: tuple[str, ...]               # why the outcome is what it is

@dataclass(frozen=True, slots=True)
class CalibrationGas:                       # the gas the operator names (#89)
    value: float; unit: Unit | None; label: str | None

@dataclass(frozen=True, slots=True)
class SteadinessRule:                       # #78; defaults to tune on the bench
    window_s: float = 30.0; response_factor: float = 2.0
    band_percent_fs: float = 0.5; tolerance_percent_fs: float = 10.0; timeout_s: float = 600.0
    max_gap_s: float = 5.0

@dataclass(frozen=True, slots=True)
class RemoteCalibrationResult:              # a driven run (§6.5)
    plan: ManualCalibrationPlan
    gases: Mapping[ChannelId, CalibrationGas]
    rule: SteadinessRule
    event: ManualCalibrationEvent | None    # None when no key opened a pass
    steadiness: SteadinessVerdict | None
    calibrating_key_sent: bool
    keys: tuple[KeyPress, ...]              # key, step, cursor, acknowledged, taken, after_s
    cleanup: CleanupReport                  # clean, actions, flags_left, error
    samples: tuple[WaitSample, ...]         # every read on the wait step
    started_at: datetime; ended_at: datetime; error: str | None

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
- `settling` for a reading that none of the above applies to, polled within
  `settle_after_reopen_s` (90 s by default; 0 for never) of a reopen that followed a
  connection failure (§13.1 #81). The analyzer may have been switched off meanwhile, and
  for about a minute after power-on its readings are far off with no flag of its own
  (findings §15.5). The period is the session's (`Session.settling_until`), and it does
  not start at the first open, when the host cannot know how long the analyzer has been
  on;
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
├── FujiAnalyzerStateError                  the analyzer's state forbids a write now (§6.1)
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
  writes minus 00A4h–00ABh, and at 07D0h only the six calibration keys. The list is written out in `testing/mock.py`, not
  imported from `WRITE_ENVELOPE`, so a widened envelope fails the tests. Any other write
  raises `MockWriteViolation`, which fails the test. A write is stored as sent; a
  test that wants the analyzer to refuse a value injects an exception reply, and
  one that wants a write acknowledged but not stored injects `FaultKind.IGNORE`.
- **Operation commands act as the manuals describe**, on the AnyIO clock with the
  flow times scaled by `time_scale`: return to measurement shows the measurement
  screen; auto calibration zeroes the enabled channels together, spans them one at a
  time and holds for the extension when output hold is on; auto zero zeroes and holds
  as long again. While one runs, 30049 and the channels' auto-zero or auto-span and
  hold flags are set and each channel measures on its auto-calibration range. Errors
  set beforehand appear at the end. A command during a run changes nothing. This is
  written from the manuals, not from fujilib's operations code, and is unverified on
  hardware: the bench analyzer cannot auto-calibrate.
- **The front panel's manual calibration**, as the bench analyzer showed it (findings
  §14). `MockAnalyzer.press(key)` is an operator at the panel: the steps, the cursor
  with "at once" channels sharing a position, the zero and span flags, 00B9h and
  00BDh. The calibrating ENT sets the channels' readings to their calibration gases.
  What the bench has not shown is written from the manuals and marked so in the
  simulator: the error display, and hold. A key written to 07D0h acts as a key at the
  panel but never shows in 30190; key lock swallows it and the analyzer then falls
  silent; the cursor wraps round, an "at once" position reading by direction; ZERO
  opens on the first position after a 42002; and optionally a flag lags ESC, the
  analyzer falls silent while it stores, and the cursor resets after a pause
  (findings §18). `MockAnalyzer.flow(channel, value, tau_s=...)` changes the gas at
  a channel's inlet, so a steadiness rule can be exercised.
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

**Model builders.** `fujilib.testing.frames` builds the frozen models directly:
`reading()`, `status()`, `analyzer()`, `frame()`, `timing()` and `bench_readings()`
(§13.1 #103). They are for code that consumes frames and rows rather than the line: the
row and contract tests here, and capa's adapter tests and simulator, whose rows then
come from the same `Sample.from_frame()` and `sample_to_row()` as a recording's. They
decode nothing, so a reading's state is taken as given; the simulated analyzer is the
tool when the decoding itself is under test.

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
| Builders | the public model builders give the models the suite's own tests use; a built frame makes the sample and the row of a recording |
| Private API | no fujilib module uses a private `anymodbus` or `anyserial` name |
| Gates | a refused operation leaves the mock's request log **empty**; preflight refusals send no write frame |
| Write policy | every public path (facade, CLI, snapshot file, forged spec, operation name in a settings file) is refused outside the envelope; FC10 spans never include unrequested words |
| Uncertain outcomes | lost write acknowledgement; a write whose deadline expires after transmission, still read back; a port that fails during a write or its read-back (the session breaks); a write acknowledged but not stored; a setting changed between write and verify; a command whose reply is lost |
| Validity | hold or instrument error arriving between the two poll blocks; block-2 failure; derived-channel validity; `detail=False` gives `valid=None` |
| Labels and presence | a populated channel reading an all-zero triple stays present; contradictory type codes; asserted map wins |
| Recording | on a manual clock, exact tick, late, drift and drop counts for every overflow policy; error-first recording keeps a fixed schema; every name in every batch; a disconnect ends the recording after its batch, and a reconnect policy rides it out (over a simulated cable pulled and put back); a channel established later stays out of the rows; cancellation and early exit |
| Sinks | CSV and Parquet read back exactly what was written (hypothesis); `pipe()` flushes on time while idle and writes what it holds when cancelled; a failed sink is not retried |
| Commands | `fuji-stream`, `fuji-capture` and `fuji-diag` on the simulator, including a real SIGINT mid-recording (the files are closed and readable, exit 0) |
| Remote calibration | every key only where it belongs; every refusal leaves no write in the request log; a zero, a span and an "at once" zero on the simulator; keys swallowed, lost, refused or landing elsewhere; a key or a menu at the panel meanwhile; the cleanup from every step, a flag left set, a port that fails, cancellation; the steadiness rule, property-tested; `fuji-calibrate` end to end on `--fixture bench` |
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
| `hardware_stateful` | `FUJILIB_ENABLE_STATEFUL_TESTS` | settings writes with restore, a settings document and its baseline, return-to-measurement; the owner-attended steps (key lock, power cycle, values out of range) are `scripts/probe_write.py` |
| `hardware_destructive` | `FUJILIB_ENABLE_DESTRUCTIVE_TESTS` | auto calibration and auto zero, with calibration gas; none written: the bench analyzer cannot auto-calibrate. A remote zero or span is no pytest: the operator switches the valves, so it is an attended session with `fuji-calibrate` (#94) |

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
| `.github/workflows/docs.yml`, `release.yml` | `servomexlib` | package name, PyPI URL; make publishing depend on the same revision having passed lint, type, test and docs. The docs deploy to Cloudflare Pages, project `fujilib-docs`, served at `fujilib.graysonbellamy.dev`, as the siblings' do (#96) |
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
  `anyio` 4.14). Extras: `docs`, and `parquet` (`pyarrow>=22`) for the Parquet sink;
  others as sinks are added. The type checkers use `pyarrow-stubs`. Python ≥ 3.13.
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
  been published with the docs site, included in the sdist, and rejected by the pre-commit
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
    `docs/manuals/` into `site/`. The site is deployed only from CI, where the manuals do
    not exist; never deploy a local build.
- **Evidence.** Future captures record exact probe arguments, timeout, idle and retry
  settings, package versions, individual failures and file hashes. They distinguish a
  coherent block capture from a bank assembled one word at a time over minutes.

---

## 12. Implementation plan

Each phase ends with CI green on the full matrix. Software exits and hardware exits are
separate gates. Effort figures are rough working days, judged after two reviews; they
assume the scope below, not the first draft's.

### Decisions still open

The capture policy (§13.1 #11) and the sample shape (#13) are settled; the bench
channel map (#14) was adopted with Phase 4. The remaining items marked *(awaiting)* in
§13.1 are each needed before the work that uses them:

- the acceptance criteria for O2 (#15), before the O2 comparison;
- whether an analyzer that falls silent and answers again, with the port still open,
  starts the settling period too (#81), before 0.2.0.

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

### Phase 4 — Session, facade, discovery, sync, capa spike (**done 2026-09-28**, but for the capa spike)

- ~~`devices/{session,analyzer,factory,discovery,snapshot,profile}.py`~~ (`metadata.py`
  dropped, #37).
- ~~`sync/` with the parity test; `fuji-read`, `fuji-discover`, `fuji-configure dump`~~.
- ~~`tests/hardware/test_hardware_reads.py`, `docs/hardware-test-day.md`, quickstarts~~.
- **capa adapter spike** against `MockAnalyzer`, in a capa branch (1–2 days, separately
  authorized): the `FujiChannel` binding, the `wide_row` record, `ChannelSample` status
  and expected-gas assertion. *Never built as a spike: the adapter itself is Phase 8
  (#44, #97).*
- **O2 comparison, if the analog output is wired:** simultaneous Modbus and analog O2
  across relevant O2 changes, ranges, hold and response settings. Record the Premus
  variant and its specification. *Taken off the Phase 4 path (#43).*

*Exit:*

- ~~the read-only API passes against the bench analyzer~~ — 45 of 45 hardware tests,
  asyncio and trio (findings §11);
- ~~the unified-API tests are green~~ — §A, §B, §C, §E, §G, §H, §J and §K;
- the capa spike consumes real rows without adapter-side reshaping — *carried to
  Phase 8 (#44)*.

Locally (Windows, Python 3.13), lint, both type checkers, the docs build and 1586 tests
at 100 % coverage pass, on asyncio and trio. An independent review found nine issues,
all fixed before the commit; the ones that changed behaviour are listed below.

Differences from the plan above (decisions §13.1 #32–#44):

- **The streaming entry point came forward** (#32): `DeviceResult` and
  `PollSourceAdapter` exist; a sample-returning poll is left to the recorder, which
  times failed polls itself.
- **`read_metadata()` reads the current ranges** (#33), one more block, so the snapshot
  never depends on an earlier poll. The read procedures gained `read_poll()`, which
  returns the words before they are decoded, so a channel can join the poll that shows
  it alive (#34).
- **A connection failure breaks the session** (#35); later calls are refused before any
  I/O, and there is no reconnect.
- **`refresh_ranges()` is gone** (#36): `read_ranges()` refreshes the cache.
- **The profile holds what code uses** (#40): name, register map, default protocol and
  serial framing, the identify strategy and the discovery probe.
- **The safety-tier gate is not built** (#42): no public path above `READ_ONLY` exists
  before Phase 6, which adds the gate with the first write.
- **The commands** (#39, #41): `fuji-read` and `fuji-configure` run on a register bank
  with `--fixture`; `fuji-discover` needs named ports or `--all-ports`; the settings
  document is `fujilib-settings/1`.
- **Reads of a capability-gated register** by name (`read_parameter("clock.year")`) are
  refused before I/O once the capability is known to be absent, and a read of one
  probed block by name keeps that capability's availability current.
- **`identify()` reads the current ranges** with the readings (`0000h+42` instead of
  `0000h+36`), so a range change before the first poll is seen.
- **Replacing the channel map** with `identify(channel_map=...)` keeps the channels the
  old map established (§6.7).
- **A call refused while it waited for the port** (the session was closed or broke
  meanwhile) is not kept as the last error, and every `close()` waits for the
  operation in progress.
- **`PollSourceAdapter(name, device)`**, as in the siblings (unified API §E).
- **Discovery scans each port once**, however it is spelled, and refuses an empty or
  non-string port name.
- **The commands refuse bad arguments with exit code 2** (station, timeouts, alarm
  numbers, a gas asserted twice or as `unknown`), and a settings file that cannot be
  written is an error, not a traceback.
- **Tooling:** `ASYNC109` is ignored for the facade, the session and the factory, whose
  `timeout=` is the family's; a test that the write policy imports nothing of the
  register map now loads the package without its `__init__`, which imports the facade.

### Phase 5 — Streaming, sinks, CLI (software **done 2026-09-28**, hardware **done 2026-09-30**)

- ~~Port `streaming/` (the `sartoriuslib`-shaped recorder), `sinks/` (memory, CSV,
  Parquet) and their sync wrappers~~.
- ~~`fuji-stream`, `fuji-capture`, `fuji-diag timing`~~.
- ~~Guides (including the O2 scope statement of §2.11), API reference stubs, one
  example~~: `docs/recording.md`, `docs/cli.md`, `docs/measurement-quality.md`,
  `docs/api/streaming.md`, `docs/api/sinks.md`, `examples/record_to_parquet.py`.

*Software exit:* all of the above green without hardware. Met locally (Windows, Python
3.13): lint, both type checkers and the unit tests at 100 % coverage, on asyncio and
trio. An independent review preceded the commits.

*Hardware exit:* a 12-hour recording at 1 Hz on the bench (24 hours in the plan; 12 by
the owner's decision, #58) that checks:

- expected tick and row counts;
- status and provenance retention;
- bounded memory;
- error accounting;
- clean shutdown;
- readable output.

A controlled disconnect/reconnect is tested separately. `scripts/soak_monitor.py` runs
the recording and logs its memory; `scripts/check_soak.py` checks every item above
(`docs/hardware-test-day.md`). A 60-second rehearsal on the bench passed every check.
The read-only hardware tests, now including recording, the sinks and the three new
commands, pass 52 of 52 under asyncio and trio (findings §12). The first 24-hour
attempt (2026-09-29) polled for 10 h 17 min without a failure or a gap, but its window
ignored Ctrl-C and was closed, which killed it; 37,000 rows were recovered from its
Parquet file (findings §12.1). The unplug test passed on 2026-09-29 (findings §12.2):
with `--reconnect`, two pulls became two outages of error rows on an unbroken 1 Hz
schedule, each ended by reopening the analyzer on `COM8`; without it, the first pull
ended the capture with its files complete. The 12-hour recording (2026-09-29/30,
findings §12.3) passed every check: 43,200 polls of 43,200, none late, dropped or
failed, and memory within its bound. Its memory trend, +1.47 MB/h, came from the
Parquet sink's Arrow table per write; runs on the simulated analyzer showed it gone with
one table per row group, which the sink now makes (#87). *Hardware exit met* on
2026-09-30, by the owner's acceptance of this recording with the fix shown offline.

Differences from the plan above (decisions §13.1 #45–#56):

- **`Sample` carries `channels`** (#45), and a poll source describes its analyzers with
  `layout()` (#46), so every row of a recording has the same keys.
- **A connection failure ends a recording** unless a `ReconnectPolicy` is given, which
  reopens the analyzer with the new `Analyzer.reopen()` (#47). The session keeps what it
  learned and its traffic counters, and requires the same analyzer. Reopens are taken
  one at a time, `close()` waits for one in progress, a closed analyzer cannot be
  reopened, and the run loop moves a call that waited on the old port's lock to the new
  port.
- **The summary gains** `samples_dropped`, `error_samples`, `disconnects`, `reconnects`
  and `target_total_samples`; drift is the lateness of a poll's start; no percentiles
  (#48).
- **All three overflow policies** exist (#49).
- **Sinks lock their columns from `row_columns()`** and refuse an unknown channel; CSV
  quotes text so `None` and `""` differ (#50).
- **`pipe()` flushes on a timer** and finishes its last write under cancellation (#51).
- **The commands** stop cleanly on Ctrl-C with exit 0; `fuji-capture` writes a
  `.meta.json` beside the data and never replaces files without `--force` (#52).
- **The recorder's clock is injectable** for tests (#53); `fuji-diag timing` is the pairs
  probe on fujilib's own client (#54).
- **Along the way:** `fujilib._groups.unwrap` (shared with the portal), the blocking
  `PollSourceAdapter`, and `SyncAnalyzer.reopen()`.
- **After the first 24-hour attempt** (#57): Ctrl-Break stops the recording commands
  as Ctrl-C does; `fuji-capture` rewrites its `.meta.json` every minute and writes its
  progress line from a worker thread; `scripts/soak_monitor.py` turns Ctrl-C back on
  for the command and logs private memory; `scripts/check_soak.py` judges memory by
  its fitted trend; `scripts/recover_parquet.py` recovers a killed Parquet file.
- **After the 12-hour recording** (#87): the Parquet sink keeps the rows waiting for
  their row group as rows and makes one Arrow table per group, and closing writes the
  footer even when the last rows cannot be written.

**Release 0.1.0** (**released 2026-09-30**): monitoring, metadata and acquisition, the
settings writes and operation commands of Phase 6 (#59), and all of Phase 7: the
watching of manual calibrations (7A, #71) and the remote zero and span (7C, #95).
Before it (#55):
- ~~the 12-hour recording~~ (#58; passed 2026-09-30);
- ~~the unplug test~~ (passed 2026-09-29);
- ~~Phase 7A~~ (and 7B and 7C, #95);
- the owner's review of `docs/registers.md` (Phase 1's exit): *not done; the owner
  released 0.1.0 without it* (#55);
- ~~a decision on the capa spike~~ (#44: after the release).

### Phase 6 — Settings and operation commands (software and hardware **done 2026-09-29**)

- ~~Session write path: name resolution, validation, scaling refresh, function selection,
  envelope check, read-back verification, uncertain-outcome reporting~~:
  `devices/encode.py`, `devices/writes.py`, `Session.gate()`, `Session.write_setting()`.
- ~~`devices/settings.py` for a reviewed subset of fully specified settings, and
  `SettingsSnapshot` diff/apply with preflight~~: the subset is declared in the
  registry (§5.4); `devices/settings.py` compares and applies settings documents.
- ~~`devices/operations.py`: auto calibration, auto zero, blowback, return to measurement,
  `calibration_status()`, `wait_for_calibration()`, each reporting its affected channels
  and ranges~~, with `plan_auto_calibration()` and `plan_auto_zero_calibration()`.
- ~~`fuji-configure diff/apply`; `docs/safety.md`~~.

*Software exit:* met on 2026-09-29, locally on Windows and Python 3.13. Lint, both type
checkers and 2,346 unit tests at 100 % branch coverage pass under asyncio and trio. The
tests cover the write-policy and uncertain-outcome suites, a refusal test for every gate
that checks nothing was sent (with stale range tables too), and a recording across a
simulated calibration. An independent review made sixteen findings; fifteen were fixed
before handing back, and the ones that changed behaviour are listed with the
differences below. The sixteenth is a question for the owner: whether the type code
alone may authorize a calibration (#65), or only an assertion.

*Hardware exit:* **met on 2026-09-29** (below). The stateful session of
`docs/hardware-test-day.md`, authorized separately and attended by the owner:

- the `hardware_stateful` tests, which write and restore registers of absent channels
  first, then measurement-affecting settings, then a document and its baseline;
- key lock (§13.2 #12), a menu at the panel, a power cycle (§13.2 #14) and, if
  authorized, values out of range, with `scripts/probe_write.py`;
- the panel's schedule start time (§13.2 #26);
- a final `fuji-configure diff` against the settings saved first must show nothing to
  write.

Auto calibration and auto zero calibration are verified only on the simulator: the bench
analyzer has no auto-calibration option to drive gas valves (§2.6), so the
`hardware_destructive` tier stays unwritten until a rig with plumbed calibration gases
exists.

*The bench session* (findings §13):

- The read-only tests passed 52 of 52, and the stateful tests 9 of 10 at first.
- `test_selecting_the_other_range` failed. The range write was verified, but it read
  the channel's current range once, straight after, and the analyzer's current-range
  register can lag the setting by some tens of milliseconds (findings §13.2). The
  owner saw the panel switch, and four more round trips all switched, so the fault
  was that `set_range` returned before the range was in effect. It now returns once
  the channel measures on the range (#70). Rerun the same day, the stateful tests
  passed 10 of 10 (findings §13.8).
- Key lock, the panel menu, the power cycle and the out-of-range values ran as planned
  (§13.2 #12, #14, #32).
- The start time could not be read: this unit's panel has no auto-calibration menu
  (#26).
- The final `fuji-configure diff` showed nothing to write, after the session and again
  after the rerun.

It goes into **0.1.0** (#59).

Differences from the plan above (decisions §13.1 #59–#70):

- **Research before building changed the design** (§2.6, §2.7, §6.2). "At once" does not
  widen auto calibration or auto zero; the auto-calibration channels and ranges also
  drive auto zero; a panel forced stop exists but key lock blocks it; the MODBUS manual
  defers setting ranges to the instruction manual; the ZPA has no calibration valves
  of its own. No sibling reads a write back or reports an unknown outcome, so those
  have no family precedent.
- **The reviewed subset is 52 registers** (#60), declared writable in the registry;
  everything else became read-only, including documented settings (§5.4). Every
  writable setting is one FC06 word, so the write path has no function choice and no
  coalescing.
- **Write limits are the narrower of the two manuals'** (#61), plus percent-of-full-scale
  limits for calibration gases, and a calibration gas needs its range's unit (#62).
- **Writes and commands wait for a quiet analyzer** (#63): nothing is written while a
  calibration runs or the panel is in a menu, in a new error class,
  `FujiAnalyzerStateError`. A calibration is also refused during an instrument error.
- **The read-back after a lost reply decides the outcome** (#64).
- **Options are asserted or listed by the type code** (#65): `open_device(options=...)`,
  `TypeCode.options` from digits 21 and 22, and `MODEL_OPTIONS`; options never gate
  reads.
- **No command-line tool for operations** (#66); `apply` stops at the first failure and
  rolls nothing back (#67); a write-rate warning after `alicatlib` (#69).
- **The typed helpers are one setting each**: `set_hold_mode` and `set_hold_value`
  rather than one `set_hold`, and `set_calibration_gas(channel, range, kind, value,
  unit=...)`. There is no `configure_alarm`, since the alarms are read-only.
- **A range the channel does not have is refused.** The range tables list two ranges
  for every channel, so a channel's range count decides; the bench unit's Ch1 and Ch2
  have one.
- **The simulator acts on commands** (§10), and can acknowledge a write without storing
  it.
- **After the bench session** (findings §13): a range write returns once the channel
  measures on the range, and the simulator switches ranges after a configurable lag
  (#70); the `key_lock` register note, `docs/safety.md` and `fuji-configure apply`'s
  recovery hint no longer suggest that key lock blocks writes; `docs/safety.md` says
  that written settings are kept and that out-of-range values are stored.
- **Along the way:** a write whose port fails, or whose read-back's port fails, breaks the
  session, as does a port that fails in the status read after a command;
  `Session.verify_timeout`, derived from the port's timing;
  `apply_settings(max_tier=...)`, so the CLI's destructive flag is checked against
  apply's own comparison; `CommandResult.before` and
  `wait_for_calibration(since=...)`; commands that need an option are refused
  before `identify()`; `REVIEWED_SETTINGS` in `write_policy.py`, so a custom
  registry cannot widen the subset (§5.4);
  `fujilib-settings/1` moved to `devices/settings.py`; the register notes gained the
  instruction manual's side effects (a moving-average change restarts the average,
  switching the peak alarm on restarts its count).

### Phase 7 — The front panel: manual calibration watched, then driven (reopened 2026-09-29)

The first design did not plan this phase (§6.5): it was to wait for a real workflow,
then start with a design and a hardware prototype. The owner named the workflow on
2026-09-29 (#71), in three parts:

- capa's cone profile needs zero and span times, and firmware 1.02 has no calibration
  log to supply them;
- zero and span should be started from the host, with the operator switching the gas
  valves and naming the gas;
- fujilib may use the keys where they reach what nothing else can.

A calibration the owner made at the panel, watched read-only, answered the first design
questions (findings §14):

- the calibration leaves no trace in the holding registers;
- the step register, the zero and span flags and two undocumented words follow it
  closely;
- the detector's raw A/D count shows what the calibration was computed from.

The watch probe is `scripts/probe_calibration.py`.

**7A — Watching manual calibrations** (read-only; into 0.1.0):

- The registry gains `display.calibration_result` (00B9h) and `display.key` (00BDh) as
  observed registers (#73), and `DisplayState` carries both.
- `devices/panel.py`:
  - `PanelObservation`: one read of the panel, from a status read or a frame.
  - `ManualCalibrationTracker`: pure; it turns successive observations into
    `ManualCalibrationEvent`s. The outcome is completed, failed, cancelled or
    ambiguous, and the event records the evidence for it. It reads the step only on
    the measurement screen (#80).
  - `plan_manual_calibration()`: which channels and ranges a zero or span at the panel
    would calibrate, and against which gases.
- On the facade: `plan_manual_calibration(channel, kind)` and
  `wait_for_manual_calibration(timeout=..., interval=0.5, adc=False)`, with their
  blocking twins.
- In the simulator, `MockAnalyzer.press(key)`: the panel as findings §14 found it.
- No new row columns (#74). A calibration is its own record, which capa's
  `AnalyzerCalibration` (serial, zero and span time, span gas) can be filled from.

*Exit:*
- lint, both type checkers, tests at 100 % coverage and the docs build;
- on the bench, a zero and a span made at the panel while `wait_for_manual_calibration`
  watches, each reported completed, and a cancel reported cancelled
  (`examples/watch_manual_calibration.py`, `docs/hardware-test-day.md`).

*Software exit met* on 2026-09-29, locally on Windows and Python 3.13: lint, both
type checkers, 2,449 unit tests at 100 % branch coverage under asyncio and trio,
and the docs build. *Hardware exit met* on 2026-09-29 (findings §16): the owner's
O2 zero and span at the panel were each reported completed, and a cancel
cancelled. Found while building it and on the bench:

- A poll reads the readings and the zero and span flags in its first block and
  the step in its second, so a calibration can end between them. The first read
  back on measurement then still shows the old reading with its flag set. When
  it does, the tracker takes the readings after from the next read, and
  `ended_at` stays the first read on measurement.
- The bench unit has the absent Ch4 and Ch5 set to "at once" as well, so a plan
  for a zero of Ch1 lists them, marked not established.
- A cancelled calibration first reported a deviation from the calibration gas
  it never used; an event now reports deviations only when it ran.

**7B — Key prototype** (**done 2026-09-30**; one attended session, authorized
separately, #79, #82):

- `scripts/probe_panel.py`, on `anymodbus` directly as the probes of Phase 2 were. It
  sends only ZERO, SPAN, UP, DOWN, the ENT that selects a channel, and ESC, to 42001,
  and 1 to 42002. It never sends MODE, SIDE, or the ENT that starts a calibration.
  - Its one write takes an address and a value from a fixed list.
  - It reads the panel before every key and refuses a key the step does not allow.
  - It cleans up for the step it stops on.
  - `probe_panel.py check` runs every experiment, refusal and cleanup path on the
    simulator through an in-process fake station.
- It answers:
  - whether a key written to 42001 acts, and shows in 00BDh, as a key pressed at the
    panel;
  - how soon the step follows it;
  - whether key lock or the backlight swallows it;
  - what ESC and 42002 do from each step.
- In the same session, the owner answers findings §14.5 at the panel: the error
  display, the "at once" pair, a "both" channel, and output hold, with the A/D values
  watched under hold.

*Exit:* findings §18, and the design of 7C revised from them and estimated.

*Exit met* on 2026-09-30, 12:56–13:12 UTC (findings §18). Eight experiments sent
40 keys and two 42002, and nothing was calibrated. What it found:

- **A key written to 42001 acts** as the same key at the panel. The change shows by
  the first read after the reply, within about 0.1 s.
- **It never shows in 00BDh**, which reflects the keys pressed at the panel only.
- **Key lock swallows it**, although the analyzer acknowledges it, and the analyzer
  is then silent for about two seconds.
- **The cursor wraps round.** The "at once" pair's position reads Ch1 or Ch2 by the
  direction it was reached from. ZERO sometimes opens it on Ch1.
- **42002 on a wait step returns the display but leaves the flag set.** No timeout
  cleared it; the operator's ESC on that channel's wait step did.
- **The "at once" pair:** ENT on its position sets the zero flags of Ch1 and Ch2,
  not those of the absent Ch4 and Ch5.

Left open, and not in the session:

- the backlight and output hold, prepared as `probe_panel.py backlight` and `hold`
  and not run, at the owner's choice;
- the error display and a "both" channel, which need a real calibration.

**7C — Remote manual zero and span** (software and hardware **done 2026-09-30**; into
0.1.0, #95):

- The key driver of §6.5 in `devices/keys.py`: `Analyzer.manual_calibration(plan, gas=...,
  confirm=True)`, an async context manager (`RemoteCalibration`) that cleans up for the
  step it stops on, with `wait_steady()`, `read()`, `calibrate(confirm=True)` and
  `cancel()`, and its blocking twin `SyncRemoteCalibration`. The key that starts the
  calibration is `DANGEROUS`; the other keys are `STATEFUL`. From 7B (findings §18):
  - a key is confirmed by the step, the cursor and the flags, never by 00BDh;
  - the cursor is read after every key and moved with DOWN only, so an "at once"
    position is approached going down (#92);
  - key lock is read first and refused (#85), and output hold (#86);
  - a wait step is cancelled only with ESC;
  - a flag still set after the cleanup raises (#84).
- The write envelope takes 07D0h for the six calibration keys, with the value checked
  in the client and, independently, in the simulator (§5.4, §10).
- The simulator's panel brought in line with findings §18: the cursor wraps round and
  the "at once" position reads by direction; a key written to 07D0h acts but does not
  show in 30190; key lock swallows keys and the analyzer falls silent; 42002 on a wait
  step leaves the flags set; ZERO opens on Ch1 after a 42002. `MockAnalyzer.press()`
  stays the operator at the panel, and `MockAnalyzer.flow()` puts a gas at the inlet.
- A steadiness rule (#78, `devices/steadiness.py`), and the gas the operator names,
  checked against the calibration-gas setting (#89) and the reading.
- `fuji-calibrate`, an interactive command that walks the operator through the valves
  (#76, #77, #93).
- A calibration record, `fujilib-calibration/1` (#75, #90), written beside each run and
  the same for a calibration watched at the panel.

*Software exit met* on 2026-09-30, locally on Windows and Python 3.13: lint, both type
checkers, the unit tests at 100 % branch coverage under asyncio and trio, the docs
build, and `probe_panel.py check` on the changed simulator. What differs from the plan:

- **The driver has a module of its own**, `devices/keys.py`, the only one that writes
  42001; `devices/panel.py` stays the read-only watcher, and records both (#92).
- **The cursor moves with DOWN only.** The plan had it move towards the channel. Down
  only needs no direction logic, always approaches an "at once" position as the bench
  did, and comes round to a channel already passed when the panel does not offer the
  one wanted (#92).
- **Refused as well, before any key:** a plan widened to both ranges (#88), a hold
  flag set, and a second run on the same session (#91).
- **A key that may have reached the panel is recorded however its reads end**, a
  failed port included, so the cleanup reads the panel after it. A first draft lost
  such a key and skipped the cleanup; a test of a port failing mid-confirmation found
  it.
- **The gas is checked twice before the key that calibrates.** A gas judged steady
  can move out of the band by the next read; `calibrate()` then refuses, and
  `fuji-calibrate` waits for it to settle again (#93). The simulator's gas showed it.
- **An independent review of the change**, before the bench session, found and had
  fixed: a calibrating ENT that the panel swallowed left the run on the wait step, so
  a second `calibrate()` could send another (a key not seen taken now ends the run); a
  key write cancelled while it waited for its reply went unrecorded, and the cleanup
  then skipped a panel left on channel selection (keys are now recorded before they
  are written); the driver's key write was public, with no step check (now private);
  Ctrl-C in the blocking twin left the coroutine running on the loop while the
  cleanup ran (`SyncPortal.call_interruptible`); a steadiness verdict could rest on two
  reads either side of a long pause (`max_gap_s`, and `fuji-calibrate` reads on while
  it asks); `fuji-calibrate`'s prompt ran in a thread that could hold up the exit after
  Ctrl-C. Smaller ones: the cleanup's keys were not bounded by its deadline, a failed
  read before a cleanup ESC ended the cleanup, the result was lost if the cleanup was
  interrupted, the calibrating ENT's re-check read the panel before the settings and
  did not look at the hold flags or the reading's unit, and Ctrl-C always reported
  `cancelled`.

*Estimate:* about 7–10 working days, plus one attended bench session of an hour or two
with both gases:

| Part | Days |
|---|---|
| the key driver, its cleanup and the envelope's value check | 2–3 |
| the simulator's panel | 1 |
| the steadiness rule and the gas checks | 1–2 |
| `fuji-calibrate` and the record | 2–3 |
| the docs | 1 |

If #86 is settled by running the hold step first, that adds about 15 minutes at the
panel, before the session.

*Exit:*
- the software exit as for 7A;
- on the bench, with the owner at the gases, a remote zero and span of O2 that each
  complete (`docs/hardware-test-day.md`, "Remote zero and span");
- every cleanup path, driven by the simulator and, where it is safe to, by the
  prototype.

*Hardware exit met* on 2026-09-30, 15:32–15:36 UTC (findings §19), with the owner at
the gas valves: `fuji-calibrate` made a zero of O2 on N2 (0.05 → 0.00 vol%) and a span
on air (20.89 → 20.95 vol%), each `completed`, and a zero answered no was `cancelled`
with nothing run. Every key was taken by the first read after it; each cleanup found
the panel clean; every setting read as before. The gases were already steady when each
run began, so the steadiness rule's defaults stand (#78): the settling time after a
change of gas is still to be recorded with them. The cleanup paths were driven on the
simulator only: the bench session gave none of them cause to run.

### Phase 8 — The capa adapter (begun 2026-10-01)

capa gets a device adapter for the analyzer, built on fujilib (#44, #97–#106). It reads
CO2, CO and O2 with their validity, records the analyzer's metadata, changes its
settings, and drives a manual zero or span from capa's manual control panel (#98). The
work spans five repositories; each step ends with its repository's checks green.

**8A — The siblings' pins** (#101). fujilib 0.1.0 needs `anyserial` 0.2 and `anymodbus`
0.3. `alicatlib` 0.3.0, `sartoriuslib` 0.4.2 and `watlowlib` 0.7.0 cap `anyserial` below
0.2, and `watlowlib` 0.7.0 caps `anymodbus` below 0.2, so capa cannot install fujilib
beside them. Each sibling widens its pins in a patch release (0.3.1, 0.4.3, 0.7.1),
which capa's own pins already accept. `anymodbus` 0.3 checks a reply against its
request and retries a read on more kinds of bad reply, so `watlowlib`'s Modbus path
changes behaviour with it, without a code change.

**8B — fujilib 0.2.0.**

- `fujilib.testing.frames`: the model builders, public and in the wheel (#103).
- The settling period after a reopen (#81): `ReadingState.SETTLING`,
  `open_device(settle_after_reopen_s=...)` and `Session.settling_until`.
- `tests/unit/test_contract_capa.py` grows with whatever capa's mapping adds.

**8C — The adapter in capa** (`capa/devices/fuji.py`; its shape is in §7.8):

- **Plumbing:** the `fujilib` dependency; the `gas_concentration` channel kind (#99) and
  the `fuji_channel` binding (#106); the `fuji` adapter family; a capability flag for
  gas calibration; a check that every bound channel is in its device's channel map;
  channel templates for O2, CO2 and CO.
- **The read path:**
  - parameters: port, station, channel map, rate, time-out, reconnect, overflow and the
    asserted options;
  - open, identify and close, and the identity capa's equipment record reads;
  - `record()` with a `ReconnectPolicy` behind `stream()`: one `wide_row` record per
    tick, a channel sample per bound channel, and error rows;
  - the unit check and its quarantine;
  - an event at each change: connection lost and restored, an instrument error, hold,
    and a calibration seen at the panel (`ManualCalibrationTracker`, fed from the
    poll's own frames, so no extra I/O);
  - the snapshot, with the cached metadata flattened to scalars;
  - handshake and discovery.
- **The write path** (#104, #105): the settings writes, return to
  measurement, and a manual zero or span. capa calls `open()`, `stream()`, `command()`
  and `close()` in separate tasks of one event loop, and a `RemoteCalibration` must be
  entered and left in one task (§6.5). So one task of the adapter owns a run from its
  first key to its cleanup, and the commands signal it.
- **Around it:** the simulator (#100), example configurations, the Setup tab, the
  manual control card, the command-line help, tests and documentation. capa's
  documentation states that Modbus O2 is not validated for oxygen-consumption
  calorimetry (§2.11) and that the analyzer needs its warm-up time.
- **Not in the first capa pull request:** any change to capa's domain profiles (#102).

*Exit:*

- every repository touched passes its lint, type checks, tests and docs build; fujilib
  stays at 100 % coverage;
- a capa run on the simulated rig and one on the bench analyzer each write
  `device_records/fuji.parquet` and the analyzer's channels in `scalars.parquet`;
- capa's Setup tab discovers and configures the analyzer;
- the manual control panel changes a setting and runs a guarded calibration against
  the simulator;
- on the bench, reading only: handshake, discovery, a free run of several minutes, the
  USB adapter unplugged and plugged back mid-run, and a session with live plots;
- on the bench, writing, authorized separately as every write session is: a setting
  changed and restored from the manual card, and a calibration begun and cancelled on
  its wait step.

*Checked on the bench before the adapter was written* (2026-10-01, reads only): fujilib
on the event loop of a worker thread, driven as capa drives an adapter. capa gives every
serial port a thread with its own asyncio loop and runs `open()`, `stream()`,
`command()` and `close()` on it as separate tasks; fujilib had run on a worker thread's
loop only through the blocking facade's portal (§7.3, findings §11). On `COM8`, with the
analyzer opened in one task, a recording at 2 Hz with a `ReconnectPolicy` in a second,
two metadata reads from a third while it recorded, and the close in a fourth: 21 of 21
polls answered, about 48 ms each, no error. The loop was Windows' Proactor loop, the
default, which `anyserial` needs; capa does not change the loop policy.

**After Phase 8, as hardware, manuals and need allow:**

- `FujiManager` and multi-drop (#17). Until then capa takes one analyzer per port.
- Further sinks (SQLite, JSONL, Postgres).
- Validate ZPB / ZPG / ZPAJ / ZPG3E; add their type-code tables; exercise the RS-232C
  path.
- The remaining upstream `anymodbus` item (§4.7 item 15).
- In capa, each needing its own decision:
  - calibration as a procedure with its own bundle;
  - calibration records feeding the cone profile's `AnalyzerCalibration` and a
    zero/span-recency check;
  - the oxygen channel group accepting the new channel kind, after the
    Modbus-against-analog comparison (§2.11);
  - an indicator for readings whose status is not `ok`;
  - record files per device, for more than one analyzer on a rig.

### Sequencing

```
decisions ─► Phase 0 ─► Phase 1 ─► Phase 3 ─► Phase 4 ─► Phase 5 ─► Phase 6 ─► 7A ─► 7B ─► 7C ─► 0.1.0
                            ▲         ▲           ▲
anymodbus 0.2.1 ────────────┼─────────┘           │
Phase 2 (bench) ────────────┴─────────────────────┘   (findings feed registry, defaults, O2 scope)
```

The read-and-record slice was estimated at about 18–23 working days (3.5–4.5
engineer-weeks) plus the hardware session and the soak, and the writes at another
1–1.5 weeks. Both are in 0.1.0 (§13.1 #59), and so is all of Phase 7 (#71, #95).

Phase 8 follows the release. The siblings' patch releases (8A) and fujilib 0.2.0 (8B)
come before capa can install the adapter's dependencies from PyPI; until then capa
develops against local checkouts.

---

## 13. Open items

### 13.1 Decisions for the owner

| # | Decision | Recommendation |
|---|---|---|
| 1 | ~~`Sample` and timestamp names: unified API (`t_mono_ns`, …) or `servomexlib`'s (`monotonic_ns`)~~ | **RESOLVED 2026-09-28: unified API.** It is what `capa` reads from `watlowlib`, `sartoriuslib` and `alicatlib` |
| 2 | Scope of the first release | ~~Read-only monitoring, metadata and acquisition (0.1.0); a reviewed subset of writes in 0.2.0~~ **Revised 2026-09-29 by the owner (#59):** 0.1.0 has both |
| 3 | Key simulation and manual calibration | ~~Not planned (§6.5)~~ **Revised 2026-09-29 by the owner (#71):** manual calibrations are watched (Phase 7A) and will be driven with the calibration keys only (7C) |
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
| 14 | Asserted channel map for the bench rig | **Adopted 2026-09-28** with Phase 4 on the owner's "proceed"; not separately confirmed: Ch1 CO2, Ch2 CO, Ch3 O2; inferred labels never bind scientific channels |
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
| 32 | Bring `DeviceResult`, `PollSourceAdapter` and a sample-returning poll into Phase 4 | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: `DeviceResult` and `PollSourceAdapter` only. The recorder builds samples and times failed polls, as in the siblings |
| 33 | Where `read_metadata()` gets the current ranges | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: it reads FC04 `0025h+5` itself (5 transactions with the clock) |
| 34 | When a channel seen non-zero joins the established channels | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: in the poll that shows it. A recording fixes its columns when it starts, so a later channel is left out of its rows |
| 35 | What a connection failure does to the session | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: it breaks it, as in `sartoriuslib` and `alicatlib`; later calls fail before I/O; `close()` works; no reconnect (a recorder policy, Phase 5). Timeouts do not break it |
| 36 | `refresh_ranges()` beside `read_ranges()` | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: dropped; `read_ranges()` refreshes the cache |
| 37 | `devices/metadata.py` | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: dropped; `AnalyzerMetadata` is in `models.py`, its read in `reads.py` |
| 38 | A station whose type code names no ZP model; `DeviceHealth` | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: `identify()` raises `FujiProtocolUnsupportedError` and `open_device` closes what it opened. `PARTIAL` means a probe had no definite answer or the type code's table is unknown; a failed identify raises rather than returning `FAILED` |
| 39 | Which ports discovery scans from the command line | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: named ports, or every host port only with `--all-ports`; the library keeps `ports=None` = every port. Nothing found exits 2, as in `servomexlib` and `sartoriuslib` |
| 40 | What a `DeviceProfile` holds | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: what code uses (§12 Phase 4). The read regions, word limit and type-code tables stay module constants until a second profile needs them |
| 41 | What the commands' `--fixture` is | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: a register bank on the simulated analyzer, or `bench`; not arrow replay |
| 42 | Build the confirm gate before any operation above `READ_ONLY` exists | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: no; it comes with the first write (Phase 6) |
| 43 | The O2 comparison in Phase 4 | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: off the Phase 4 path; it needs the analog output wired (§13.4 Q3) and acceptance limits (#15) |
| 44 | The capa adapter spike before 0.1.0 | **Decided 2026-09-30 by the owner**, by releasing 0.1.0 without it: the capa adapter is written after the release, as the other adapters were; `tests/unit/test_contract_capa.py` already builds capa's record shapes from fujilib's rows. (The alternative was a first draft on a capa branch before the release, against fujilib as a local path dependency, while the sample shape could still change) |
| 45 | How a failed poll's row keeps the recording's columns | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: `Sample.channels`, set by the recorder on every sample; `sample_to_row(sample)` uses them. capa calls `sample_to_row(sample)` without channels and raises on schema drift |
| 46 | How the recorder learns each analyzer's station, protocol and channels | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: `PollSource.layout()`, read once at the start; an analyzer with no established channel is refused |
| 47 | What a disconnect does to a recording | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: it ends it after the tick's batch is delivered, raising at the block's exit; an opt-in `ReconnectPolicy` reopens the analyzer (`Analyzer.reopen()`, same serial number and type code required) on a back-off schedule |
| 48 | The summary's fields | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: the siblings' five plus `target_total_samples`, `samples_dropped`, `error_samples`, `disconnects`, `reconnects`; drift is the lateness of a poll's start; no latency percentiles. **2026-09-29, at the owner's request** after the unplug test: `disconnects` counts the connection failure that ends a recording too, not only the outages a `ReconnectPolicy` rides out |
| 49 | Overflow policies | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: `BLOCK`, `DROP_NEWEST` and `DROP_OLDEST`, dropping whole batches, counted apart from late ticks |
| 50 | How sinks fix their columns | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: from `row_columns()` (never from values), locked at `open()` or by the first batch; an unknown channel raises `FujiSinkSchemaError`; file I/O in worker threads; CSV quotes text |
| 51 | `pipe()` | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: groups of `batch_size`, a timer flush while idle, the last write finished under cancellation, counts in polls; the commands report the recorder's summary |
| 52 | The recording commands | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: run until `--duration` or Ctrl-C, which exits 0; `fuji-capture` writes `<out>.meta.json`, refuses to replace files without `--force`, and checks for `pyarrow` before opening the port |
| 53 | Testing the schedule | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: the recorder runs on an injectable clock; tests use a manual one |
| 54 | `fuji-diag timing` | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: built, as the pairs probe on fujilib's own client |
| 55 | What 0.1.0 waits for | **Adopted 2026-09-28** on the owner's "proceed"; not separately confirmed: the 24-hour recording and the unplug test, the owner's review of `docs/registers.md`, and a decision on #44. **2026-09-30:** the owner released 0.1.0 once the recording (12 hours, #58), the unplug test and Phase 7 had passed and #44 was decided; the review of `docs/registers.md` is still outstanding |
| 56 | The 24-hour recording | **Adopted 2026-09-28**: the owner left the analyzer connected and allowed any hardware test; read-only, 1 Hz, `fuji-capture` to Parquet with `--reconnect`, under `scripts/soak_monitor.py`. 12 hours rather than 24 since 2026-09-29 (#58) |
| 57 | What a recording keeps when it cannot be stopped with Ctrl-C, or is killed | **Adopted 2026-09-29 at the owner's request**, after the first 24-hour attempt (findings §12.1): Ctrl-Break stops the recording commands as Ctrl-C does; `fuji-capture` rewrites its `.meta.json` every minute with the counters so far, each write replacing the file whole; its progress line is written from a worker thread; the soak tools gain `recover_parquet.py`, Ctrl-C for the command under `soak_monitor.py`, private memory in its log, and a memory check by fitted trend. Parquet stays the soak's format |
| 58 | The length of the hardware exit's long recording | **Decided 2026-09-29 by the owner:** 12 hours at 1 Hz, run overnight, instead of 24; a day is not expected to show anything 12 hours would not. The first attempt's 10 h 17 min without a failure or a gap (findings §12.1) supports it |
| 59 | How Phase 6 is sequenced against 0.1.0 | **Decided 2026-09-29 by the owner:** Phase 6 merges into `main` once its bench session has passed, before 0.1.0 is tagged, so 0.1.0 includes the reviewed writes and the operation commands. (First adopted: the registry corrections before 0.1.0 and the rest after it, so that 0.1.0 stayed read-only) |
| 60 | Which settings are written | **Adopted 2026-09-29** on the owner's "proceed"; not separately confirmed: the 52 registers of §5.4 (calibration gases and scope, response times, output hold, hold mode and values, range and range method); everything else read-only |
| 61 | The limits of a write | **Adopted 2026-09-29** on the owner's "proceed"; not separately confirmed: the narrower of the two manuals' where they disagree (the MODBUS manual defers to the instruction manual, TN5A1190a p.28); calibration gases 0-100 % (zero) and 1-105 % (span) of their range's full scale |
| 62 | The unit of a scaled write | **Adopted 2026-09-29** on the owner's "proceed"; not separately confirmed: required (`unit=`), and refused unless it is the unit of the gas's (channel, range), against a vol%/ppm slip of 10^4 |
| 63 | Writing while the analyzer is busy | **Adopted 2026-09-29** on the owner's "proceed"; not separately confirmed: refused, nothing written, while a calibration runs, a channel is being calibrated, the panel is in a menu or in a manual calibration (`FujiAnalyzerStateError`); return to measurement is the exception, except while a calibration flag is set (#83). A calibration is also refused during an instrument error |
| 64 | The outcome of a write whose reply is lost | **Adopted 2026-09-29** on the owner's "proceed"; not separately confirmed: the read-back decides: as written is verified, otherwise a mismatch; unknown only when the read-back fails too. The write is never retried |
| 65 | How an option is known to be fitted | **Adopted 2026-09-29** on the owner's "proceed"; not separately confirmed: listed by the type code (digits 21 and 22) or asserted with `open_device(options=...)`, like gas labels; refused otherwise, and always on a model whose manual has no such option (blowback on the ZPA). Options never gate reads |
| 66 | A command-line tool for the operation commands | **Adopted 2026-09-29** on the owner's "proceed"; not separately confirmed: none; the operation commands are for programs |
| 67 | What a failed apply does | **Adopted 2026-09-29** on the owner's "proceed"; not separately confirmed: stops at the first failure, reports what completed, failed and was not attempted, and rolls nothing back |
| 68 | Probing the analyzer's own handling of out-of-range values | **Adopted 2026-09-29** on the owner's "proceed"; not separately confirmed: prepared as `scripts/probe_write.py out-of-range` on a register of an absent component; run only if authorized on the day of the bench session |
| 69 | A warning against periodic writes | **Adopted 2026-09-29** on the owner's "proceed"; not separately confirmed: after `alicatlib`, a warning above `write_warn_per_minute` setting writes a minute, 10 by default, an argument of `open_device` |
| 70 | When a range write is complete | **Adopted 2026-09-29** on the owner's "fix what the bench session found"; not separately confirmed: once the channel measures on the range, not when the setting reads back (findings §13.2). The current range is read within the read-back budget until it follows; if it never does, or cannot be read, `FujiVerificationError`. The simulator switches the current range after `MockAnalyzerConfig.range_lag_s` |
| 71 | Phase 7 | **Decided 2026-09-29 by the owner:** reopened. Watching manual calibrations (7A) goes into 0.1.0. Remote manual zero and span from the host (7C) is wanted, with the operator switching the gas valves by hand and naming the gas. fujilib may use the keys where they reach what nothing else can. (First: not planned, #3) |
| 72 | What the keys are used for | **Adopted 2026-09-29**, as explained to the owner: starting and cancelling a manual zero or span, nothing else. Modbus reports which screen is shown, never its text, so a panel-only screen such as the maintenance-mode calibration log cannot be read by navigating to it; and MODE and SIDE, the keys into the menus and their passwords, are never sent |
| 73 | 00B9h and 00BDh | **Adopted 2026-09-29** on the owner's "proceed"; not separately confirmed: observed input registers, `display.calibration_result` and `display.key` (findings §14.2), read with the poll. An outcome never rests on 00B9h alone when the step or the flags say otherwise |
| 74 | Row columns for the panel | **Adopted 2026-09-29** on the owner's "proceed"; not separately confirmed: none. Manual calibrations are records of their own, so the 0.1.0 row shape is unchanged |
| 75 | What a calibration record keeps | **Adopted 2026-09-29**, at the owner's request to log the A/D values: the readings before and after, the deviation from the calibration gas, and the raw A/D values at the last read before it ran. That is what firmware 2.24's calibration log keeps; the counts are no finer than the reading (findings §14.4) |
| 76 | A command-line tool for manual calibration | **Adopted 2026-09-29** on the owner's "proceed"; not separately confirmed: `fuji-calibrate`, interactive, in 7C. #66 stands for the operation commands |
| 77 | Who starts a remote calibration once the gas is steady | **Adopted 2026-09-29** on the owner's "proceed"; not separately confirmed: fujilib judges the gas steady; it asks before the calibrating key unless told not to (`--auto`) |
| 78 | The steadiness rule | **Decided 2026-09-30 by the owner** (first adopted on the owner's \"proceed\"): `SteadinessRule`, with the recommended defaults, to be tuned from the wait-step reads the bench session records: the reading moves no more than 0.5 %FS over the longer of 30 s and twice the channel's response time, and the window's mean lies within 10 %FS of the named gas, with no two reads in it more than 5 s apart; every channel of the calibration at once; give up after 10 minutes. The response time is the channel's slot where the asserted gases say which, else the longest of the five. The read before the calibrating key must still be steady. On the bench on 2026-09-30 (findings §19) steady gases met it with a wide margin, 0.00–0.05 %FS of movement and 0.2–0.3 %FS from the gas; the defaults stand until a session records the settling after a change of gas. On 2026-09-29 O2 settled within 0.01 vol% about 36 s after the gas changed (findings §14.4). The A/D counts are recorded, not judged: output hold is refused (#86) |
| 79 | Calibrations and keys on the bench analyzer | **Decided 2026-09-29 by the owner:** calibrations at the panel by the owner are allowed (an O2 zero and span were made, findings §14). Each session in which fujilib sends a key needs its own authorization |
| 80 | The step register on a menu screen | **Adopted 2026-09-29 at the owner's request** ("fix the calibration tracker"), after findings §15.2. The menus put page numbers in 30182, some equal to calibration steps, and the tracker had turned a menu walk into five calibration events. 30182 is now a step only while 30181 shows measurement, as the manual defines it (TN5A1190a p.46). `PanelObservation.step` is `NONE` on any other screen, so a menu page neither starts a pass nor continues one. The decoder keeps the raw page number there. An event whose first read after it shows a menu says so in its evidence. The write refusal already checked the screen first (#63) |
| 81 | Readings while the analyzer warms up after a power cycle | **Decided 2026-10-01 by the owner: (b)**, with one case still open (below). For about a minute after power-on the readings are far off, CO2 up to 220 %FS, and no flag says so (findings §15.5). A recording with `--reconnect` rides out the outage and keeps these rows with state `ok`. Options: (a) leave them, and document it; (b) give the polls of a settling period after a reconnect, about 90 s, a state that is not `ok`; (c) detect the warm-up from the analyzer, but nothing found so far marks it. The only trace of a power cycle is that 00B9h and the cursor read 0, which they may do anyway. Recommendation: (b), since a USB unplug and a power cut look the same from the host. **As built for 0.2.0:** after a reopen that follows a connection failure, every reading polled in the next `settle_after_reopen_s` seconds (90 by default, an argument of `open_device`; 0 turns it off) that would be `ok` has a new state, `settling`, so it is not valid; its raw value is kept, as with every other state, and a reading with a reason of its own keeps that reason. The period is the session's (`Session.settling_until`), so `poll()`, the recorder and capa all see it. Nothing is flagged at the first open: the host cannot know how long the analyzer has been on, and the documentation says to respect the manual's warm-up time. A replug with the analyzer still powered is flagged too, since flagging 90 s of good readings is the cheaper mistake. capa passes the state through as the channel sample's status, the word its balance adapter already uses for an unstable reading. **Still open *(awaiting)*:** the recommendation's premise is wrong for one case. A pulled USB cable fails the port, but a power cut that leaves the adapter powered does not: the port stays open, the polls time out, and no reopen follows. That is the case findings §15.5 observed, and its readings are still `ok`. Proposed: the period also starts when a poll is answered after one that got no reply at all (a timeout after its retries). A poll that fails that way is rare (none in the 43,200 of the 12-hour recording), so little good data would be marked |
| 82 | The key prototype's session | **Decided 2026-09-30 by the owner:** authorized, with the owner at the panel (#79). Experiments 1-8 of `docs/hardware-test-day.md` ran, key lock included; the backlight and output-hold steps did not. Zero gas was at the inlet for the zero experiments and air for the span one. When 42002 left Ch3's zero flag set, the owner chose to clear it at the panel (ZERO, O2, ENT, ESC) rather than by a power cycle (findings §18.4) |
| 83 | `return_to_measurement()` during a manual calibration | **Decided 2026-09-30 by the owner:** (b) with (c) as a backstop, in 0.1.0. 42002 on a manual calibration's wait step returns the display to measurement but leaves the channel's flag set, and nothing at the panel shows it (findings §18.4); #63 let the command through in every state, and it reported `done` on the measurement screen. It now reads the status first and is refused, nothing sent, while any calibration flag is set, automatic or manual (`FujiAnalyzerStateError`, saying that ESC on the wait step cancels). It still closes menus and a manual calibration's channel selection. It is `done` only when the panel shows the measurement screen with no flag set; a flag set after it, from a calibration begun at the panel meanwhile, raises `FujiVerificationError`. (The other options were (a) documenting it only, and (c) alone: keep sending it, but not `done` while a flag is set) |
| 84 | How a remote calibration cancels and recovers (7C) | **Decided 2026-09-30 by the owner** (first adopted on the owner's \"proceed\"): as recommended. ESC is the only cancel on a wait step, never 42002 (findings §18.4). A flag still set on the measurement screen after the cleanup raises `FujiAnalyzerStateError`, naming the channel and the recovery: enter its wait step and press ESC. fujilib does not attempt that recovery itself; it would need ZERO or SPAN while a flag is set, which the driver otherwise refuses |
| 85 | Key lock and remote keys (7C) | **Decided 2026-09-30 by the owner** (first adopted on the owner's \"proceed\"): as recommended, and read again before the calibrating key. Key lock (40074) on refuses the run, nothing sent. Its keys are acknowledged and swallowed, and the analyzer is then silent for about two seconds (findings §18.6); a key swallowed anyway is not seen taken and stops the run |
| 86 | Output hold and remote calibration (7C) | **Decided 2026-09-30 by the owner** (first adopted on the owner's \"proceed\"): (b), until (a) has run. A remote calibration is refused while output hold (40093) is on, or any channel's hold flag is set. Whether the readings and the A/D values freeze under hold is still open (§13.2 #38); (a), `probe_panel.py hold` before a bench session, would let the rule use the A/D counts under hold |
| 87 | How the Parquet sink holds the rows waiting for their row group | **Decided 2026-09-30 by the owner**, after the 12-hour recording (findings §12.3): its private memory rose 1.47 MB/h, within the bound, because each write, a row a second, became its own Arrow table, and the native memory those tables used was never given back, with mimalloc or the system allocator. The waiting rows are now kept as rows and made into one Arrow table per row group: on the simulated analyzer, 46 B a poll instead of 1,459. Closing writes the footer even when the last rows cannot be written. A row Arrow cannot convert now fails the write of its group rather than its own write; rows come from `sample_to_row()`, so that would be a fujilib bug. The owner accepted the recording as Phase 5's hardware exit with the fix shown offline, without a second recording on the bench |
| 88 | A remote calibration that "both" widens to both ranges (7C) | **Decided 2026-09-30 by the owner** (first adopted on the owner's \"proceed\"): refused, nothing sent, until the bench has shown which ranges such a calibration changes (§13.2 #37). The operator sets the channel's calibration range to "current" first |
| 89 | The gas the operator names (7C) | **Decided 2026-09-30 by the owner** (first adopted on the owner's \"proceed\"): `CalibrationGas(value, unit, label)`, one for every established channel of the plan, or one for all. It must equal the calibration-gas setting of every range the calibration touches, at that range's resolution and in its unit, since the analyzer calibrates against the setting; otherwise the run is refused and the setting is changed first (`set_calibration_gas`, DANGEROUS). A zero gas of 0 needs no unit. The label (a cylinder, a lot) is kept in the record |
| 90 | The calibration record (7C) | **Decided 2026-09-30 by the owner** (first adopted on the owner's \"proceed\"): a `fujilib-calibration/1` JSON document, the same for a calibration watched at the panel (`source` "panel") and one driven from the host ("remote"): the analyzer's identity; the kind, outcome and evidence; the channels, their gases and ranges; when it started, was selected, ran and ended, and `calibrated_at`; the calibration-gas settings, the readings before and after, the deviations, new errors and detector counts. A driven run adds the plan, the gas named, the steadiness and its rule, every read on the wait step, the keys, the cleanup, the operator and notes. `fuji-calibrate` writes one per run. capa's `AnalyzerCalibration` (analyzer, serial, zero and span times, span gas as text; no other fields) is filled from the latest completed zero and span per channel, with the gas's label: the capa adapter's work (Phase 8) |
| 91 | Concurrency during a remote calibration (7C) | **Decided 2026-09-30 by the owner** (first adopted on the owner's \"proceed\"): one run per session holds the panel (`Session.claim_panel`); meanwhile that session refuses setting writes, commands and another run. The port is free between keys and between reads on the wait step, so a recording goes on, its rows `calibrating`. Each key holds the port for its read, write and confirming reads |
| 92 | Where the key driver lives and how it moves (7C) | **Decided 2026-09-30 by the owner** (first adopted on the owner's \"proceed\"): `devices/keys.py`, the only module that writes 42001, rather than `devices/panel.py` (§3). The cursor moves with DOWN only, each key confirmed by the cursor moving, until it reads the planned channel or the first "at once" channel; round to a channel already passed, the run stops. A key not seen taken within 2 s (`key_timeout`) stops the run; a lost reply is settled by the reads; the calibrating key is followed for up to 30 s (`run_timeout`), riding out the silence while the analyzer stores |
| 93 | `fuji-calibrate` (7C) | **Decided 2026-09-30 by the owner** (first adopted on the owner's \"proceed\"): `--plan` only reads; otherwise `--confirm` and `--i-understand-this-is-destructive` are needed before the port opens, and the gas named with `--gas-value`, `--gas-unit` and `--gas-label`. It asks before the calibrating key unless `--auto` (#77), and waits again when the gas moves between steady and the key. Ctrl-C cancels, with the cleanup, and exits 1. It ends with `status:` (`completed`, `failed`, `ambiguous`, `cancelled`, `refused`, `stopped`, `not_clean` or `plan`) and exits 0 only for `completed` and `plan` |
| 94 | Hardware checks for 7C | **Decided 2026-09-30 by the owner** (first adopted on the owner's \"proceed\"): no pytest; the operator switches the valves by hand, so the exit is an attended session with `fuji-calibrate` (`docs/hardware-test-day.md`), authorized as every key session is (#79) |
| 95 | How Phase 7C is sequenced against 0.1.0 | **Decided 2026-09-30 by the owner:** all of Phase 7 merges into `main` once 7C's bench session has passed, before 0.1.0 is tagged, so 0.1.0 includes the remote zero and span and the write envelope's key register. (First: 7C after 0.1.0, so that 0.1.0 wrote no key) |
| 96 | Where the documentation is hosted | **Decided 2026-09-30 by the owner:** Cloudflare Pages, like every sibling: project `fujilib-docs`, served at `https://fujilib.graysonbellamy.dev/`, deployed from CI by `cloudflare/wrangler-action` with the repository secrets `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`. `docs/_headers` sets the siblings' security headers, and the footer links to graysonbellamy.dev, GitHub and PyPI. Pushes to `main` and manual runs deploy; pull requests only build. (First: GitHub Pages, from a copy of `servomexlib`'s docs workflow older than its own move to Cloudflare) |
| 97 | Where the capa adapter lives | **Decided 2026-10-01 by the owner:** in capa itself, as `capa/devices/fuji.py`, not in a plugin package. capa's `SourceBinding` is a closed union in its core, so a new binding needs a change there however the adapter ships |
| 98 | What the capa adapter may change on the analyzer | **Decided 2026-10-01 by the owner:** settings and calibration, from capa's interface. (The recommendation was an adapter that only reads, with the writes later.) Which writes, and behind which gate, is #104 |
| 99 | The channel kind of a concentration in capa | **Decided 2026-10-01 by the owner:** a new kind, `gas_concentration`, rather than capa's process-variable kind |
| 100 | capa's simulated analyzer | **Decided 2026-10-01 by the owner:** hand-written like capa's other simulators, not `MockAnalyzer` on a serial port pair. It builds real frames (#103), so its rows have exactly the adapter's keys |
| 101 | The siblings' `anyserial` and `anymodbus` pins | **Decided 2026-10-01 by the owner:** `alicatlib`, `sartoriuslib` and `watlowlib` may be changed to accept `anyserial` 0.2 and, in `watlowlib`, `anymodbus` 0.3, in patch releases. Without them capa cannot install fujilib (Phase 8A) |
| 102 | capa's domain profiles | **Decided 2026-10-01 by the owner:** unchanged in the first capa pull request. The cone profile's oxygen group stays analog-only and its `AnalyzerCalibration` stays typed by the operator (§2.11) |
| 103 | The model builders | **Adopted 2026-10-01** with #100; not separately confirmed: `timing`, `status`, `reading`, `analyzer`, `frame` and `bench_readings` move from the test suite into `fujilib.testing.frames`, public and in the wheel, for 0.2.0 (§10). `timing()` takes a caller's clock origins and `frame()` its timings, so a simulator can stamp its frames; the defaults stay the fixed ones the suite uses |
| 104 | Which writes capa exposes, and behind which gate | **Decided 2026-10-01 by the owner**, as proposed: capa's own authorization gate first, then a rule by fujilib's tier (§6.2). The `PERSISTENT` setters (response time, output hold, hold mode and value, range, range method) and the `STATEFUL` commands (return to measurement, cancelling a calibration) pass with either of capa's authorizations: a run's, or a person's confirmation at the interface. Everything `DANGEROUS` (a calibration gas, the key that calibrates, auto calibration and auto zero where the option is asserted) needs a person's confirmation even inside an authorized run, so a method step cannot calibrate on its own; beginning a calibration needs it too. `write_parameter` and `apply_settings` take the tier of what they would write. An unsteady reading blocks the key that calibrates unless it is forced with a second confirmation. A refusal by fujilib (a value, the analyzer's state, a missing option, an exception reply) is a result, not an exception; an unverified or unknown outcome is a failed result and an error event. Blowback is not exposed |
| 105 | Where capa keeps calibration records | **Decided 2026-10-01 by the owner**, as proposed: the manual control card saves each `fujilib-calibration/1` record (#90) as a JSON file under the workspace's `configs/calibrations/analyzer/`, named by serial number, kind and UTC time, never overwriting one, and logs a manual event. A calibration that ends during a run is also an event in that run's bundle |
| 106 | What a capa channel binds | **Adopted 2026-10-01** with #97; not separately confirmed: a channel (`CH1`–`CH12`) and a field, `value` (the concentration) or `valid` (1.0 or 0.0 from `Reading.valid`; no sample while it is unknown). capa stores `ChannelSample.status` but nothing in it reads it, so validity needs a channel of its own for alarms and procedures to watch. A reading whose unit is not the channel's declared unit quarantines the channel until the next run starts, with one event. The check compares canonical unit names, since capa's unit registry cannot tell percent from ppm by dimension, and runs on every sample, since a range change can change the unit |
| 107 | A response time of 0 | **Decided 2026-10-01 by the owner:** allowed. A response time is written as 0–60 s, the MODBUS manual's limits. On the bench unit 0 switches the filter off: the A/D values then equal the unsmoothed detector counts. Any other setting is the length of a moving average, so 1 s still averages over a second (findings §20). The other writable limits stay the narrower of the two manuals' (§2.6). (First: 1–60 s, the narrower of the two manuals', as for every writable limit) |

### 13.2 Hardware verification

Answered by the read-only probes of 2026-09-28, the writes of 2026-09-29 and the keys
of 2026-09-30. Details and data are in [protocol-findings.md](protocol-findings.md).

| # | Question | Result |
|---|---|---|
| 1 | Inter-frame gap and latency | **≤ 1 ms after any reply, normal or exception**, measured from the reply by busy-wait, in all four normal/exception pairings in randomized order. The first draft's "20 ms after an exception" was a measurement artifact (§2.4). 12–27 ms per word read, 50–63 ms per 64 words |
| 24 | The link re-run after `anymodbus` 0.2.1 | Done (findings §6.3). Confirms item 1, including normal → exception. `anymodbus` 0.2.1 keeps its 5 ms idle after every reply on the analyzer (minimum 5.2 ms, 0 of 1,000 failed); 0.2.0 collapsed it to about 0.1 ms after an exception |
| 2 | Reply to unsupported function codes | FC01 and FC02 answer exception **02**, not 01. FC07, FC08/0000h, FC0B, FC0C, FC11, FC14, FC18 and FC2B/0Eh answer **01** (findings §15.1) |
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
| 30 | The facade, discovery, the blocking facade and the commands on the analyzer | Done 2026-09-28 (findings §11). 45 of 45 hardware tests under asyncio and trio; open and identify in 0.3 s; an empty station times out and releases the port |
| 31 | Recording, the sinks and the recording commands on the analyzer | Done 2026-09-28 (findings §12). 52 of 52 hardware tests under asyncio and trio; a 60-second capture at 1 Hz passed every soak check. The first 24-hour attempt was killed after 10 h 17 min without a failed poll (findings §12.1); a 12-hour run replaces it (#58); the unplug test passed 2026-09-29 (findings §12.2); the 12-hour recording passed every check 2026-09-29/30, its memory trend traced to the Parquet sink (findings §12.3, #87) |
| 12 | Whether key lock (40074) blocks Modbus writes or commands | **It does not block writes** (2026-09-29, findings §13.3). With key lock on, a setting write was acknowledged and verified, and return to measurement was acknowledged. The panel was already on the measurement screen, so whether the command would close a menu under key lock is not shown |
| 14 | Whether settings survive a power cycle without an explicit save | **They do** (2026-09-29, findings §13.5). A value written over Modbus read back unchanged after the analyzer was off for 10 s; there is no save step |
| 32 | Whether the analyzer refuses, clamps or stores a value outside a setting's documented range | **It stores it** (2026-09-29, findings §13.6). 61 and 0 written to the response time of NDIR component 4 were acknowledged without an exception and read back as written; fujilib's limits are the only guard. One register tried, of an absent component |
| 35 | What a manual calibration at the panel leaves in the registers | Done 2026-09-29 (findings §14), watched read-only while the owner zeroed and spanned O2 and cancelled a zero. No holding word changes, the factory blocks included. The step register, the per-channel zero and span flags, 00B9h (the last calibration) and 00BDh (the key being pressed) follow it; the detector's A/D count is not changed by it. fujilib's own watcher then recorded a zero, a span and a cancel as they happened (findings §16) |
| 40 | What the undocumented registers hold | Done 2026-09-29 (findings §15), read-only, while the owner walked the menus and power-cycled the analyzer. 30182 numbers menu pages off the measurement screen (#80). 046Ah–0471h are the unsmoothed detector counts. The factory blocks hold the "other parameters" at 0C2Dh–0C34h, but no calibration coefficients, and they did not change across a power cycle. The readings are wrong for about a minute after power-on, with no flag set (#81). The register capture of 2026-09-28 has one bad word, 0440h |
| 39 | Whether a key written to 42001 acts, and shows in 00BDh, as a key pressed at the panel; whether key lock swallows it | Done 2026-09-30 (findings §18), with `scripts/probe_panel.py` and the owner at the panel. It acts, by the first read after the reply (within about 0.1 s). It never shows in 00BDh. Key lock swallows it, and the analyzer is then silent for about 2 s. 42002 on a wait step leaves the flag set. The cursor wraps round. The backlight was not tested |
| 43 | A remote zero and span of O2 with `fuji-calibrate` | Done 2026-09-30 (findings §19), the owner at the gases. A zero on N2 (0.05 → 0.00 vol%) and a span on air (20.89 → 20.95 vol%) each completed, and a zero answered no was cancelled with nothing run. Every key was taken by the first read after it; each cleanup found the panel clean; all 162 settings read as before |
| 34 | Setting writes and commands on the analyzer | Done 2026-09-29 (findings §13). The current range lags a verified range write by tens of milliseconds (findings §13.2), so a range write now waits for it (#70); with that, 10 of 10 stateful tests pass (findings §13.8). A menu at the panel refuses writes, and return to measurement closes it. Every setting matched the saved ones at the end |
| 44 | What a response time of 0 does, and whether it differs from 1 s | Done 2026-10-01 (findings §20), with `scripts/probe_response.py`, the gas at rest. The analyzer takes 0 on its live components, and 0 switches the filter off: A/D values No. 0 and No. 1 then equal the unsmoothed counts at 046Ah–0471h, which never follow the setting. The filter is a moving average as long as the setting: a 0 s step averaged over 15 s matches the 15 s step within 5 counts of 2,724, and 1 s averages over 1.0 s. O2 has no count before the filter, and its reading follows its count. On a change between air and nitrogen the gas path was most of the response: about 9 s before the count moved and 6 s from 10 to 90 % with the filter off, a tenth of a second more at 1 s, and 13 s at 15 s. CO2 and CO have no gas connected, and their readings did not move a step |

Still open:

4. FC06 against 009Eh–00A3h. *Not needed: fujilib uses FC10 there (§6.3).*
8. Whether an O2-average channel exists, and at which index. *Needs a unit with the O2
   correction option.*
11. Calibration-log depth. *Needs firmware 2.24 or later.*
12. ~~Whether key lock (40074) blocks Modbus writes or commands~~: it does not block
    writes (table above).
13. Whether "Ch n" alarm registers are indexed by alarm number, as assumed in §2.6.
    *A unit with the alarm option; the alarms are read-only until then.*
14. ~~Whether settings survive a power cycle without an explicit save~~: they do
    (table above).
15. ~~Whether `COM10` and above need the `\\.\` prefix~~ — `anyserial` already
    normalizes it; test canonical port identity instead (§4.1).
18. Whether the undocumented holding blocks accept writes. **Will not be tested.**
21. Modbus O2 against the analog output, simultaneously. *Phase 4, needs analog wiring.*
22. The Premus variant and specification. *Owner, from Hummingbird or Fuji.*
23. The analyzer's current calibration state (§2.11). *Owner.*
25. Linux timing. *If the rig runs Linux.*
26. The encoding of the schedule start hour and minute (§2.6). *Not answerable on the
    bench unit:* its panel has no auto-calibration menu without the option (findings
    §13.7), and the registers read hour `0x000C`, minute 0. *A unit with the
    auto-calibration or auto-zero option: compare its panel with the registers.*
27. The encoding of the alarm target channel (§2.6). *A unit with the alarm option; the
    alarms are read-only until then.*
32. ~~Whether the analyzer refuses, clamps or stores a value outside a setting's
    documented range~~: it stores it (table above).
33. What auto calibration and auto zero calibration do on an analyzer without the
    valve-drive option, and whether 30049 covers the hold extension. *Never on the bench
    unit; a unit with the option and plumbed gases.*
36. A manual calibration that fails: the error display (step 10), what 00B9h and the
    flags show, and whether ENT forces it (§2.8). *Needs a real calibration that
    fails; not in the key prototype. The owner's call.*
37. Which ranges a manual zero of the "at once" pair changes, and a channel set to
    "both": its flags and ranges. *Needs a real calibration.* ENT on the pair's
    position sets the zero flags of Ch1 and Ch2, not those of the absent Ch4 and Ch5
    (findings §18.5).
38. Output hold during a manual calibration: whether the readings and the A/D values
    freeze (ZPA p.64 says the readings do). *Prepared as `scripts/probe_panel.py hold`;
    not run on 2026-09-30 (#86).*
39. ~~Whether a key written to 42001 acts, and shows in 00BDh, as a key pressed at the
    panel; whether key lock swallows it~~: it acts, never shows in 00BDh, and key lock
    swallows it (table above). Whether the backlight swallows it is still open.
    *Prepared as `scripts/probe_panel.py backlight`.*
41. What 00B7h, 00B8h, 00BAh, 00BBh, 00BFh–00C1h and 0472h–0478h mean, why each
    detector count at 046Ah–0471h appears twice, and the rest of the factory blocks
    (findings §15.7). *No plan: nothing depends on them.*
42. Why ZERO sometimes opens with the cursor on Ch1: after a 42002, and after a long
    pause (findings §18.2). *No plan: the key driver reads the cursor instead.*
43. ~~A remote zero and span of O2 with `fuji-calibrate`~~: done (table above).
    *Still open:* the settling of a gas after the valves change, recorded by the
    steadiness rule, from a run started before the gas is switched (#78).
29. ~~Trio with a real Windows COM port (§4.7 item 14)~~ — fixed in `anyserial` 0.2.0;
    the hardware tests pass on trio on the bench (findings §10.4). *Linux and macOS
    untested.*

### 13.3 The bench analyzer

| Field | Value | Source |
|---|---|---|
| Brand | California Analytical Instruments (CAI), now part of ENVEA: a CAI-branded ZPA | owner, 2026-10-01 |
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
| Response time | 1 s on every channel since 2026-10-01; 15 s, the factory setting, on 2026-09-29 | bench |
| Panel user-mode menu | Switch Ranges, Calibration Parameters, Parameter Setting; no alarm, peak-alarm, auto-calibration or auto-zero menu | owner, 2026-09-29 |
| Calibration state | doubtful: O2 20.29 vol%, CO2 −0.11 vol% at capture; error log full of calibration errors 5, 6, 7. O2 zeroed and spanned at the panel on 2026-09-29 (findings §14, §15.6) | bench |
| Factory "other parameters" | zero limit off, **range limit off** (readings not held at 110 %FS), 4 analog outputs, English, cylinder zero gas, Modbus, varied range on, no DIO | owner, factory screen; 0C2Dh–0C34h (findings §15.3) |
| Panel calibration log | present on 1.02 in maintenance mode, but not over Modbus | owner, 2026-09-29 (findings §15.6) |
| Factory options | alarm 0, auto calibration off, zero check off: hence no alarm, auto-calibration or auto-zero menu | owner, factory screen (findings §17.2) |
| O2 range 2 (0–25 vol%) | looks never calibrated (zero coefficient 0, span 10.00000); range 1 is in use. Not to be selected before it is zeroed and spanned | owner, factory screen (findings §17.3) |

The predictions made from the nameplate held: the channel layout, ranges that differ
from the nameplate, and firmware older than 2.24. What "ZPA3" denotes is still unknown;
it is not in the manuals and the registers report `ZPA`.

**Firmware.** Version 1.02 is older than 2.24, which costs this unit two things over
Modbus: the calibration log (the panel has one) and type-code digits 27–29. Neither is
needed for measurement, status, settings or commands. No update procedure is documented
(§1, non-goals), so the library is designed to work fully on this firmware rather than
to depend on an upgrade.

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
5. ~~**Automated calibration.** Is it needed, and is the gas system plumbed for auto
   calibration?~~ **Answered 2026-09-29 (#71):** zero and span are wanted from the
   host, with the valves switched by hand, as manual calibrations driven by keys (Phase
   7C). The bench unit's type code lists no valve-drive option (§2.6), so fujilib still
   refuses auto calibration there unless the option is asserted.
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
| ENVEA, [CAI ZPA data sheet](https://envea.global/design/pdf/cai/ZPA_DATA_SHEET_321.pdf) and the [CAI ENVEA Group announcement](https://envea.global/news/new-group-formed-to-enhance-customer-service-in-environmental-management-solutions-and-multi-gas-sensing-technologies/) | the ZPA as sold by California Analytical Instruments; ENVEA bought CAI in May 2023 |
| Yokogawa, [IM 11G02Q02-51EN](https://web-material3.yokogawa.com/IM11G02Q02-51EN.pdf), *IR202 Communication Functions (MODBUS)* (3rd ed., July 2022) | read on 2026-10-01: 38400 8-N-1 and the register ranges and worked examples of TN5A1190 (40001–40172, 30001–30194, 31062–31130, 31147–31149, 34097–35896, 42001–42005). The IR202 is named in the README as untested |
| Teledyne, [7500 / 7600 communication manual](https://www.teledyne-ai.com/en-us/Products_/Documents/Manuals/man_75007600comm.pdf) | read on 2026-10-01: an older, smaller map (40001–40110, 30001–30190, 31062–31096, 42001–42002) at 9600 baud over RS-232. Out of scope, and not named as supported |

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
