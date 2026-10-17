# -*- coding: utf-8 -*-
"""
diagnose.py - work out what brightness control this PC actually supports.

Read-only: this script never changes your brightness. Run it when the widget
does not behave as expected and include the output in a bug report.

    python diagnose.py

It checks, in order of quality:
    1. DDC/CI   (dxva2.dll)  - real hardware brightness on external monitors
    2. WMI      (root\\wmi)   - internal laptop panels
    3. Gamma    (gdi32.dll)  - software dimming, works on almost anything
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import POINTER, byref, wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor", wintypes.HANDLE),
        ("szPhysicalMonitorDescription", wintypes.WCHAR * 128),
    ]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
    POINTER(wintypes.RECT), wintypes.LPARAM,
)

user32.EnumDisplayMonitors.argtypes = [
    wintypes.HDC, POINTER(wintypes.RECT), MONITORENUMPROC, wintypes.LPARAM
]
user32.EnumDisplayDevicesW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, POINTER(DISPLAY_DEVICEW), wintypes.DWORD
]
dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [
    wintypes.HMONITOR, POINTER(wintypes.DWORD)
]
dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [
    wintypes.HMONITOR, wintypes.DWORD, POINTER(PHYSICAL_MONITOR)
]
dxva2.GetMonitorBrightness.argtypes = [
    wintypes.HANDLE, POINTER(wintypes.DWORD), POINTER(wintypes.DWORD),
    POINTER(wintypes.DWORD),
]
dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [
    wintypes.HANDLE, ctypes.c_ubyte, POINTER(wintypes.DWORD),
    POINTER(wintypes.DWORD), POINTER(wintypes.DWORD),
]
dxva2.DestroyPhysicalMonitors.argtypes = [
    wintypes.DWORD, POINTER(PHYSICAL_MONITOR)
]
gdi32.CreateDCW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p
]
gdi32.CreateDCW.restype = wintypes.HDC
gdi32.GetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [wintypes.HDC]

RAMP_TYPE = wintypes.WORD * 256 * 3
ATTACHED_TO_DESKTOP = 0x00000001
MIRRORING_DRIVER = 0x00000008


def header(text: str) -> None:
    print()
    print(text)
    print("-" * len(text))


def check_ddc():
    header("1. DDC/CI - real hardware brightness (best option)")

    handles = []

    @MONITORENUMPROC
    def collect(hmon, _hdc, _rect, _data):
        handles.append(hmon)
        return True

    user32.EnumDisplayMonitors(None, None, collect, 0)
    print(f"   display adapters seen: {len(handles)}")

    found = 0
    arrays = []
    for adapter, hmon in enumerate(handles):
        count = wintypes.DWORD(0)
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, byref(count)):
            print(f"   adapter {adapter}: could not query physical monitors")
            continue
        if count.value == 0:
            print(f"   adapter {adapter}: 0 physical monitors")
            continue

        array = (PHYSICAL_MONITOR * count.value)()
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, count.value, array):
            print(f"   adapter {adapter}: enumeration failed")
            continue
        arrays.append(array)

        for position, phys in enumerate(array):
            name = phys.szPhysicalMonitorDescription.strip() or "(unnamed)"
            minimum = wintypes.DWORD(0)
            current = wintypes.DWORD(0)
            maximum = wintypes.DWORD(0)
            if dxva2.GetMonitorBrightness(
                phys.hPhysicalMonitor, byref(minimum), byref(current), byref(maximum)
            ):
                found += 1
                print(f"   [OK] {name}")
                print(f"        brightness {current.value}% "
                      f"(hardware range {minimum.value}-{maximum.value})")
                print("        -> fully supported")
            else:
                error = ctypes.get_last_error()
                raw = wintypes.DWORD(0)
                vcp = dxva2.GetVCPFeatureAndVCPFeatureReply(
                    phys.hPhysicalMonitor, 0x10, None, byref(raw),
                    byref(wintypes.DWORD(0)),
                )
                print(f"   [--] {name}")
                print(f"        no brightness control "
                      f"(GetMonitorBrightness failed, error {error}); "
                      f"raw VCP 0x10 read "
                      f"{'ok (' + str(raw.value) + ')' if vcp else 'also failed'}")
                print("        -> enable DDC/CI in the monitor's own OSD menu")

    for array in arrays:
        dxva2.DestroyPhysicalMonitors(len(array), array)

    if found == 0:
        print("   RESULT: no DDC/CI brightness available -> the app will use")
        print("           software (gamma) dimming instead.")
    else:
        print(f"   RESULT: {found} monitor(s) support real hardware brightness.")
    return found


def check_wmi():
    header("2. WMI - internal laptop panel brightness")
    script = (
        "Get-CimInstance -Namespace root\\wmi -ClassName WmiMonitorBrightness "
        "-ErrorAction SilentlyContinue | "
        "Select-Object InstanceName,CurrentBrightness | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=40,
        )
        output = result.stdout.strip()
    except Exception as exc:
        print(f"   could not run PowerShell: {exc}")
        return 0
    if not output:
        print("   no WmiMonitorBrightness class on this machine")
        print("   (normal on a desktop, or on any PC using an external monitor)")
        return 0
    print(f"   {output}")
    print("   RESULT: internal-panel control available")
    return 1


def check_gamma():
    header("3. Gamma ramp - software dimming (fallback)")
    count = 0
    index = 0
    while True:
        device = DISPLAY_DEVICEW()
        device.cb = ctypes.sizeof(DISPLAY_DEVICEW)
        if not user32.EnumDisplayDevicesW(None, index, byref(device), 0):
            break
        index += 1
        flags = device.StateFlags
        if not flags & ATTACHED_TO_DESKTOP or flags & MIRRORING_DRIVER:
            continue
        hdc = gdi32.CreateDCW(device.DeviceName, None, None, None)
        if not hdc:
            continue
        ramp = RAMP_TYPE()
        ok = gdi32.GetDeviceGammaRamp(hdc, byref(ramp))
        name = (device.DeviceString or device.DeviceName).strip()
        print(f"   [{'OK' if ok else '--'}] {name} "
              f"({device.DeviceName})")
        if ok:
            count += 1
        gdi32.DeleteDC(hdc)
    if count:
        print(f"   RESULT: software dimming available on {count} display(s).")
    else:
        print("   RESULT: software dimming NOT available.")
    return count


def main() -> int:
    print("Brightness Scroll - hardware diagnostic")
    print(f"python {sys.version.split()[0]} on {os.name}")

    ddc = check_ddc()
    wmi = check_wmi()
    gamma = check_gamma()

    header("Summary")
    print(f"   DDC/CI hardware brightness : {ddc} monitor(s)"
          f"{'   <-- the app will use this' if ddc else ''}")
    print(f"   WMI internal panel         : {wmi} monitor(s)")
    print(f"   Gamma software dimming     : {gamma} display(s)"
          f"{'   <-- the app will use this' if (ddc == 0 and gamma) else ''}")

    if ddc == 0 and gamma == 0:
        print()
        print("   Nothing usable was found. Please report the output above.")
        return 1
    print()
    print("   Also try:  python brightness_scroll.pyw --selftest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
