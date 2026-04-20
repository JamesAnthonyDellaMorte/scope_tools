# scope_tools

Automated measurement tools for the RIGOL MSO5074 oscilloscope via SCPI over TCP.

## measure_burst_delta.py

Measures the time delta between repeating TX burst patterns from WhisperTrack (NFWF) acoustic modem hardware. Finds the rising edge of the second TX burst, snaps the scope cursor to it at sample-aligned precision, and reports the timing delta.

### Usage

```bash
# Send LocationRequest to modem + trigger + measure (10 runs)
python3 measure_burst_delta.py --send -n 10

# Trigger scope only (no modem send)
python3 measure_burst_delta.py

# Snap BX cursor on current capture (no trigger)
python3 measure_burst_delta.py --calibrate

# Use current capture without triggering
python3 measure_burst_delta.py --no-trigger
```

### Options

| Flag | Description |
|------|-------------|
| `--send` | Send `maria fontus:LocationRequest` to modem via telnet before triggering |
| `-n N` | Run N measurement cycles (default: 1) |
| `--calibrate` | Snap BX to edge on current capture only |
| `--no-trigger` | Use current stopped capture |
| `--device-id ID` | Fontus device ID (default: 3245) |
| `--modem HOST` | Modem hostname (default: pmm6081.local) |
| `--host HOST` | Scope hostname (default: scope.local) |
| `--threshold V` | Voltage threshold for TX burst detection (default: 1.0) |

### How It Works

1. **Arm scope** (`SINGle`) and optionally send LocationRequest to modem via telnet
2. **Read BX cursor position** (user places it roughly near the expected TX burst)
3. **Zoom to 1ms/div** centered on BX — find first rising edge at ~10us resolution
4. **Zoom to 20us/div** — find sample-precise edge, calculate pixel, set CBX
5. **Restore original view** and report AX, BX, delta

### Requirements

- Python 3 with numpy
- RIGOL MSO5074 on the network (default: `scope.local:5555`)
- Popoto modem on the network (default: `pmm6081.local:23`) if using `--send`

## MSO5000 Programming Guide

`MSO5000_ProgrammingGuide_EN-V2.0.pdf` — official RIGOL SCPI reference.

## MSO5000 SCPI Gotchas

Hard-won lessons from extensive automated use of the MSO5074:

### Cursor Positioning is Pixel-Only (Integer 0-999)

`:CURSor:MANual:CAX` and `:CURSor:MANual:CBX` accept **integers only** (0-999). There is no "set cursor to time value" command. `AXValue?` and `BXValue?` are **read-only queries**.

**Pixel formula:** `pixel = round((time - toff) / tscale * 100 + 500)`

At normal zoom levels (e.g., 500ms/div), each pixel = 5ms — way too coarse for sample-aligned measurements. The workaround is the **zoom-snap method**: temporarily set the timebase to 20us/div centered on the target time (so pixel 500 = target), set the cursor, then restore the original view. At 20us/div each pixel = 0.2us, matching a 5MSa/s sample period.

### NORMal Waveform Mode Aliases the Carrier at Intermediate Zoom Levels

The `NORMal` waveform mode returns one sample per screen point. For a 25kHz carrier (40us period):

- **500ms/div (5ms/pt):** Scope shows min/max envelope — the burst looks like a solid block and peak voltage is accurate (~2V for TX burst). Great for finding approximate burst locations with a voltage threshold.
- **1ms/div to 50ms/div:** Individual samples of the carrier. Peak voltage appears low (~0.2V) because you're sampling random points on the sinusoid. **You cannot use a voltage threshold at these zoom levels.** Use a relative threshold (50% of max) to find carrier crossings instead.
- **20us/div (200ns/pt):** Carrier is fully resolved (~200 samples per cycle). True voltage is visible. Good for precise edge detection.

### RAW Waveform Data Range Depends on Current Timebase

In STOP mode, the RAW data range reported by `:WAVeform:PREamble?` changes based on the current timebase setting. Zooming in can shrink the available RAW data window, even though the scope visually displays data across the full capture. If you need RAW data at a specific time position, make sure the timebase is set wide enough that the position is within the RAW range before reading.

### Memory Depth and Sample Rate Are Coupled

Setting `ACQuire:MDEPth` to a specific value may not stick — the scope auto-adjusts based on the timebase and number of active channels. The sample rate changes when you change the timebase:
- 500ms/div with 1 channel: 5MSa/s, 25Mpts
- Wider timebases: lower sample rate, same memory depth = longer capture

The capture time coverage depends on the timebase **at trigger time**, not at read time. If you need both bursts in one capture (~3.5s span), make sure the timebase is wide enough when you trigger.

### Trigger Status STOP Means Acquisition Complete

`:TRIGger:STATus?` returns `WAIT` (waiting for trigger), `TD` (triggered, filling post-trigger buffer), or `STOP` (acquisition complete). Always poll for `STOP` before reading waveform data.

### Screen Waveform (NORMal) vs Internal Memory (RAW)

- **NORMal:** Returns what's on screen — always available at the current zoom level, but resolution depends on timebase. Good for progressive zoom edge detection.
- **RAW:** Returns internal memory at full sample rate — higher resolution but the available range depends on timebase state and may not cover your region of interest.

For burst edge detection, **NORMal mode with progressive zoom** is more reliable than RAW mode because NORMal always has data where the scope is currently looking.

### WebSocket Screenshot vs SCPI Screenshot

The web interface's PrintScreen uses WebSocket (`ws://host/tcp_proxy` with `:DISP:PULL? SNAP`). For programmatic screenshots, use `:DISPlay:DATA?` over SCPI port 5555 — it returns a BMP in TMC binary format. The file can be large (~1.8MB BMP).

### Telnet to Modem Needs Time

When automating modem commands via telnet (port 23), the modem shell takes ~2s to initialize after connection. Send the command, then wait ~8s for the TX to complete before checking scope trigger status. Keeping the telnet connection open across multiple runs avoids the 2s reconnect overhead each time.
