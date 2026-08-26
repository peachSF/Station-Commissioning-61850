# ══════════════════════════════════════════════════════════════════════════════
# goose_manager.py
# ══════════════════════════════════════════════════════════════════════════════

import json
import os
import subprocess
import logging
from dataclasses import dataclass, field

from kivy.clock import Clock

try:
    import pyiec61850 as iec
    IEC_AVAILABLE = True
except ImportError:
    IEC_AVAILABLE = False
    logging.warning("GooseManager: pyiec61850 ไม่พบ — ทำงานในโหมด DRY RUN")

NETWORK_IFACE  = "eth0"
WATCH_INTERVAL = 0.5   # ตรวจ JSON เปลี่ยนแปลงทุกกี่วินาที
JSON_DIR       = "/home/developer/Desktop/SC61850/Json_File"

# ตัวคูณของ unit prefix ที่มาจาก LNAttributePopup (UI.py)
# ต้องตรงกับ UNIT_MAP ใน UI.py — ใช้แปลง '230k' → 230 * 1e3 = 230000.0
UNIT_MULTIPLIERS = {
    'k': 1e3,    # Kilo
    'M': 1e6,    # Mega
    'm': 1e-3,   # milli
    'μ': 1e-6,   # micro
}

log = logging.getLogger("GooseManager")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def _ensure_vlan_iface(base_iface, vlan_id):
    """
    สร้าง VLAN interface เช่น eth0.100 ถ้ายังไม่มี
    ให้ kernel ใส่ VLAN tag เอง (ไม่โดน driver แกะออก)
    คืนค่าชื่อ interface ที่สร้าง เช่น "eth0.100"
    """
    vlan_iface = f"{base_iface}.{vlan_id}"

    # เช็คว่ามีอยู่แล้วหรือยัง
    result = subprocess.run(
        ["ip", "link", "show", vlan_iface],
        capture_output=True
    )

    if result.returncode != 0:
        # ยังไม่มี → สร้างใหม่
        subprocess.run([
            "sudo", "ip", "link", "add",
            "link", base_iface,
            "name", vlan_iface,
            "type", "vlan",
            "id", str(vlan_id)
        ], check=True)
        log.info(f"_ensure_vlan_iface: สร้าง '{vlan_iface}' สำเร็จ")

    # เปิดใช้งาน
    subprocess.run(["sudo", "ip", "link", "set", vlan_iface, "up"], check=True)
    log.info(f"_ensure_vlan_iface: '{vlan_iface}' พร้อมใช้งาน")

    return vlan_iface


# ══════════════════════════════════════════════════════════════════════════════
# GooseSession — สถานะของ GCB 1 ตัวที่กำลัง publish
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class GooseSession:
    ied_name      : str    # "OUT1"
    json_path     : str    # path ของ JSON
    gcb_info      : dict   # ข้อมูล GCB ทั้งหมด
    publisher     : object # GoosePublisher object
    comm_params   : object # CommParameters — keep reference ป้องกัน GC ทำลาย
    st_num        : int   = 1
    sq_num        : int   = 0
    last_snapshot : list  = field(default_factory=list)

    # ── Retransmission state ──────────────────────────────────────────────────
    # retransmit_event : Clock.schedule_once event ที่กำลัง pending อยู่
    # next_interval    : delay ของการส่งครั้งถัดไป (วินาที)
    #                    เริ่มที่ MinTime แล้ว x2 ทุกครั้งจนถึง MaxTime
    retransmit_event : object = None
    next_interval    : float  = 0.0


# ══════════════════════════════════════════════════════════════════════════════
# GooseManager
# ══════════════════════════════════════════════════════════════════════════════

