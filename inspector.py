#!/usr/bin/env python3
"""Windows Laptop Inspector — standalone inspection engine.

Collects hardware, software, battery, temperature, and stability facts from a
Windows machine using native tools (PowerShell, WMI/CIM, powercfg) plus the
optional ``psutil`` library, then computes battery health, a 0-100 health score,
and a rough performance estimate. It can render the result as JSON, a
self-contained HTML dashboard (gauges + bar charts), or a minimal PDF.

Usage:
    python inspector.py                    # print JSON report to stdout
    python inspector.py --format html      # write an HTML dashboard next to cwd
    python inspector.py --format pdf --output report.pdf
    python inspector.py --lang my          # Burmese plain-language text
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import webbrowser
from datetime import datetime

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - psutil is optional
    psutil = None

SCHEMA_VERSION = 1
PRODUCT_NAME = "Windows Laptop Inspector"

# --------------------------------------------------------------------------- #
# Localization (plain-language labels only; JSON keys stay English)           #
# --------------------------------------------------------------------------- #

_STR = {
    "en": {
        "good": "Good",
        "fair": "Fair",
        "poor": "Poor",
        "healthy": "Healthy",
        "warning": "Warning",
        "critical": "Critical",
        "charging": "Charging",
        "discharging": "Discharging",
        "on_ac": "On AC power",
        "full": "Fully charged",
        "unknown": "Unknown",
        "no_battery": "No battery detected (desktop or battery driver unavailable)",
    },
    "my": {
        "good": "ကောင်း",
        "fair": "အလယ်အလတ်",
        "poor": "ညံ့",
        "healthy": "ကျန်းမာ",
        "warning": "သတိပေး",
        "critical": "အရေးကြီး",
        "charging": "အားသွင်းနေသည်",
        "discharging": "အားသုံးစွဲနေသည်",
        "on_ac": "AC ပါဝါသုံးနေသည်",
        "full": "အားပြည့်",
        "unknown": "မသိ",
        "no_battery": "ဘက်ထရီ မတွေ့ပါ (desktop သို့မဟုတ် ဘက်ထရီ driver မရှိ)",
    },
}

_HEALTH_LABEL = {
    "en": {"good": "Good", "fair": "Fair", "poor": "Poor"},
    "my": {"good": "ကောင်း", "fair": "အလယ်အလတ်", "poor": "ညံ့"},
}

_PERF_LABEL = {
    "en": {"good": "Good", "fair": "Fair", "poor": "Poor"},
    "my": {"good": "ကောင်း", "fair": "အလယ်အလတ်", "poor": "ညံ့"},
}


def _t(lang: str, key: str) -> str:
    return _STR.get(lang, _STR["en"]).get(key, _STR["en"][key])


# --------------------------------------------------------------------------- #
# PowerShell helpers                                                          #
# --------------------------------------------------------------------------- #

_PW = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]
_UTF8_PREFIX = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "


def _pwsh(script: str, as_json: bool = True, timeout: int = 60):
    """Run a PowerShell snippet and return JSON / text, or None on failure."""
    full = _UTF8_PREFIX + script
    if as_json:
        full += " | ConvertTo-Json -Compress -Depth 8"
    try:
        proc = subprocess.run(
            _PW + [full],
            capture_output=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0 or not out:
        return None
    if not as_json:
        return out
    try:
        return json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return None


def _first(seq):
    """First element of a list, or None."""
    if isinstance(seq, list):
        return seq[0] if seq else None
    return seq


def _num(value, scale: float = 1.0):
    """Coerce to float with a scale factor, or None."""
    if value is None:
        return None
    try:
        return round(float(value) * scale, 2)
    except (TypeError, ValueError):
        return None


def _parse_date(value):
    """Normalize WMI `/Date(ms)/` or ISO dates to an ISO-8601 string."""
    if value is None:
        return None
    s = str(value)
    m = re.search(r"/Date\((-?\d+)\)/", s)
    if m:
        try:
            return datetime.fromtimestamp(int(m.group(1)) / 1000.0).isoformat()
        except (ValueError, OSError, OverflowError):
            return s
    m = re.search(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    return s


_BATTERY_CHEMISTRY = {
    1: "Other", 2: "Unknown", 3: "Lead acid", 4: "Nickel cadmium",
    5: "Nickel metal hydride", 6: "Lithium-ion", 7: "Zinc air",
    8: "Lithium polymer",
}


# --------------------------------------------------------------------------- #
# Collectors                                                                   #
# --------------------------------------------------------------------------- #


def _collect_os():
    osys = _pwsh(
        "Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,"
        "BuildNumber,OSArchitecture,InstallDate,LastBootUpTime"
    )
    osys = _first(osys) or {}
    reg = _pwsh(
        "Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion' "
        "| Select-Object ProductName,DisplayVersion,EditionID,CurrentBuildNumber,UBR"
    )
    reg = _first(reg) or {}
    install = _parse_date(osys.get("InstallDate"))
    uptime = None
    boot = _parse_date(osys.get("LastBootUpTime"))
    if boot:
        try:
            boot_dt = datetime.fromisoformat(boot)
            uptime = str(datetime.now() - boot_dt).split(".")[0]
        except (ValueError, TypeError):
            pass
    build = reg.get("CurrentBuildNumber") or osys.get("BuildNumber")
    return {
        "product_name": reg.get("ProductName") or osys.get("Caption"),
        "edition": reg.get("EditionID") or (osys.get("Caption") or "").split(" ")[-1],
        "version": reg.get("DisplayVersion") or osys.get("Version"),
        "build": str(build) if build else None,
        "architecture": osys.get("OSArchitecture"),
        "install_date": install,
        "uptime": uptime,
    }


def _collect_machine():
    cs = _first(
        _pwsh(
            "Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,"
            "SystemFamily,TotalPhysicalMemory"
        )
        or {}
    ) or {}
    bios = _first(
        _pwsh(
            "Get-CimInstance Win32_BIOS | Select-Object Manufacturer,SMBIOSBIOSVersion,ReleaseDate"
        )
        or {}
    ) or {}
    return {
        "hostname": os.environ.get("COMPUTERNAME") or _pwsh("$env:COMPUTERNAME", as_json=False),
        "manufacturer": cs.get("Manufacturer"),
        "model": cs.get("Model"),
        "system_family": cs.get("SystemFamily"),
        "bios": {
            "manufacturer": bios.get("Manufacturer"),
            "version": bios.get("SMBIOSBIOSVersion"),
            "release_date": _parse_date(bios.get("ReleaseDate")),
        },
    }


def _collect_cpu():
    proc = _first(
        _pwsh(
            "Get-CimInstance Win32_Processor | Select-Object Name,Manufacturer,"
            "NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,CurrentClockSpeed"
        )
        or {}
    ) or {}
    usage = None
    if psutil is not None:
        try:
            usage = psutil.cpu_percent(interval=0.5)
        except Exception:
            usage = None
    return {
        "name": (proc.get("Name") or "").strip(),
        "manufacturer": proc.get("Manufacturer"),
        "physical_cores": proc.get("NumberOfCores"),
        "logical_processors": proc.get("NumberOfLogicalProcessors"),
        "max_clock_mhz": proc.get("MaxClockSpeed"),
        "current_clock_mhz": proc.get("CurrentClockSpeed"),
        "current_usage_percent": usage,
    }


def _collect_memory():
    mem = None
    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            mem = {
                "total_gb": round(vm.total / (1024 ** 3), 2),
                "used_gb": round(vm.used / (1024 ** 3), 2),
                "used_percent": round(vm.percent, 1),
            }
        except Exception:
            mem = None
    modules = _pwsh(
        "Get-CimInstance Win32_PhysicalMemory | Select-Object Manufacturer,Capacity,"
        "Speed,PartNumber,FormFactor"
    )
    mod_list = []
    speed = None
    if isinstance(modules, list):
        for m in modules:
            if speed is None:
                speed = m.get("Speed")
            mod_list.append(
                {
                    "manufacturer": m.get("Manufacturer"),
                    "capacity_gb": _num(m.get("Capacity"), 1.0 / (1024 ** 3)),
                    "speed_mhz": m.get("Speed"),
                    "part_number": m.get("PartNumber"),
                    "form_factor": _form_factor(m.get("FormFactor")),
                }
            )
    elif isinstance(modules, dict):
        if speed is None:
            speed = modules.get("Speed")
        mod_list.append(
            {
                "manufacturer": modules.get("Manufacturer"),
                "capacity_gb": _num(modules.get("Capacity"), 1.0 / (1024 ** 3)),
                "speed_mhz": modules.get("Speed"),
                "part_number": modules.get("PartNumber"),
                "form_factor": _form_factor(modules.get("FormFactor")),
            }
        )
    total = mem.get("total_gb") if mem else _num(
        sum(m.get("capacity_gb") or 0 for m in mod_list)
    )
    return {
        "total_gb": total,
        "used_gb": mem.get("used_gb") if mem else None,
        "used_percent": mem.get("used_percent") if mem else None,
        "speed_mhz": speed,
        "module_count": len(mod_list),
        "modules": mod_list,
    }


def _form_factor(code):
    table = {
        0: "Unknown", 1: "Other", 2: "SIP", 3: "DIP", 4: "ZIP", 5: "SOJ",
        6: "Proprietary", 7: "SIMM", 8: "DIMM", 9: "TSOP", 10: "PGA", 11: "RIMM",
        12: "SODIMM", 13: "SRIMM", 14: "SMD", 15: "SSMP", 16: "QFP", 17: "TQFP",
        18: "SOIC", 19: "LCC", 20: "PLCC", 21: "BGA", 22: "FPBGA", 23: "LGA",
    }
    return table.get(code, code)


def _collect_gpu():
    cards = _pwsh(
        "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,"
        "DriverVersion,DriverDate,VideoProcessor,CurrentHorizontalResolution,"
        "CurrentVerticalResolution,CurrentRefreshRate,VideoModeDescription"
    )
    result = []
    items = cards if isinstance(cards, list) else ([cards] if isinstance(cards, dict) else [])
    for c in items:
        ram = c.get("AdapterRAM")
        ram_gb = None
        if isinstance(ram, (int, float)) and ram > 0:
            gb = ram / (1024 ** 3)
            # AdapterRAM over 4 GiB is often truncated to 0xFFFFFFFF on 32-bit WMI.
            if gb < 3.99:
                ram_gb = round(gb, 1)
        result.append(
            {
                "name": (c.get("Name") or "").strip(),
                "adapter_ram_gb": ram_gb,
                "driver_version": c.get("DriverVersion"),
                "driver_date": _parse_date(c.get("DriverDate")),
                "video_processor": c.get("VideoProcessor"),
                "resolution": (
                    f"{c.get('CurrentHorizontalResolution')}x{c.get('CurrentVerticalResolution')}"
                    if c.get("CurrentHorizontalResolution") and c.get("CurrentVerticalResolution")
                    else c.get("VideoModeDescription")
                ),
                "refresh_hz": c.get("CurrentRefreshRate"),
            }
        )
    return result


def _collect_storage():
    phys = _pwsh(
        "Get-PhysicalDisk | Select-Object FriendlyName,MediaType,BusType,Size,"
        "HealthStatus,SerialNumber"
    )
    items = phys if isinstance(phys, list) else ([phys] if isinstance(phys, dict) else [])
    disks = []
    for d in items:
        media = d.get("MediaType")
        if media in (0, "0", None):
            media = "Unspecified"
        disks.append(
            {
                "model": d.get("FriendlyName"),
                "media_type": str(media) if media else None,
                "bus_type": d.get("BusType"),
                "size_gb": _num(d.get("Size"), 1.0 / (1024 ** 3)),
                "health_status": d.get("HealthStatus"),
                "serial": d.get("SerialNumber"),
                "temperature_c": None,
                "wear_percent": None,
            }
        )
    # Try to enrich with SMART reliability counters (temperature + wear).
    rel = _pwsh(
        "Get-PhysicalDisk | Get-StorageReliabilityCounter | "
        "Select-Object Temperature,Wear,DeviceId"
    )
    rel_items = rel if isinstance(rel, list) else ([rel] if isinstance(rel, dict) else [])
    for r in rel_items:
        if not isinstance(r, dict):
            continue
        idx = r.get("DeviceId")
        if isinstance(idx, int) and 0 <= idx < len(disks):
            disks[idx]["temperature_c"] = r.get("Temperature")
            disks[idx]["wear_percent"] = r.get("Wear")
    return disks


def _collect_partitions():
    parts = []
    if psutil is not None:
        try:
            for p in psutil.disk_partitions():
                if "cdrom" in (p.opts or "").lower() or not p.mountpoint:
                    continue
                try:
                    u = psutil.disk_usage(p.mountpoint)
                except OSError:
                    continue
                parts.append(
                    {
                        "mount": p.mountpoint,
                        "filesystem": p.fstype,
                        "total_gb": round(u.total / (1024 ** 3), 2),
                        "used_gb": round(u.used / (1024 ** 3), 2),
                        "free_gb": round(u.free / (1024 ** 3), 2),
                        "used_percent": round(u.percent, 1),
                    }
                )
        except Exception:
            parts = []
    return parts


def _decode_wmi_name(bytes_val):
    """Decode a WmiMonitorID UserFriendlyName / ManufacturerName byte array."""
    if not isinstance(bytes_val, list):
        return None
    chars = [b for b in bytes_val if b != 0]
    try:
        return bytes(chars).decode("utf-8", errors="replace").strip() or None
    except Exception:
        return None


def _collect_display(gpu_list):
    monitors = _pwsh(
        "Get-CimInstance -Namespace root\\wmi -ClassName WmiMonitorID | "
        "Select-Object InstanceName,UserFriendlyName,ManufacturerName,ProductCodeID"
    )
    items = monitors if isinstance(monitors, list) else ([monitors] if isinstance(monitors, dict) else [])
    result = []
    for m in items:
        if not isinstance(m, dict):
            continue
        result.append(
            {
                "name": _decode_wmi_name(m.get("UserFriendlyName")),
                "manufacturer": _decode_wmi_name(m.get("ManufacturerName")),
                "instance": m.get("InstanceName"),
            }
        )
    if not result and gpu_list:
        # Fall back to the active video mode of the first GPU.
        result = [{"name": gpu_list[0].get("name"), "resolution": gpu_list[0].get("resolution"),
                   "refresh_hz": gpu_list[0].get("refresh_hz")}]
    return result


def _collect_network():
    adapters = _pwsh(
        "Get-NetAdapter -Physical -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Status -ne 'Not Present' } | "
        "Select-Object Name,InterfaceDescription,MacAddress,LinkSpeed,Status"
    )
    items = adapters if isinstance(adapters, list) else ([adapters] if isinstance(adapters, dict) else [])
    result = []
    seen = set()
    for a in items:
        if not isinstance(a, dict):
            continue
        key = a.get("InterfaceDescription")
        if key in seen:
            continue
        seen.add(key)
        link = a.get("LinkSpeed")
        link_mbps = None
        if isinstance(link, (int, float)) and link > 0:
            link_mbps = int(link / 1_000_000)
        result.append(
            {
                "name": a.get("Name"),
                "description": a.get("InterfaceDescription"),
                "mac": a.get("MacAddress"),
                "link_speed_mbps": link_mbps,
                "status": a.get("Status"),
            }
        )
    return result


def _collect_drivers():
    # A bounded snapshot: total signed-driver count plus key device drivers.
    total = _first(
        _pwsh(
            "Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue | "
            "Measure-Object | Select-Object -ExpandProperty Count",
            as_json=False,
            timeout=30,
        )
    )
    key = _pwsh(
        "Get-CimInstance Win32_PnPSignedDriver | "
        "Where-Object { $_.DeviceClass -match 'DISPLAY|NET|SCSIADAPTER|HDC|SYSTEM|MEDIA' } | "
        "Select-Object DeviceName,DeviceClass,DriverVersion,DriverDate | "
        "Sort-Object DeviceClass -Unique"
    )
    key_list = []
    seen = set()
    items = key if isinstance(key, list) else ([key] if isinstance(key, dict) else [])
    for d in items:
        if not isinstance(d, dict):
            continue
        cls = d.get("DeviceClass")
        if cls in seen:
            continue
        seen.add(cls)
        key_list.append(
            {
                "device": d.get("DeviceName"),
                "class": cls,
                "driver_version": d.get("DriverVersion"),
                "driver_date": _parse_date(d.get("DriverDate")),
            }
        )
    return {
        "total_signed_count": int(total) if total and str(total).isdigit() else None,
        "key_drivers": key_list,
    }


def _collect_temperature():
    cpu = None
    gpu = None
    # CPU / thermal zone via ACPI (tenths of Kelvin).
    zones = _pwsh(
        "Get-CimInstance -Namespace root\\wmi -ClassName MSAcpi_ThermalZoneTemperature | "
        "Select-Object -ExpandProperty CurrentTemperature"
    )
    if isinstance(zones, list) and zones:
        kelvin = max(zones) / 10.0
        cpu = round(kelvin - 273.15, 1)
    elif isinstance(zones, int):
        cpu = round(zones / 10.0 - 273.15, 1)
    if psutil is not None:
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                candidates = []
                for _name, entries in temps.items():
                    for e in entries:
                        if e.current:
                            candidates.append(e.current)
                if candidates and cpu is None:
                    cpu = round(max(candidates), 1)
        except Exception:
            pass
    # GPU via nvidia-smi if present.
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, timeout=10,
        )
        if r.returncode == 0:
            line = r.stdout.decode("utf-8", errors="replace").strip().splitlines()
            if line:
                gpu = int(line[0])
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError):
        gpu = None
    return {"cpu_c": cpu, "gpu_c": gpu}


def _collect_crashes():
    bsod = 0
    try:
        dump_dir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Minidump")
        if os.path.isdir(dump_dir):
            bsod = len([f for f in os.listdir(dump_dir) if f.lower().endswith(".dmp")])
    except OSError:
        bsod = 0
    critical = _first(
        _pwsh(
            "Get-WinEvent -FilterHashtable @{LogName='System'; Level=1; "
            "StartTime=(Get-Date).AddDays(-30)} -ErrorAction SilentlyContinue | "
            "Measure-Object | Select-Object -ExpandProperty Count",
            as_json=False,
            timeout=30,
        )
    )
    kp41 = _first(
        _pwsh(
            "Get-WinEvent -FilterHashtable @{LogName='System'; Id=41; "
            "StartTime=(Get-Date).AddDays(-30)} -ErrorAction SilentlyContinue | "
            "Measure-Object | Select-Object -ExpandProperty Count",
            as_json=False,
            timeout=30,
        )
    )
    return {
        "bsod_minidump_count": bsod,
        "critical_event_count_30d": int(critical) if critical and str(critical).isdigit() else None,
        "kernel_power_41_count_30d": int(kp41) if kp41 and str(kp41).isdigit() else None,
    }


def _collect_battery():
    """Battery facts: static (powercfg) + live (root\\wmi / Win32_Battery)."""
    present = False
    design = None
    full = None
    cycles = None
    # --- powercfg /batteryreport ------------------------------------------- #
    report_path = None
    try:
        fd, report_path = tempfile.mkstemp(prefix="battery-report-", suffix=".html")
        os.close(fd)
        subprocess.run(
            ["powercfg.exe", "/batteryreport", "/output", report_path, "/duration", "1"],
            capture_output=True, timeout=40,
        )
        if os.path.exists(report_path):
            with open(report_path, "r", encoding="utf-8", errors="replace") as fh:
                html = fh.read()
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text).upper()
            m = re.search(r"DESIGN\s*CAPACITY\s*([\d,]+)\s*MWH", text)
            if m:
                design = float(m.group(1).replace(",", ""))
            m = re.search(r"FULL\s*CHARGE\s*CAPACITY\s*([\d,]+)\s*MWH", text)
            if m:
                full = float(m.group(1).replace(",", ""))
            m = re.search(r"CYCLE\s*COUNT\s*([\d,]+)", text)
            if m:
                cycles = int(m.group(1))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    finally:
        if report_path and os.path.exists(report_path):
            try:
                os.remove(report_path)
            except OSError:
                pass

    # --- Win32_Battery (root\cimv2) ---------------------------------------- #
    wb = _first(
        _pwsh(
            "Get-CimInstance Win32_Battery | Select-Object Name,Manufacturer,Chemistry,"
            "EstimatedChargeRemaining,BatteryStatus,DesignVoltage,EstimatedRunTime"
        )
        or {}
    ) or {}
    if isinstance(wb, dict) and (wb.get("Name") or wb.get("Manufacturer") or wb.get("EstimatedChargeRemaining") is not None):
        present = True

    # --- root\wmi live status ---------------------------------------------- #
    status = _first(
        _pwsh(
            "Get-CimInstance -Namespace root\\wmi -ClassName BatteryStatus | "
            "Select-Object ChargeRate,DischargeRate,RemainingCapacity,Voltage,PowerOnline"
        )
        or {}
    ) or {}
    if isinstance(status, dict) and (
        status.get("RemainingCapacity") is not None or status.get("Voltage") is not None
    ):
        present = True
    info = _first(
        _pwsh(
            "Get-CimInstance -Namespace root\\wmi -ClassName BatteryStaticData | "
            "Select-Object DesignedCapacity,CycleCount"
        )
        or {}
    ) or {}
    if isinstance(info, dict):
        if design is None and info.get("DesignedCapacity"):
            design = info["DesignedCapacity"]
        if cycles is None and info.get("CycleCount"):
            cycles = int(info["CycleCount"])

    if not present and design is None and full is None and cycles is None:
        return {"present": False}

    # Full-charge capacity fallback from BatteryFullChargedCapacity.
    if full is None:
        fcc = _first(
            _pwsh(
                "Get-CimInstance -Namespace root\\wmi -ClassName BatteryFullChargedCapacity | "
                "Select-Object -ExpandProperty FullChargedCapacity"
            )
        )
        if isinstance(fcc, (int, float)):
            full = float(fcc)

    health = None
    if design and full:
        health = round(full / design * 100.0, 1) if design > 0 else None

    charge_pct = wb.get("EstimatedChargeRemaining") if isinstance(wb, dict) else None
    if charge_pct is None and isinstance(status, dict):
        rem = status.get("RemainingCapacity")
        if rem is not None and full:
            charge_pct = round(rem / full * 100.0, 1)

    charge_rate_w = _num(status.get("ChargeRate") if isinstance(status, dict) else None, 1.0 / 1000.0)
    discharge_rate_w = _num(status.get("DischargeRate") if isinstance(status, dict) else None, 1.0 / 1000.0)
    voltage_mv = status.get("Voltage") if isinstance(status, dict) else None
    power_online = bool(status.get("PowerOnline")) if isinstance(status, dict) and status.get("PowerOnline") is not None else None

    raw_status = wb.get("BatteryStatus") if isinstance(wb, dict) else None
    # BatteryStatus: 1=discharging, 2=AC, 3=fully charged, 4=low, 5=critical,
    # 6=charging, 7=charging&high, 8=charging&low, 9=charging&critical.
    state = "unknown"
    if raw_status in (1, 4, 5):
        state = "discharging"
    elif raw_status in (2,):
        state = "on_ac"
    elif raw_status in (3,):
        state = "full"
    elif raw_status in (6, 7, 8, 9):
        state = "charging"
    elif isinstance(status, dict) and status.get("PowerOnline"):
        state = "on_ac"

    runtime_min = None
    time_to_full_min = None
    if isinstance(status, dict):
        rem = status.get("RemainingCapacity")
        if state == "discharging" and discharge_rate_w and discharge_rate_w > 0 and rem:
            runtime_min = round((rem / 1000.0) / discharge_rate_w * 60.0, 1)
        if state == "charging" and charge_rate_w and charge_rate_w > 0 and rem is not None and full:
            runtime_min = round(((full - rem) / 1000.0) / charge_rate_w * 60.0, 1)
            time_to_full_min = runtime_min

    category = _battery_category(health)

    return {
        "present": True,
        "name": wb.get("Name") if isinstance(wb, dict) else None,
        "manufacturer": wb.get("Manufacturer") if isinstance(wb, dict) else None,
        "chemistry": _BATTERY_CHEMISTRY.get(wb.get("Chemistry") if isinstance(wb, dict) else None, wb.get("Chemistry") if isinstance(wb, dict) else None),
        "design_capacity_mwh": design,
        "full_charge_capacity_mwh": full,
        "health_percent": health,
        "health_category": category,
        "cycle_count": cycles,
        "charge_percent": charge_pct,
        "status": state,
        "power_online": power_online,
        "charge_rate_w": charge_rate_w,
        "discharge_rate_w": discharge_rate_w,
        "voltage_mv": voltage_mv,
        "estimated_runtime_minutes": runtime_min,
        "estimated_time_to_full_minutes": time_to_full_min,
    }


def _battery_category(health):
    if health is None:
        return "unknown"
    if health >= 80:
        return "good"
    if health >= 60:
        return "fair"
    return "poor"


# --------------------------------------------------------------------------- #
# Scoring                                                                      #
# --------------------------------------------------------------------------- #


def _battery_score(battery):
    if not battery.get("present") or battery.get("health_percent") is None:
        return None, None
    hp = battery["health_percent"]
    score = max(0.0, min(100.0, hp))
    return round(score, 1), battery["health_category"]


def _storage_score(disks, partitions):
    if not disks:
        return None, None
    scores = []
    for d in disks:
        hs = str(d.get("health_status") or "Healthy")
        if hs in ("Healthy", "0"):
            s = 100.0
        elif hs in ("Warning", "1"):
            s = 55.0
        else:
            s = 20.0
        # Wear penalty (percentage consumed).
        wear = d.get("wear_percent")
        if isinstance(wear, (int, float)):
            s -= max(0.0, float(wear) - 10.0) * 1.2
        scores.append(s)
    base = sum(scores) / len(scores)
    # Free-space penalty across fixed partitions.
    free_pct = 100.0
    fixed = [p for p in partitions if p.get("mount") and ":" in p.get("mount", "")]
    if fixed:
        free_pct = min(p.get("free_gb", 0) / p.get("total_gb", 1) * 100 for p in fixed)
    if free_pct < 10:
        base -= (10 - free_pct) * 1.5
    score = max(0.0, min(100.0, base))
    category = _battery_category(score)  # reuse good/fair/poor bands
    return round(score, 1), category


def _temperature_score(temp):
    cpu = temp.get("cpu_c")
    if cpu is None:
        return None, None
    if cpu < 50:
        s = 100.0
    elif cpu < 70:
        s = 85.0
    elif cpu < 85:
        s = 60.0
    elif cpu < 95:
        s = 40.0
    else:
        s = 20.0
    return round(s, 1), _battery_category(s)


def _stability_score(crashes):
    bsod = crashes.get("bsod_minidump_count", 0) or 0
    kp41 = crashes.get("kernel_power_41_count_30d", 0) or 0
    crit = crashes.get("critical_event_count_30d") or 0
    events = max(bsod * 3, kp41, min(crit, 30))
    if events == 0:
        s = 100.0
    elif events <= 2:
        s = 82.0
    elif events <= 5:
        s = 62.0
    elif events <= 10:
        s = 42.0
    else:
        s = 20.0
    return round(s, 1), _battery_category(s)


def _compute_health(battery, disks, partitions, temp, crashes, lang):
    comps = {}
    weights = []
    b_score, b_cat = _battery_score(battery)
    s_score, s_cat = _storage_score(disks, partitions)
    t_score, t_cat = _temperature_score(temp)
    st_score, st_cat = _stability_score(crashes)

    if b_score is not None:
        comps["battery"] = {"score": b_score, "category": b_cat}
        weights.append(("battery", b_score, 0.30))
    if s_score is not None:
        comps["storage"] = {"score": s_score, "category": s_cat}
        weights.append(("storage", s_score, 0.25))
    if t_score is not None:
        comps["temperature"] = {"score": t_score, "category": t_cat}
        weights.append(("temperature", t_score, 0.25))
    if st_score is not None:
        comps["stability"] = {"score": st_score, "category": st_cat}
        weights.append(("stability", st_score, 0.20))

    if not weights:
        return {"score": None, "grade": None, "components": comps, "summary": None}

    total_w = sum(w for _k, _s, w in weights)
    overall = sum(s * w for _k, s, w in weights) / total_w
    overall = round(max(0.0, min(100.0, overall)), 1)
    grade = "A" if overall >= 85 else "B" if overall >= 70 else "C" if overall >= 55 else "D" if overall >= 40 else "F"

    summary = _health_summary(overall, grade, lang)
    return {"score": overall, "grade": grade, "components": comps, "summary": summary}


def _health_summary(overall, grade, lang):
    if lang == "my":
        if overall >= 85:
            return "စက်အခြေအနေ အလွန်ကောင်းသည်။"
        if overall >= 70:
            return "စက်အခြေအနေ ကောင်းသည်။"
        if overall >= 55:
            return "စက်အခြေအနေ အလယ်အလတ်ဖြစ်သည်။ ပြုပြင်ထိန်းသိမ်းရန် လိုအပ်နိုင်သည်။"
        if overall >= 40:
            return "စက်အခြေအနေ ညံ့နေသည်။ အစိတ်အပိုင်းများ စစ်ဆေးသင့်သည်။"
        return "စက်အခြေအနေ အလွန်ညံ့သည်။ ချက်ချင်းစစ်ဆေးသင့်သည်။"
    if overall >= 85:
        return "This machine is in excellent condition."
    if overall >= 70:
        return "This machine is in good condition."
    if overall >= 55:
        return "This machine is in fair condition; some maintenance may be needed."
    if overall >= 40:
        return "This machine is in poor condition; components should be checked."
    return "This machine is in very poor condition and should be inspected immediately."


def _compute_performance(cpu, memory, disks, gpu_list, lang):
    # CPU score 0-100.
    threads = cpu.get("logical_processors") or cpu.get("physical_cores") or 1
    if threads >= 12:
        cpu_s = 95.0
    elif threads >= 8:
        cpu_s = 82.0
    elif threads >= 4:
        cpu_s = 60.0
    elif threads >= 2:
        cpu_s = 40.0
    else:
        cpu_s = 22.0

    ram = memory.get("total_gb") or 0
    if ram >= 32:
        mem_s = 95.0
    elif ram >= 16:
        mem_s = 85.0
    elif ram >= 8:
        mem_s = 65.0
    elif ram >= 4:
        mem_s = 42.0
    else:
        mem_s = 20.0

    storage_s = 70.0
    if disks:
        has_ssd = any(str(d.get("media_type") or "").lower() in ("ssd", "4") for d in disks)
        storage_s = 90.0 if has_ssd else 55.0

    vram = max([g.get("adapter_ram_gb") or 0 for g in gpu_list], default=0)
    has_discrete = any(
        (g.get("name") or "").lower() not in ("microsoft basic display adapter", "microsoft basic render driver")
        and ("nvidia" in (g.get("name") or "").lower() or "amd" in (g.get("name") or "").lower()
             or "radeon" in (g.get("name") or "").lower())
        for g in gpu_list
    )
    if vram >= 8 or (has_discrete and vram >= 4):
        gpu_s = 92.0
    elif vram >= 4:
        gpu_s = 70.0
    elif has_discrete:
        gpu_s = 55.0
    else:
        gpu_s = 35.0

    overall = round(cpu_s * 0.35 + mem_s * 0.3 + storage_s * 0.2 + gpu_s * 0.15, 1)

    def band(s):
        return "good" if s >= 75 else "fair" if s >= 50 else "poor"

    office = band(cpu_s * 0.4 + mem_s * 0.4 + storage_s * 0.2)
    gaming = band(gpu_s * 0.5 + cpu_s * 0.3 + mem_s * 0.2)
    video = band(cpu_s * 0.45 + mem_s * 0.35 + storage_s * 0.2)

    notes = _perf_notes(office, gaming, video, lang)
    return {
        "cpu_score": round(cpu_s, 1),
        "memory_score": round(mem_s, 1),
        "storage_score": round(storage_s, 1),
        "gpu_score": round(gpu_s, 1),
        "overall": overall,
        "office": office,
        "gaming": gaming,
        "video_editing": video,
        "notes": notes,
    }


def _perf_notes(office, gaming, video, lang):
    if lang == "my":
        return (
            f"ရုံးအလုပ်: {_PERF_LABEL['my'][office]} · "
            f"ဂိမ်း: {_PERF_LABEL['my'][gaming]} · "
            f"ဗီဒီယိုတည်းဖြတ်: {_PERF_LABEL['my'][video]}"
        )
    return (
        f"Office work: {_PERF_LABEL['en'][office]} · "
        f"Gaming: {_PERF_LABEL['en'][gaming]} · "
        f"Video editing: {_PERF_LABEL['en'][video]}"
    )


def _build_recommendations(battery, disks, partitions, temp, crashes, lang):
    recs = []
    L = _HEALTH_LABEL.get(lang, _HEALTH_LABEL["en"])

    if battery.get("present") and battery.get("health_percent") is not None:
        hp = battery["health_percent"]
        if hp < 60:
            recs.append({"level": "critical", "area": "battery",
                         "message": _rec(lang, "battery_poor", hp)})
        elif hp < 80:
            recs.append({"level": "warning", "area": "battery",
                         "message": _rec(lang, "battery_fair", hp)})
    for p in partitions or []:
        if p.get("mount") and ":" in p.get("mount", ""):
            free_pct = p.get("free_gb", 0) / p.get("total_gb", 1) * 100
            if free_pct < 10:
                recs.append({"level": "warning", "area": "storage",
                             "message": _rec(lang, "disk_low", p.get("mount"), free_pct)})
    for d in disks or []:
        hs = str(d.get("health_status") or "Healthy")
        if hs not in ("Healthy", "0"):
            recs.append({"level": "critical", "area": "storage",
                         "message": _rec(lang, "disk_health", d.get("model"), hs)})
    cpu = temp.get("cpu_c")
    if cpu is not None and cpu >= 85:
        recs.append({"level": "warning", "area": "temperature",
                     "message": _rec(lang, "temp_high", cpu)})
    bsod = crashes.get("bsod_minidump_count", 0) or 0
    if bsod > 0:
        recs.append({"level": "warning", "area": "stability",
                     "message": _rec(lang, "bsod", bsod)})
    if not recs:
        recs.append({"level": "info", "area": "general",
                     "message": _rec(lang, "all_ok")})
    return recs


def _rec(lang, key, *args):
    texts = {
        "en": {
            "battery_poor": "Battery health is {0}% — consider replacing the battery.",
            "battery_fair": "Battery health is {0}% — it is degrading; plan for replacement.",
            "disk_low": "Drive {0} has only {1}% free space — free up space soon.",
            "disk_health": "Disk \"{0}\" reports health \"{1}\" — back up data and check the drive.",
            "temp_high": "CPU temperature is {0}°C — clean vents and check cooling.",
            "bsod": "{0} blue-screen dump(s) found — investigate recent crashes.",
            "all_ok": "No critical issues found. The machine looks healthy.",
        },
        "my": {
            "battery_poor": "ဘက်ထရီကျန်းမာရေး {0}% သာရှိသည် — ဘက်ထရီ လဲလှယ်ရန် စဉ်းစားပါ။",
            "battery_fair": "ဘက်ထရီကျန်းမာရေး {0}% ရှိသည် — ယိုယွင်းနေပြီး လဲလှယ်ရန် ပြင်ဆင်ပါ။",
            "disk_low": "Drive {0} တွင် နေရာလွတ် {1}% သာကျန်သည် — မကြာမီ ရှင်းလင်းပါ။",
            "disk_health": "Disk \"{0}\" ၏ ကျန်းမာရေး \"{1}\" ဖြစ်သည် — ဒေတာ backup လုပ်ပြီး စစ်ဆေးပါ။",
            "temp_high": "CPU အပူချိန် {0}°C ရှိသည် — လေဝင်ပေါက်များ ရှင်းလင်းပြီး အအေးပေးစနစ် စစ်ဆေးပါ။",
            "bsod": "Blue-screen dump {0} ခု တွေ့သည် — မကြာသေးမီ crash များ စစ်ဆေးပါ။",
            "all_ok": "အရေးကြီးသော ပြဿနာ မတွေ့ပါ။ စက်အခြေအနေ ကောင်းသည်။",
        },
    }
    template = texts.get(lang, texts["en"])[key]
    try:
        return template.format(*args)
    except (IndexError, KeyError):
        return template


# --------------------------------------------------------------------------- #
# Report assembly                                                              #
# --------------------------------------------------------------------------- #


def collect_all(lang: str = "en") -> dict:
    os_info = _collect_os()
    machine = _collect_machine()
    cpu = _collect_cpu()
    memory = _collect_memory()
    gpu = _collect_gpu()
    disks = _collect_storage()
    partitions = _collect_partitions()
    display = _collect_display(gpu)
    network = _collect_network()
    drivers = _collect_drivers()
    temp = _collect_temperature()
    crashes = _collect_crashes()
    battery = _collect_battery()

    health = _compute_health(battery, disks, partitions, temp, crashes, lang)
    performance = _compute_performance(cpu, memory, disks, gpu, lang)
    recommendations = _build_recommendations(battery, disks, partitions, temp, crashes, lang)

    now = datetime.now()
    return {
        "schema_version": SCHEMA_VERSION,
        "product": PRODUCT_NAME,
        "lang": lang,
        "generated_at": now.isoformat(),
        "machine": machine,
        "os": os_info,
        "cpu": cpu,
        "memory": memory,
        "gpu": gpu,
        "storage": disks,
        "partitions": partitions,
        "display": display,
        "network": network,
        "drivers": drivers,
        "temperature": temp,
        "crashes": crashes,
        "battery": battery,
        "health": health,
        "performance": performance,
        "recommendations": recommendations,
        "export": {"html": None, "pdf": None},
    }


# --------------------------------------------------------------------------- #
# HTML dashboard                                                               #
# --------------------------------------------------------------------------- #

_HTML_CSS = """
:root {
  --bg: #0f1420; --panel: #171d2e; --panel2: #1d2436; --line: #27304a;
  --text: #e7ecf5; --muted: #93a0bd; --accent: #4f8cff; --good: #34d399;
  --fair: #fbbf24; --poor: #f87171; --info: #38bdf8;
}
* { box-sizing: border-box; }
body { margin: 0; font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.5; }
.wrap { max-width: 1060px; margin: 0 auto; padding: 32px 20px 64px; }
h1 { font-size: 26px; margin: 0 0 4px; }
.sub { color: var(--muted); margin: 0 0 24px; font-size: 13px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px; }
.card h2 { font-size: 15px; margin: 0 0 14px; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: .04em; }
.gauge-row { display: flex; gap: 20px; flex-wrap: wrap; align-items: center; }
.gauge { flex: 0 0 auto; }
.big { font-size: 34px; font-weight: 700; }
.metric { margin: 0 0 10px; }
.metric .k { color: var(--muted); font-size: 12px; }
.metric .v { font-size: 16px; }
.badge { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.badge.good { background: rgba(52,211,153,.16); color: var(--good); }
.badge.fair { background: rgba(251,191,36,.16); color: var(--fair); }
.badge.poor { background: rgba(248,113,113,.16); color: var(--poor); }
.badge.info { background: rgba(56,189,248,.16); color: var(--info); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; }
.bar { height: 10px; background: var(--panel2); border-radius: 999px; overflow: hidden; }
.bar > span { display: block; height: 100%; border-radius: 999px; }
.recs { list-style: none; padding: 0; margin: 0; }
.recs li { padding: 10px 12px; border-radius: 10px; margin-bottom: 8px; border: 1px solid var(--line); }
.recs li.critical { border-color: var(--poor); background: rgba(248,113,113,.08); }
.recs li.warning { border-color: var(--fair); background: rgba(251,191,36,.08); }
.recs li.info { border-color: var(--info); background: rgba(56,189,248,.08); }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 640px) { .two { grid-template-columns: 1fr; } }
"""


def _svg_gauge(value, label, color):
    # SVG semicircle gauge, value 0-100.
    v = max(0, min(100, value or 0))
    angle = -180 + (v / 100.0) * 180.0
    from math import cos, sin, pi, radians
    cx, cy, r = 70, 70, 54
    x = cx + r * cos(radians(angle))
    y = cy + r * sin(radians(angle))
    large = 1 if angle <= -90 else 0
    d = f"M {cx + r * cos(radians(-180))} {cy + r * sin(radians(-180))} A {r} {r} 0 {large} 1 {x} {y}"
    return f"""<svg viewBox="0 0 140 84" width="150" role="img" aria-label="{label}: {value}">
  <path d="{d}" fill="none" stroke="#27304a" stroke-width="12" stroke-linecap="round"/>
  <path d="{d}" fill="none" stroke="{color}" stroke-width="12" stroke-linecap="round"/>
  <text x="70" y="70" text-anchor="middle" fill="#e7ecf5" font-size="26" font-weight="700">{int(round(value))}</text>
