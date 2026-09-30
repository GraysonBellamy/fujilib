---
description: What the bench Fuji ZPA analyzer actually does on the wire, measured read-only from 2026-09-28, with writes on 2026-09-29 and with front-panel keys on 2026-09-30, and where it differs from the MODBUS manual.
---

# Protocol findings — bench ZPA, 2026-09-28

Measured on the bench analyzer with the read-only probes in `scripts/`
(`probe_connect.py`, `probe_map.py`, `probe_scan.py`, `probe_link.py`, and, for
fujilib's own client, `probe_client.py`, §10). Through §12 only Modbus *read* function
codes were sent; no register was written and no command was issued. §13 records the
first writes and commands, made in a session the owner authorized and attended. §14
records a calibration the owner made at the front panel, watched read-only, and §15 a
read-only session on what the registers still left unexplained. §16 is fujilib's own
watch of a panel calibration, and §17 what the factory-mode screens showed. §18
records the first front-panel keys written over Modbus, in a session the owner
authorized and attended.

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
| 046Ah–0471h | four long words close to the IR input counts | the unsmoothed CO2 and CO detector counts, each twice (§15.4) |
| 0472h–0479h | zero, but 0472h–0478h read 1 now and then | purpose unknown (§15.4) |

The A/D values were live: two reads minutes apart differed by a few counts.

### 4.2 Undocumented holding registers (FC03)

| Address | Contents |
|---|---|
| 009Eh–00A3h | reference-gas and averaging settings (documented for ZPB / ZPG) |
| 00A4h–00ABh | four long words, each 1,000,000 — the interference compensation coefficients, of which the manual lists only the first |
| 03E8h–069Bh | factory data: tables of breakpoints (300, 600, 1000 … 20000), long-word coefficients near 100,000 and word coefficients near 10,000 |
| 0BB8h–0C66h | factory configuration, including a copy of the ranges and of the type code and serial, and the factory menu's "other parameters" at 0C2Dh–0C34h (§15.3) |

Neither block holds the zero and span calibration coefficients (§15.3).

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
| FC07, FC08/0000h, FC0B, FC0C, FC11, FC14, FC18, FC2B/0Eh | exception **01** (§15.1) | — |

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
- Any write: settings, commands, key simulation. (Setting writes and return to
  measurement later; see §13. Key simulation, calibration and blowback are never sent;
  a calibration made at the panel was watched later, see §14.)
- Whether key lock affects Modbus writes. (Later; see §13.3.)
- Persistence of settings across a power cycle. (Later; see §13.5.)
- Addresses 2000h–FFFFh at full resolution (sampled only).
- Function codes other than 01–04, 06 and 10h. (Later: eight read-only diagnostic and
  identification codes answer exception 01; see §15.1.)
- Multi-drop with more than one station.
- A comparison of the Modbus O2 value against the analog output.
- Linux timing. (Normal → exception gap pairs and randomized gap order were measured
  later; see §6.3.)

## 10. fujilib's own client on the bench (2026-09-28, evening)

fujilib's transport, Modbus port, client and read procedures (commit `8c04481`), run
against the same analyzer on `COM8`, station 1. They used the library defaults: 0.5 s
request timeout, 5 ms inter-frame idle, 50 ms startup settle, 2 read retries and a
0.1 s quiet window. `anymodbus` 0.2.1, `anyserial` 0.1.2, `anyio` 4.15.1, Python 3.13,
Windows 11.

- **The probe:** `scripts/probe_client.py`, read-only by construction (the read
  procedures get a client whose write methods raise).
- **The tests:** `tests/hardware/test_hardware_client.py`, run with
  `FUJILIB_ENABLE_HARDWARE_TESTS=1 FUJILIB_HARDWARE_PORT=COM8 uv run pytest -m hardware tests/hardware`.

### 10.1 Every read procedure

`--mode smoke` ran every read procedure once.

- **Identify.** Type code and serial as in §2; channels 1–3 present.
- **Probes.** The clock and the A/D values are supported; type-code digits 27–29 and the
  calibration log are unsupported (exception 02, as in §5).
- **Poll.** Two blocks, 48 ms each. Every reading was in state "ok": CO2 −0.10, CO
  −0.006, O2 20.17 vol%. No hold and no errors.
- **Metadata.** Response times 15 s on every channel.
- **Error log.** 14 entries, as in §7.
- **Clock.** Still about 6.5 minutes behind the host.
- **A/D.** Reference voltage 38927.
- **Counters.** 27 requests, no retries; the only failed attempts were the 3 expected
  exception replies.

The eight hardware tests passed under asyncio.

### 10.2 Sustained polls

`--mode polls --count 300`: 300 full polls back to back.

| Quantity | min | median | p95 | max |
|---|---|---|---|---|
| block 1 (`0000h+61`) round trip, ms | 46.5 | 48.9 | 51.1 | 54.3 |
| block 2 (`0083h+60`) round trip, ms | 46.0 | 48.3 | 50.7 | 53.9 |
| block 1 reply → block 2 request, ms | 5.1 | 16.9 | 20.3 | 22.8 |
| whole poll, ms | 110.9 | 130.9 | 136.8 | 140.9 |

- **No failures.** 603 requests, no retries, no failed attempt.
- **7.78 polls per second**, within the 7–8 Hz ceiling estimated in design §2.4.
- **The idle holds.** The 5 ms idle was never shortened. Its median of 17 ms is the
  Windows timer.
- **Timing is honest.** Each block's round trip excludes that wait: the client waits out
  the gap before it timestamps the request (design §4.2).

### 10.3 A reply still on the wire after its read is cancelled

`--mode resync`, 30 trials per condition. Each trial:

1. reads block B (`0083h+36`, the error and hold flags) as a reference;
2. idles 40 ms;
3. reads block A (`0000h+36`, the readings; about 33 ms round trip) and cancels it
   after 15 or 30 ms, so A has been sent and its reply is on its way;
4. reads B again at once and compares.

In all 120 trials A reached the wire before it was cancelled.

| Cancel A after | Quiet window | B right first time | B lost (timed out, the retry recovered it) | Wrong data accepted | Cancel → B done, median |
|---|---|---|---|---|---|
| 15 ms | 0 | 7 | **23** | 0 | 571 ms (lost) / 55 ms (first time) |
| 15 ms | 0.1 s | 30 | 0 | 0 | 136 ms |
| 30 ms | 0 | 30 | 0 | 0 | 53 ms |
| 30 ms | 0.1 s | 30 | 0 | 0 | 138 ms |

- **The hazard is real on this line**, but it takes a different form from the one the
  simulator reproduces. Cancelled 15 ms in, A's reply is still arriving when B's request
  goes out on the half-duplex line. In 23 of 30 trials the analyzer never answered B:
  the request collided with A's reply, and B cost a 0.5 s timeout and a retry.
- **Stale data was never accepted.** In no trial was A's late reply accepted as B's. The
  hazard the design's §4.2 describes (a same-length reply to another request) remains
  possible in principle, but was not observed with this adapter.
- **When the reply has landed, the input reset clears it.** Cancelled 30 ms in, A's reply
  has almost entirely arrived by the time B is sent, so the reset clears it and B
  succeeds even with no window.
- **With the default 0.1 s window every trial succeeded at once.** A cancellation or
  timeout then costs about 80 ms instead of a possible 0.5 s timeout.
- **An invalid first run.** Its cancellations landed while the client was still waiting
  out the inter-frame gap, before anything was sent, so it tested nothing. It is kept
  (`probe_client_resync_20260928T203149Z.json`) but not counted. The probe now idles
  before A.

### 10.4 Trio could not read a real COM port on Windows (fixed in `anyserial` 0.2.0)

With `anyserial` 0.1.2, every hardware test under trio failed within about 4 ms of its
first read, with `anyserial.SerialError: [WinError 1460] This operation returned because
the timeout period expired`. A plain idle `receive()` on `COM8`, with nothing sent,
reproduced it: under asyncio it waited and was cancelled cleanly; under trio it raised.

- **Cause.** `anyserial` reads with the "wait-for-any" `COMMTIMEOUTS` policy, under
  which an overlapped read with no data completes after about 1 ms with
  `STATUS_TIMEOUT`. That is a success status: asyncio's Proactor returns it as 0 bytes,
  and `anyserial` reissues the read. Trio raised it as an error, and `anyserial` treated
  it as a failed port.
- **Why CI did not catch it.** The simulator's port pair does not take this path, which
  is why the unit tests pass on trio.
- **Until `anyserial` 0.2.0**, fujilib on Windows with a real port had to run on asyncio,
  and the hardware tests marked trio on Windows as a strict expected failure (design
  §4.7 item 14).
- **The fix.** `anyserial` 0.2.0's trio read path treats that `STATUS_TIMEOUT` as the
  empty completion asyncio reports, and reissues the read. With the expected-failure
  mark removed, the eight hardware tests pass on `COM8` under both asyncio and trio (16
  of 16), run twice the same evening: once with `anymodbus` 0.2.1 and once with 0.3.0
  (§10.5). `anyserial` 0.2.0, `anyio` 4.15.1, `trio` 0.34.0, Python 3.13, Windows 11.

Raw files, in `probe_out/` (git-ignored):

| File | Run | `probe_client.py` SHA-256 |
|---|---|---|
| `probe_client_smoke_20260928T203048Z.json` | smoke | `2475306b…` |
| `probe_client_polls_20260928T203100Z.json` | 300 polls | `2475306b…` |
| `probe_client_resync_20260928T203149Z.json` | resync, invalid (cancelled before sending) | `2475306b…` |
| `probe_client_resync_20260928T203243Z.json` | resync, cancel after 15 ms | `694951e7…` |
| `probe_client_resync_20260928T203354Z.json` | resync, cancel after 30 ms | `694951e7…` |

### 10.5 The same checks on `anymodbus` 0.3.0

After fujilib moved to `anymodbus` 0.3.0 (design §4.7), the checks of §10.1–§10.3 were
run again, read-only, the same evening. `anymodbus` now waits the gap, retries, checks
each reply and keeps the late-reply window, and fujilib takes the timing and counters
from its per-attempt reports.

- **Every read procedure.** The results are identical to §10.1, and so are the counters:
  27 requests, and 3 exception replies, all expected. The eight hardware tests pass
  under asyncio.
- **Sustained polls.** 300 polls, no failures, 7.86 polls per second.
  - Block round trips: medians 48.1 and 47.5 ms, maxima 51.8 and 52.2 ms.
  - Reply → next request: at least 5.5 ms, median 16.8 ms.
  - Each block's round trip still excludes the wait: its request time is `anymodbus`'s
    report of when the request had been sent.
- **Late replies**, cancelled after 15 ms, 30 trials per window:

| Quiet window | B right first time | B lost, the retry recovered it | Wrong data accepted |
|---|---|---|---|
| 0 | 5 | **25** (24 timeouts, 1 reply that did not answer the request) | 0 |
| 0.1 s (`late_reply_window`) | 30 | 0 | 0 |

The one mismatched reply is new. Since 0.3.0 `anymodbus` checks every reply against its
request, so it was rejected and retried rather than returned.

Raw files: `probe_client_smoke_20260928T215219Z.json`,
`probe_client_polls_20260928T215234Z.json` and
`probe_client_resync_20260928T215313Z.json`, all with `probe_client.py` `694951e7…`.

## 11. The analyzer facade on the bench (2026-09-28, night)

The `Analyzer` facade, `open_device`, discovery, the blocking facade and the `fuji-*`
commands, read-only, on `COM8`, station 1, with the library defaults. `anymodbus`
0.3.0, `anyserial` 0.2.0, `anyio` 4.15.1, `trio` 0.34.0, Python 3.13, Windows 11.

- **The hardware tests.** `test_hardware_client.py`, `test_hardware_reads.py` and
  `test_hardware_sync.py`: 45 of 45 pass, under asyncio and trio (`hardware_sync` runs
  on its own portal's asyncio loop).
- **Opening.** `open_device` with identification takes 0.30-0.32 s (13 requests
  including the probes, no retries); `read_metadata()` 0.24-0.25 s; a poll 0.12-0.13 s.
  Opening, closing and opening the port again at once worked every time.
- **Readings.** CO2 −0.10, CO −0.006, O2 20.20 vol%, all in state "ok", with the
  asserted labels.
- **The clock** still ran about 6.5 minutes behind the host.
- **50 facade polls** took about 6.5 s, the same 7.7 polls per second as §10.2.
- **The calibration log** was refused before any request, once the probe had found it
  absent.
- **An empty station** (2) timed out after the retries, with the station and port in
  the error, and the port it had opened was closed, so the analyzer opened again at once.
- **Discovery** on `COM8`, stations 1 and 2: the analyzer at 1, identified; a timeout
  at 2.
- **The cancelled-read test of §10.3** (`test_a_cancelled_read_does_not_disturb_the_next`)
  failed once in the first full run, under trio: the read after the cancelled one needed
  one retry, and returned the right words. Run on its own 14 more times (140 trials,
  asyncio and trio) it never recurred. One retry in about 150 trials is read as the
  background loss of §6.3; stale data was never accepted.

## 12. Recording on the bench (2026-09-28, night)

The recorder, the sinks and the recording commands, read-only, on `COM8`, station 1,
with the library defaults. `anymodbus` 0.3.0, `anyserial` 0.2.0, `anyio` 4.15.1,
`pyarrow` 25.0.1, Python 3.13, Windows 11.

- **The hardware tests.** With `test_hardware_recording.py` added (a 5 Hz recording,
  `pipe()` to CSV and Parquet, `fuji-stream`, `fuji-capture` and `fuji-diag timing`),
  52 of 52 pass, under asyncio and trio.
- **A 60-second capture at 1 Hz** (`fuji-capture ... --reconnect` under
  `scripts/soak_monitor.py`): 60 polls, none late, dropped or failed, 131 requests with
  no retries. The two failed attempts are the exception replies of identification's
  probes of the capabilities firmware 1.02 lacks. Intervals between samples had a median
  of 1.003 s and a maximum of 1.015 s, and the worst start of a poll was 16 ms late: the
  Windows timer. Every row carried CH1-3 with the asserted gases, `label_source`
  "asserted" and state "ok". `scripts/check_soak.py` passed every check. The capture's
  process tree used 62-63 MB.
- **The analyzer's clock** read 21:05:12 when the host's local time was 21:11:40: still
  about 6.5 minutes behind.
- **`fuji-diag timing`**, 800 trials (50 per pairing and gap, seed 20260928), took 40 s
  and agrees with §6.3. The one failure was a timeout at a 0 ms gap (exception, then
  exception); every gap of 1 ms or more succeeded. A second read's round trip was
  2.6-19.9 ms (median 13.4). In 15 trials the busy-wait overran its gap by more than
  5 ms, up to 29 ms, when the thread was descheduled; each trial records the gap it
  actually had. Raw file `probe_out/diag_timing_20260929.json` (git-ignored).
- **The 24-hour recording** (design §12) started at 02:06 UTC on 2026-09-29:
  `fuji-capture` at 1 Hz to Parquet with `--reconnect`, under `scripts/soak_monitor.py`
  (`probe_out/soak_20260929.*`, git-ignored). Its results are in §12.1.

### 12.1 The first 24-hour attempt (2026-09-29): stopped by a kill after 10 h 17 min

**How it ended.** The recording's window was opened by a non-interactive process
with `cmd /c start`, and every process in a window opened that way inherits that
process's "ignore Ctrl-C" flag. Ctrl-C in the window therefore did nothing, and at
about 12:30 UTC the window was closed, which killed the recording. A test window
opened the same way reproduced it with the simulated analyzer: Ctrl-C was ignored,
and Ctrl-Break killed every process in the chain, leaving a 4-byte Parquet file. A
Parquet file is readable only once its footer is written, so this one was not, and
its `.meta.json` still said `recording`, with no counters.

**What was recovered.** The 37 row groups written before the kill were intact.
`scripts/recover_parquet.py` rebuilt the footer: 37,000 rows, 02:06:24 to 12:23:03
UTC, ending exactly at the last byte written. The rows after 12:23:03, which were
waiting for their row group, and the final counters are lost.

**The recovered rows** (`scripts/check_soak.py`; tick counts and error accounting
cannot be judged without counters):

| Check | Result |
|---|---|
| Readable output | 54 columns, 37,000 rows |
| Failed polls | none; no disconnect |
| Gaps | none: 37,000 polls in 36,999 s, every interval 0.92–1.08 s (median 1.0026 s) |
| Start of a poll after its slot | 0–16 ms (the 15.6 ms Windows timer), not accumulating; 16 polls over 20 ms, 4 over 50 ms, worst 87 ms |
| Round trip of the concentration block | median 48.5 ms, 99.9th percentile 53 ms, worst 0.19 s |
| Status and provenance | every row CO2 / CO / O2, `asserted`, state `ok`, valid, no hold, alarm or analyzer error |
| Process tree | 0.5 % of one core; 310–312 handles throughout |
| Resident memory | a 25 MB sawtooth, one tooth per row group written; the fitted trend after the first hour is +0.92 MB/h (+8.6 MB over 9.3 h), within the 20 MB bound |
| Shutdown | failed: killed |

The readings barely moved (CO2 −0.12 to −0.10 vol%, CO −0.010 to −0.004 vol%,
O2 20.22–20.28 vol%), as expected of an idle analyzer in the calibration state of
design §13.3. The wall clock gained 0.2 s on the monotonic clock over the run.

**Whether the memory trend is a leak is open.** The monitor logged only resident
memory, which on Windows is the working set that the system trims and refills.
It now logs private memory too, and `check_soak.py` judges that by the fitted
trend over the whole run. The rerun decides it: see §12.3.

**What changed because of it** (design §12, §13.1 #57): `soak_monitor.py` turns
Ctrl-C back on for the command it starts; the recording commands stop cleanly on
Ctrl-Break too; `fuji-capture` rewrites its `.meta.json` every minute with the
counters so far, and writes its progress line from a worker thread, so a console
that stops taking output cannot hold up the recording; `recover_parquet.py` joins
the soak tools. The same test window then stopped cleanly on Ctrl-C (in 0.1 s) and
on Ctrl-Break, with the Parquet file readable and the exit logged. Ctrl-Break still
ends `uv run` itself at once (exit code `0xC000013A`), but not the recording under it.
On the bench, a rerun started by double-clicking its `.cmd` and stopped with Ctrl-C
after 26 s ended as `stopped`, with 27 rows in a readable Parquet file and the
monitor's exit logged.

### 12.2 The unplug test (2026-09-29)

The procedure of `docs/hardware-test-day.md`, read-only, on `COM8`, with the owner
pulling the adapter's USB plug.

**With `--reconnect`** (13:07:52–13:17:51 UTC, 600 s at 1 Hz, `probe_out/unplug_20260929.csv`):
the plug was pulled twice, for about 30 s each time as the procedure asks (not
timed). The capture finished (exit 0,
state `finished`) with 600 polls, none late or dropped, 108 failed, 2 disconnects and
2 reconnects, and every tick kept its slot: intervals 0.945–1.033 s, worst start
22 ms late. The CSV has 600 rows, and its 108 error rows are the two outages:

| Outage | Polls failed | First failure | Then | Polled again |
|---|---|---|---|---|
| 1 | 53 (13:09:56.8–13:10:48.7) | `FujiConnectionError`: bus stream was closed while reading the reply | 52 refusals: the connection to COM8 failed | 13:10:49.7, 53 s after the first failure |
| 2 | 55 (13:11:43.7–13:12:37.7) | `FujiConnectionError`: `[WinError 5] Access is denied` while resetting the input buffer | 54 refusals | 13:12:38.7, 55 s after the first failure |

The first failure of a pull depends on where the poll was when the adapter went:
reading a reply, or starting a request. The port came back as `COM8`, and the
reopened analyzer passed the same-analyzer check. Each of the 492 successful rows
has the asserted labels and state `ok`. The outages outlasted the pulls by roughly
20–25 s, which fits Windows bringing the adapter back plus the wait for the next
attempt of the back-off (0.5, 1, 2, 5, 10, then every 30 s); the attempts themselves
are not logged, so this is not measured.

**Without `--reconnect`** (13:18:21–13:19:55 UTC, `probe_out/unplug_20260929_noreconnect.csv`):
at the pull the capture ended with state `failed` and the error
`FujiConnectionError: poll: [Errno 13] stream failed while resetting the input buffer:
[WinError 5] Access is denied.`. The CSV and its `.meta.json` are complete up to the
failure: 95 rows (94 polls, then the failed one), `polls` 95 and `failed_polls` 1.
Its `disconnects` was 0: the summary counted only the outages a `ReconnectPolicy`
rides out. It now counts the failure that ends a recording too (design §13.1 #48).

Both runs' `.meta.json` record fujilib as `0.1.0.dev33+g7a99f2771.d20260929`: the
editable install's version was built before the day's commits. The code was that of
`47e9dfa`.

### 12.3 The 12-hour recording (2026-09-29/30)

The long recording of design §12 Phase 5 (#58), read-only, on `COM8`: `fuji-capture COM8
--gas CH1=co2 --gas CH2=co --gas CH3=o2 --rate 1 --duration 43200 --reconnect` to
Parquet, under `scripts/soak_monitor.py --every 600`, started by double-clicking its
`.cmd`. It ran from 18:56:48 UTC on 2026-09-29 to 06:56:48 UTC on 2026-09-30 and ended
on its duration, exit 0. The code was `a16ea77` (`0.1.0.dev47+ga16ea770f`), with
`anymodbus` 0.3.0, `anyserial` 0.2.0, `anyio` 4.15.1, `pyarrow` 25.0.1, Python 3.13.13
and Windows 11; the files are `probe_out/soak_20260929c.*` (git-ignored).
`scripts/check_soak.py` passed every check:

| Check | Result |
|---|---|
| Readable output | 54 columns; 43,200 rows in 44 row groups (43 of 1,000, and the last 200 written at close); 1.9 MB |
| Clean shutdown | `finished`, no error, the final counters in the `.meta.json` |
| Tick and row counts | 43,200 polls of 43,200 ticks; none late, dropped or failed; no disconnect or reconnect |
| Error accounting | no error rows. 86,411 requests (two a poll and 11 at the start), no retries; the two failed attempts are identification's probes (§12) |
| Timing | no gaps: every interval 0.939–1.059 s (median 1.0013 s); no poll started more than 22.5 ms after its slot |
| Round trip of the concentration block | median 48.8 ms, 99.9th percentile 53.9 ms, worst 62 ms; the median of each hour 48.8–48.9 ms |
| Status and provenance | every row CO2 / CO / O2, `asserted`, state `ok`, valid, with no hold, calibration, alarm or analyzer error |
| Process | 226 s of CPU in 11.8 h (0.5 % of one core); 309–314 handles |
| Memory | private memory: a sawtooth of about 25 MB, one tooth per row group; the fitted trend after the first hour +1.47 MB/h, +15.9 MB over 10.8 h, within the 20 MB bound |

**Nine rows have a round trip shorter than a reply takes** (0.1–31 ms). `anymodbus`
stamps a request as sent when its `send` and `drain` return, and in those nine polls
the host held the task there for 20–75 ms: part or all of the reply had arrived by the
time the request was stamped. Their `requested_at` is late, their `latency_s` short,
and their `t_utc`, the midpoint, up to about 55 ms late; every row's `t_utc` still lies
between the request going out and the reply ending. §12.1 showed the same stalls from
the other side: five round trips over 100 ms (the worst 0.19 s) and none short, where
the host held the task while the reply was read. The recorder's drift, taken as a poll
starts, is not affected.

**The readings** barely moved: CO2 −0.10 and −0.09 vol%, CO −0.009 to −0.005 vol%, and
O2 20.80 vol% at the start, then 20.67–20.70 from the first hour on, above §12.1's
20.22–20.28 since the O2 zeros and spans of §14 and §16. The wall clock gained 0.24 s on the
monotonic clock, steadily, as in §12.1. The analyzer's clock was 6 min 37 s behind the
host's; it has read 6 min 27 s to 6 min 37 s behind since 2026-09-28.

**The memory trend was the Parquet sink.** The low point of each 2 hours rose
steadily, 44, 48, 52, 55, 56 and 58 MB, so the growth was real, not the sawtooth: about
400 B per poll, more than §12.1's +0.92 MB/h of resident memory. `fuji-capture`, run
in-process on the bundled register bank at about 30 polls a second for 25 minutes,
found where. Private memory, fitted after the first 5,000 polls of each run:

| Run | Private memory |
|---|---|
| A write per poll, as `pipe()` makes them at 1 Hz, with the sink as recorded | +1,459 B a poll |
| The same with Arrow's system allocator instead of mimalloc (28,000 polls) | +1,510 B a poll |
| Writes of about 30 rows, with `tracemalloc` | +521 B a poll, of which Python objects +5 B |
| A write per poll, the waiting rows kept as rows and made into one Arrow table per row group | +46 B a poll |

The simulated analyzer keeps every exchange it answers, about 0.75 KB a poll, which hid
everything else at first; it was capped at 64 exchanges for these runs. Python objects
did not grow, and Arrow's own pool never held more than 5 MB: what grew was native
memory, never given back, from making each write's row or two into its own Arrow table,
with either allocator. The sink now keeps the rows waiting for their group as rows and
makes one Arrow table per row group, and closing writes the footer even when the last
rows cannot be written (design §13.1 #87). The fixed sink itself, a write per poll for
15 minutes, grew +26 B a poll over 28,000 polls. The owner accepted this recording as the
hardware exit with the fix shown offline, not rerun on the bench.

Phase 5's hardware exit is met.

## 13. Writes on the bench (2026-09-29)

The stateful session of `docs/hardware-test-day.md`, authorized by the owner, who was
at the front panel, 15:40–16:03 UTC. It ran on `COM8`, station 1, with the library
defaults. `anymodbus` 0.3.0, `anyserial` 0.2.0, `anyio` 4.15.1, Python 3.13.13,
Windows 11. The code was `c4bf0e7` with the settings-and-commands work uncommitted
on top; the editable install reports `0.1.0.dev40+gc4bf0e720`.

Nothing started a calibration, a blowback or a key simulation. The session made
37 setting writes and 3 return-to-measurement commands, and restored every setting
it changed. The 162 settings saved first (`fuji-configure dump`,
`probe_out/settings_before_20260929.json`) all matched at the end:
`fuji-configure diff` gave `write: none`, `refused: none`, `unchanged: 162`. The raw
files named below are in `probe_out/` (git-ignored).

### 13.1 The hardware tests

- **Read-only.** 52 of 52 pass, in 91 s. The 23 skips are the uvloop variants, which
  cannot run on Windows.
- **Stateful** (`-m hardware_stateful`, asyncio): 9 of 10 pass, in 14.5 s
  (`stateful_tests_20260929.log`):
  - FC06 and FC10 wrote `hold.ch4.value` the same way;
  - the hold value, response time and span gas of channels and components the unit
    lacks (Ch5, NDIR 4) were written, verified and restored;
  - the O2 response time went 15 → 16 → 15 s, and output hold and hold mode were
    switched and restored;
  - a settings document and its baseline applied `ok`, and the diff after them was
    empty;
  - the refusals sent no request;
  - return to measurement came back `done`.

  No test's fixture found a setting left changed. The range test failed (§13.2).

### 13.2 The current range follows a range write a little later

`test_selecting_the_other_range` wrote range 2 to Ch3 (40108). The write read back
as written, so the result was verified. But the Ch3 current range (30040), read next,
still said range 1, and the assertion failed. The test's `finally` put range 1 back,
and the settings then matched the saved ones.

With the owner's approval, two follow-up probes repeated the change, with the owner
watching the panel. Each wrote through `set_range` and restored range 1:

- **A timing check** (`probe_range_timing_20260929T154510Z.json`). The owner saw
  the O2 range switch to 0–25 vol% and back. The first read after each
  `set_range` returned showed the new range. Each call took 0.28–0.30 s: the
  status, range and setting reads, the write and its read-back.
- **Three round trips with back-to-back reads** (`probe_range_lag_20260929T154606Z.json`).
  After the first switch to range 2, two reads still showed range 1, and the third
  showed range 2, 70 ms after the call returned. In the other five legs, the first
  read (about 30 ms after) already showed the new range.

So the analyzer applies a range change, but its current-range register can lag the
verified setting by some tens of milliseconds. A single read straight after the write
can see the old range. Both lags were on the first switch to range 2 of a run, but
nine legs are too few to say whether that matters.

**What changed because of it** (design §13.1 #70): a range write now returns only once
the channel's current range shows the range written, read within the read-back
budget, and raises `FujiVerificationError` if it never does. The simulator switches
the current range after a configurable lag.

### 13.3 Key lock does not stop Modbus writes (design §13.2 #12)

The owner switched key lock on at the panel, and 40074 read 1. Then
`scripts/probe_write.py key-lock` (`probe_write_key-lock_20260929T155103Z.json`):

- `write_parameter("hold.ch5.value", 37)` was acknowledged and verified: 37 read back.
- Return to measurement was acknowledged, with outcome `done`. The panel was already
  on the measurement screen, so this shows the command is not refused under key lock,
  but not that it would close a menu.
- The register was restored to 0.

Key lock guards the panel against the operator, not the settings against a program:
fujilib's writes go through while it is on.

### 13.4 A menu at the panel

The owner switched key lock off (40074 read 0) and opened a menu. The status showed
the parameter-setting screen (7).

- **`fuji-configure apply`** of a one-setting document (`hold.ch5.value` 37) was
  refused before anything was sent (`menu_apply_20260929.log`). It exited 1 with
  `status: failed`, `written: none` and the error "apply_settings refused, nothing
  was written: the front panel shows the parameter setting screen". `hold.ch5.value`
  stayed 0.
- **`return_to_measurement(confirm=True)`**, with the menu still open, was
  acknowledged in 13 ms. The status after it showed the measurement screen
  (`done`), and the owner saw the menu close
  (`probe_menu_return_20260929T155307Z.json`).

### 13.5 Settings survive a power cycle (design §13.2 #14)

`persist-write` wrote 37 to `hold.ch5.value` (it was 0), verified, at 15:53:25 UTC.
The owner switched the analyzer off, waited 10 s, switched it on and waited for the
measurement screen. `persist-check` at 15:55:27 read 37: **kept**. It then restored 0.
There was no save step, so a setting written over Modbus goes to non-volatile memory.
No manual gives that memory's write endurance
(`probe_write_persist-write_20260929T155325Z.json`,
`probe_write_persist-check_20260929T155527Z.json`).

### 13.6 Values out of range are stored (design §13.2 #32)

`out-of-range` wrote to `response_time.ndir4` (15 s) through the client, past the
library's 1–60 s limit (`probe_write_out-of-range_20260929T155844Z.json`):

| Written | Reply | Read back |
|---|---|---|
| 61 | acknowledged, no exception | 61 |
| 0 | acknowledged, no exception | 0 |

Each was restored to 15. The analyzer neither refuses nor clamps: it stores the value.
fujilib's own limits are the only guard, and the simulator, which stores a write as
sent, already behaves this way. Only this one register was tried, on a component the
unit does not have.

### 13.7 The schedule start time is not on this unit's panel (design §13.2 #26)

The owner could not find the auto-calibration settings. The user-mode menu lists only
Switch Ranges, Calibration Parameters and Parameter Setting. The ZPA manual's menu
tree (p.21) marks alarm setting and auto and auto zero calibration as optional, and
the peak-alarm menu is missing too. This agrees with the type code, which lists none
of these options (§2). The panel therefore cannot show the start time, and the
encoding stays unconfirmed. The three schedules still read day 0, hour `0x000C` and
minute 0.

### 13.8 The stateful tests again, with range writes followed (2026-09-29)

The owner authorized a rerun once a range write waited for the channel (§13.2), and
it ran at 16:52 UTC with the same code otherwise. A fresh dump
(`settings_before_20260929b.json`) came first. **10 of 10 stateful tests pass**,
in 14.8 s (`stateful_tests_20260929b.log`). The range test took 1.28 s, against
0.88 s before. The diff against the settings saved at the start of the session
showed nothing to write, with 162 unchanged.

## 14. A manual calibration at the panel (2026-09-29)

The owner calibrated the O2 channel at the front panel, 17:32–17:38 UTC, while
`scripts/probe_calibration.py watch` read the analyzer. That probe sends only read
function codes, so fujilib wrote nothing and pressed no key. The questions were:

- whether the analyzer keeps any trace of a calibration in the registers it
  answers but the manual does not document, which could stand in for the
  calibration log that firmware 1.02 lacks (design §2.11, §6.5);
- what the panel and status registers do during a manual calibration.

Setup: `COM8`, station 1, `anymodbus` 0.3.0, `anyserial` 0.2.0, `anyio` 4.15.1,
Python 3.13.13, Windows 11. The probe took a full snapshot of every readable region
(1,379 words: FC04 0000h–00C1h and 03E8h–0479h, FC03 0000h–00ABh, 03E8h–069Bh and
0BB8h–0C66h) twice before the first calibration and 20 s after each one. In between,
it read FC04 0000h+61, 0083h+60 and 03E8h+49 every 0.5 s: 661 reads, none lost
after retries. The raw files are in `probe_out/calwatch_20260929T173222Z/`
(git-ignored).

Before it, Ch3 was set to zero mode "each" and calibration range "current", so a
zero or span of Ch3 touches Ch3's range 1 only. Its range 1 calibration gases are
0.00 (zero) and 20.95 vol% (span). Ch1 and Ch2 are set to "at once". Output hold and
key lock were off.

The owner made three passes. At the panel, each is ZERO or SPAN, the cursor to the
channel, ENT to select it, and ENT again to start the calibration:

| UTC | Keys | Steps (30182) | O2 before → after |
|---|---|---|---|
| 17:32:49–17:32:54 | ZERO, DOWN, ENT, ENT | 4 → 5 → 6 → 0 | −0.12 → 0.00 vol% on zero gas |
| 17:34:38–17:34:43 | SPAN, ENT, ENT | 7 → 8 → 9 → 0 | 20.69 → 20.95 vol% on span gas |
| 17:37:39–17:37:44 | ZERO, ENT, ESC | 4 → 5 → 0 | 20.95 → 20.95: cancelled, nothing calibrated |

No calibration error followed any of them, and Ch1 and Ch2 read −0.09 and −0.006
vol% throughout.

### 14.1 Nothing in the holding registers changed

Across all three passes **no FC03 word changed**: not the 172 user settings, and
not one of the 867 words of the two undocumented factory blocks. The zero and span
are kept somewhere the Modbus map does not reach. The words that did change, outside
the live readings, clock and A/D values, are two undocumented input words (§14.2)
and the manual-calibration cursor.

### 14.2 Two undocumented display words

The manual marks 30184–30188 "do not use" and lists nothing at 30190. Two of those
words follow the panel:

- **00BDh (30190) is the key being pressed**, in the codes of the key register 42001.
  It read 64 as ZERO was pressed, 8 for DOWN, 32 for each ENT, 128 for SPAN and 16
  for ESC, each for one read only, then 0. The 16 in the capture of 2026-09-28
  (§4.3) was therefore an ESC at the panel.
- **00B9h (30186) follows the last calibration.** It went to 0 when ENT selected
  the channel (the wait step), to 4 when the calibration started, and to 6 when it
  finished, and it stayed 6 until the next channel was selected. The cancelled pass
  left it at 0. The 6 in the register capture of 2026-09-28 (§4.3), with the cursor
  on Ch3, fits an O2 calibration made at the panel before it. Its value after a
  calibration error is not known.

### 14.3 The steps and flags

- **The screen register (30181) stayed 0**, measurement, throughout, as the manual
  says for a manual calibration (TN5A1190a p.46). Only 30182 shows one.
- **The per-channel zero and span flags cover manual calibration.** The Ch3 zero flag
  (30052) came on with the wait step, or one read later, and stayed on until the
  calibration ended. The span flag (30057) did the same. The cancel cleared the zero
  flag in the same read as the step. The manual does not say whether these flags
  include manual calibration.
- **ESC from the wait step goes straight back to measurement**, not to channel
  selection.
- **The cursor (30189) kept its channel.** ZERO opened with the cursor on Ch1, one
  DOWN put it on Ch3, and the next ZERO and SPAN opened on Ch3. The "at once" pair
  Ch1 and Ch2 took a single cursor position.
- **A calibration takes one to three seconds** from the second ENT: 1.6 s to the
  measurement screen for the zero, 2.4 s for the span. The span's new value (20.94)
  showed while the step still read "running".
- **The analyzer did not answer for about a second while the zero ran.** Two
  attempts at one read went unanswered (0.5 s timeouts each); the third was
  answered, with the step back at 0. Nothing went unanswered during the span.

### 14.4 The O2 detector's raw count

A/D value No. 4, `adc.input5` (03F7h), is the O2 detector. It read 636–637 on zero
gas and 3351–3353 on span gas, and **a calibration did not change it**: the zero moved
the reading from −0.12 to 0.00 at 636 counts, and the span from 20.69 to 20.95 at
3352. So the count is the detector's raw signal, before the calibration is applied.

- That is about 130 counts per vol%, one count about 0.008 vol%, against the
  Modbus reading's step of 0.01 vol%. It is not a finer O2 measurement.
- While a gas was steady, it varied by one count, and the reading by one step at most.
- Recorded at each calibration, with the reading before it, it gives what firmware
  2.24's calibration log keeps per record (design §2.6): a detector count and the
  deviation.

When the gas changed from zero to span gas, the O2 reading rose from 1.34 to 20.07
vol% in 18 s and reached 20.68 about 36 s after the change. It then stayed within
0.01 vol% until the calibration, with the response time at 15 s.

### 14.5 Not answered here

- What the flags, 00B9h and the step do after a calibration error (step 10), and
  whether ENT there forces the calibration as the manual says.
- A zero of the "at once" pair: whether both channels' flags are set.
- A channel set to "both", which should calibrate both ranges.
- Output hold on: whether the Modbus readings and the A/D values freeze during a
  manual calibration (ZPA p.64 says the readings do).
- Whether a key written to 42001 acts, and shows in 00BDh, as a key at the panel.

§18 answers the last item, and the "at once" pair's flags.

## 15. What the registers left unexplained (2026-09-29)

A read-only session at 18:04–18:16 UTC set out to explain the words the analyzer
answers but no manual describes (§4.1, §4.2), and to try the function codes it had
never been sent. The owner walked the panel's menus and noted what the maintenance and
factory screens showed, then switched the analyzer off and on.

The probe was `scripts/probe_unknowns.py`, which sends only read requests and the fixed
diagnostic frames of §15.1. Its `watch` read the status, the display block (00B4h–00C1h),
the clock and A/D block with the zero words after it (03E8h–0424h) and 046Ah–0479h
every 0.3 s: 2,231 answered reads. It took a full snapshot of every readable region
(1,379 words) twice before the walk, 15 s after the power cycle and at the end.

Setup: `COM8`, station 1, `anymodbus` 0.3.0, `anyserial` 0.2.0, `anyio` 4.15.1,
Python 3.13.13, Windows 11. Raw files are in `probe_out/`, git-ignored:
`probe_functions_20260929T180428Z.json` and `unknowns_20260929T180435Z/`.

### 15.1 Function codes the analyzer had never been sent

| Request | Reply |
|---|---|
| FC07 read exception status | exception **01** |
| FC08 sub-function 0000h, return query data | exception 01 |
| FC0B get comm event counter | exception 01 |
| FC0C get comm event log | exception 01 |
| FC11 (17) report server ID | exception 01 |
| FC14 (20) read file record, file 1, record 0 | exception 01 |
| FC18 (24) read FIFO queue at 0000h | exception 01 |
| FC2B/0E (43) read device identification, basic | exception 01 |

- Each reply took 12–14 ms (66 ms for the first). A normal read straight after them was
  answered.
- FC08 was sent with sub-function 0000h only, since its other sub-functions restart or
  silence the link.
- FC01 and FC02 answer 02 (§5), so the analyzer knows those two function codes but none
  of these.
- **The program version cannot be read over Modbus.** The display at power-on is the only
  place it appears.

### 15.2 30182 numbers the menu pages

The screen register (30181) followed every menu the owner opened. The step register
(30182), which the manual defines only for a manual calibration on the measurement
screen (TN5A1190a p.46), numbered the pages of the menus:

| Screen (30181) | 30182 |
|---|---|
| 0 measurement, 1 menu, 2 range change, 3 calibration setting | 0 |
| 7 parameter setting | 0; 1 for one read as the maintenance password was confirmed |
| 8 maintenance | 0 on the item list; 1 sensor input; 2 error log; 22 and 23 calibration log; 5 while the factory password was entered |
| 9 factory | 0 on the item list; 26 A/D data; 38 coefficients; 50 other parameters; also 1, 2, 4, 8, 11, 12, 13, 18, 34, 35, 40, 44, 56, 57, 60, 61, 63, 65 and 78 (more of them identified in §17.1) |

- **Several page numbers are calibration step values.** Examples are 5 ("zero: wait")
  during the password entry, and 4 and 8 in factory mode. Only the screen register tells
  them apart: during a manual calibration it reads 0 (§14.3).
- fujilib's calibration tracker read the step whatever the screen showed. Replayed
  through it, this session's log gave five calibration events: four cancelled and one
  ambiguous. Nothing was calibrated. It now reads 30182 as a step only on the
  measurement screen, and so does the decoder (design §13.1 #80). The same replay now
  gives no event, and §14's log still gives its zero, span and cancel.
- **The "do not use" words 00B7h, 00B8h, 00BAh, 00BBh and 00BFh–00C1h read 0 on every
  screen**, as did 0419h–0424h.
- **00BDh showed every key read, the password keys included.** A program polling fast
  enough could follow a password entered at the panel.
- The last-calibration word (00B9h) kept 6 through the menus, and the cursor (00BCh)
  kept Ch3.

### 15.3 The factory blocks: no calibration coefficients, and the "other parameters"

The two undocumented holding blocks (§4.2) did not change at any point:

- through the menu walk;
- through the power cycle;
- through a second visit to factory mode after it.

All 1,039 holding words were the same at the end as at the start. The blocks are
therefore not a copy that is refreshed at power-on.

**The calibration coefficients are not in them.** The factory "Coefficient" screen showed
Ch1's zero and span coefficients (every channel's are in §17.2):

| Range | Zero | Span |
|---|---|---|
| 1 | 1.529192 | 0.661410 |
| 2 | 1.773113 | 0.305870 |

None of the four values appears in any readable region, in any of these encodings:

- one word, or a long word with either word first, scaled by 10^3 to 10^6;
- the reciprocal of the value, scaled the same way;
- 32- and 64-bit floating point in each word order;
- fixed point with 8 to 30 fraction bits.

With §14.1, the conclusion is that the analyzer keeps its zero and span where the
Modbus map does not reach.

**0C2Dh–0C34h are the factory menu's "other parameters"**, in the order the panel lists
them:

| Address | Panel | Word |
|---|---|---|
| 0C2Dh | zero limit | 0 (off) |
| 0C2Eh | range limit | 0 (off) |
| 0C2Fh | AO No. | 4 |
| 0C30h | language | 1 (Eng) |
| 0C31h | zero gas | 0 (Cylinder) |
| 0C32h | protocol | 0 (MO) |
| 0C33h | varied range | 1 (on) |
| 0C34h | DIO No. | 0 |

The service manual (TN5A1191b p.29-30) describes the two limits:

- **Zero limit off:** the display hides values below zero. The Modbus readings are
  negative all the same (−0.09 vol% CO2 throughout).
- **Range limit off:** readings are not held at 110 %FS. Its default is on, and this unit
  has it off, so a reading far above full scale is reported as it is (§15.5).

The rest of the block holds these known parts:

- a copy of every channel's ranges at 0BD9h–0BE2h (1000, 1000, 1000, 1000, 2100, 2500,
  2000, 2000, 1000, 2500);
- the type code and serial number as ASCII from 0C35h.

In the larger block, 0428h, 0448h, 0468h and 0488h each start a 16-point breakpoint
table (0, 300, 600 … 20000). 03E8h and 0408h start two measured curves on the same
0–20000 scale.

**The register capture of 2026-09-28 holds one wrong word.** It gives 0440h as 13824.
Every snapshot since reads 14000, which is the twelfth point of one of the four identical
breakpoint tables; the other three read 14000 there. It was a bad read in the one-word
scan, whose link timing was faulty (§6.2). Every other factory word of that capture
matches.

### 15.4 The raw detector counts at 046Ah–0471h

The four long words are two values, each twice: the CO2 detector's count (at 046Ah and
046Ch) and the CO detector's (at 046Eh and 0470h). Each pair was equal in every read.
O2 is not among them.

**They are the unsmoothed counts that the maintenance "Sensor Input" screen shows.**
The A/D values at 03EFh on are a smoothed copy:

- The owner read 69467 for Input 2 on that screen. On that screen the A/D value No. 1
  read 69459–69463, while 046Eh read 69449–69473 and was 69467 in one of the reads.
- At rest, over 1,573 reads, 046Ah changed in 75 % of them (standard deviation 2.3
  counts). A/D No. 0 changed in 4 % (1.2 counts). For CO the figures were 90 % (4.8
  counts) and 9 % (1.7 counts).
- After the power cycle (§15.5), 046Ah led and A/D No. 0 followed about 10 s later:
  54309 against 43959 at 18:13:05, and 63987 against 59906 at 18:13:18.
- The factory "A/D data" screen showed 65695, 69461 and 3337 for Nos. 0, 1 and 4. That
  fits either set.

**0472h–0478h are usually 0.** For single reads, 0472h and 0474h read 1 together (47
of the 1,573), or 0476h and 0478h did (31), and once all four. They follow the CO2 and
CO pairs above, but not the count's value. What they mean is not known.

Why each count appears twice is not known either. One per range is a guess: both
channels have one range.

### 15.5 A power cycle

The owner switched the analyzer off for about 10 s.

- **It answered 11.8 s after its last reply before the switch-off**, so within about
  2 s of power-on (the moment was not timed). The first reads were answered with every
  reading 0.
- **00B9h and the cursor (00BCh) came back as 0**, from 6 and Ch3. They are not kept
  over a power cycle. Settings are (§13.5).
- **The readings were wrong for about a minute, and nothing said so.** Neither the
  status, hold and error flags nor either error register was set:

| UTC | CO2 vol% | CO vol% | O2 vol% |
|---|---|---|---|
| 18:12:52.9 | 0.00 | 0.000 | 0.00 |
| 18:12:55.9 | **21.95** | **1.046** | 20.58 |
| 18:13:05.1 | 11.3 | 0.451 | 20.63 |
| 18:13:18.8 | 1.91 | 0.090 | 20.65 |
| 18:13:31.1 | 0.22 | 0.010 | 20.65 |
| 18:13:43.1 | −0.01 | −0.003 | 20.66 |
| 18:13:59.5 | −0.07, within 0.02 of where it settled | | |

  The first non-zero CO2 reading was 220 % of the 10 vol% range, and CO was over its
  range too; with range limit off (§15.3), neither was clamped. A recording would keep
  these rows with state `ok` (design §13.1 #81). The O2 cell read 20.58–20.66 vol% from
  its first non-zero reading.
- **The clock ran on**: it read 14:06:16 local time at 18:12:52.9 UTC, 6 min 37 s
  behind, as before.

### 15.6 Firmware 1.02 has a calibration log at the panel

Maintenance mode has a calibration log on this firmware, although Modbus has none
(1000h–1707h answer exception 02; §2).

- The newest Ch3 entry read "S1 21.02 9 29 13 32": a span of range 1 at 13:32 on the
  analyzer's clock. 21.02 is a concentration, presumably the one shown before the span.
- That is about 17:38:40 UTC, half a minute after §14's watch stopped. It explains why
  00B9h read 6 when this session began.
- The detector count of that entry was not noted.

### 15.7 Still not explained

- 00B7h, 00B8h, 00BAh, 00BBh and 00BFh–00C1h, and 0419h–0424h: always 0 so far.
- 0472h–0478h, and why each count at 046Ah–0471h appears twice.
- Most of the two factory blocks, word by word.
- Where the zero and span coefficients are kept. Not in the Modbus map, as far as any
  encoding tried shows. (What they are, and how O2's span follows from the A/D counts,
  is in §17.)

## 16. fujilib watching a calibration at the panel (2026-09-29)

The bench check of design §12 Phase 7A, 18:28–18:31 UTC. The owner calibrated O2 at
the front panel, with zero gas and then span gas at the inlet, while
`examples/watch_manual_calibration.py` ran fujilib's `wait_for_manual_calibration`
on `COM8`. It polled every 0.5 s with the A/D block and sent reads only. The code
was `ab4073b` with the Phase 7A work uncommitted on top, including the tracker fix
of §15.2. The events are `probe_out/watch_manual_20260929T182828Z.jsonl`
(git-ignored).

| UTC | At the panel | Event | O2 before → after | Gas | Deviation | O2 count |
|---|---|---|---|---|---|---|
| 18:29:44 | ZERO, Ch3, ENT, ENT | zero, **completed**, Ch3 range 1 | −0.01 → 0.00 vol% | 0.00 | −0.01 | 634 |
| 18:30:47 | SPAN, Ch3, ENT, ENT | span, **completed**, Ch3 range 1 | 20.86 → 20.95 vol% | 20.95 | −0.09 | 3349 |
| 18:31:06 | ZERO, Ch3, ENT, ESC | zero, **cancelled** | 20.95 → 20.95 vol% | — | — | 3349 |

- Each event came within about a second of the panel's return to measurement, and
  each rested on a read that showed the step running ("a read showed it running")
  or, for the cancel, on 00B9h still at 0 after the wait step.
- The time of each calibration is bracketed by the last read on the wait step and
  the first that showed it running, half a second apart.
- The O2 counts agree with §14.4 (636–637 on zero gas, 3352 on span gas).
- One read went unanswered once during the zero and once during the span, as in
  §14.3. The retry answered each, and no poll failed.
- The cancel's event first reported a deviation of 20.95, the reading on span gas
  against the zero gas, though nothing was calibrated. An event now reports
  deviations only when the calibration ran.

Phase 7A's hardware exit is met.

## 17. The factory-mode screens (2026-09-29)

At 18:41–18:44 UTC the owner opened seven factory-mode items in a set order and noted
what each showed, changing nothing. `scripts/probe_unknowns.py watch` read the panel
state throughout (496 reads) and took full snapshots before and after. No holding word
changed. The raw files are in `probe_out/unknowns_20260929T184115Z/`, and the owner's
record is `probe_out/factory_screens_20260929.md` (both git-ignored).

### 17.1 Which page number each item shows

In factory mode, 30182 (§15.2) gave each item its own number:

| Item | 30182 |
|---|---|
| 3. Ch Data | 4 |
| 4. Option | 65 |
| 5. Pressure | 60 |
| 6. Linearization | 8 |
| 7. Temperature | 11 |
| 11. A/D Data | 26 |
| 12. Others | 50 |
| 13. Interference | 34 |
| 14. Coefficient | 38 |

Coefficient showed 38 here as in §15, and every item was opened in the order asked, so
the list above is taken as right. The other numbers seen in §15 (1, 2, 12, 13, 18, 35,
40, 44, 56, 57, 61, 63 and 78) belong to items and sub-pages not identified.
"10. Memory Access" did not open: ENT on it left the list shown.

### 17.2 What the items showed

- **4. Option:** alarm 0, autocal off, zero check off. These are the three user menus
  this unit lacks (§13.7): alarms, auto calibration and auto zero. The menus are off
  here, and the unit has no DIO board to drive valves or alarm contacts (DIO No. 0,
  §15.3).
- **5. Pressure:** a list of two tables, "pressure table" and "compensation table", with
  no live value. The type code orders no pressure compensation (digit 23 `Y`), and the
  pressure A/D input (No. 14, 7653–7657) reads like the inputs with nothing connected
  (7642–7658; the ground input reads 7641–7644), so no sensor is taken to be fitted.
- **13. Interference:** a list of three entries, Interference-1 to -3, not opened. It
  does not settle whether 00A4h–00ABh, four long words of 1,000,000, are the interference
  coefficients (§4.2).
- **14. Coefficient**, every channel and range. Ch1's was read at 18:09, before the O2
  calibrations of §16; the rest at 18:42, after them:

| Channel | Range | Zero | Span |
|---|---|---|---|
| Ch1 CO2 | 1 | 1.529192 | 0.661410 |
| Ch1 CO2 | 2 | 1.773113 | 0.305870 |
| Ch2 CO | 1 | 1.449359 | 0.390960 |
| Ch2 CO | 2 | 1.000000 | 1.000000 |
| Ch3 O2 | 1 | −099366 | 06.17320 |
| Ch3 O2 | 2 | +000000 | 10.00000 |

  The O2 values are shown as printed, with no decimal point in the zero.

### 17.3 The coefficients

- **CO2 and CO are within the service manual's limits** (TN5A1191b p.33): zero 0.5–5 and
  span 0.1–10 for an infrared component.
- **CO's range 2 reads 1.000000 for both**, which looks like a range never calibrated.
  CO has one range. CO2's range 2 has values of its own although CO2 also has one range.
- **O2's range 2 (0–25 vol%) looks never calibrated**: zero 0, span 10.00000. Its
  readings would then be wrong until it is zeroed and spanned. Range 1 is the one in use
  (§3).
- **The O2 span coefficient is 800 × span gas / (span count − zero count)**, in the A/D
  counts of No. 4 (§14.4):
  - §16's zero read 634 counts on 0.00 vol% and its span 3349 counts on 20.95 vol%. That
    gives 800 × 20.95 / (3349 − 634) = 6.1731, against the 6.17320 shown.
  - The reading follows as (count − zero count) × span / 800. At 18:41, 3339 counts
    gives (3339 − 634) × 6.1732 / 800 = 20.87 vol%, the O2 reading at that moment.
  - The factor 800 comes from the fit, not from a manual.
  - So the counts fujilib records with each calibration (design §13.1 #75) reproduce the
    analyzer's own O2 span coefficient.
- **The O2 zero coefficient, −99366, is outside the manual's −2,000 to 12,000** for O2,
  yet the zero raised no error. The manual's O2 figures evidently do not apply to this
  cell. It also expects 18,000–22,000 counts on zero gas, where this cell reads about
  634. How −99366 relates to the zero count is not known. One value cannot say; the
  coefficient read again after another O2 zero, with that zero's count, would.

## 18. Front-panel keys written over Modbus (2026-09-30)

The key prototype of design §12 Phase 7B, 12:56–13:12 UTC. The owner authorized the
session and stood at the front panel throughout. `scripts/probe_panel.py` wrote key
codes to 42001 and 1 to 42002, and after each write read the panel until it settled,
about 12 times a second.

- **What it sent:** ZERO, SPAN, UP, DOWN, ESC, and the ENT that selects a channel. It
  never sent the ENT that starts a calibration, MODE or SIDE, and each key went only
  after a read of the step showed it allowed.
- **Nothing was calibrated.** 00B9h stayed 0 and no step read "running".
- **Nothing else changed.** All 162 settings read the same at the end as in the dump
  taken before the first key. No holding word changed while §18.4's flag was cleared.
- **The gases:** zero gas at the inlet for the zero experiments and air for the span
  one, at the owner's choice. A calibration started by mistake would then have used
  the right gas.
- **Setup:** `COM8`, station 1, `anymodbus` 0.3.0, `anyserial` 0.2.0, `anyio` 4.15.1,
  Python 3.13.13, Windows 11.
- **Raw files** (git-ignored):
  - `probe_out/probe_panel_<experiment>_20260930T*.json`, one per run;
  - `probe_out/calwatch_20260930T130351Z/`, the read-only watch of §18.4;
  - `probe_out/settings_before_keys_20260930.json`, and
    `probe_out/settings_diff_close_keys_20260930.txt` for the closing diff.

| UTC | Experiment | Written | Steps (30182) |
|---|---|---|---|
| 12:56:45 | `select-esc` | ZERO, ESC, SPAN, ESC | 0 → 4 → 0, then 0 → 7 → 0 |
| 12:57:30 | `cursor` | ZERO, DOWN ×3, UP ×3, ESC; SPAN, DOWN ×3, UP ×3, ESC | 4 and 7 throughout; the cursor in §18.2 |
| 13:01:14 | `zero-cancel` | ZERO, DOWN, ENT, ESC | 0 → 4 → 5 → 0; Ch3's zero flag on at 5, off with the step |
| 13:01:29 | `return-select` | ZERO, 42002 | 0 → 4 → 0 |
| 13:01:44 | `return-wait` | ZERO, DOWN, ENT, 42002 | 0 → 4 → 5 → 0; **Ch3's zero flag stayed on** (§18.4) |
| 13:09:05 | `at-once` | ZERO, UP, UP, DOWN, ENT, ESC | 0 → 4 → 5 → 0; the zero flags of Ch1 and Ch2 (§18.5) |
| 13:10:37 | `span-cancel` | SPAN, DOWN, DOWN, ENT, ESC | 0 → 7 → 8 → 0; Ch3's span flag on at 8, off one read after the step |
| 13:11:25 | `key-lock` | ZERO, with key lock on | none: key lock swallowed it (§18.6) |

The experiments ran in the planned order except `span-cancel`. It was moved after the
zero experiments so the gas changed once. The `backlight` and `hold` experiments were
prepared and not run, at the owner's choice.

### 18.1 A key written to 42001 acts as the same key at the panel

- **Every write was acknowledged** with the normal echo in 24–41 ms: 40 keys and two
  42002. There was no exception reply and no lost reply.
- **The analyzer did what the key does at the panel.** ZERO and SPAN opened channel
  selection, UP and DOWN moved the cursor, ENT selected the channel and opened the
  wait step, and ESC went back.
- **The change was there by the first read after the reply**, in every case but one.
  That read ended 105–132 ms after the write was sent, and reads came about every
  82 ms. So a key takes effect well within a tenth of a second.
- **A flag can lag the step by one read.** On the span's ESC the step read 0 at 112 ms,
  and the span flag cleared at 199 ms. Every other flag changed in the same read as
  the step.
- **00BDh never showed a key written over Modbus.** It read 0 in all 500 reads after
  the writes. The owner's presses at the panel in the same session showed in it as on
  2026-09-29 (§14.2): 64, 16, 8 and 32, each for one read at 0.5 s. So 00BDh is the key
  pressed at the panel only, and a program cannot confirm its own key from it.

### 18.2 The cursor wraps round

On channel selection the cursor (30189) wraps at both ends:

- **For a span** it offers Ch1, Ch2 and Ch3. DOWN from Ch3 goes to Ch1, and UP from Ch1
  to Ch3.
- **For a zero** it offers two positions: Ch1 and Ch2 together, which are zeroed "at
  once", and Ch3. The absent Ch4 and Ch5, also set to "at once", are not offered.
  - The pair's position reads **Ch1 when reached going down** (DOWN from Ch3, which
    wraps round) and **Ch2 when reached going up** (UP from Ch3). The owner saw Ch1
    and Ch2 highlighted together both ways.
  - UP from the pair wraps round to Ch3.

**Where ZERO opens.** It usually opened with the cursor where it was left, as on
2026-09-29. Three times it opened on Ch1 instead:

- the first ZERO of the session, after 18 hours without a key;
- the probe's ZERO at 13:01:44, the first after a 42002;
- the owner's ZERO at the panel at 13:04:49, the first after another 42002.

ESC never had that effect. Why it happens is not known. SPAN was never opened after a
42002 or a long pause. A program has to read the cursor after ZERO or SPAN; it cannot
predict it.

### 18.3 ESC and 42002 from channel selection and the wait step

| From | ESC | 42002 |
|---|---|---|
| channel selection (step 4 or 7) | the measurement screen | the measurement screen |
| the wait step (5 or 8) | the measurement screen; the channel's flag clears | the measurement screen; **the channel's flag stays set** |

Each went straight to the measurement screen, not to the step before.

### 18.4 42002 on the wait step leaves the calibration flag set

The 42002 at 13:01:48 returned the display to measurement, and 30182 read 0. The
owner saw a normal measurement screen. But Ch3's zero flag (30052) stayed set:

- **It did not time out.** It was still set at 13:07:45, six minutes later, beyond the
  gas flow time of 300 s.
- **A cancel from channel selection did not clear it.** The owner pressed ZERO, which
  opened channel selection with the cursor on Ch1, and ESC (13:04:49–13:04:53).
- **A cancel from the wait step did.** The owner pressed ZERO, moved to O2, pressed
  ENT once (the wait step) and then ESC (13:08:22–13:08:28). The flag cleared with the
  step. 00B9h stayed 0 throughout: nothing ran.

So 42002 closes the display, but not the analyzer's manual calibration. While the flag
is set, fujilib reads the channel as calibrating, and refuses setting writes (design
§13.1 #63). An operator would see nothing wrong at the panel. On an analyzer with
calibration valves, the calibration gas might go on flowing; this unit has none, so
that is untested. **The way to cancel a manual calibration is ESC on its wait step, not
42002.**

### 18.5 The "at once" pair

ENT on the pair's position set the zero flags of Ch1 and Ch2 (30050 and 30051). It did
not set those of the absent Ch4 and Ch5, although they are set to "at once" too. ESC
cleared both in the same read as the step. This answers the flags half of design
§13.2 #37. Which ranges a completed "at once" zero changes still needs a real
calibration.

### 18.6 Key lock swallows keys written over Modbus

With key lock switched on at the panel, a ZERO written to 42001 was acknowledged
normally (25 ms), and nothing changed: the step stayed 0. Key lock does not stop
setting writes (§13.3), but it does stop keys.

The analyzer then answered no read for 1.6 s:

- the next read timed out on all three attempts;
- the one after it timed out once;
- then a late reply to the earlier request arrived, which `anymodbus` rejected and
  retried.

Reads were normal again 2.3 s after the key. What the display
showed meanwhile was not seen.

### 18.7 Not answered here

- Whether a key is swallowed while the backlight is off. The session's first ZERO
  acted after 18 hours without a key, but whether the backlight was off then was not
  seen.
- Output hold during a manual calibration: whether the readings and the A/D values
  freeze (design §13.2 #38). This is prepared as `probe_panel.py hold`.
- The error display (§13.2 #36) and a channel set to "both" (#37). Both need a real
  calibration.
- Why ZERO sometimes opens on Ch1 (§18.2).

In passing: O2 read 21.08–21.09 vol% on air, at 3,367–3,369 counts. The span of
2026-09-29 at 18:30 (§16) set 20.95 at 3,349 counts, so the detector count had risen
by about 19 counts (0.14 vol%) in 19 hours. Whether that was pressure or drift is not
known.
