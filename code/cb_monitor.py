# ══════════════════════════════════════════════════════════════════════════════
# cb_monitor.py  —  Monitor สถานะ CB ผ่าน MMS Polling (IEC 61850)
#
# Architecture: Thread-per-IED
#   - 1 IEDPoller ต่อ 1 IED = 1 TCP connection ต่อ 1 IED
#   - แต่ละ IEDPoller อ่านได้หลาย XCBR ของ IED เดียวกัน
#   - ทำงานพร้อมกันหลาย IED ได้ (parallel threads)
#
# Lifecycle:
#   CBMonitor สร้างครั้งเดียวใน LoginScreen.on_enter()
#   add_subscription(ln_name, ied_name)    → swipe XCBR เข้า CB zone
#   remove_subscription(ln_name, ied_name) → กด X ออกจาก CB zone
#   stop()                                 → exit program
#
# callback signature:
#   def my_callback(ied_name: str, ln_name: str, status: str): ...
#   status = 'on' | 'off' | 'intermediate' | 'bad'
# ══════════════════════════════════════════════════════════════════════════════

import json
import os
import time
import threading
import logging
from dataclasses import dataclass, field

from kivy.clock import Clock

try:
    import pyiec61850 as iec
    IEC_AVAILABLE = True
except ImportError:
    IEC_AVAILABLE = False
    logging.warning("CBMonitor: pyiec61850 ไม่พบ — ทำงานในโหมด DRY RUN")

JSON_DIR         = "/home/developer/Desktop/SC61850/Json_File"
MMS_PORT         = 102     # standard MMS port
POLL_INTERVAL    = 2.0     # วิ: ถามค่า XCBR.Pos ทุกกี่วิ
CONNECT_TIMEOUT  = 5.0     # วิ: timeout ตอน connect
DRY_RUN_INTERVAL = 5.0     # วิ: dry-run toggle interval

DBPOS_MAP = {0: 'intermediate', 1: 'off', 2: 'on', 3: 'bad'}

log = logging.getLogger("CBMonitor")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


# ══════════════════════════════════════════════════════════════════════════════
# CBEntry  —  ข้อมูล 1 XCBR ที่กำลัง monitor
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class CBEntry:
    ied_name : str    # "LINE_01_MAIN1"
    ln_name  : str    # "XCBR1"
    ld_inst  : str    # "CB"   — LDevice ที่ XCBR อยู่
    obj_ref  : str    # "LINE_01_MAIN1CB/XCBR1.Pos.stVal"
    last_val : str    = field(default='', repr=False)   # ค่าล่าสุด (กัน callback ซ้ำ)
    # dry-run
    _dry_toggle: bool = field(default=False, repr=False)


# ══════════════════════════════════════════════════════════════════════════════
# IEDPoller  —  1 thread ต่อ 1 IED, ถือ MMS connection ตลอด session
# ══════════════════════════════════════════════════════════════════════════════