</svg>"""


def _html_escape(text):
    if text is None:
        return ""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _fmt(value):
    if value is None:
        return "—"
    return str(value)


def _category_color(cat):
    return {"good": "var(--good)", "fair": "var(--fair)", "poor": "var(--poor)"}.get(cat, "var(--info)")


def render_html(report: dict) -> str:
    L = _HEALTH_LABEL.get(report.get("lang", "en"), _HEALTH_LABEL["en"])
    machine = report.get("machine", {})
    os_info = report.get("os", {})
    cpu = report.get("cpu", {})
    memory = report.get("memory", {})
    gpu = report.get("gpu", [])
    disks = report.get("storage", [])
    parts = report.get("partitions", [])
    display = report.get("display", [])
    net = report.get("network", [])
    battery = report.get("battery", {})
    health = report.get("health", {})
    perf = report.get("performance", {})
    recs = report.get("recommendations", [])
    drivers = report.get("drivers", {})
    crashes = report.get("crashes", {})

    title = f"{machine.get('manufacturer') or ''} {machine.get('model') or machine.get('hostname') or 'Windows Laptop'}".strip()

    gauges = ""
    if battery.get("present") and battery.get("health_percent") is not None:
        gauges += f"""<div class="metric"><div class="k">Battery health</div><div class="gauge-row">
          {_svg_gauge(battery['health_percent'], 'battery', _category_color(battery.get('health_category')))}
          <div><div class="big" style="color:{_category_color(battery.get('health_category'))}">{battery['health_percent']}%</div>
          <span class="badge {battery.get('health_category') or 'info'}">{L.get(battery.get('health_category'), battery.get('health_category'))}</span></div>
        </div></div>"""
    if health.get("score") is not None:
        gauges += f"""<div class="metric"><div class="k">Health score</div><div class="gauge-row">
          {_svg_gauge(health['score'], 'health', _category_color(_battery_category(health['score'])))}
          <div><div class="big" style="color:{_category_color(_battery_category(health['score']))}">{health['score']}</div>
          <span class="badge info">Grade {health.get('grade')}</span></div>
        </div></div>"""

    # Component health bars.
    comp_rows = ""
    comps = health.get("components", {}) or {}
    for name, c in comps.items():
        color = _category_color(c.get("category"))
        comp_rows += f"""<tr><td>{name.capitalize()}</td><td>{_fmt(c.get('score'))}</td>
        <td><div class="bar"><span style="width:{c.get('score', 0)}%;background:{color}"></span></div></td>
        <td><span class="badge {c.get('category') or 'info'}">{L.get(c.get('category'), c.get('category'))}</span></td></tr>"""

    perf_rows = ""
    for key in ("office", "gaming", "video_editing"):
        label = key.replace("_", " ").capitalize()
        val = perf.get(key)
        perf_rows += f"""<tr><td>{label}</td><td><span class="badge {val or 'info'}">{L.get(val, val) if val else '—'}</span></td></tr>"""

    gpu_rows = ""
    for g in gpu:
        gpu_rows += f"""<tr><td>{_html_escape(g.get('name'))}</td><td>{_fmt(g.get('adapter_ram_gb'))}</td><td>{_fmt(g.get('driver_version'))}</td></tr>"""
    if not gpu_rows:
        gpu_rows = "<tr><td colspan=3>No GPU detected</td></tr>"

    disk_rows = ""
    for d in disks:
        disk_rows += f"""<tr><td>{_html_escape(d.get('model'))}</td><td>{_fmt(d.get('media_type'))}</td>
        <td>{_fmt(d.get('size_gb'))} GB</td><td>{_fmt(d.get('health_status'))}</td></tr>"""
    if not disk_rows:
        disk_rows = "<tr><td colspan=4>No storage detected</td></tr>"

    part_rows = ""
    for p in parts:
        part_rows += f"""<tr><td>{p.get('mount')}</td><td>{p.get('filesystem')}</td><td>{p.get('total_gb')}</td>
        <td>{p.get('free_gb')}</td><td>{p.get('used_percent')}%</td></tr>"""
    if not part_rows:
        part_rows = "<tr><td colspan=5>No partitions detected</td></tr>"

    net_rows = ""
    for n in net:
        net_rows += f"""<tr><td>{_html_escape(n.get('name'))}</td><td>{_fmt(n.get('link_speed_mbps'))}</td><td>{_fmt(n.get('status'))}</td><td>{_fmt(n.get('mac'))}</td></tr>"""
    if not net_rows:
        net_rows = "<tr><td colspan=4>No physical network adapters detected</td></tr>"

    driver_rows = ""
    for d in drivers.get("key_drivers", []):
        driver_rows += f"""<tr><td>{_html_escape(d.get('device'))}</td><td>{_fmt(d.get('class'))}</td><td>{_fmt(d.get('driver_version'))}</td></tr>"""
    if not driver_rows:
        driver_rows = "<tr><td colspan=3>No driver details</td></tr>"

    rec_html = ""
    for r in recs:
        rec_html += f"""<li class="{r.get('level', 'info')}"><strong>{r.get('area', '').capitalize()}:</strong> {_html_escape(r.get('message'))}</li>"""

    return f"""<!doctype html>
