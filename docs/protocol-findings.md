---
description: What the bench Fuji ZPA analyzer actually does on the wire, measured read-only on 2026-09-28, and where it differs from the MODBUS manual.
---

# Protocol findings — bench ZPA, 2026-09-28

Measured on the bench analyzer with the read-only probes in `scripts/`
(`probe_connect.py`, `probe_map.py`, `probe_scan.py`, `probe_link.py`). Only Modbus
*read* function codes were sent; no register was written and no command was issued.

This document records what was **observed**. The manual is INZ-TN5A1190a-E unless noted.
Addresses are relative (on-the-wire) hexadecimal. Raw results are in `probe_out/`
(git-ignored); the full register capture is
`tests/fixtures/captures/zpa_bench_20260928.json`, which is also kept local for now
because it carries the analyzer's serial number and factory calibration tables
(design §13.1 #11).

## 1. Test setup

| Item | Value |
|---|---|
| Analyzer | Fuji ZPA, nameplate serial N8A0259, manufactured 2018-02 |
| Program version | 1.02, as shown on the display at power-on (reported by the owner) |
| Components | CO2, CO (NDIR) and O2 (Hummingbird Premus paramagnetic cell, per the owner; probably an upgrade) |
| Interface | RS-485, two-wire |
| Adapter | DTECH USB–RS-485, FTDI FT232R, enumerated as `COM8` |
| Framing | 38400 8-N-1 |
| Station No. | 1 |
| Host stack | `anyserial` 0.1.2 + `anymodbus` 0.2.0, Python 3.14, Windows 11 |

The machine also has a B&B / Advantech 485USBTB-2W on `COM6`; nothing is connected to
it that answers Modbus at any framing tried.

## 2. Identity

| Register | Contents |
|---|---|
| 0448h–0461h, type code digits 1–26 | `ZPACBJY1MPFYYYYYY2DEYAYAY0` |
| 0462h–0469h, "board" digits 1–8 | `N8A0259T` |
| 047Ah–047Ch, type code digits 27–29 | exception 02 |

- Each register holds one ASCII character in its low byte, high byte zero.
- **The "board" code is the serial number.** Its first seven characters match the
  nameplate serial. The manual does not say so.
- Digits 27–29 and the calibration log (1000h–1707h) are absent, as the manual says
  they are before version 2.24. This unit is on 1.02.
- The program version is not in any register found. It is shown on the display at
  power-on only.

Everything in this document was measured on this one unit and this one firmware
version. It should not be assumed of other versions or of ZPB / ZPG.

### The type code does not match the current manual's table

Decoded against the code table in the instruction manual (4th edition, which prints
revision code `2` at digit 8; this unit has `1`):

| Digit | Value | Current table says | Agrees with the analyzer? |
|---|---|---|---|
| 4 | `C` | only `A` and `D` are listed | not in table |
| 5 | `B` | 19-inch rack | yes |
| 6 | `J` | CO2 + CO | **yes** — Ch1 and Ch2 |
| 7 | `Y` | no O2 sensor | **no** — Ch3 measures O2 |
| 8 | `1` | revision code | — |
| 9–11 | `M P F` | 0–10 vol%, 0–50 vol%, 0–1000 ppm | only digit 9 matches (CO2 0–10 vol%) |
| 18 | `2` | NPT 1/4 | plausible |
| 19 | `D` | 4–20 mA + communication | yes |
| 20 | `E` | English, 125 V cord | plausible |
| 21 | `Y` | no O2 correction | yes — no corrected channels |
| 24 | `A` | ppm / vol% | yes |
| 25, 26 | `Y`, `0` | not listed | not in table |

**Consequence:** the type code cannot be trusted to give the channel layout. Digit 6 was
right; digit 7 was wrong. Range, unit and population must come from the range registers
and the live readings, not from the code.

## 3. Channels and ranges

| Ch | Gas | Ranges | Range 1 | Range 2 | Decimals | Reading at capture |
|---|---|---|---|---|---|---|
| 1 | CO2 | 1 | 10.00 vol% | — | 2 | −0.11 vol% |
| 2 | CO | 1 | 1.000 vol% | — | 3 | −0.009 vol% |
| 3 | O2 | 2 | 21.00 vol% | 25.00 vol% | 2 | 20.29 vol% |
| 4–12 | unused | | | | | concentration 0, decimals 0, unit 0 |

- Negative concentrations are two's complement (`FFF5h` = −11).
- **O2 is transmitted with two decimals**, so the Modbus value is quantized to
  0.01 vol% (100 ppm). CO is quantized to 0.001 vol% (10 ppm), CO2 to 0.01 vol%.
- Unused channels return an all-zero triple. Their range registers are **not** empty
  (Ch4 and Ch5 report two ranges with plausible values), so the range-count register is
  not a test for whether a channel exists.
- The configured O2 range (0–21 / 0–25 vol%) differs from the nameplate (0–10 %).

## 4. The readable map

Found by reading every address 0000h–1FFFh one word at a time with each function code,
then sampling 2000h–FFFFh every 256 addresses and every multiple of 1000.

| FC | Readable | Manual says |
|---|---|---|
| 04 | 0000h–00C1h | same |
| 04 | **03E8h–0479h** | 0425h–0469h |
| 03 | 0000h–00ABh | same |
| 03 | **03E8h–069Bh** | not mentioned |
| 03 | **0BB8h–0C66h** | not mentioned |

Blocks start at decimal 0, 1000 and 3000; the write-only commands are at 2000.

The one-word scans also recorded 43 addresses (23 FC04, 20 FC03) whose reply was
malformed rather than an exception: "function code 0", "exception echoes fc 0x20",
wrong function codes. A follow-up read-only session re-read exactly those addresses,
three times each, with a 5 ms busy-wait gap measured from each reply. All 129 reads
returned exception 02. The malformed replies were link corruption from the timing
defect in §6. The readable map above stands.
FC03 and FC04 address **separate tables**, including at 03E8h and above.

### 4.1 Undocumented input registers (FC04)

| Address | Contents | Evidence |
|---|---|---|
| 03E8h–03EEh | **real-time clock**, BCD: year, month, day, day of week, hour, minute, second | read `26 09 28 01 11 51 39` at 11:58:07 PC time on Monday 2026-09-28; advanced correctly between reads; about 6.5 minutes behind the PC |
| 03EFh–0418h | **21 A/D conversion values**, long words, low word first | values match the service manual's A/D table: the reference voltage (No. 15) read 38929, inside its stated 35,000–80,000 window; IR inputs No. 0 and No. 1 read about 65,700 and 69,700 |
| 0419h–0424h | zero | |
| 046Ah–0471h | four long words close to the IR input counts | purpose unknown |

The A/D values were live: two reads minutes apart differed by a few counts.

### 4.2 Undocumented holding registers (FC03)

| Address | Contents |
|---|---|
| 009Eh–00A3h | reference-gas and averaging settings (documented for ZPB / ZPG) |
| 00A4h–00ABh | four long words, each 1,000,000 — the interference compensation coefficients, of which the manual lists only the first |
| 03E8h–069Bh | factory data: tables of breakpoints (300, 600, 1000 … 20000), long-word coefficients near 100,000 and word coefficients near 10,000 |
| 0BB8h–0C66h | factory configuration, including a copy of the ranges and of the type code and serial |

**These two factory blocks must never be written.** They hold the linearization and
calibration data. Whether the analyzer would accept a write there was not tested and
will not be.

### 4.3 A coherent block capture (2026-09-28)

The register capture was read one word at a time over several minutes, so its live
words do not come from one moment. A second read-only capture read the same addresses in
block reads:

- `probe_map.py` with `anymodbus` 0.2.1, `anyserial` 0.1.2, `anyio` 4.15.1, Python
  3.13.13 on Windows 11; timeout 0.5 s, inter-frame idle 5 ms, 2 retries, blocks of up
  to 64 words.
- Every documented region and the clock and A/D block (03E8h–0418h): 312 input and 172
  holding words, the same addresses as the committed bank. Ten block reads, all
  successful, plus the read of type-code digits 27–29, which answered exception 02 as
  before; 0.63 s in all, from 18:52:38 UTC.
- Saved as `tests/fixtures/captures/zpa_bench_block_20260928.json` (kept local, like the
  register capture), SHA-256 `7c97590745e658ee2bdfd8a271605325fe5a8ae894dc86b5601e48b795bbeddd`;
  the same file as `probe_out/probe_map_20260928T185238Z.json`. Its `input` and `holding`
  tables have the capture's shape, so `fuji-decode --dump` reads it.

Compared with the register capture, three hours earlier:

| Words | Result |
|---|---|
| FC03 0000h–00ABh, all 172 | identical |
| FC04 0425h–0469h: ranges, type code, serial | identical |
| FC04 0000h–00C1h except the six words below; error log included | identical |
| Readings 0000h, 0003h, 0006h | CO2 −0.11 → −0.10 vol%, CO −0.009 → −0.007 vol%, O2 20.30 → 20.18 vol% |
| Display-state words 00B9h, 00BCh, 00BDh | 6 → 0, 2 → 0, 0 → 16 |
| Clock 03ECh–03EEh | read 14:46:12 at 14:52:39 PC time: still about 6.5 minutes behind |
| A/D 03EFh–0418h | 19 of the 21 low words moved, by 1 to 211 counts; every high word identical; reference voltage (No. 15) 38928 |

Everything that is not a live value matched, so the register capture's settings,
ranges and identity stand. The block capture is the coherent one: its readings, status,
clock and A/D values come from the same 0.63 s.

## 5. Replies to requests the analyzer does not support

| Request | Reply | Manual |
|---|---|---|
| FC01, FC02 | exception **02** | implies 01 (illegal function) |
| address outside the map | exception 02 | same |
| 65 words | exception 03 | same |
| block crossing the end of a region | exception **03** | — |
| FC03 read of a command register (07D0h) | exception 02 | — |

A CRC-valid exception of any kind therefore means "a station is present".

## 6. Link timing

### 6.1 Corrected measurement (follow-up session, 2026-09-28)

`anymodbus` inter-frame idle was set to 0 and retries to 0. Each gap was enforced by a
`perf_counter` busy-wait from the moment the previous reply was returned, and the
achieved minimum gap matched the target to within 0.01 ms. There were 500 requests per
row, FC04, one word each. 0000h is a valid register; 00C2h is one past the map and draws
exception 02.

| Sequence | Gap after the reply | Failed |
|---|---|---|
| normal → normal | 0 ms | 7 (3 silent, 4 frame errors) |
| normal → normal | 1, 2, 3, 5 ms | 0 each |
| exception → exception | 0 ms | 5 silent |
| exception → exception | 1, 2, 3, 5 ms | 0 each |
| exception → normal, alternating | 0 ms | 3 silent, all on the normal read |
| exception → normal, alternating | 2, 5 ms | 0 each |

- **The analyzer needs at most 1 ms after any reply, and an exception reply needs no
  longer than a normal one.** Only a zero gap fails, about 1 % of the time.
- These are host-side gaps. The FTDI latency timer delays the host's view of each
  reply, so the analyzer saw at least these gaps. The data cannot resolve requirements
  below about 1 ms.
- Limits of this run: gap order was fixed, not randomized; normal → exception was not
  tested; only counts were kept. The randomized re-run in §6.3 addresses all three.
- Round trip: 13–27 ms for one word, 50–63 ms for 64 words. A two-block poll therefore
  takes about 120 ms; the practical ceiling is 7–8 polls per second.
- The adapter is an FTDI chip at its default 16 ms latency timer, which sets the floor on
  these figures.

### 6.2 The first measurement, and why its conclusion was wrong

The first run, below, concluded that the analyzer needs 20–30 ms after an exception
reply. The method produced that result by itself:

1. **`anymodbus` counts the gap from the wrong moment after an exception.**
   `probe_link.py` relied on `anymodbus`'s `inter_frame_idle`, which is counted from
   `_last_io_monotonic`. That value is set when a request is sent (`bus.py:439`) and
   again only after a *successful* reply (`bus.py:475`). An exception reply raises
   before line 475, so after an exception the "gap" was counted from the request. With
   a 7–13 ms round trip, a configured 5 ms meant no gap at all. That explains the 1 %
   failures, the same rate as the normal-read 0 ms row. It also explains
   `latency_ms.min = 0.0` at 20, 50 and 100 ms in the raw file: calls finished before
   the idle they supposedly waited.
2. **Short sleeps on Windows are rounded up.** On this machine (Python 3.14.4) any
   `anyio.sleep` below about 16 ms takes about 16 ms, and 20 or 30 ms takes about 33 ms.
   The configured gaps below are not the gaps on the wire; "1.75 ms" was really about
   16 ms.

The raw counts are kept for the record.

Tight loops with no retries and nothing executed between requests.

**Valid reads**, 300 requests per row (configured `anymodbus` idle):

| Gap before request | 1 word: failed | 64 words: failed |
|---|---|---|
| 0 ms | 4 (1.3 %) | 6 (2.0 %) |
| 1.75 ms | 0 | 0 |
| 2.5 ms | 0 | 0 |
| 5 ms | 0 | 0 |
| 10, 20, 50 ms | 0 | 0 |

**Requests answered by an exception**, 500 per row (configured idle, counted from the request; see above):

| Gap before request | Failed |
|---|---|
| 5 ms | 5 (1.0 %) |
| 20 ms | 0 |
| 50 ms | 0 |
| 100 ms | 0 |

These rows are superseded by §6.1.

### 6.3 Randomized re-run, and `anymodbus` 0.2.1 on the analyzer (2026-09-28)

`probe_link.py --mode pairs`. Each trial is two one-word FC04 reads, each either a
normal read (0000h) or a read that draws exception 02 (00C2h), so all four pairings are
covered. Trials ran in randomized order, with 10 ms of idle before each trial. The gap is
measured from the moment the first read returned to the moment `anymodbus` logged the
second frame for sending. No retries; request timeout 0.3 s. Every trial is kept in the
raw file.

**What the analyzer needs.** `anymodbus` idle 0, gap busy-waited. 250 trials per cell;
failures are all silent (no reply within the timeout):

| Gap after the reply | normal → normal | normal → exception | exception → normal | exception → exception |
|---|---|---|---|---|
| 0 ms | 0 | 5 | 2 | 1 |
| 1 ms | 0 | 0 | 0 | 0 |
| 2 ms | 0 | 0 | 0 | 1 |
| 5 ms | 0 | 0 | 0 | 0 |

- **This confirms §6.1, now including normal → exception.** Only a zero gap fails
  consistently: 8 of 1,000 (0.8 %), in three of the four pairings. A gap of 1 ms had no
  failures in 1,000 trials, whichever kind of reply came first.
- **The one failure at 2 ms looks like background loss, not a gap requirement.** Its
  measured gap was 2.08 ms, the neighbouring trials succeeded, and 1 ms and 5 ms had no
  failures in 2,000 trials. It suggests a loss rate of about 1 in 3,000 requests, which
  read retries absorb. This run cannot rule out a very small rate specific to 2 ms.
- Measured gaps were accurate: the medians were 0.08 ms above target at every gap.
- Round trip, from the frame being sent to the reply being returned, one word:
  11.8–26.3 ms, median 13.9 ms.

**What `anymodbus` delivers.** The probe added no wait, leaving the gap to `anymodbus`'s
own `inter_frame_idle` of 5 ms (fujilib's default). 250 trials per pairing:

| `anymodbus` | Gap after a normal reply | Gap after an exception reply | Failed |
|---|---|---|---|
| 0.2.0 | 5.09–22.4 ms | **0.05–0.22 ms** | 2 of 500 after an exception |
| 0.2.1 | 5.49–20.2 ms | 5.22–27.5 ms | 0 of 1,000 |

- **0.2.0 reproduces the defect of §6.2 on the analyzer.** After an exception reply the
  configured 5 ms collapsed to about 0.1 ms, and failures appeared at the zero-gap rate.
- **0.2.1 holds the gap after every reply.** The medians of about 12 ms come from the
  Windows timer (§6.2), not from the setting.
- fujilib's default of 5 ms leaves a wide margin over the 1 ms the analyzer needs.

Raw files, in `probe_out/` (git-ignored), each recording the probe's arguments, the
package versions and the SHA-256 of the probe scripts:

| File | Run | Seed |
|---|---|---|
| `probe_link_pairs_busywait_20260928T174956Z.json` | busy-wait, `anymodbus` 0.2.1 | 3120660651 |
| `probe_link_pairs_anymodbus_20260928T175306Z.json` | `anymodbus` 0.2.1 idle | 3025024686 |
| `probe_link_pairs_anymodbus_20260928T175411Z.json` | `anymodbus` 0.2.0 idle | 2979417567 |

## 7. Status at capture

- No instrument error, no calibration error, no alarms, no calibration running, no hold.
- Key lock off, output hold off, auto calibration off, auto zero off.
- All range-switch methods manual; every channel on range 1.
- Response time is 15 s on all channels.
- **The error log is full** (14 of 14 entries), all calibration errors: No. 5 (zero
  calibration amount over 50 %FS), No. 6 (span outside the allowable range) and No. 7
  (span calibration amount over 50 %FS), on channels 1, 2 and 3. The newest is error 6 on
  channel 1. The log stores day, hour and minute only.

## 8. What this changes

| Topic | Change |
|---|---|
| Region map | per-profile and as measured, with the manual's narrower map as the documented subset |
| Writes | allowed only to a whitelist of documented user settings and commands; the factory blocks are unreachable by construction |
| Timing | inter-frame gap 5 ms (the manual's recommendation), measured from the end of every reply; **no** special recovery after an exception (§6.1, §6.3); `anymodbus` ≥ 0.2.1 records the end of every transaction, verified on the analyzer (§6.3) |
| Channel layout | from live data and range registers; the type code is a hint, labelled as such |
| Serial number | read from the "board" code |
| Clock | exposed; used to complete the partial timestamps in the error log |
| Firmware | the calibration log and type digits 27–29 are probed, and absent here |
| Discovery | any CRC-valid reply, including an exception, identifies a station |

## 9. Not tested

- Any other analyzer or firmware version.
- Any write: settings, commands, key simulation.
- Whether key lock affects Modbus writes.
- Persistence of settings across a power cycle.
- Addresses 2000h–FFFFh at full resolution (sampled only).
- Multi-drop with more than one station.
- A comparison of the Modbus O2 value against the analog output.
- Normal → exception gap pairs, randomized gap order, and Linux timing.