class GooseManager:

    def __init__(self):
        self.sessions     = []   # GooseSession ที่ active ทั้งหมด
        self._watch_event = None # Clock event สำหรับ polling JSON

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────────────────────


    def start(self, selected_lns, comm_overrides=None,
            transmiss_overrides=None, simulation=False):
        """
        selected_lns        = [("XCBR1", "OUT1"), ...]
        comm_overrides      = { (ied_name, cb_name): {MAC, APPID_hex, ...} }
        transmiss_overrides = { (ied_name, cb_name): {MinTime, MaxTime} }
        simulation          = True  → set simulation bit ใน GOOSE header
                            False → ส่งปกติ (default)
        """
        if not selected_lns:
            log.warning("start() — ไม่มี LN ที่เลือก")
            return
        if self.sessions:
            log.warning("start() — กำลัง publish อยู่แล้ว ให้ stop() ก่อน")
            return

        _comm      = comm_overrides      or {}
        _transmiss = transmiss_overrides or {}

        log.info(f"start() — {selected_lns} | simulation={simulation}")  # ← log เพิ่ม

        gcb_list = self._find_gcbs(selected_lns)
        if not gcb_list:
            log.warning("start() — ไม่พบ GCB ที่ตรงกับ LN ที่เลือก")
            return

        for ied_name, gcb_info, json_path, json_data in gcb_list:
            key = (ied_name, gcb_info['CBName'])

            if key in _comm:
                gcb_info = {**gcb_info, **_comm[key]}
            if key in _transmiss:
                gcb_info = {**gcb_info, **_transmiss[key]}

            publisher, params = self._create_publisher(gcb_info, simulation)  # ← ส่งต่อ
            snapshot = self._build_snapshot(json_data, ied_name, gcb_info)

            session = GooseSession(
                ied_name      = ied_name,
                json_path     = json_path,
                gcb_info      = gcb_info,
                publisher     = publisher,
                comm_params   = params,
                last_snapshot = snapshot,
            )
            self.sessions.append(session)
            self._start_retransmission(session, json_data)
            log.info(f"  ✓ {ied_name}/{gcb_info['CBName']} — simulation={simulation}")

        self._watch_event = Clock.schedule_interval(self._watch_loop, WATCH_INTERVAL)

    def stop(self):
        """เรียกเมื่อกดปุ่ม STOP — หยุดทุกอย่าง"""

        # ยกเลิก file watcher
        if self._watch_event:
            self._watch_event.cancel()
            self._watch_event = None

        # ยกเลิก retransmit ที่ pending และ destroy publisher
        for session in self.sessions:
            self._cancel_retransmit(session)
            if IEC_AVAILABLE and session.publisher:
                iec.GoosePublisher_destroy(session.publisher)
            log.info(f"stop() — {session.ied_name}/{session.gcb_info['CBName']}")

        self.sessions.clear()
        log.info("stop() — เสร็จสิ้น")

    # ─────────────────────────────────────────────────────────────────────────
    # RETRANSMISSION — หัวใจหลักของระบบ
    # ─────────────────────────────────────────────────────────────────────────

    def _start_retransmission(self, session, json_data):
        """
        เริ่ม retransmission sequence ใหม่ตั้งแต่ต้น
        เรียกเมื่อ: กด START หรือตรวจพบค่าเปลี่ยนใน JSON

        ลำดับการส่ง:
          ส่งทันที          (sqNum=0)
          รอ MinTime ms    → ส่ง (sqNum=1)
          รอ MinTime x2 ms → ส่ง (sqNum=2)
          รอ MinTime x4 ms → ส่ง (sqNum=3)
          ...
          รอ MaxTime ms    → ส่ง  ← ค้างที่ MaxTime ตลอดจนกด STOP
        """
        # ยกเลิก schedule เก่าก่อน (ถ้ามี)
        self._cancel_retransmit(session)

        # reset sqNum เพราะเริ่ม sequence ใหม่
        session.sq_num = 0

        # กำหนด interval เริ่มต้น = MinTime (ms → วินาที)
        min_time_s = session.gcb_info.get("MinTime", 2) / 1000.0
        session.next_interval = min_time_s

        # ส่งทันที (sqNum=0)
        self._publish_session(session, json_data)

        # schedule การส่งครั้งถัดไป
        self._schedule_next_retransmit(session)

    def _schedule_next_retransmit(self, session):
        """schedule การส่ง packet ถัดไปตาม next_interval"""
        session.retransmit_event = Clock.schedule_once(
            lambda dt, s=session: self._do_retransmit(s),
            session.next_interval
        )

    def _do_retransmit(self, session):
        """
        เรียกโดย Clock เมื่อถึงเวลา retransmit
        โหลด JSON ล่าสุด ส่ง packet แล้ว schedule ครั้งถัดไป
        """
        # ถ้า session ถูกลบออกไปแล้ว (หลัง stop) ไม่ต้องทำอะไร
        if session not in self.sessions:
            return

        # โหลด JSON ล่าสุด (อาจเปลี่ยนแล้วระหว่างรอ)
        try:
            with open(session.json_path, encoding='utf-8') as f:
                json_data = json.load(f)
        except Exception as e:
            log.error(f"_do_retransmit: อ่าน JSON ไม่ได้ — {e}")
            # ยังคง schedule ต่อไปแม้อ่านไม่ได้
            self._advance_interval(session)
            self._schedule_next_retransmit(session)
            return

        # ส่ง packet
        self._publish_session(session, json_data)

        # คำนวณ interval ถัดไป: x2 จนถึง MaxTime แล้วค้างที่ MaxTime
        self._advance_interval(session)
        self._schedule_next_retransmit(session)

    def _advance_interval(self, session):
        """เพิ่ม interval เป็น 2 เท่า แต่ไม่เกิน MaxTime"""
        max_time_s = session.gcb_info.get("MaxTime", 1000) / 1000.0
        session.next_interval = min(session.next_interval * 2, max_time_s)

    def _cancel_retransmit(self, session):
        """ยกเลิก retransmit event ที่ pending อยู่"""
        if session.retransmit_event:
            session.retransmit_event.cancel()
            session.retransmit_event = None

    # ─────────────────────────────────────────────────────────────────────────
    # FILE WATCHER — ตรวจการเปลี่ยนแปลงของ JSON
    # ─────────────────────────────────────────────────────────────────────────

    def _watch_loop(self, dt):
        """
        เรียกทุก WATCH_INTERVAL วินาที
        ถ้า JSON เปลี่ยน → stNum++ แล้วเริ่ม retransmission ใหม่ตั้งแต่ต้น
        """
        for session in self.sessions:
            try:
                with open(session.json_path, encoding='utf-8') as f:
                    json_data = json.load(f)
            except Exception as e:
                log.error(f"_watch_loop: อ่าน JSON ไม่ได้ ({session.ied_name}) — {e}")
                continue

            new_snapshot = self._build_snapshot(
                json_data, session.ied_name, session.gcb_info)

            if new_snapshot != session.last_snapshot:
                log.info(f"_watch_loop: ค่าเปลี่ยน "
                         f"{session.ied_name}/{session.gcb_info['CBName']}")

                # อัพเดต state
                session.st_num       += 1
                session.last_snapshot = new_snapshot

                # เริ่ม retransmission ใหม่ตั้งแต่ต้น (sqNum reset → 0)
                self._start_retransmission(session, json_data)

    # ─────────────────────────────────────────────────────────────────────────
    # FIND GCBs
    # ─────────────────────────────────────────────────────────────────────────

    def _find_gcbs(self, selected_lns):
        found      = {}
        json_cache = {}

        for ln_name, ied_name in selected_lns:
            if ied_name not in json_cache:
                json_cache[ied_name] = self._load_ied_json(ied_name)
            json_path, json_data = json_cache[ied_name]

            if not json_data:
                log.warning(f"_find_gcbs: ไม่พบ JSON ของ IED '{ied_name}'")
                continue

            goose_list = (json_data.get(ied_name, {})
                                   .get("Communication", {})
                                   .get("GOOSE", []))

            for gcb in goose_list:
                ld_inst      = gcb.get("LDInst", "")
                dataset_name = gcb.get("DataSet", "")
                if not dataset_name:
                    continue

                entries = (json_data[ied_name]["LDevices"]
                               .get(ld_inst, {})
                               .get("DataSets", {})
                               .get(dataset_name, []))

                if ln_name in {e["LN"] for e in entries}:
                    key = (ied_name, gcb["CBName"])
                    if key not in found:
                        found[key] = (ied_name, gcb, json_path, json_data)
                        log.info(f"_find_gcbs: '{ln_name}' → "
                                 f"GCB '{gcb['CBName']}' ({ied_name})")

        return list(found.values())

    def _load_ied_json(self, ied_name):
        if not os.path.isdir(JSON_DIR):
            log.error(f"_load_ied_json: ไม่พบ directory '{JSON_DIR}'")
            return None, None

        for fname in sorted(os.listdir(JSON_DIR)):
            if not fname.endswith('.json'):
                continue
            fpath = os.path.join(JSON_DIR, fname)
            try:
                with open(fpath, encoding='utf-8') as f:
                    data = json.load(f)
                if ied_name in data:
                    return fpath, data
            except Exception as e:
                log.error(f"_load_ied_json: อ่าน '{fname}' ไม่ได้ — {e}")

        log.warning(f"_load_ied_json: ไม่พบ IED '{ied_name}'")
        return None, None

    # ─────────────────────────────────────────────────────────────────────────
    # CREATE PUBLISHER
    # ─────────────────────────────────────────────────────────────────────────

    def _create_publisher(self, gcb_info, simulation=False):
        log.info(f"_create_publisher: {gcb_info['CBName']} "
                f"| MAC={gcb_info['MAC']} | APPID={gcb_info['APPID_int']:#06x} "
                f"| simulation={simulation}")   # ← log เพิ่ม

        if not IEC_AVAILABLE:
            return None, None

        mac = [int(b, 16) for b in gcb_info["MAC"].split(":")]

        params = iec.CommParameters()
        iec.CommParameters_setDstAddress(
            params,
            mac[0], mac[1], mac[2],
            mac[3], mac[4], mac[5],
        )
        params.appId        = gcb_info["APPID_int"]
        params.vlanId       = gcb_info["VLAN-ID"]
        params.vlanPriority = gcb_info["VLAN-Priority"]

        vlan_id = gcb_info.get("VLAN-ID", 0)
        if vlan_id > 0:
            try:
                iface = _ensure_vlan_iface(NETWORK_IFACE, vlan_id)
            except Exception as e:
                log.error(f"_create_publisher: สร้าง VLAN interface ไม่ได้ — {e}")
                return None, None
            use_vlan_tag = False
        else:
            iface = NETWORK_IFACE
            use_vlan_tag = False

        publisher = iec.GoosePublisher_createEx(params, iface, use_vlan_tag)

        if publisher is None:
            raise RuntimeError(
                f"GoosePublisher_create ล้มเหลว ({gcb_info['CBName']}) — "
                "ลองรัน: sudo setcap cap_net_raw+eip $(readlink -f .venv/bin/python3)"
            )

        iec.GoosePublisher_setGoCbRef(publisher,    gcb_info["GoCBRef"])
        iec.GoosePublisher_setGoID(publisher,        gcb_info.get("GoID") or "")
        iec.GoosePublisher_setDataSetRef(publisher,  gcb_info["DataSetRef"])
        iec.GoosePublisher_setConfRev(publisher,     gcb_info["ConfRev"])

        # ── ตั้งค่า simulation bit ตามที่ผู้ใช้เลือก ────────────────────
        iec.GoosePublisher_setSimulation(publisher, simulation)

        return publisher, params

    # ─────────────────────────────────────────────────────────────────────────
    # BUILD SNAPSHOT
    # ─────────────────────────────────────────────────────────────────────────

    def _build_snapshot(self, json_data, ied_name, gcb_info):
        ld_inst      = gcb_info["LDInst"]
        dataset_name = gcb_info["DataSet"]

        entries  = (json_data[ied_name]["LDevices"]
                        .get(ld_inst, {})
                        .get("DataSets", {})
                        .get(dataset_name, []))
        lns_data = (json_data[ied_name]["LDevices"]
                        .get(ld_inst, {})
                        .get("LNs", {}))

        snapshot = []
        for entry in entries:
            try:
                val = (lns_data[entry["LN"]]["DOs"][entry["DO"]]
                               ["Attributes"][entry["DA"]]["InitialValue"])
            except KeyError:
                val = entry.get("InitialValue")
            snapshot.append(val)

        return snapshot

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLISH SESSION
    # ─────────────────────────────────────────────────────────────────────────

    def _add_dataset_entries(self, dataset_list, paired):
        """
        เพิ่ม dataset member เข้า dataset_list ตามลำดับ FCDA เดิมใน SCL
        (สำคัญ: IEC 61850 กำหนดว่า allData ต้องเรียงตามลำดับ dataset definition)

        entry ที่มาจาก FCDA เดียวกันและ IsGrouped=True (คือ FCDA อ้างอิงระดับ DO
        ทั้งก้อน ไม่ได้ระบุ daName) จะถูกห่อเป็น MMS structure เดียว ให้ตรงกับ
        dataset definition จริง (1 FCDA = 1 dataset member) แทนที่จะแตกเป็น
        top-level item หลายตัว ซึ่งทำให้ numDatSetEntries ไม่ตรงกับที่ subscriber
        คาดหวัง (เจอจาก capture ของ IED จริงที่ encode เป็น structure)
        """
        groups = []          # [[(entry, val), ...], ...] แบ่งตาม FCDAIndex
        current_key  = object()   # ค่า sentinel ที่ไม่มีทาง match อะไรได้ตอนเริ่ม
        current_group = []
        for idx, (entry, val) in enumerate(paired):
            # legacy JSON ที่ยังไม่มี FCDAIndex (parse ด้วยโค้ดเก่าก่อน patch นี้)
            # → ใช้ index ของตัวเองเป็น key เฉพาะตัว กันไม่ให้ merge กันผิดๆ
            key = entry.get("FCDAIndex")
            if key is None:
                key = f"__legacy_{idx}__"

            if key != current_key:
                if current_group:
                    groups.append(current_group)
                current_group = []
                current_key = key
            current_group.append((entry, val))
        if current_group:
            groups.append(current_group)

        for group in groups:
            is_grouped = bool(group[0][0].get("IsGrouped", False))

            if is_grouped and len(group) > 1:
                struct = iec.MmsValue_createEmptyStructure(len(group))
                ok = True
                for i, (entry, val) in enumerate(group):
                    da_type = (entry.get("Type") or "").upper()
                    mms = self._to_mms_value(val, da_type)
                    if mms is None:
                        ok = False
                        continue
                    iec.MmsValue_setElement(struct, i, mms)
                if ok:
                    iec.LinkedList_add(dataset_list, struct)
                else:
                    log.error(
                        f"_add_dataset_entries: struct ของ FCDA "
                        f"{group[0][0].get('DO')} มี element ที่แปลงไม่ผ่าน — ข้าม member นี้ทั้งก้อน")
            else:
                for entry, val in group:
                    da_type = (entry.get("Type") or "").upper()
                    mms = self._to_mms_value(val, da_type)
                    if mms is not None:
                        iec.LinkedList_add(dataset_list, mms)

    def _publish_session(self, session, json_data):
        ld_inst      = session.gcb_info["LDInst"]
        dataset_name = session.gcb_info["DataSet"]

        entries = (json_data[session.ied_name]["LDevices"]
                       .get(ld_inst, {})
                       .get("DataSets", {})
                       .get(dataset_name, []))

        if IEC_AVAILABLE and session.publisher:
            iec.GoosePublisher_setStNum(session.publisher, session.st_num)
            iec.GoosePublisher_setSqNum(session.publisher, session.sq_num)
            dataset_list = iec.LinkedList_create()

        log_vals   = []
        paired     = []   # [(entry, val), ...] ตามลำดับ dataset เดิม
        for i, entry in enumerate(entries):
            if i >= len(session.last_snapshot):
                break
            val = session.last_snapshot[i]
            log_vals.append(
                f"{entry['LN']}.{entry['DO']}.{entry['DA']}={val}")
            paired.append((entry, val))

        if IEC_AVAILABLE and session.publisher:
            self._add_dataset_entries(dataset_list, paired)

        if IEC_AVAILABLE and session.publisher:
            time_to_live = int(session.next_interval * 1000 * 2)
            iec.GoosePublisher_setTimeAllowedToLive(session.publisher, time_to_live)

            iec.GoosePublisher_publish(session.publisher, dataset_list)
            iec.LinkedList_destroy(dataset_list)

        session.sq_num += 1

        log.info(
            f"TX {session.ied_name}/{session.gcb_info['CBName']} "
            f"stNum={session.st_num} sqNum={session.sq_num - 1} "
            f"next={session.next_interval * 1000:.0f}ms "
            f"| {', '.join(log_vals)}"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # TO MMS VALUE
    # ─────────────────────────────────────────────────────────────────────────

    def _parse_numeric_str(self, val):
        """
        แปลงค่าตัวเลขที่อาจมี unit suffix จาก UI (เช่น '230k', '1.5M', '75m')
        ให้เป็น float จริงตาม prefix — รองรับ int/float ที่ส่งมาตรงๆ ด้วย

        แก้บั๊ก: เดิม _to_mms_value ใช้ float(val)/int(val) ตรงๆ ซึ่ง crash
        ทันทีถ้า val เป็น string ที่มี unit suffix ต่อท้าย (เช่น '230k')
        ทำให้ attribute นั้นถูกตัดออกจาก GOOSE dataset ที่ publish จริงแบบเงียบๆ
        """
        if val is None:
            return 0.0
        if isinstance(val, (int, float)):
            return float(val)

        s = str(val).strip()
        if not s:
            return 0.0

        last = s[-1]
        if last in UNIT_MULTIPLIERS:
            try:
                return float(s[:-1]) * UNIT_MULTIPLIERS[last]
            except ValueError:
                log.error(f"_parse_numeric_str: parse '{s}' (unit='{last}') ไม่ได้")
                return 0.0

        try:
            return float(s)
        except ValueError:
            log.error(f"_parse_numeric_str: parse '{s}' เป็นตัวเลขไม่ได้")
            return 0.0

    def _to_mms_value(self, val, da_type):
        try:
            if da_type == "BOOLEAN":
                return iec.MmsValue_newBoolean(bool(val))
            elif da_type in ("FLOAT32", "FLOAT64"):
                return iec.MmsValue_newFloat(self._parse_numeric_str(val))
            elif da_type in ("INT8", "INT16", "INT32", "INT64",
                             "INT8U", "INT16U", "INT32U"):
                return iec.MmsValue_newIntegerFromInt32(int(self._parse_numeric_str(val)))
            elif da_type == "QUALITY":
                mms = iec.MmsValue_newBitString(13)
                iec.MmsValue_setBitStringFromInteger(mms, int(val or 0))
                return mms
            elif da_type == "DBPOS":
                mms = iec.MmsValue_newBitString(2)
                iec.MmsValue_setBitStringFromInteger(mms, int(val or 0))
                return mms
            elif da_type in ("TIMESTAMP", "UTC TIME", "UTCTIME"):
                import time
                # แก้บั๊ก: MmsValue_newUtcTime รับ uint32_t (วินาที) เท่านั้น
                # ค่า ms-since-epoch เกิน uint32_t มาก ทำให้ SWIG throw TypeError
                # เปลี่ยนไปใช้ MmsValue_newUtcTimeByMsTime ซึ่งรับ uint64_t (ms) แทน
                ts = int(time.time() * 1000) if val is None else int(val or 0)
                return iec.MmsValue_newUtcTimeByMsTime(ts)
            elif da_type in ("VISIBLE STRING", "VISIBLESTRING", "VISIBLE_STRING"):
                return iec.MmsValue_newVisibleString(str(val or ""))
            elif da_type in ("OCTET STRING", "OCTETSTRING"):
                return iec.MmsValue_newOctetString(bytes.fromhex(str(val or "00")), 1)
            else:
                return None
        except Exception as e:
            log.error(f"_to_mms_value: '{val}' type={da_type} — {e}")
            return None