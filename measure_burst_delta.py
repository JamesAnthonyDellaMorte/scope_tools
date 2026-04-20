#!/usr/bin/env python3
"""
Measure the time delta between repeating TX burst patterns on RIGOL MSO5074.

AX cursor is trigger-locked on the first TX burst edge (don't move it).
BX cursor should be placed roughly near the second TX burst edge.
This script reads BX's current position, zooms to 1ms/div there to find
the rising edge, then snaps BX to sample-aligned precision at 20µs/div.

Usage:
    python3 measure_burst_delta.py --send -n 10       # Send + measure 10 times
    python3 measure_burst_delta.py --send              # Send + measure once
    python3 measure_burst_delta.py                     # Trigger only (no send)
    python3 measure_burst_delta.py --no-trigger        # Use current capture
    python3 measure_burst_delta.py --calibrate         # Snap BX on current capture
"""

import socket
import time
import argparse
import sys
import telnetlib

try:
    import numpy as np
except ImportError:
    print("ERROR: numpy required.  pip3 install numpy")
    sys.exit(1)

HOST = 'scope.local'
PORT = 5555
MODEM_HOST = 'pmm6081.local'
MODEM_PORT = 23
FINE_ZOOM_SCALE = 0.000020  # 20µs/div for sample-aligned cursor placement


