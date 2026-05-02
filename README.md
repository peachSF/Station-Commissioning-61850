# IEC 61850 GOOSE Simulator
### IED Interoperability Test Device

A Python-based IED (Intelligent Electronic Device) interoperability test tool developed during an internship at the **Provincial Electricity Authority (PEA), Substation Equipment Testing Division**.

This tool simulates and publishes **GOOSE (Generic Object Oriented Substation Event)** messages compliant with **IEC 61850 Edition 2** standards, enabling functional interoperability testing between IEDs in a software-defined lab environment — without requiring live substation hardware.

---

## 📁 Project Structure

```
iec61850-goose-simulator/
├── install_libiec61850.sh    # libiec61850 build & install script
├── iec61850_api.txt          # pyiec61850 API reference
└── code/
    ├── UI.py                     # Main Kivy application & all screen/popup logic
    ├── main_ui.kv                # Kivy language layout file
    ├── datacollect.py            # SCD/IID XML parser → JSON output
    ├── cb_monitor.py             # MMS-based circuit breaker status monitor
    ├── goose_manager.py          # GOOSE publisher with retransmission engine
    └── Icon/                     # UI icon assets
        ├── cb_closed.png
        ├── cb_open.png
        ├── cb_unknown.png
        ├── gear.png
        └── exclamation_mark.png
```

---

## ✨ Features

### GOOSE Publishing (`goose_manager.py`)
- Publishes GOOSE frames via **pyiec61850** `GoosePublisher`
- Implements **IEC 61850 retransmission sequence**:
  - Sends immediately on event → retransmits at `MinTime` → doubles interval → caps at `MaxTime`
- Increments `stNum` on dataset value change, `sqNum` on each retransmission
- Supports **simulation bit** (`GoosePublisher_setSimulation`)
- Automatic **VLAN interface creation** (`eth0.{vlan_id}`) via `ip link`
- **File watcher** polling JSON every 0.5s — triggers new retransmission sequence on value change
- **Dry-run mode** when `pyiec61850` is unavailable (for UI development/testing)

### MMS Circuit Breaker Monitoring (`cb_monitor.py`)
- Polls `XCBR.Pos.stVal` via MMS on port **102**
- **Thread-per-IED** architecture — one TCP connection per IED, shared across multiple XCBRs
- Auto-reconnect on connection loss
- Maps DBPOS values: `0=intermediate`, `1=off`, `2=on`, `3=bad`
- Fires callback on status change only (change-of-value, no redundant updates)
- Dry-run mode with toggle simulation every 5s

### SCD/XML Parsing (`datacollect.py`)
- Parses **SCD** (`.scd`) and **IID** (`.iid`) files using `lxml`
- Extracts and resolves:
  - IED communication parameters (IP, APPID, VLAN-ID, MAC, MinTime, MaxTime)
  - GOOSE Control Blocks (GCB) and Sampled Value Control Blocks (SV)
  - DataSets with FCDA entries
  - Logical Nodes, Data Objects, Data Attributes with type resolution
  - ExtRef / Inputs for IED subscriptions
- Multi-level **DataType resolution** — recursively resolves DOType → DAType → BDA chains
- Outputs structured **JSON** per IED for consumption by GOOSE and MMS modules

### Kivy GUI (`UI.py` + `main_ui.kv`)
- Fullscreen Kivy application
- **USB device detection** via `pyudev` — auto-loads SCD from USB
- Live **CB status icons** updating from MMS callbacks (`on`/`off`/`intermediate`/`bad`)
- **GearButton** with modification badge (exclamation icon) indicating unsaved parameter changes
- Interactive dataset value editing (Boolean toggles, numeric inputs, quality/timestamp fields)
- Multi-window configuration popups:

| Popup | Purpose |
|---|---|
| `CommConfigPopup` | Configure MAC, APPID, VLAN-ID per GCB |
| `TransmissConfigPopup` | Set MinTime / MaxTime retransmission timing |
| `SimulationConfirmPopup` | Enable/disable simulation bit before publishing |

---

## 🛠️ Tech Stack

| Component | Technology |
|---|---|
| Language | Python 3.8+ |
| GUI Framework | Kivy 2.x |
| IEC 61850 Library | pyiec61850 (built from libiec61850 v1.6) |
| XML Parsing | lxml |
| USB Detection | pyudev |
| System Info | psutil |
| Native Library | libiec61850 + mbedTLS 3.6.0 |
| Protocol | IEC 61850 Ed.2 — GOOSE, MMS (port 102) |