<html lang="{report.get('lang', 'en')}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html_escape(title)} — {PRODUCT_NAME}</title><style>{_HTML_CSS}</style></head>
<body><div class="wrap">
<h1>{_html_escape(title)}</h1>
<p class="sub">{_html_escape(os_info.get('product_name'))} · Build {_fmt(os_info.get('build'))} · {_html_escape(os_info.get('architecture'))} · Generated {_fmt(report.get('generated_at'))}</p>

<div class="grid">
  <div class="card"><h2>Health overview</h2>{gauges}
    <table>{comp_rows}</table>
  </div>
  <div class="card"><h2>Battery</h2>
    <table>
      <tr><th>Design capacity</th><td>{_fmt(battery.get('design_capacity_mwh'))} mWh</td></tr>
      <tr><th>Full-charge capacity</th><td>{_fmt(battery.get('full_charge_capacity_mwh'))} mWh</td></tr>
      <tr><th>Health</th><td>{_fmt(battery.get('health_percent'))}%</td></tr>
      <tr><th>Cycle count</th><td>{_fmt(battery.get('cycle_count'))}</td></tr>
      <tr><th>Charge level</th><td>{_fmt(battery.get('charge_percent'))}%</td></tr>
      <tr><th>Status</th><td>{_fmt(battery.get('status'))}</td></tr>
      <tr><th>Charge rate</th><td>{_fmt(battery.get('charge_rate_w'))} W</td></tr>
      <tr><th>Discharge rate</th><td>{_fmt(battery.get('discharge_rate_w'))} W</td></tr>
      <tr><th>Est. runtime</th><td>{_fmt(battery.get('estimated_runtime_minutes'))} min</td></tr>
    </table>
  </div>