class IEDPoller:
    """
    Poll สถานะ XCBR ทุกตัวของ IED เดียว ผ่าน MMS อ่านทุก POLL_INTERVAL วิ

    ใช้ 1 TCP connection ต่อ IED ตลอดอายุ poller
    ถ้า connection หลุด → พยายาม reconnect อัตโนมัติ
    """

    def __init__(self, ied_name, ip, on_change):
        self._ied_name   = ied_name
        self._ip         = ip
        self._on_change  = on_change      # callback(ied_name, ln_name, status)
        self._entries    = []             # list ของ CBEntry
        self._lock       = threading.Lock()
        self._stop_flag  = threading.Event()
        self._connection = None
        self._thread     = None

    # ── Public ───────────────────────────────────────────────────────────────

    def add_entry(self, entry: CBEntry):
        """เพิ่ม XCBR entry เข้า poller (thread-safe)"""
        with self._lock:
            if not any(e.ln_name == entry.ln_name for e in self._entries):
                self._entries.append(entry)
                log.info(f"IEDPoller.add: {self._ied_name}/{entry.ln_name} "
                         f"ref={entry.obj_ref}")

    def remove_entry(self, ln_name):
        """ลบ XCBR entry ออก (thread-safe) คืน True ถ้า list ว่างแล้ว"""
        with self._lock:
            self._entries = [e for e in self._entries if e.ln_name != ln_name]
            log.info(f"IEDPoller.remove: {self._ied_name}/{ln_name} "
                     f"(remaining={len(self._entries)})")
            return len(self._entries) == 0

    def has_entries(self):
        with self._lock:
            return len(self._entries) > 0

    def start(self):
        """เริ่ม background thread"""
        self._stop_flag.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name=f"IEDPoller-{self._ied_name}",
            daemon=True   # ตายตามโปรแกรมหลักถ้า stop() ไม่ถูกเรียก
        )
        self._thread.start()
        log.info(f"IEDPoller.start: {self._ied_name} ({self._ip})")

    def stop(self):
        """หยุด thread และ disconnect"""
        self._stop_flag.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=POLL_INTERVAL + 2)
        log.info(f"IEDPoller.stop: {self._ied_name}")

    # ── Internal ─────────────────────────────────────────────────────────────

    def _poll_loop(self):
        """รันใน background thread — connect แล้ววนอ่านซ้ำ"""
        while not self._stop_flag.is_set():
            # พยายาม connect ถ้ายังไม่ได้หรือ connection หลุด
            if not self._ensure_connected():
                # connect ไม่ได้ → แจ้ง bad ทุกตัว แล้วรอก่อน retry
                self._notify_all_bad()
                self._stop_flag.wait(timeout=POLL_INTERVAL)
                continue

            # อ่านค่าทุก XCBR ใน list
            with self._lock:
                entries_snapshot = list(self._entries)

            for entry in entries_snapshot:
                if self._stop_flag.is_set():
                    break
                self._read_entry(entry)

            # รอก่อนรอบถัดไป (ตรวจ stop_flag ทุก 0.1 วิ)
            self._stop_flag.wait(timeout=POLL_INTERVAL)

        # cleanup เมื่อออกจาก loop
        self._disconnect()

    def _ensure_connected(self):
        """ตรวจว่า connection ยังอยู่ ถ้าไม่ → connect ใหม่"""
        if self._connection is not None:
            # ตรวจสอบ connection state
            state = iec.IedConnection_getState(self._connection)
            if state == iec.IED_STATE_CONNECTED:
                return True
            # connection หลุดแล้ว
            log.warning(f"IEDPoller: {self._ied_name} connection หลุด → reconnect")
            self._disconnect()

        return self._connect()

    def _connect(self):
        """สร้าง MMS connection ไปยัง IED"""
        if not self._ip:
            log.error(f"IEDPoller: {self._ied_name} ไม่มี IP address")
            return False

        try:
            con   = iec.IedConnection_create()
            error = iec.IedClientError()
            iec.IedConnection_setConnectTimeout(con, int(CONNECT_TIMEOUT * 1000))
            iec.IedConnection_connect(con, error, self._ip, MMS_PORT)

            code = iec.IedClientError_getErrorCode(error)
            if code != iec.IED_ERROR_OK:
                iec.IedConnection_destroy(con)
                log.error(f"IEDPoller: connect {self._ied_name} ({self._ip}) "
                          f"ล้มเหลว code={code}")
                return False

            self._connection = con
            log.info(f"IEDPoller: connected {self._ied_name} ({self._ip})")
            return True

        except Exception as e:
            log.error(f"IEDPoller: connect exception {self._ied_name} — {e}")
            return False

    def _disconnect(self):
        """ปิด MMS connection"""
        if self._connection:
            try:
                iec.IedConnection_close(self._connection)
                iec.IedConnection_destroy(self._connection)
            except Exception:
                pass
            self._connection = None

    def _read_entry(self, entry: CBEntry):
        """อ่าน Pos.stVal ของ 1 XCBR แล้ว callback ถ้าค่าเปลี่ยน"""
        try:
            error   = iec.IedClientError()
            mms_val = iec.IedConnection_readObject(
                self._connection,
                error,
                entry.obj_ref,
                iec.IEC61850_FC_ST
            )

            code = iec.IedClientError_getErrorCode(error)
            if code != iec.IED_ERROR_OK:
                log.warning(f"IEDPoller: read {entry.obj_ref} ล้มเหลว code={code}")
                self._notify(entry, 'bad')
                # ถ้า error บ่งบอกว่า connection หลุด → reset connection
                if code in (iec.IED_ERROR_CONNECTION_LOST,
                            iec.IED_ERROR_TIMEOUT):
                    self._disconnect()
                return

            dbpos  = iec.MmsValue_getBitStringAsInteger(mms_val)
            status = DBPOS_MAP.get(dbpos, 'bad')
            iec.MmsValue_delete(mms_val)

            self._notify(entry, status)

        except Exception as e:
            log.error(f"IEDPoller: read exception {entry.obj_ref} — {e}")
            self._notify(entry, 'bad')
            self._disconnect()

    def _notify(self, entry: CBEntry, status: str):
        """แจ้ง callback เฉพาะเมื่อค่าเปลี่ยน — ลด noise"""
        if entry.last_val == status:
            return
        entry.last_val = status
        log.info(f"POLL {self._ied_name}/{entry.ln_name} → {status}")
        # ส่งกลับ UI บน main thread
        Clock.schedule_once(
            lambda dt, n=self._ied_name, l=entry.ln_name, s=status:
                self._on_change(n, l, s),
            0
        )

    def _notify_all_bad(self):
        """แจ้ง 'bad' ทุก XCBR เมื่อ connect ไม่ได้"""
        with self._lock:
            for entry in self._entries:
                self._notify(entry, 'bad')


