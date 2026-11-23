# Windows Laptop Inspector

Inspect a Windows laptop's hardware, software, battery health, and overall condition, then export the result as JSON, HTML, or PDF.

## What it inspects

- **Hardware** — CPU, memory, GPU, storage (SSD/HDD media type and SMART health), display, and physical network adapters.
- **Software / OS** — Windows edition, version, build, architecture, BIOS, and key device drivers.
- **Battery** — design vs. full-charge capacity (from `powercfg /batteryreport`), cycle count, live charge/discharge rate (watts), and estimated runtime (from WMI `root\wmi`).
- **Health** — a 0–100 score combining battery health, storage health, temperature, and crash history.
- **Performance** — an estimate of how well the machine handles office work, gaming, and video editing.

## How to use

1. Ask the agent to "inspect this laptop". It calls the `inspect_windows_laptop` tool.
2. The tool returns a report plus a summary. To get a file, ask for an export:

   - **HTML** — an interactive dashboard with gauges and bar charts: `inspect_windows_laptop` with `format: html`.
   - **PDF** — a printable report: `format: pdf`.
   - **JSON** — the raw structured report: `format: json` with an `output_path`.

## Requirements

- Windows only.
- The engine uses PowerShell, WMI/CIM, and `powercfg` (all built in). The `psutil` Python library is used when available and is optional; the engine degrades gracefully without it.
- Python 3 must be available on `PATH`. Override with the `WINDOWS_LAPTOP_INSPECTOR_PYTHON` environment variable if needed.

## Notes

- Battery capacity is read from `powercfg /batteryreport`; on machines where that report is unavailable, the engine falls back to WMI and reports fewer battery fields.
- Temperature readings depend on the hardware exposing ACPI thermal zones or an NVIDIA driver; they may be unavailable on some laptops.