</div>

<div class="grid" style="margin-top:16px">
  <div class="card"><h2>Processor</h2>
    <div class="metric"><div class="k">Name</div><div class="v">{_html_escape(cpu.get('name'))}</div></div>
    <div class="metric"><div class="k">Cores / threads</div><div class="v">{_fmt(cpu.get('physical_cores'))} / {_fmt(cpu.get('logical_processors'))}</div></div>
    <div class="metric"><div class="k">Max clock</div><div class="v">{_fmt(cpu.get('max_clock_mhz'))} MHz</div></div>
    <div class="metric"><div class="k">Current usage</div><div class="v">{_fmt(cpu.get('current_usage_percent'))}%</div></div>
  </div>
  <div class="card"><h2>Memory</h2>
    <div class="metric"><div class="k">Total</div><div class="v">{_fmt(memory.get('total_gb'))} GB</div></div>
    <div class="metric"><div class="k">In use</div><div class="v">{_fmt(memory.get('used_percent'))}%</div></div>
    <div class="metric"><div class="k">Speed</div><div class="v">{_fmt(memory.get('speed_mhz'))} MHz</div></div>
    <div class="metric"><div class="k">Modules</div><div class="v">{_fmt(memory.get('module_count'))}</div></div>
  </div>
</div>

<div class="grid" style="margin-top:16px">
  <div class="card"><h2>Graphics (GPU)</h2><table><tr><th>Name</th><th>VRAM</th><th>Driver</th></tr>{gpu_rows}</table></div>
  <div class="card"><h2>Storage</h2><table><tr><th>Model</th><th>Type</th><th>Size</th><th>Health</th></tr>{disk_rows}</table></div>