def make_conn(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect((host, port))
    return s


def scpi(host, port, cmd, timeout=5):
    s = make_conn(host, port)
    s.sendall((cmd + '\n').encode())
    if '?' in cmd:
        time.sleep(0.3)
        s.settimeout(timeout)
        try:
            data = s.recv(4096).decode().strip()
        except socket.timeout:
            data = ''
        s.close()
        return data
    s.close()
    return None


def scpi_binary(host, port, cmd, timeout=30):
    s = make_conn(host, port)
    s.sendall((cmd + '\n').encode())
    data = b''
    while True:
        try:
            chunk = s.recv(131072)
            if not chunk:
                break
            data += chunk
            if len(data) > 11 and data[0:1] == b'#':
                n = int(data[1:2])
                exp = int(data[2:2 + n])
                if len(data) >= 2 + n + exp:
                    break
        except socket.timeout:
            break
    s.close()
    if data and data[0:1] == b'#':
        n = int(data[1:2])
        exp = int(data[2:2 + n])
        return data[2 + n:2 + n + exp]
    return data


def read_screen(host, port):
    """Read NORMal (screen) waveform."""
    scpi(host, port, ':WAVeform:SOURce CHANnel1')
    scpi(host, port, ':WAVeform:MODE NORMal')
    scpi(host, port, ':WAVeform:FORMat BYTE')

    preamble = scpi(host, port, ':WAVeform:PREamble?')
    params = preamble.split(',')
    xinc = float(params[4])
    xorig = float(params[5])
    yinc = float(params[7])
    yorig = float(params[8])
    yref = float(params[9])

    raw = scpi_binary(host, port, ':WAVeform:DATA?')
    samp = np.frombuffer(raw, dtype=np.uint8)
    voltage = (samp.astype(float) - yorig - yref) * yinc
    t_axis = np.arange(len(samp)) * xinc + xorig
    return t_axis, voltage, xinc


def run_once(h, p, orig_tscale, orig_toff, modem_tn=None, device_id=3245):
    """Run one trigger+measure cycle. Returns (ax, bx, delta) or None on error."""

    # ---- Arm scope ----
    scpi(h, p, ':SINGle')

    # ---- Send LocationRequest if modem connected ----
    if modem_tn is not None:
        cmd = f'maria fontus:LocationRequest {device_id}'
        modem_tn.write(f'{cmd}\r\n'.encode())
        time.sleep(8)
        resp = modem_tn.read_very_eager().decode(errors='replace')
        if 'TxStart' not in resp:
            print("  Warning: no TxStart")

    # ---- Wait for trigger ----
    for _ in range(120):
        time.sleep(0.5)
        status = scpi(h, p, ':TRIGger:STATus?')
        if status == 'STOP':
            break
    if status != 'STOP':
        print("  ERROR: Trigger timeout")
        return None

    # ---- Read cursors ----
    ax_time = float(scpi(h, p, ':CURSor:MANual:AXValue?'))
    bx_time = float(scpi(h, p, ':CURSor:MANual:BXValue?'))

    # ---- Step 1: 1ms/div centered on current BX ----
    scpi(h, p, ':TIMebase:MAIN:SCALe 0.001')
    scpi(h, p, f':TIMebase:MAIN:OFFSet {bx_time:.6f}')
    time.sleep(1)

    t_mid, v_mid, xinc_mid = read_screen(h, p)
    quiet = v_mid[:min(100, len(v_mid) // 4)]
    bl = np.median(quiet)
    pk = np.max(v_mid)
    th = bl + 0.5 * (pk - bl)
    crossings = np.where(np.diff((v_mid > th).astype(int)) == 1)[0]

    if len(crossings) == 0:
        scpi(h, p, f':TIMebase:MAIN:SCALe {orig_tscale}')
        scpi(h, p, f':TIMebase:MAIN:OFFSet {orig_toff}')
        print("  ERROR: No edge at 1ms/div")
        return None

    mid_time = t_mid[crossings[0] + 1]

    # ---- Step 2: 20µs/div snap ----
    scpi(h, p, f':TIMebase:MAIN:SCALe {FINE_ZOOM_SCALE}')
    scpi(h, p, f':TIMebase:MAIN:OFFSet {mid_time:.9f}')
    time.sleep(0.5)

    t_fine, v_fine, xinc_fine = read_screen(h, p)
    q = v_fine[:min(100, len(v_fine) // 4)]
    bl2 = np.median(q)
    pk2 = np.max(v_fine)
    th2 = bl2 + 0.5 * (pk2 - bl2)
    c2 = np.where(np.diff((v_fine > th2).astype(int)) == 1)[0]

    if len(c2) == 0:
        scpi(h, p, f':TIMebase:MAIN:SCALe {orig_tscale}')
        scpi(h, p, f':TIMebase:MAIN:OFFSet {orig_toff}')
        print("  ERROR: No edge at 20µs/div")
        return None

    edge_time = t_fine[c2[0] + 1]
    toff_now = float(scpi(h, p, ':TIMebase:MAIN:OFFSet?'))
    tscale_now = float(scpi(h, p, ':TIMebase:MAIN:SCALe?'))
    pixel = round((edge_time - toff_now) / tscale_now * 100 + 500)
    pixel = max(0, min(999, pixel))

    scpi(h, p, f':CURSor:MANual:CBX {pixel}')
    time.sleep(0.3)

    # ---- Restore view ----
    scpi(h, p, f':TIMebase:MAIN:SCALe {orig_tscale}')
    scpi(h, p, f':TIMebase:MAIN:OFFSet {orig_toff}')
    time.sleep(0.5)

    # ---- Readback ----
    ax_final = float(scpi(h, p, ':CURSor:MANual:AXValue?'))
    bx_final = float(scpi(h, p, ':CURSor:MANual:BXValue?'))
    dt_final = bx_final - ax_final

    return ax_final, bx_final, dt_final


def main():
    parser = argparse.ArgumentParser(
        description='Measure TX burst-to-burst ΔX on RIGOL MSO5074')
    parser.add_argument('--host', default=HOST)
    parser.add_argument('--port', type=int, default=PORT)
    parser.add_argument('--no-trigger', action='store_true',
                        help='Use current stopped capture')
    parser.add_argument('--calibrate', action='store_true',
                        help='Snap BX to edge on current capture')
    parser.add_argument('--send', action='store_true',
                        help='Send LocationRequest to modem')
    parser.add_argument('--device-id', type=int, default=3245,
                        help='Fontus device ID (default: 3245)')
    parser.add_argument('--modem', default=MODEM_HOST,
                        help='Modem hostname (default: pmm6081.local)')
    parser.add_argument('-n', type=int, default=1,
                        help='Number of runs (default: 1)')
    args = parser.parse_args()

    if args.calibrate:
        args.no_trigger = True

    h, p = args.host, args.port

    idn = scpi(h, p, '*IDN?')
    print(f"Connected: {idn}")

    orig_tscale = scpi(h, p, ':TIMebase:MAIN:SCALe?')
    orig_toff = scpi(h, p, ':TIMebase:MAIN:OFFSet?')

    # ---- Single shot (no-trigger / calibrate) ----
    if args.no_trigger:
        result = run_once(h, p, orig_tscale, orig_toff)
        if result:
            ax, bx, dt = result
            print(f"\n{'=' * 50}")
            print(f"  AX:  {ax * 1e3:.6f} ms")
            print(f"  BX:  {bx * 1e3:.6f} ms")
            print(f"  ΔX:  {dt:.9f} s  ({dt * 1e3:.6f} ms)")
            print(f"{'=' * 50}")
        return

    # ---- Connect modem if --send ----
    modem_tn = None
    if args.send:
        print(f"Connecting to modem {args.modem}...")
        modem_tn = telnetlib.Telnet(args.modem, MODEM_PORT, timeout=10)
        time.sleep(2)
        modem_tn.read_very_eager()
        print("  Modem connected")

    # ---- Run N times ----
    results = []
    for i in range(args.n):
        print(f"\n--- Run {i + 1}/{args.n} ---")
        result = run_once(h, p, orig_tscale, orig_toff,
                          modem_tn=modem_tn, device_id=args.device_id)
        if result:
            ax, bx, dt = result
            results.append(dt)
            print(f"  ΔX: {dt * 1e3:.6f} ms")
        else:
            results.append(None)
            print("  FAILED")

    # ---- Close modem ----
    if modem_tn:
        modem_tn.close()

    # ---- Summary ----
    valid = [r for r in results if r is not None]
    if valid:
        print(f"\n{'=' * 50}")
        print(f"  {'Run':>4}  {'ΔX (ms)':>12}  {'Drift (ms)':>12}")
        print(f"  {'-'*4}  {'-'*12}  {'-'*12}")
        for i, r in enumerate(results):
            if r is not None:
                print(f"  {i+1:4d}  {r*1e3:12.6f}  {(r-2.0)*1e3:+12.6f}")
            else:
                print(f"  {i+1:4d}  {'FAILED':>12}")
        print(f"  {'-'*4}  {'-'*12}  {'-'*12}")
        mean = np.mean(valid)
        spread = (max(valid) - min(valid))
        std = np.std(valid)
        print(f"  Mean:    {mean*1e3:.6f} ms")
        print(f"  Spread:  {spread*1e6:.1f} µs")
        print(f"  StdDev:  {std*1e6:.1f} µs")
        print(f"  N:       {len(valid)}/{len(results)}")
        print(f"{'=' * 50}")


if __name__ == '__main__':
    main()