# ══════════════════════════════════════════════════════════════════════════════
# CBMonitor  —  จัดการ IEDPoller หลายตัว + dry-run
# ══════════════════════════════════════════════════════════════════════════════

class CBMonitor:
    def __init__(self, on_status_change):
        self._callback  = on_status_change
        self._pollers   = {}     # { ied_name: IEDPoller }
        self._dry_event = None
        _dry_entries: list = []

    # ── Public API ───────────────────────────────────────────────────────────

    def add_subscription(self, ln_name, ied_name):
        """
        เรียกเมื่อ swipe XCBR เข้า CB zone

        ถ้า IEDPoller ของ IED นี้ยังไม่มี → สร้างใหม่ + start thread
        ถ้ามีอยู่แล้ว → เพิ่ม entry เข้าใน poller เดิม (ใช้ connection เดิม)
        """
        # กัน duplicate
        if ied_name in self._pollers:
            poller = self._pollers[ied_name]
            # ตรวจว่า ln_name นี้มีอยู่แล้วมั้ย
            with poller._lock:
                if any(e.ln_name == ln_name for e in poller._entries):
                    log.warning(f"add_subscription: {ied_name}/{ln_name} มีอยู่แล้ว")
                    return

        # โหลด JSON หา IP + ld_inst + obj_ref
        entry = self._build_entry(ln_name, ied_name)
        if entry is None:
            log.warning(f"add_subscription: ไม่พบข้อมูลสำหรับ {ied_name}/{ln_name}")
            return

        if not IEC_AVAILABLE:
            # DRY RUN path
            self._dry_add(entry)
            return

        # สร้าง IEDPoller ถ้ายังไม่มี
        if ied_name not in self._pollers:
            ip = self._load_ip(ied_name)
            poller = IEDPoller(
                ied_name  = ied_name,
                ip        = ip,
                on_change = self._callback,
            )
            self._pollers[ied_name] = poller
            poller.add_entry(entry)
            poller.start()
        else:
            self._pollers[ied_name].add_entry(entry)

        log.info(f"add_subscription: {ied_name}/{ln_name} "
                 f"(pollers={len(self._pollers)})")

    def remove_subscription(self, ln_name, ied_name):
        """
        เรียกเมื่อกด X ออกจาก CB zone

        ลบ entry ออกจาก poller
        ถ้า poller ไม่เหลือ entry เลย → stop thread + ลบ poller
        """
        if not IEC_AVAILABLE:
            self._dry_remove(ln_name, ied_name)
            return

        if ied_name not in self._pollers:
            log.warning(f"remove_subscription: ไม่พบ poller ของ {ied_name}")
            return

        poller   = self._pollers[ied_name]
        is_empty = poller.remove_entry(ln_name)

        if is_empty:
            poller.stop()
            del self._pollers[ied_name]
            log.info(f"remove_subscription: poller {ied_name} ถูกลบ "
                     f"(pollers={len(self._pollers)})")

    def stop(self):
        """หยุดทั้งหมด — เรียกตอน exit program"""
        # หยุด dry-run timer
        if self._dry_event:
            self._dry_event.cancel()
            self._dry_event = None

        # หยุดทุก poller
        for ied_name, poller in list(self._pollers.items()):
            poller.stop()
            log.info(f"stop: poller {ied_name} หยุดแล้ว")

        self._pollers.clear()
        log.info("CBMonitor.stop() — เสร็จสิ้น")

    # ── Build entry ──────────────────────────────────────────────────────────

    def _build_entry(self, ln_name, ied_name):
        """
        โหลด JSON หา ld_inst ของ XCBR แล้วสร้าง CBEntry
        Object reference รูปแบบ: {ied_name}{ld_inst}/{ln_name}.Pos.stVal
        """
        _, json_data = self._load_ied_json(ied_name)
        if not json_data:
            return None

        # หา LDevice ที่มี ln_name นี้อยู่
        ld_inst = self._find_ld_for_ln(json_data, ied_name, ln_name)
        if not ld_inst:
            log.warning(f"_build_entry: ไม่พบ {ln_name} ใน {ied_name}")
            return None

        obj_ref = f"{ied_name}{ld_inst}/{ln_name}.Pos.stVal"
        log.info(f"_build_entry: {obj_ref}")

        return CBEntry(
            ied_name = ied_name,
            ln_name  = ln_name,
            ld_inst  = ld_inst,
            obj_ref  = obj_ref,
        )

    def _find_ld_for_ln(self, json_data, ied_name, ln_name):
        """หา ld_inst ที่มี ln_name อยู่"""
        for ld_inst, ld_data in (json_data.get(ied_name, {})
                                          .get("LDevices", {})
                                          .items()):
            if ln_name in ld_data.get("LNs", {}):
                return ld_inst
        return None

    def _load_ip(self, ied_name):
        """โหลด IP address จาก JSON"""
        _, json_data = self._load_ied_json(ied_name)
        if not json_data:
            return ''
        ip = (json_data.get(ied_name, {})
                       .get("Communication", {})
                       .get("IP", ''))
        if not ip:
            log.warning(f"_load_ip: ไม่พบ IP ของ {ied_name} ใน JSON")
        return ip

    # ── Load JSON ────────────────────────────────────────────────────────────

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

    def _dry_add(self, entry: CBEntry):
        self._dry_entries.append(entry)
        # ส่งสถานะ 'off' ทันที
        self._callback(entry.ied_name, entry.ln_name, 'off')
        log.info(f"DRY ADD {entry.ied_name}/{entry.ln_name}")
        # เปิด timer ถ้ายังไม่มี
        if not self._dry_event:
            self._dry_event = Clock.schedule_interval(
                self._dry_toggle_all, DRY_RUN_INTERVAL
            )

    def _dry_remove(self, ln_name, ied_name):
        self._dry_entries = [
            e for e in self._dry_entries
            if not (e.ln_name == ln_name and e.ied_name == ied_name)
        ]
        log.info(f"DRY REMOVE {ied_name}/{ln_name}")
        if not self._dry_entries and self._dry_event:
            self._dry_event.cancel()
            self._dry_event = None

    def _dry_toggle_all(self, dt):
        for entry in self._dry_entries:
            entry._dry_toggle = not entry._dry_toggle
            status = 'on' if entry._dry_toggle else 'off'
            log.info(f"DRY TOGGLE {entry.ied_name}/{entry.ln_name} -> {status}")
            self._callback(entry.ied_name, entry.ln_name, status)