</div>

<div class="grid" style="margin-top:16px">
  <div class="card"><h2>Partitions</h2><table><tr><th>Drive</th><th>FS</th><th>Total GB</th><th>Free GB</th><th>Used</th></tr>{part_rows}</table></div>
  <div class="card"><h2>Performance estimate</h2><table><tr><th>Task</th><th>Rating</th></tr>{perf_rows}</table>
    <div class="metric" style="margin-top:10px"><div class="k">Overall</div><div class="v">{_fmt(perf.get('overall'))}/100</div></div>
  </div>
</div>

<div class="grid" style="margin-top:16px">
  <div class="card"><h2>Network adapters</h2><table><tr><th>Name</th><th>Link Mbps</th><th>Status</th><th>MAC</th></tr>{net_rows}</table></div>
  <div class="card"><h2>Drivers &amp; stability</h2>
    <table><tr><th>Device</th><th>Class</th><th>Driver</th></tr>{driver_rows}</table>
    <div class="metric" style="margin-top:10px"><div class="k">Blue-screen dumps</div><div class="v">{_fmt(crashes.get('bsod_minidump_count'))}</div></div>
    <div class="metric"><div class="k">Critical events (30d)</div><div class="v">{_fmt(crashes.get('critical_event_count_30d'))}</div></div>
  </div>
