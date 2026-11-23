# Windows Laptop Inspector

Inspect a Windows laptop's hardware, software, battery health, and overall condition — then export the result as JSON, HTML, or PDF. Available as a standalone Windows EXE, a Python CLI, and a [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) plugin.

## Features

- **Hardware** — CPU, memory, GPU, storage (SSD/HDD media type + SMART health), display, and physical network adapters.
- **Software / OS** — Windows edition, version, build, architecture, BIOS, and key device drivers.
- **Battery** — design vs. full-charge capacity (from `powercfg /batteryreport`), cycle count, live charge/discharge rate in watts, and estimated runtime (from WMI `root\wmi`).
- **Health score** — a 0–100 score (battery + storage + temperature + crash history) with an A–F grade.
- **Performance estimate** — how well the machine handles office work, gaming, and video editing.
- **Recommendations** — plain-language advice in English or Burmese (`--lang my`).
- **Export** — JSON (structured), HTML (interactive dashboard with gauges + bar charts), and PDF (printable).

## Get it

### Option 1 — Windows EXE (no Python required)

Download `WindowsLaptopInspector.exe` from the [latest release](../../releases/latest), then either:

- **Double-click it** — it inspects the machine, writes an HTML dashboard, and opens it in your browser.
- **Run it from a terminal:**

  ```powershell
  WindowsLaptopInspector.exe --format html
  WindowsLaptopInspector.exe --format pdf -o report.pdf
  WindowsLaptopInspector.exe --format json
  WindowsLaptopInspector.exe --lang my
  ```

> SmartScreen may warn about an unsigned executable the first time. Click **More info → Run anyway**.

### Option 2 — Python

Requires Python 3.8+. `psutil` is optional (used when available; the engine otherwise falls back to pure PowerShell/WMI).

```powershell
python inspector.py                    # JSON report
python inspector.py --format html      # HTML dashboard
python inspector.py --format pdf -o report.pdf
python inspector.py --lang my          # Burmese text
```

### Option 3 — DeepSeek Harness plugin

This repository is also a DSH bundle. Install the local directory from **Plugins → Add plugin**, then ask the agent to *"inspect this laptop"*. See [docs/DETAIL.md](docs/DETAIL.md).

## Build the EXE yourself

```powershell
python -m pip install pyinstaller
python -m PyInstaller --onefile --console --name WindowsLaptopInspector --clean inspector.py
```

The output is `dist\WindowsLaptopInspector.exe`.

## How it works

The engine (`inspector.py`) shells out to PowerShell, WMI/CIM, and `powercfg` (all built into Windows) and optionally uses `psutil`. Battery capacity and cycle count come from `powercfg /batteryreport`; live charge/discharge rates and voltage come from the WMI `root\wmi` namespace.

| File | Purpose |
|---|---|
| `inspector.py` | Standalone Python inspection engine. |
| `index.js` | DSH Host plugin — registers the `inspect_windows_laptop` agent tool. |
| `client.js` | DSH Client plugin — a composer-dock entry for discoverability. |
| `cordis.patch.yml` | DSH bundle patch. |
| `package.json` | DSH bundle manifest. |
| `locale/`, `docs/`, `icon.svg` | DSH plugin metadata, detail page, and icon. |

## Notes & limitations

- Windows only.
- Battery facts rely on `powercfg /batteryreport`; when that report is unavailable the engine falls back to WMI and reports fewer battery fields.
- Temperature depends on the hardware exposing ACPI thermal zones or an NVIDIA driver; it may be unavailable on some laptops.
- The EXE is unsigned, so Windows SmartScreen may prompt on first run.

## License

[MIT](LICENSE)