---

## ⚙️ Installation

### 1. Clone the repository
```bash
git clone https://github.com/[your-username]/iec61850-goose-simulator.git
cd iec61850-goose-simulator
```

### 2. Build and install libiec61850
```bash
chmod +x install_libiec61850.sh
./install_libiec61850.sh
```

This script will:
- Clone `libiec61850` from GitHub
- Download mbedTLS 3.6.0 dependency
- Compile with Python bindings (`-DBUILD_PYTHON_BINDINGS=ON`)
- Install system-wide and update `ld` cache

### 3. Install Python dependencies
```bash
pip install kivy lxml psutil pyudev
```

### 4. Grant network capability (required for raw socket GOOSE publishing)
```bash
sudo setcap cap_net_raw+eip $(readlink -f .venv/bin/python3)
```

> ⚠️ Without this, `GoosePublisher_create` will fail with a permission error.

### 5. Run the application
```bash
python UI.py
```

---

## 🚀 How to Use

1. **Load SCD file** — Insert USB containing `.scd` or `.iid` file, or load manually
2. **Select IEDs / LNs** — Choose Logical Nodes (XCBR) to publish and monitor
3. **Configure communication** — Tap ⚙️ to open `CommConfigPopup` and set APPID, VLAN-ID, MAC per GCB
4. **Configure timing** — Set `MinTime` and `MaxTime` for retransmission via `TransmissConfigPopup`
5. **Set simulation mode** — Enable simulation bit via `SimulationConfirmPopup` if needed
6. **Start publishing** — Press START to begin GOOSE transmission
7. **Monitor CB status** — CB icons update live from MMS polling

---

## 📡 GOOSE Parameters

| Parameter | Description |
|---|---|
| **APPID** | Application identifier in GOOSE Ethernet frame header |
| **MAC** | Multicast destination MAC address (e.g., `01:0C:CD:01:00:00`) |
| **VLAN-ID** | Virtual LAN identifier — auto-creates `eth0.{id}` interface |
| **VLAN-Priority** | 802.1Q priority bits |
| **GoCBRef** | GOOSE Control Block reference (`{IED}{LD}/LLN0$GO${CB}`) |
| **DataSetRef** | Dataset reference path |
| **ConfRev** | Configuration revision number |
| **stNum** | State number — increments on dataset value change |
| **sqNum** | Sequence number — increments on each retransmission |
| **MinTime** | Minimum retransmission interval (ms) |
| **MaxTime** | Maximum retransmission interval (ms) |
| **Simulation** | Sets simulation bit in GOOSE header |

---

## 🔄 Retransmission Sequence

```
Event detected
    │
    ▼
Send immediately (sqNum=0)
    │
    ▼
Wait MinTime ms → Send (sqNum=1)
    │
    ▼
Wait MinTime×2 ms → Send (sqNum=2)
    │
    ▼
Wait MinTime×4 ms → Send (sqNum=3)
    │
    ▼
    ... (doubles each time)
    │
    ▼
Wait MaxTime ms → Send  ←── stays here until STOP or next event
```

---

## 📋 Requirements

```
Python          3.8+
Kivy            2.x
lxml            latest
psutil          latest
pyudev          latest
pyiec61850      built from libiec61850 v1.6
libiec61850     v1.6 (with mbedTLS 3.6.0)
OS              Linux (Ubuntu recommended)
Network         Ethernet interface (eth0) with raw socket capability
```

---

## 📄 Related Standards

| Standard | Description |
|---|---|
| IEC 61850-6 | SCD file format (Substation Configuration Description) |
| IEC 61850-7-2 | MMS (Manufacturing Message Specification) |
| IEC 61850-7-4 | Compatible Logical Node classes and data object classes |
| IEC 61850-8-1 | GOOSE message specification |
| IEC 61850-9-2 | Sampled Values (SV) communication |

---

## 🏢 Project Background

Developed during an internship at:

> **Provincial Electricity Authority (PEA)**  
> Substation Equipment Testing Division  
> May 2025 – September 2025

**Objective:** Enable engineers to validate IED-to-IED GOOSE communication and protection scheme behavior in a controlled software-defined lab environment, reducing reliance on live substation hardware during pre-commissioning testing phases.

---

## 👤 Author

**Purinat Saereewattana**  
Electrical and Automation Engineering Technology  
King Mongkut's University of Technology North Bangkok — Rayong Campus  
📧 peach4434@gmail.com