</div>

<div class="card" style="margin-top:16px"><h2>Recommendations</h2><ul class="recs">{rec_html}</ul></div>
<p class="sub" style="margin-top:24px">Generated by {PRODUCT_NAME} · schema v{report.get('schema_version')}</p>
</div></body></html>"""


# --------------------------------------------------------------------------- #
# Minimal PDF writer (Courier, standard 14 fonts)                              #
# --------------------------------------------------------------------------- #


class _Pdf:
    def __init__(self):
        self.pages = []
        self._buf = None

    def _page(self):
        return {"content": []}

    def add_page(self):
        self.pages.append(self._page())

    def text(self, page, x, y, size, s, bold=False):
        page["content"].append((x, y, size, s, bold))

    def rect(self, page, x, y, w, h):
        page["content"].append(("rect", x, y, w, h))

    def save(self, path):
        out = bytearray()
        out += b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
        n_pages = len(self.pages)
        objects = []
        # Object ids: 1 catalog, 2 pages, 3 font, then per-page: content, page.
        font_id = 3
        content_start = 4
        # Build objects.
        for _ in self.pages:
            objects.append(None)  # placeholder for content
            objects.append(None)  # placeholder for page
        # We'll assign ids as we serialize.
        offsets = {}

        def add_obj(obj_id, body):
            nonlocal out
            offsets[obj_id] = len(out)
            out += f"{obj_id} 0 obj\n".encode()
            out += body
            out += b"\nendobj\n"

        # Font.
        add_obj(font_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")

        # Pages.
        kids = " ".join(f"{content_start + 1 + i * 2} 0 R" for i in range(n_pages))
        add_obj(2, f"<< /Type /Pages /Kids [ {kids} ] /Count {n_pages} >>".encode())

        # Content streams + pages.
        for i, page in enumerate(self.pages):
            stream = self._render_page(page)
            content_id = content_start + i * 2
            page_id = content_id + 1
            add_obj(content_id, f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"endstream")
            add_obj(page_id, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>".encode())

        add_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")

        xref_pos = len(out)
        out += f"xref\n0 {len(objects) + 1}\n".encode()
        out += b"0000000000 65535 f \n"
        for i in range(1, len(objects) + 1):
            off = offsets.get(i, 0)
            out += f"{off:010d} 00000 n \n".encode()
        out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
        with open(path, "wb") as fh:
            fh.write(out)

    def _render_page(self, page):
        ops = []
        # 0.6 * font size is the exact Courier advance width.
        for item in page["content"]:
            if item[0] == "rect":
                _t, x, y, w, h = item
                ops.append(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re f".encode())
            else:
                x, y, size, s, bold = item
                text = s.encode("latin-1", errors="replace").decode("latin-1")
                ops.append(b"BT /F1 " + f"{size:.1f}".encode() + b" Tf " +
                           f"{x:.2f} {y:.2f}".encode() + b" Td (" + _pdf_escape(text).encode("latin-1") + b") Tj ET")
        return b"\n".join(ops)


def _pdf_escape(s):
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def render_pdf(report: dict, path: str):
    L = _HEALTH_LABEL.get(report.get("lang", "en"), _HEALTH_LABEL["en"])
    pdf = _Pdf()
    pdf.add_page()
    pg = pdf.pages[0]
    y = 770
    machine = report.get("machine", {})
    os_info = report.get("os", {})
    cpu = report.get("cpu", {})
    memory = report.get("memory", {})
    battery = report.get("battery", {})
    health = report.get("health", {})
    perf = report.get("performance", {})
    recs = report.get("recommendations", [])

    def line(text, size=9):
        nonlocal y
        pdf.text(pg, 40, y, size, text)
        y -= size + 4

    def header(text):
        nonlocal y
        y -= 6
        pdf.text(pg, 40, y, 11, text, bold=True)
        y -= 14

    title = f"{machine.get('manufacturer') or ''} {machine.get('model') or machine.get('hostname') or 'Windows Laptop'}".strip()
    pdf.text(pg, 40, y, 16, f"{PRODUCT_NAME}", bold=True)
    y -= 22
    line(f"{title}  |  {os_info.get('product_name') or ''}  Build {os_info.get('build') or '?'}")
    line(f"Generated {report.get('generated_at') or ''}")
    y -= 10

    header("HEALTH")
    line(f"Overall health score: {health.get('score') if health.get('score') is not None else 'n/a'} / 100  (grade {health.get('grade') or '?'})")
    for name, c in (health.get("components") or {}).items():
        line(f"  {name.capitalize():<14} {c.get('score')}  ({L.get(c.get('category'), c.get('category'))})")
    y -= 4

    header("BATTERY")
    if battery.get("present"):
        line(f"  Design capacity      : {battery.get('design_capacity_mwh')} mWh")
        line(f"  Full-charge capacity : {battery.get('full_charge_capacity_mwh')} mWh")
        line(f"  Health               : {battery.get('health_percent')}%  ({L.get(battery.get('health_category'), '')})")
        line(f"  Cycle count          : {battery.get('cycle_count')}")
        line(f"  Charge level         : {battery.get('charge_percent')}%")
        line(f"  Status               : {battery.get('status')}")
        line(f"  Charge / discharge   : {battery.get('charge_rate_w')} W / {battery.get('discharge_rate_w')} W")
        line(f"  Estimated runtime    : {battery.get('estimated_runtime_minutes')} min")
    else:
        line("  No battery detected")
    y -= 4

    header("SYSTEM")
    line(f"  CPU   : {cpu.get('name') or '?'} ({cpu.get('physical_cores')}C/{cpu.get('logical_processors')}T @ {cpu.get('max_clock_mhz')} MHz)")
    line(f"  RAM   : {memory.get('total_gb')} GB  (used {memory.get('used_percent')}%)")
    for g in report.get("gpu", []):
        line(f"  GPU   : {g.get('name')}  ({g.get('adapter_ram_gb')} GB VRAM)")
    for d in report.get("storage", []):
        line(f"  Disk  : {d.get('model')}  {d.get('media_type')}  {d.get('size_gb')} GB  {d.get('health_status')}")
    y -= 4

    header("PERFORMANCE ESTIMATE")
    line(f"  Office work    : {L.get(perf.get('office'), '')}")
    line(f"  Gaming         : {L.get(perf.get('gaming'), '')}")
    line(f"  Video editing  : {L.get(perf.get('video_editing'), '')}")
    y -= 4

    header("RECOMMENDATIONS")
    for r in recs:
        line(f"  [{r.get('level', 'info').upper()}] {r.get('message') or ''}")

    # Simple bar chart for component scores.
    y -= 16
    pdf.text(pg, 40, y, 11, "Component health", bold=True)
    y -= 20
    for name, c in (health.get("components") or {}).items():
        score = max(0, min(100, c.get("score", 0)))
        pdf.text(pg, 40, y + 2, 8, f"{name.capitalize()}")
        pdf.rect(pg, 140, y, 180, 9)
        pdf.rect(pg, 140, y, 180 * score / 100.0, 9)
        pdf.text(pg, 330, y + 2, 8, f"{c.get('score')}")
        y -= 16

    pdf.save(path)


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def _default_export_path(fmt: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(os.getcwd(), f"windows-laptop-inspector-{stamp}.{fmt}")


def _emit_json(report):
    """Print the report JSON, tolerating a windowed EXE with no stdout."""
    if sys.stdout is None:
        return
    try:
        print(json.dumps(report, ensure_ascii=False, default=str))
    except Exception:
        pass


def main(argv=None):
    # Emit UTF-8 regardless of the console codepage so the Host tool and any
    # terminal read non-ASCII labels (—, ·, Burmese) correctly.
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    fmt = None
    fmt_explicit = False
    output = None
    lang = "en"
    open_report = False
    no_open = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--format", "-f") and i + 1 < len(argv):
            fmt = argv[i + 1].lower()
            fmt_explicit = True
            i += 2
        elif a in ("--output", "-o") and i + 1 < len(argv):
            output = argv[i + 1]
            i += 2
        elif a in ("--lang", "-l") and i + 1 < len(argv):
            lang = argv[i + 1].lower()
            i += 2
        elif a in ("--open",):
            open_report = True
            i += 1
        elif a in ("--no-open",):
            no_open = True
            i += 1
        else:
            i += 1
    if lang not in ("en", "my"):
        lang = "en"

    # Defaults: piped output (scripts / the DSH tool) returns JSON; an
    # interactive run (double-clicking the EXE or a bare `python inspector.py`)
    # builds an HTML dashboard and opens it in the browser.
    if not fmt_explicit:
        # A console (double-clicked console EXE or a terminal) and a windowed
        # EXE (sys.stdout is None) both produce an HTML dashboard; a piped
        # invocation (scripts / the DSH tool) returns JSON.
        if sys.stdout is None or sys.stdout.isatty():
            fmt = "html"
            open_report = True
        else:
            fmt = "json"
    if fmt not in ("json", "html", "pdf"):
        fmt = "json"

    report = collect_all(lang)

    if fmt == "html":
        html = render_html(report)
        path = output or _default_export_path("html")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(html)
            report["export"]["html"] = path
        except OSError as exc:
            report["export"]["html"] = None
            report["export_error"] = str(exc)
    elif fmt == "pdf":
        path = output or _default_export_path("pdf")
        try:
            render_pdf(report, path)
            report["export"]["pdf"] = path
        except OSError as exc:
            report["export"]["pdf"] = None
            report["export_error"] = str(exc)
    elif fmt == "json" and output:
        try:
            with open(output, "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
            report["export"]["json"] = output
        except OSError as exc:
            report["export"]["json"] = None
            report["export_error"] = str(exc)

    _emit_json(report)

    if open_report and not no_open and report.get("export", {}).get("html"):
        try:
            webbrowser.open(report["export"]["html"])
        except Exception:
            pass
        # Keep the console open so a double-clicked EXE shows the summary.
        if sys.stdout is not None and sys.stdout.isatty():
            try:
                input("\nReport opened in your browser. Press Enter to close…")
            except (EOFError, KeyboardInterrupt):
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
