import psutil
import shutil
import os
import json
import subprocess

from kivy.config import Config
Config.set('input', 'mouse', 'mouse,multitouch_on_demand,disable_on_activity')
Config.set('graphics', 'fullscreen', '1')
Config.set('kivy', 'keyboard_mode', 'dock')

from kivy.app import App
from kivy.uix.screenmanager import Screen, ScreenManager
from kivy.core.window import Window
from kivy.properties import StringProperty
from kivy.clock import Clock
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.popup import Popup
from kivy.uix.progressbar import ProgressBar
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.slider import Slider
from kivy.factory import Factory
from kivy.metrics import dp
from kivy.lang import Builder
from kivy.animation import Animation
from kivy.graphics import Color, Rectangle, RoundedRectangle
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.image import Image
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.relativelayout import RelativeLayout
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget

from pyudev import Context, Monitor, MonitorObserver
from datacollect import process_scd_file
from goose_manager import GooseManager
from cb_monitor import CBMonitor


# ══════════════════════════════════════════════════════════════════════════════
# ImageButton
# ══════════════════════════════════════════════════════════════════════════════

class ImageButton(ButtonBehavior, Image):

    def on_press(self):
        self._original_size = self.size[:]
        Animation(
            size     = (self.width * 0.85, self.height * 0.85),
            duration = 0.08
        ).start(self)

    def on_release(self):
        target = getattr(self, '_original_size', self.size)
        Animation(
            size     = target,
            duration = 0.08
        ).start(self)


class GearButton(RelativeLayout):
    """ImageButton + badge แจ้งเตือนเมื่อค่าถูกแก้ไข"""

    def __init__(self, on_gear=None, **kwargs):
        super().__init__(**kwargs)
        self.size_hint = (None, 1)
        self.width     = dp(52)
        self._on_gear  = on_gear
        self._original_values = None

        # ── ปุ่ม gear ──────────────────────────────────────────────
        self._btn = ImageButton(
            source    = 'Icon/gear.png',
            size_hint = (1, 1),
        )
        self._btn.bind(on_press=self._pressed)
        self.add_widget(self._btn)

        self._badge = Image(
            source    = 'Icon/exclamation_mark.png',   
            size_hint = (None, None),
            size      = (dp(20), dp(20)),
            pos_hint  = {'right': 1, 'top': 1},
            opacity   = 0,                
        )
        self.add_widget(self._badge)

    def _pressed(self, *args):
        if self._on_gear:
            self._on_gear(self)

    def set_modified(self, is_modified):
        """เรียกจาก popup หลังกด OK"""
        self._badge.opacity = 1 if is_modified else 0


# ══════════════════════════════════════════════════════════════════════════════
# CBStatusIcon — แสดงรูปสถานะ CB เปลี่ยนตาม GOOSE ที่รับได้
# ══════════════════════════════════════════════════════════════════════════════

CB_ICON = {
    'on'          : 'Icon/cb_closed.png',   # CB ปิดวงจร (Closed)
    'off'         : 'Icon/cb_open.png',     # CB เปิดวงจร (Open)
    'intermediate': 'Icon/cb_unknown.png',  # กำลังเปลี่ยนสถานะ
    'bad'         : 'Icon/cb_unknown.png',  # ผิดปกติ / ไม่รู้สถานะ
}

class CBStatusIcon(RelativeLayout):
    """
    Widget รูปสถานะ CB ที่ด้านหน้าของ CB row
    เรียก update_status(status) เมื่อได้รับ GOOSE callback เพื่อเปลี่ยนรูป
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.size_hint = (None, 1)
        self.width     = dp(48)

        self._icon = Image(
            source    = 'Icon/cb_unknown.png',   # เริ่มต้น unknown ก่อนรับ GOOSE
            size_hint = (1, 1),
        )
        self.add_widget(self._icon)

    def update_status(self, status):
        """
        เปลี่ยนรูปตามสถานะที่รับมาจาก CBMonitor callback
        status = 'on' | 'off' | 'intermediate' | 'bad'
        """
        self._icon.source = CB_ICON.get(status, 'Icon/cb_unknown.png')


# ══════════════════════════════════════════════════════════════════════════════
# Helper — แยกประเภท widget ตาม DA name และ Type
# ══════════════════════════════════════════════════════════════════════════════

def get_widget_type(da_name, da_type):
    da_type_up   = (da_type or '').upper()
    da_name_low  = (da_name or '').lower()

    if da_name_low == 'stval' and da_type_up == 'BOOLEAN':
        return 'boolean'

    if da_name_low == 'q' or da_type_up == 'QUALITY':
        return 'quality'

    if da_name_low in ('t', 'timestamp') or da_type_up == 'TIMESTAMP':
        return 'readonly'

    if da_type_up == 'DBPOS':
        return 'dbpos'

    if da_type_up in ('FLOAT32', 'FLOAT64', 'INT8', 'INT16', 'INT32',
                      'INT8U', 'INT16U', 'INT32U', 'INT64'):
        return 'numeric'

    if da_type_up == 'BOOLEAN':
        return 'boolean'

    return 'readonly'


# ══════════════════════════════════════════════════════════════════════════════
# LNAttributePopup — popup แสดง/แก้ไข attribute ของ LN
# ══════════════════════════════════════════════════════════════════════════════

class LNAttributePopup(Popup):

    JSON_DIR = "/home/developer/Desktop/SC61850/Json_File"

    QUALITY_OPTIONS = ['good', 'invalid', 'questionable', 'overflow', 'test']
    QUALITY_VALUES  = {'good': 0, 'invalid': 1, 'questionable': 2,
                       'overflow': 4, 'test': 8}
    QUALITY_REVERSE = {v: k for k, v in QUALITY_VALUES.items()}

    DBPOS_OPTIONS = ['intermediate', 'off', 'on', 'bad']
    DBPOS_VALUES  = {'intermediate': 0, 'off': 1, 'on': 2, 'bad': 3}
    DBPOS_REVERSE = {v: k for k, v in DBPOS_VALUES.items()}

    UNIT_OPTIONS = ['-', 'k (Kilo)', 'M (Mega)', 'm (milli)', 'μ (micro)']
    UNIT_MAP     = {'-': '', 'k (Kilo)': 'k', 'M (Mega)': 'M',
                    'm (milli)': 'm', 'μ (micro)': 'μ'}

    def __init__(self, ln_name, ied_name, gear_ref=None,original_values=None, **kwargs):
        super().__init__(**kwargs)
        self.ln_name        = ln_name
        self.ied_name       = ied_name
        self.title          = f"{ied_name}  /  {ln_name}"
        self.size_hint      = (0.72, 0.88)
        self.auto_dismiss   = True
        self._gear_ref = gear_ref
        self._original_values = original_values

        self.current_values = {}
        self.default_values  = {}

        ln_data = self._load_ln_data()

        # ── layout หลัก ───────────────────────────────────────────────
        root = BoxLayout(orientation='vertical', spacing=dp(8), padding=dp(10))

        # ── scroll area ───────────────────────────────────────────────
        scroll = ScrollView(do_scroll_x=False)
        inner  = BoxLayout(
            orientation = 'vertical',
            size_hint_y = None,
            spacing     = dp(8),
            padding     = (dp(4), dp(4)),
        )
        inner.bind(minimum_height=inner.setter('height'))

        if not ln_data:
            inner.add_widget(Label(
                text        = 'ไม่พบข้อมูล DO/DA สำหรับ LN นี้',
                size_hint_y = None,
                height      = dp(40),
                color       = (1, 0.4, 0.4, 1),
            ))
        else:
            for do_name, do_data in ln_data.items():
                # header ชื่อ DO
                inner.add_widget(Label(
                    text        = f'[b]{do_name}[/b]',
                    markup      = True,
                    size_hint_y = None,
                    height      = dp(32),
                    color       = (0.5, 0.85, 1, 1),
                    halign      = 'left',
                    valign      = 'middle',
                ))

                for da_name, da_info in do_data.get('Attributes', {}).items():
                    da_type     = da_info.get('Type', '')
                    init_val    = da_info.get('InitialValue')
                    widget_type = get_widget_type(da_name, da_type)
                    row         = self._build_row(do_name, da_name,
                                                  da_type, widget_type, init_val)
                    inner.add_widget(row)

        scroll.add_widget(inner)
        root.add_widget(scroll)

        # ── ปุ่ม OK / Cancel ──────────────────────────────────────────
        btn_row    = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(12))
        btn_ok     = Button(text='OK',     background_color=(0.2, 0.65, 0.2, 1),
                            font_size=dp(16))
        btn_cancel = Button(text='Cancel', background_color=(0.45, 0.45, 0.45, 1),
                            font_size=dp(16))
        btn_ok.bind(on_press=self._on_ok)
        btn_cancel.bind(on_press=self.dismiss)
        btn_row.add_widget(btn_ok)
        btn_row.add_widget(btn_cancel)
        root.add_widget(btn_row)

        self.content = root

    # ── โหลด DOs ของ LN จาก JSON ─────────────────────────────────────

    def _load_ln_data(self):
        if not os.path.isdir(self.JSON_DIR):
            return {}
        for fname in sorted(os.listdir(self.JSON_DIR)):
            if not fname.endswith('.json'):
                continue
            fpath = os.path.join(self.JSON_DIR, fname)
            try:
                with open(fpath, encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                continue

            ied = data.get(self.ied_name, {})
            if not ied:
                continue

            # ── หา DataSet ของ GOOSE CB ทั้งหมด ────────────────────────
            goose_datasets = {
                gcb["DataSet"]
                for gcb in ied.get("Communication", {}).get("GOOSE", [])
                if gcb.get("DataSet")
            }
            if not goose_datasets:
                continue

            # ── รวม DO/DA จาก GOOSE DataSet entries ของ LN นี้ ──────────
            # result = { do_name: { "Attributes": { da_name: {Type, InitialValue} } } }
            result = {}
            for ld_inst, ld_data in ied.get("LDevices", {}).items():
                for ds_name, entries in ld_data.get("DataSets", {}).items():
                    if ds_name not in goose_datasets:
                        continue   # ข้าม DataSet ที่ไม่ใช่ GOOSE
                    for entry in entries:
                        if entry.get("LN") != self.ln_name:
                            continue

                        do_name = entry.get("DO", "")
                        da_name = entry.get("DA", "")
                        if not do_name or not da_name:
                            continue

                        result.setdefault(do_name, {"Attributes": {}})

                        # ดึง Type + InitialValue จาก entry โดยตรง
                        # (ถูกต้องกว่าอ่านจาก LNs เพราะ resolve มาจาก DataSet แล้ว)
                        result[do_name]["Attributes"][da_name] = {
                            "Type"        : entry.get("Type", ""),
                            "InitialValue": entry.get("InitialValue"),
                        }

            if result:
                return result
        return {}

    # ── สร้าง row แต่ละ DA ───────────────────────────────────────────

    def _build_row(self, do_name, da_name, da_type, widget_type, init_val):
        row = BoxLayout(
            orientation = 'horizontal',
            size_hint_y = None,
            height      = dp(52),
            spacing     = dp(8),
            padding     = (dp(4), dp(2)),
        )

        # label ชื่อ DA + Type
        name_lbl = Label(
            text      = f'{da_name}\n[size=11][color=888888]{da_type}[/color][/size]',
            markup    = True,
            size_hint = (0.28, 1),
            halign    = 'left',
            valign    = 'middle',
        )
        name_lbl.bind(size=lambda w, _: setattr(w, 'text_size', (w.width, None)))
        row.add_widget(name_lbl)

        key = (do_name, da_name)

        # ── Boolean ──────────────────────────────────────────────────
        if widget_type == 'boolean':
            # ค่าเริ่มต้น: ถ้า InitialValue มีอยู่แล้วให้ใช้, ไม่งั้น False
            if init_val is not None:
                default_text = 'True' if init_val else 'False'
            else:
                default_text = 'False'
            self.current_values[key] = (default_text == 'True')
            self.default_values[key]  = (default_text == 'True')

            sp = Spinner(
                text      = default_text,
                values    = ['True', 'False'],
                size_hint = (0.55, 0.85),
            )
            sp.bind(text=lambda sp, val, k=key:
                    self.current_values.update({k: val == 'True'}))
            row.add_widget(sp)

        # ── Quality ──────────────────────────────────────────────────
        elif widget_type == 'quality':
            if init_val is not None:
                default_text = self.QUALITY_REVERSE.get(init_val, 'good')
            else:
                default_text = 'good'
            self.current_values[key] = self.QUALITY_VALUES.get(default_text, 0)
            self.default_values[key]  = self.QUALITY_VALUES.get(default_text, 0)

            sp = Spinner(
                text      = default_text,
                values    = self.QUALITY_OPTIONS,
                size_hint = (0.55, 0.85),
            )
            sp.bind(text=lambda sp, val, k=key:
                    self.current_values.update({k: self.QUALITY_VALUES.get(val, 0)}))
            row.add_widget(sp)

        # ── DBPOS ────────────────────────────────────────────────────
        elif widget_type == 'dbpos':
            if init_val is not None:
                default_text = self.DBPOS_REVERSE.get(init_val, 'off')
            else:
                default_text = 'off'
            self.current_values[key] = self.DBPOS_VALUES.get(default_text, 1)
            self.default_values[key]  = self.DBPOS_VALUES.get(default_text, 1)

            sp = Spinner(
                text      = default_text,
                values    = self.DBPOS_OPTIONS,
                size_hint = (0.55, 0.85),
            )
            sp.bind(text=lambda sp, val, k=key:
                    self.current_values.update({k: self.DBPOS_VALUES.get(val, 1)}))
            row.add_widget(sp)

        # ── Numeric: slider + unit ────────────────────────────────────
        elif widget_type == 'numeric':
            # parse init_val เช่น "230k" → (230, 'k')
            init_num, init_unit_key = self._parse_numeric_init(init_val)
            self.current_values[key] = (float(init_num), init_unit_key)
            self.default_values[key]  = (float(init_num), init_unit_key)

            val_lbl = Label(
                text      = str(int(init_num)),
                size_hint = (0.12, 1),
                halign    = 'center',
                valign    = 'middle',
            )

            sl = Slider(
                min       = 1,
                max       = 999,
                value     = float(init_num),
                step      = 1,
                size_hint = (0.32, 1),
            )

            # หา display text ของ unit เริ่มต้น
            init_unit_display = next(
                (k for k, v in self.UNIT_MAP.items() if v == init_unit_key), '-')

            unit_sp = Spinner(
                text      = init_unit_display,
                values    = self.UNIT_OPTIONS,
                size_hint = (0.28, 0.85),
            )

            def _on_slider(sl_inst, val, k=key, lbl=val_lbl):
                lbl.text = str(int(val))
                cur_unit = self.current_values.get(k, (1.0, ''))[1]
                self.current_values[k] = (float(val), cur_unit)

            def _on_unit(sp_inst, val, k=key):
                unit_str = self.UNIT_MAP.get(val, '')
                cur_num  = self.current_values.get(k, (1.0, ''))[0]
                self.current_values[k] = (cur_num, unit_str)

            sl.bind(value=_on_slider)
            unit_sp.bind(text=_on_unit)

            row.add_widget(val_lbl)
            row.add_widget(sl)
            row.add_widget(unit_sp)

        # ── Readonly ─────────────────────────────────────────────────
        else:
            disp = str(init_val) if init_val is not None else '-'
            row.add_widget(Label(
                text      = disp,
                size_hint = (0.55, 1),
                color     = (0.5, 0.5, 0.5, 1),
                halign    = 'left',
                valign    = 'middle',
            ))

        return row

    # ── parse ค่า numeric เช่น "230k" → (230, 'k') ──────────────────

    def _parse_numeric_init(self, init_val):
        if init_val is None:
            return 1, ''
        s = str(init_val).strip()
        # ลอง parse ตัวเลขล้วน
        try:
            return max(1, min(999, int(float(s)))), ''
        except ValueError:
            pass
        # ตัดหน่วยท้าย เช่น "230k"
        unit_chars = set(v for v in self.UNIT_MAP.values() if v)
        if s and s[-1] in unit_chars:
            unit = s[-1]
            try:
                num = max(1, min(999, int(float(s[:-1]))))
                return num, unit
            except ValueError:
                pass
        return 1, ''

    # ── บันทึกค่ากลับลง JSON ─────────────────────────────────────────

    def _on_ok(self, *args):
        if not os.path.isdir(self.JSON_DIR):
            self.dismiss()
            return

        for fname in sorted(os.listdir(self.JSON_DIR)):
            if not fname.endswith('.json'):
                continue
            fpath = os.path.join(self.JSON_DIR, fname)
            try:
                with open(fpath, encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                continue

            ied     = data.get(self.ied_name, {})
            changed = False

            for ld_inst, ld_data in ied.get('LDevices', {}).items():
                ln_info = ld_data.get('LNs', {}).get(self.ln_name)
                if not ln_info:
                    continue

                for (do_name, da_name), new_val in self.current_values.items():
                    # numeric → รวมเป็น string เช่น "230k" หรือ "230"
                    if isinstance(new_val, tuple):
                        val_num, unit = new_val
                        save_val = f"{int(val_num)}{unit}" if unit else int(val_num)
                    else:
                        save_val = new_val

                    try:
                        attrs = ln_info['DOs'][do_name]['Attributes']
                        if da_name in attrs:
                            attrs[da_name]['InitialValue'] = save_val
                            changed = True
                    except KeyError:
                        pass

            if changed:
                try:
                    with open(fpath, 'w', encoding='utf-8') as f:
                        json.dump(data, f, indent=4, ensure_ascii=False)
                except Exception as e:
                    print(f"Error saving JSON: {e}")

        if self._gear_ref:
            if self._gear_ref._original_values is None:
                self._gear_ref._original_values = dict(self.default_values)
            is_modified = self.current_values != self._gear_ref._original_values
            self._gear_ref.set_modified(is_modified)

        self.dismiss()

    def _check_modified(self):
        compare = self._original_values if self._original_values else self.default_values
        return self.current_values != compare

# ══════════════════════════════════════════════════════════════════════════════
# SwipeLNItem
# ══════════════════════════════════════════════════════════════════════════════

class SwipeLNItem(BoxLayout):

    SWIPE_THRESHOLD = dp(60)

    def __init__(self, ln_name, ied_name, config_zone_ref, **kwargs):
        super().__init__(**kwargs)
        self.ln_name         = ln_name
        self.ied_name        = ied_name
        self.config_zone_ref = config_zone_ref
        self.is_selected     = False
        self.size_hint_y     = None
        self.height          = dp(55)

        self._swipe_x   = 0.0
        self._swiping   = False
        self._cancelled = False
        self._tx0       = 0
        self._ty0       = 0

        with self.canvas.before:
            self._col_bg  = Color(0.2, 0.2, 0.2, 1)
            self._rect_bg = Rectangle(pos=self.pos, size=self.size)
            self._col_sw  = Color(0.1, 0.6, 0.2, 1)
            self._rect_sw = Rectangle(pos=self.pos, size=(0, self.height))

        self.bind(pos=self._redraw, size=self._redraw)

        self._lbl = Label(
            text      = ln_name,
            size_hint = (1, 1),
            halign    = 'left',
            valign    = 'middle',
            padding   = (dp(12), 0),
            color     = (1, 1, 1, 1),
        )
        self._lbl.bind(size=lambda w, _: setattr(w, 'text_size', (w.width, None)))
        self.add_widget(self._lbl)

    def _redraw(self, *args):
        self._rect_bg.pos  = self.pos
        self._rect_bg.size = self.size
        self._rect_sw.pos  = self.pos
        self._rect_sw.size = (self._swipe_x, self.height)

    def _set_swipe_x(self, val):
        self._swipe_x      = max(0.0, min(float(val), self.width))
        self._rect_sw.pos  = self.pos
        self._rect_sw.size = (self._swipe_x, self.height)

    def on_touch_down(self, touch):
        if not self.collide_point(*touch.pos):
            return False
        touch.grab(self)
        self._tx0       = touch.x
        self._ty0       = touch.y
        self._swiping   = False
        self._cancelled = False
        return True

    def on_touch_move(self, touch):
        if touch.grab_current is not self:
            return False

        dx = touch.x - self._tx0
        dy = touch.y - self._ty0

        if not self._swiping and abs(dy) > abs(dx) and abs(dy) > dp(8):
            touch.ungrab(self)
            self._cancelled = True
            self._anim_reset()
            return False

        if abs(dx) <= dp(8) and abs(dy) <= dp(8):
            return True

        if abs(dx) > dp(8):
            self._swiping = True

        if self._swiping and dx > 0:
            self._set_swipe_x(dx)

        return True

    def on_touch_up(self, touch):
        if touch.grab_current is not self:
            return False
        touch.ungrab(self)

        if self._cancelled or not self._swiping:
            self._anim_reset()
            return True

        dx = touch.x - self._tx0
        if dx >= self.SWIPE_THRESHOLD:
            self._on_swipe_complete()
        else:
            self._anim_reset()

        return True

    def _on_swipe_complete(self):
        if self.is_selected:
            self.is_selected = False
            self.config_zone_ref.remove_ln(self.ln_name, self.ied_name)
            self._col_sw.rgba = (0.2, 0.2, 0.2, 1)
            anim = Animation(_swipe_x=0, duration=0.2, t='out_quad')
            anim.bind(on_progress=lambda *a: self._redraw(),
                      on_complete=lambda *a: self._restore_sw_color())
            anim.start(self)
        else:
            self.is_selected = True
            self.config_zone_ref.receive_ln(self.ln_name, self.ied_name)
            anim = Animation(_swipe_x=self.width, duration=0.15, t='out_quad')
            anim.bind(on_progress=lambda *a: self._redraw())
            anim.start(self)

    def _anim_reset(self):
        anim = Animation(_swipe_x=0, duration=0.18, t='out_quad')
        anim.bind(on_progress=lambda *a: self._redraw())
        anim.start(self)

    def _restore_sw_color(self):
        self._col_sw.rgba = (0.1, 0.6, 0.2, 1)
        self._set_swipe_x(0)
        self._redraw()


# ══════════════════════════════════════════════════════════════════════════════
# LNConfigRow
# ══════════════════════════════════════════════════════════════════════════════

class LNConfigRow(BoxLayout):

    def __init__(self, ln_name, ied_name, on_remove=None,
                 show_gear=False, on_gear=None,
                 show_cb_icon=False, **kwargs):
        super().__init__(**kwargs)
        self.ln_name     = ln_name
        self.ied_name    = ied_name
        self._on_remove  = on_remove
        self._on_gear    = on_gear
        self._gear_btn   = None
        self.cb_icon     = None          # ← reference ไว้ให้ LoginScreen เรียก update
        self.orientation = 'horizontal'
        self.size_hint_y = None
        self.height      = dp(55)
        self.spacing     = dp(5)
        self.padding     = (dp(6), dp(4))

        # คำนวณ size_hint ของ label ตาม widget ที่มี
        if show_gear and show_cb_icon:
            lbl_hint = 0.52
        elif show_gear or show_cb_icon:
            lbl_hint = 0.62
        else:
            lbl_hint = 0.82

        lbl = Label(
            text      = f"{ied_name}  /  {ln_name}",
            size_hint = (lbl_hint, 1),
            halign    = 'left',
            valign    = 'middle',
        )
        lbl.bind(size=lambda w, _: setattr(w, 'text_size', (w.width, None)))

        btn_del = Button(
            text             = 'X',
            size_hint        = (0.18, 1),
            background_color = (0.8, 0.1, 0.1, 1),
            font_size        = dp(16),
        )
        btn_del.bind(on_press=self._remove_self)

        # ── CB icon ด้านหน้าสุด ─────────────────────────────────────
        if show_cb_icon:
            self.cb_icon = CBStatusIcon()
            self.add_widget(self.cb_icon)

        # ── Gear button ──────────────────────────────────────────────
        if show_gear:
            self._gear_btn = GearButton(on_gear=self._on_gear_pressed)
            self.add_widget(self._gear_btn)

        self.add_widget(lbl)
        self.add_widget(btn_del)

    def _remove_self(self, *args):
        if self._on_remove:
            self._on_remove(self.ln_name)
        if self.parent:
            self.parent.remove_widget(self)

    def _on_gear_pressed(self, gear_ref):
        if self._on_gear:
            self._on_gear(self.ln_name, self.ied_name, gear_ref)


# ══════════════════════════════════════════════════════════════════════════════
# ConfigZone
# ══════════════════════════════════════════════════════════════════════════════

class ConfigZone(BoxLayout):

    def __init__(self, zone_type='GCB', **kwargs):
        super().__init__(**kwargs)
        self._swipe_refs = {}
        self.zone_type   = zone_type
        self.cb_monitor  = None   # ← LoginScreen ส่งมาให้หลัง on_enter

    def register_swipe(self, ln_name, ied_name, item):
        self._swipe_refs[(ied_name, ln_name)] = item

    def receive_ln(self, ln_name, ied_name):
        for child in self.children:
            if (hasattr(child, 'ln_name') and hasattr(child, 'ied_name')
                    and child.ln_name == ln_name and child.ied_name == ied_name):
                return

        def _on_x_pressed(name, iname):
            self.remove_ln(name, iname)
            ref = self._swipe_refs.get((iname, name))
            if ref:
                ref.is_selected  = False
                ref._set_swipe_x(0)
                ref._col_sw.rgba = (0.1, 0.6, 0.2, 1)
                ref._redraw()

        row = LNConfigRow(
            ln_name      = ln_name,
            ied_name     = ied_name,
            on_remove    = lambda n, i=ied_name: _on_x_pressed(n, i),
            show_gear    = (self.zone_type == 'GCB'),
            on_gear      = self._on_gear_pressed,
            show_cb_icon = (self.zone_type == 'CB'),
        )
        self.add_widget(row)

        # เริ่มฟัง GOOSE ทันทีเมื่อ swipe เข้า CB zone
        if self.zone_type == 'CB' and self.cb_monitor:
            self.cb_monitor.add_subscription(ln_name, ied_name)

    def _on_gear_pressed(self, ln_name, ied_name, gear_ref):
        popup = LNAttributePopup(ln_name=ln_name, 
                                 ied_name=ied_name, 
                                 gear_ref=gear_ref,
                                 original_values = gear_ref._original_values,
                                )
        popup.open()

    def remove_ln(self, ln_name, ied_name=None):
        for child in list(self.children):
            if not (hasattr(child, 'ln_name') and hasattr(child, 'ied_name')):
                continue
            if child.ln_name == ln_name and (ied_name is None or child.ied_name == ied_name):
                self.remove_widget(child)
                # หยุดฟัง GOOSE ของ CB ตัวนี้ทันที
                if self.zone_type == 'CB' and self.cb_monitor:
                    self.cb_monitor.remove_subscription(ln_name, child.ied_name)
                return


# ══════════════════════════════════════════════════════════════════════════════
# CommConfigPopup — แก้ค่า Communication ของ GOOSE CB
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# CommConfigPopup — แก้ค่า Communication ของ GOOSE CB
# ══════════════════════════════════════════════════════════════════════════════

class CommConfigPopup(Popup):
    """
    แสดงและแก้ไข Communication fields ของ GOOSE CB ทุกตัว:
      MAC Address, APPID, GoID, VLAN-ID, VLAN-Priority, ConfRev
    ค่าเริ่มต้นดึงจาก JSON ถ้าเคย override ไว้จะแสดงค่า override แทน
    กด OK  → บันทึกลง dict ที่ LoginScreen ถือไว้
    กด Cancel → ไม่เปลี่ยนอะไร
    """

    JSON_DIR = "/home/developer/Desktop/SC61850/Json_File"

    # ฟิลด์ที่แสดง: (label, json_key, ประเภท)
    FIELDS = [
        ('MAC Address',   'MAC',           'str'),
        ('APPID (hex)',   'APPID_hex',     'hex'),
        ('GoID',          'GoID',          'str'),
        ('VLAN-ID',       'VLAN-ID',       'int'),
        ('VLAN-Priority', 'VLAN-Priority', 'int'),
        ('ConfRev',       'ConfRev',       'int'),
    ]

    def __init__(self, selected_ieds, comm_overrides, **kwargs):
        super().__init__(**kwargs)
        self.title        = "Comm Config"
        self.size_hint    = (0.85, 0.90)
        self.auto_dismiss = True

        self._selected_ieds  = selected_ieds
        self._comm_overrides = comm_overrides   # reference จาก LoginScreen
        self._widgets        = {}               # { (ied, cb, field_key): TextInput }
        self._json_defaults  = {}               # { (ied, cb, field_key): str } ไว้ reset

        root   = BoxLayout(orientation='vertical', spacing=dp(8), padding=dp(10))
        scroll = ScrollView(do_scroll_x=False)
        inner  = BoxLayout(orientation='vertical', size_hint_y=None, spacing=dp(4))
        inner.bind(minimum_height=inner.setter('height'))

        self._build_content(inner)

        scroll.add_widget(inner)
        root.add_widget(scroll)

        btn_row = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(12))
        btn_ok  = Button(text='OK',      background_color=(0.2, 0.65, 0.2, 1),   font_size=dp(16))
        btn_def = Button(text='Default', background_color=(0.55, 0.35, 0.1, 1),  font_size=dp(16))
        btn_cl  = Button(text='Cancel',  background_color=(0.45, 0.45, 0.45, 1), font_size=dp(16))
        btn_ok.bind(on_press=self._on_ok)
        btn_def.bind(on_press=self._on_default)
        btn_cl.bind(on_press=self.dismiss)
        btn_row.add_widget(btn_ok)
        btn_row.add_widget(btn_def)
        btn_row.add_widget(btn_cl)
        root.add_widget(btn_row)
        self.content = root

    # ── โหลด GCB ทั้งหมดของ IED ที่ selected ─────────────────────────────────

    def _build_content(self, container):
        if not os.path.isdir(self.JSON_DIR):
            container.add_widget(Label(text='ไม่พบ JSON directory',
                                       size_hint_y=None, height=dp(40),
                                       color=(1, 0.4, 0.4, 1)))
            return

        found = False
        for fname in sorted(os.listdir(self.JSON_DIR)):
            if not fname.endswith('.json'):
                continue
            try:
                with open(os.path.join(self.JSON_DIR, fname), encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                continue

            for ied_name in self._selected_ieds:
                if ied_name not in data:
                    continue
                gcbs = data[ied_name].get("Communication", {}).get("GOOSE", [])
                for gcb in gcbs:
                    self._add_gcb_section(container, ied_name, gcb)
                    found = True

        if not found:
            container.add_widget(Label(text='Not found GOOSE CB — Please Select IED first',
                                       size_hint_y=None, height=dp(40),
                                       color=(1, 0.7, 0.2, 1)))

    def _add_gcb_section(self, container, ied_name, gcb):
        cb_name = gcb.get('CBName', '')
        key     = (ied_name, cb_name)
        current = {**gcb, **self._comm_overrides.get(key, {})}

        # เก็บค่า JSON default ไว้ให้ปุ่ม Default ใช้
        for _, field_key, _ in self.FIELDS:
            self._json_defaults[(ied_name, cb_name, field_key)] = \
                str(gcb.get(field_key, '') or '')

        # header
        hdr = Label(
            text        = f'[b]{ied_name}  /  {cb_name}[/b]',
            markup      = True,
            size_hint_y = None,
            height      = dp(34),
            color       = (0.5, 0.85, 1, 1),
            halign      = 'left',
        )
        hdr.bind(size=lambda w, _: setattr(w, 'text_size', (w.width, None)))
        container.add_widget(hdr)

        for label_text, field_key, _ in self.FIELDS:
            row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))

            lbl = Label(text=label_text, size_hint=(0.35, 1),
                        halign='right', valign='middle',
                        color=(0.85, 0.85, 0.85, 1))
            lbl.bind(size=lambda w, _: setattr(w, 'text_size', (w.width, None)))

            default_val = str(current.get(field_key, '') or '')
            ti = TextInput(
                text             = default_val,
                size_hint        = (0.65, 0.85),
                multiline        = False,
                font_size        = dp(16),
                background_color = (0.18, 0.18, 0.18, 1),
                foreground_color = (1, 1, 1, 1),
            )
            self._widgets[(ied_name, cb_name, field_key)] = ti

            row.add_widget(lbl)
            row.add_widget(ti)
            container.add_widget(row)

        container.add_widget(Widget(size_hint_y=None, height=dp(12)))

    # ── OK: บันทึกลง overrides dict ──────────────────────────────────────────

    def _on_ok(self, *args):
        for (ied_name, cb_name, field_key), ti in self._widgets.items():
            val = ti.text.strip()
            key = (ied_name, cb_name)
            self._comm_overrides.setdefault(key, {})

            # หา type จาก FIELDS
            typ = next((t for _, k, t in self.FIELDS if k == field_key), 'str')

            if typ == 'hex':
                try:
                    int_val = int(val, 16)
                    self._comm_overrides[key]['APPID_hex'] = val.upper().zfill(4)
                    self._comm_overrides[key]['APPID_int'] = int_val
                except ValueError:
                    pass
            elif typ == 'int':
                try:
                    self._comm_overrides[key][field_key] = int(val)
                except ValueError:
                    pass
            else:
                if val:
                    self._comm_overrides[key][field_key] = val

        self.dismiss()

    def _on_default(self, *args):
        """คืน TextInput ทุกช่องกลับเป็นค่า JSON default และล้าง overrides"""
        for (ied_name, cb_name, field_key), ti in self._widgets.items():
            default_val = self._json_defaults.get((ied_name, cb_name, field_key), '')
            ti.text     = default_val
        self._comm_overrides.clear()


# ══════════════════════════════════════════════════════════════════════════════
# TransmissConfigPopup — แก้ค่า Retransmission timing ของ GOOSE CB
# ══════════════════════════════════════════════════════════════════════════════

class TransmissConfigPopup(Popup):
    """
    แสดงและแก้ไข MinTime / MaxTime ของ GOOSE CB แต่ละตัว
    ค่าเริ่มต้นดึงจาก JSON
    กด OK      → บันทึก override
    กด Default → คืนค่ากลับ JSON default (ล้าง override ทิ้ง)
    กด Cancel  → ไม่เปลี่ยนอะไร

    MinTime = interval แรกหลัง event (ms) — น้อย = ส่งถี่ช่วงแรก
    MaxTime = interval stable (ms)         — น้อย = ส่งถี่ตลอด
    """

    JSON_DIR = "/home/developer/Desktop/SC61850/Json_File"

    def __init__(self, selected_ieds, transmiss_overrides, **kwargs):
        super().__init__(**kwargs)
        self.title        = "Transmiss Config"
        self.size_hint    = (0.75, 0.85)
        self.auto_dismiss = True

        self._selected_ieds       = selected_ieds
        self._transmiss_overrides = transmiss_overrides   # reference จาก LoginScreen
        self._widgets             = {}   # { (ied, cb, 'MinTime'/'MaxTime'): TextInput }
        self._json_defaults       = {}   # { (ied, cb): {MinTime, MaxTime} } ไว้ reset

        root   = BoxLayout(orientation='vertical', spacing=dp(8), padding=dp(10))
        scroll = ScrollView(do_scroll_x=False)
        inner  = BoxLayout(orientation='vertical', size_hint_y=None, spacing=dp(4))
        inner.bind(minimum_height=inner.setter('height'))

        self._build_content(inner)

        scroll.add_widget(inner)
        root.add_widget(scroll)

        btn_row = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(10))
        btn_ok  = Button(text='OK',      background_color=(0.2, 0.65, 0.2, 1),   font_size=dp(16))
        btn_def = Button(text='Default', background_color=(0.55, 0.35, 0.1, 1),  font_size=dp(16))
        btn_cl  = Button(text='Cancel',  background_color=(0.45, 0.45, 0.45, 1), font_size=dp(16))
        btn_ok.bind(on_press=self._on_ok)
        btn_def.bind(on_press=self._on_default)
        btn_cl.bind(on_press=self.dismiss)
        btn_row.add_widget(btn_ok)
        btn_row.add_widget(btn_def)
        btn_row.add_widget(btn_cl)
        root.add_widget(btn_row)
        self.content = root

    # ── โหลดข้อมูล ───────────────────────────────────────────────────────────

    def _build_content(self, container):
        if not os.path.isdir(self.JSON_DIR):
            container.add_widget(Label(text='ไม่พบ JSON directory',
                                       size_hint_y=None, height=dp(40),
                                       color=(1, 0.4, 0.4, 1)))
            return

        found = False
        for fname in sorted(os.listdir(self.JSON_DIR)):
            if not fname.endswith('.json'):
                continue
            try:
                with open(os.path.join(self.JSON_DIR, fname), encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                continue

            for ied_name in self._selected_ieds:
                if ied_name not in data:
                    continue
                gcbs = data[ied_name].get("Communication", {}).get("GOOSE", [])
                for gcb in gcbs:
                    self._add_gcb_row(container, ied_name, gcb)
                    found = True

        if not found:
            container.add_widget(Label(text='Not found GOOSE CB — Please Select IED first',
                                       size_hint_y=None, height=dp(40),
                                       color=(1, 0.7, 0.2, 1)))

    def _add_gcb_row(self, container, ied_name, gcb):
        cb_name  = gcb.get('CBName', '')
        key      = (ied_name, cb_name)

        # เก็บ JSON default ไว้ให้ปุ่ม Default ใช้
        json_min = gcb.get('MinTime', 2)
        json_max = gcb.get('MaxTime', 1000)
        self._json_defaults[key] = {'MinTime': json_min, 'MaxTime': json_max}

        # ค่าปัจจุบัน = override ถ้ามี ไม่งั้นใช้ JSON
        current = {**self._json_defaults[key],
                   **self._transmiss_overrides.get(key, {})}

        # header
        hdr = Label(
            text        = f'[b]{ied_name}  /  {cb_name}[/b]',
            markup      = True,
            size_hint_y = None,
            height      = dp(34),
            color       = (0.5, 0.85, 1, 1),
            halign      = 'left',
        )
        hdr.bind(size=lambda w, _: setattr(w, 'text_size', (w.width, None)))
        container.add_widget(hdr)

        # hint อธิบายการทำงาน
        hint = Label(
            text        = '[color=666666]MinTime = First interval before  event  |  MaxTime = interval stable[/color]',
            markup      = True,
            size_hint_y = None,
            height      = dp(22),
            font_size   = dp(12),
            halign      = 'left',
        )
        hint.bind(size=lambda w, _: setattr(w, 'text_size', (w.width, None)))
        container.add_widget(hint)

        for field_key, label_text in [('MinTime', 'Min Time (ms)'),
                                       ('MaxTime', 'Max Time (ms)')]:
            row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))

            lbl = Label(text=label_text, size_hint=(0.38, 1),
                        halign='right', valign='middle',
                        color=(0.85, 0.85, 0.85, 1))
            lbl.bind(size=lambda w, _: setattr(w, 'text_size', (w.width, None)))

            ti = TextInput(
                text             = str(current.get(field_key, '')),
                size_hint        = (0.32, 0.85),
                multiline        = False,
                input_filter     = 'int',
                font_size        = dp(16),
                background_color = (0.18, 0.18, 0.18, 1),
                foreground_color = (1, 1, 1, 1),
            )
            self._widgets[(ied_name, cb_name, field_key)] = ti

            # แสดง JSON default ทางขวา
            def_lbl = Label(
                text      = f'[color=555555]JSON: {self._json_defaults[key][field_key]} ms[/color]',
                markup    = True,
                size_hint = (0.30, 1),
                halign    = 'left',
                valign    = 'middle',
                font_size = dp(13),
            )

            row.add_widget(lbl)
            row.add_widget(ti)
            row.add_widget(def_lbl)
            container.add_widget(row)

        container.add_widget(Widget(size_hint_y=None, height=dp(12)))

    # ── actions ──────────────────────────────────────────────────────────────

    def _on_ok(self, *args):
        """บันทึกค่าปัจจุบันใน TextInput ลง overrides dict"""
        for (ied_name, cb_name, field_key), ti in self._widgets.items():
            try:
                val = int(ti.text.strip())
            except ValueError:
                continue
            key = (ied_name, cb_name)
            self._transmiss_overrides.setdefault(key, {})
            self._transmiss_overrides[key][field_key] = val
        self.dismiss()

    def _on_default(self, *args):
        """
        คืน TextInput ทุกช่องกลับเป็นค่า JSON default
        และล้าง overrides dict ทิ้ง (เหมือนไม่เคยแก้)
        """
        for (ied_name, cb_name, field_key), ti in self._widgets.items():
            key         = (ied_name, cb_name)
            default_val = self._json_defaults.get(key, {}).get(field_key, '')
            ti.text     = str(default_val)
        self._transmiss_overrides.clear()


# ══════════════════════════════════════════════════════════════════════════════
# LoginScreen
# ══════════════════════════════════════════════════════════════════════════════

class LoginScreen(Screen):

    def on_enter(self):
        json_dir           = "/home/developer/Desktop/SC61850/Json_File"
        self.ied_list      = self._scan_ieds(json_dir)
        self.selected_ieds = []
        self.current_mode  = 'GCB'
        self._goose_manager = None

        # เก็บ overrides ตลอด session — reset ใหม่ทุกครั้งที่เข้าหน้า Login
        self._comm_overrides      = {}   # { (ied_name, cb_name): {MAC, APPID_hex, ...} }
        self._transmiss_overrides = {}   # { (ied_name, cb_name): {MinTime, MaxTime} }

        # stop ของเก่าก่อนถ้ามี (กรณี re-enter หน้าจอ)
        if getattr(self, '_cb_monitor', None):
            self._cb_monitor.stop()

        # สร้าง CBMonitor ทันทีพร้อมรับ subscription ตลอด session
        self._cb_monitor = CBMonitor(on_status_change=self._on_cb_status)

        # ส่ง reference ให้ config_zone_cb เพื่อให้เรียก add/remove ได้
        # ใช้ Clock.schedule_once เพื่อรอให้ ids พร้อมก่อน
        Clock.schedule_once(self._attach_monitor_to_zone, 0)

    def _attach_monitor_to_zone(self, dt):
        """ส่ง reference ของ CBMonitor ให้ config_zone_cb เพื่อเรียก add/remove"""
        self.ids.config_zone_cb.cb_monitor = self._cb_monitor

    def _scan_ieds(self, folder):
        result = []
        if not os.path.isdir(folder):
            return result
        for fname in sorted(os.listdir(folder)):
            if not fname.lower().endswith('.json'):
                continue
            try:
                with open(os.path.join(folder, fname), encoding='utf-8') as f:
                    data = json.load(f)
                result.append((fname, list(data.keys())))
            except Exception:
                pass
        return result

    def open_ied_popup(self):
        popup           = Factory.IedSelector()
        container       = popup.ids.ied_list_container
        popup._selected = set()

        for fname, ied_names in self.ied_list:
            lbl = Label(
                text        = fname,
                size_hint_y = None,
                height      = dp(30),
                color       = (0.6, 0.6, 0.6, 1),
                halign      = 'center',
                font_size   = dp(24),
            )
            lbl.bind(size=lambda w, _: setattr(w, 'text_size', (w.width, None)))
            container.add_widget(lbl)

            for ied_name in ied_names:
                btn = Button(
                    text             = ied_name,
                    size_hint_y      = None,
                    height           = dp(60),
                    font_size        = dp(18),
                    background_color = (0.2, 0.2, 0.2, 1),
                )
                btn.bind(on_press=lambda x, b=btn, name=ied_name:
                         self._toggle_ied(b, name, popup))
                container.add_widget(btn)

        popup.confirm_selection = lambda: self._confirm_ied(popup)
        popup.open()

    def _toggle_ied(self, btn, ied_name, popup):
        if ied_name in popup._selected:
            popup._selected.remove(ied_name)
            btn.background_color = (0.2, 0.2, 0.2, 1)
        else:
            popup._selected.add(ied_name)
            btn.background_color = (0.0, 0.5, 1.0, 1)

    def _confirm_ied(self, popup):
        self.selected_ieds = list(popup._selected)
        popup.dismiss()
        self._load_ln_list()

    def _load_ln_list(self):
        ln_list        = self.ids.ln_list
        config_zone    = self.ids.config_zone
        config_zone_cb = self.ids.config_zone_cb

        config_zone.zone_type    = 'GCB'
        config_zone_cb.zone_type = 'CB'

        ln_list.clear_widgets()

        json_dir = "/home/developer/Desktop/SC61850/Json_File"
        mode     = getattr(self, 'current_mode', 'GCB')

        if mode == 'GCB':
            config_zone._swipe_refs.clear()
            config_zone.clear_widgets()
            target_zone = config_zone
        else:
            config_zone_cb._swipe_refs.clear()
            config_zone_cb.clear_widgets()
            target_zone = config_zone_cb

        for fname, ied_names in self.ied_list:
            for ied_name in ied_names:
                if ied_name not in self.selected_ieds:
                    continue

                fpath = os.path.join(json_dir, fname)
                with open(fpath, encoding='utf-8') as f:
                    data = json.load(f)

                if mode == 'GCB':
                    allowed = self._get_gcb_lns(data, ied_name)
                else:
                    allowed = self._get_cb_lns(data, ied_name)

                ln_list.add_widget(Label(
                    text        = f"-- {ied_name} --",
                    size_hint_y = None,
                    height      = dp(30),
                    color       = (0.5, 0.8, 1, 1),
                    bold        = True,
                ))

                for ld_inst, ld_data in data[ied_name]["LDevices"].items():
                    for ln_name in ld_data.get("LNs", {}).keys():
                        if ln_name not in allowed:
                            continue
                        item = SwipeLNItem(
                            ln_name         = ln_name,
                            ied_name        = ied_name,
                            config_zone_ref = target_zone,
                        )
                        target_zone.register_swipe(ln_name, ied_name, item)
                        ln_list.add_widget(item)

    def on_mode_change(self, mode, state):
        if state == 'down':
            self.current_mode = mode
            if self.selected_ieds:
                self._load_ln_list()

    def open_comm_config(self):
        """เปิด popup แก้ไข Communication parameters ของ GOOSE CB"""
        CommConfigPopup(
            selected_ieds  = self.selected_ieds,
            comm_overrides = self._comm_overrides,
        ).open()

    def open_transmiss_config(self):
        """เปิด popup แก้ไข Retransmission timing ของ GOOSE CB"""
        TransmissConfigPopup(
            selected_ieds       = self.selected_ieds,
            transmiss_overrides = self._transmiss_overrides,
        ).open()

    def _get_gcb_lns(self, data, ied_name):
        lns        = set()
        goose_list = (data.get(ied_name, {})
                          .get("Communication", {})
                          .get("GOOSE", []))
        for gcb in goose_list:
            ld_inst      = gcb.get("LDInst", "")
            dataset_name = gcb.get("DataSet", "")
            if not dataset_name:
                continue
            entries = (data[ied_name]["LDevices"]
                           .get(ld_inst, {})
                           .get("DataSets", {})
                           .get(dataset_name, []))
            for entry in entries:
                ln_name = entry.get("LN", "")
                if ln_name:
                    lns.add(ln_name)
        return lns

    def _get_cb_lns(self, data, ied_name):
        lns = set()
        for ld_inst, ld_data in (data.get(ied_name, {})
                                      .get("LDevices", {})
                                      .items()):
            for ln_name in ld_data.get("LNs", {}).keys():
                if "XCBR" in ln_name:
                    lns.add(ln_name)
        return lns

    # ══════════════════════════════════════════════════════════════════════════
    # GOOSE Publishing — เชื่อมกับปุ่ม Start / Stop
    # ══════════════════════════════════════════════════════════════════════════

    def start_publishing(self):
        """
        กดปุ่ม START → เด้ง SimulationConfirmPopup ก่อน
        หลังกด Confirm ใน popup → _do_start_publishing(simulation) จะถูกเรียก
        """
        config_zone = self.ids.config_zone

        selected_gcb = [
            (child.ln_name, child.ied_name)
            for child in config_zone.children
            if hasattr(child, 'ln_name') and hasattr(child, 'ied_name')
        ]

        if not selected_gcb:
            Popup(
                title     = 'Config Zone Empty',
                content   = Label(text='Please drag LN into Config Zone (GCB) before Start'),
                size_hint = (0.5, 0.25),
            ).open()
            return

        # ── เด้ง popup ถาม simulation mode ──────────────────────────────
        SimulationConfirmPopup(
            on_confirm = self._do_start_publishing
        ).open()


    def _do_start_publishing(self, simulation: bool):
        """
        เรียกหลังกด Confirm ใน SimulationConfirmPopup
        simulation = True  → ส่ง GOOSE พร้อม simulation bit
        simulation = False → ส่ง GOOSE ปกติ
        """
        config_zone = self.ids.config_zone

        selected_gcb = [
            (child.ln_name, child.ied_name)
            for child in config_zone.children
            if hasattr(child, 'ln_name') and hasattr(child, 'ied_name')
        ]

        if self._goose_manager:
            self._goose_manager.stop()

        self._goose_manager = GooseManager()
        self._goose_manager.start(
            selected_gcb,
            comm_overrides      = self._comm_overrides,
            transmiss_overrides = self._transmiss_overrides,
            simulation          = simulation,           # ← ส่งค่าที่เลือกมา
        )

        # สลับปุ่ม: Start → disable, Stop → enable
        self.ids.btn_start.disabled = True
        self.ids.btn_stop.disabled  = False

    def stop_publishing(self):
        """เรียกเมื่อกดปุ่ม STOP — หยุดเฉพาะ GooseManager (publish)
        CBMonitor ยังทำงานต่อ ไม่หยุดเพราะผูกกับ swipe ไม่ใช่ Start/Stop
        """
        if self._goose_manager:
            self._goose_manager.stop()
            self._goose_manager = None

        # สลับปุ่ม: Stop → disable, Start → enable
        self.ids.btn_stop.disabled  = True
        self.ids.btn_start.disabled = False

    def _on_cb_status(self, ied_name, ln_name, status):
        """
        CBMonitor callback — ถูกเรียกบน main thread เมื่อสถานะ CB เปลี่ยน
        ค้นหา row ที่ตรงกันใน CB zone แล้วอัปเดตไอคอน
        """
        config_zone_cb = self.ids.config_zone_cb
        for child in config_zone_cb.children:
            if (hasattr(child, 'ln_name') and hasattr(child, 'ied_name')
                    and child.ln_name == ln_name
                    and child.ied_name == ied_name
                    and child.cb_icon is not None):
                child.cb_icon.update_status(status)
                return


# ══════════════════════════════════════════════════════════════════════════════
# FileSettingScreen
# ══════════════════════════════════════════════════════════════════════════════

class FileSettingScreen(Screen):

    def on_enter(self):
        default_path                   = "/home/developer/Desktop/SC61850/File_Store"
        self.ids.file_chooser.rootpath = default_path
        self.ids.file_chooser.path     = default_path
        self._known_drives             = set()
        self.update_drive_list()
        self.start_monitoring()
        self._poll_event = Clock.schedule_interval(self._poll_drives, 2)

    def _get_current_drives(self):
        drives = {"/home/developer/Desktop/SC61850/File_Store"}
        for part in psutil.disk_partitions():
            if '/media/' in part.mountpoint or '/mnt/' in part.mountpoint:
                try:
                    os.listdir(part.mountpoint)
                    drives.add(part.mountpoint)
                except (PermissionError, OSError):
                    pass
        return drives

    def _poll_drives(self, dt):
        current = self._get_current_drives()
        if current != self._known_drives:
            removed            = self._known_drives - current
            self._known_drives = current
            self.update_drive_list()
            current_path = self.ids.file_chooser.path
            if any(current_path.startswith(r) for r in removed):
                default = "/home/developer/Desktop/SC61850/File_Store"
                self.ids.file_chooser.rootpath = default
                self.ids.file_chooser.path     = default

    def start_monitoring(self):
        context = Context()
        monitor = Monitor.from_netlink(context)
        monitor.filter_by(subsystem='block')
        self.observer = MonitorObserver(
            monitor, callback=self.on_device_event, name='usb-monitor')
        self.observer.start()

    def on_device_event(self, action, device):
        if action in ('add', 'remove'):
            Clock.schedule_once(self.update_drive_list, 1.5)

    def update_drive_list(self, *args):
        self.ids.drive_bar.clear_widgets()
        self.add_drive_button(
            "Internal Folder", "/home/developer/Desktop/SC61850/File_Store")
        for part in psutil.disk_partitions():
            if '/media/' in part.mountpoint or '/mnt/' in part.mountpoint:
                try:
                    os.listdir(part.mountpoint)
                    drive_name = part.mountpoint.split('/')[-1]
                    self.add_drive_button(f"USB: {drive_name}", part.mountpoint)
                except (PermissionError, OSError):
                    pass

    def add_drive_button(self, text, path):
        btn = Button(
            text         = text,
            size_hint_x  = None,
            width        = '150dp',
            text_size    = (140, None),
            shorten      = True,
            shorten_from = 'right',
            halign       = 'center',
        )
        btn.bind(on_press=lambda x, p=path: self.change_path(p))
        self.ids.drive_bar.add_widget(btn)

    def change_path(self, path):
        if not os.path.exists(path):
            return
        self.ids.file_chooser.rootpath = path
        self.ids.file_chooser.path     = path

    def on_leave(self):
        if hasattr(self, 'observer') and self.observer.is_alive():
            self.observer.stop()
            self.observer.join()
        if hasattr(self, '_poll_event'):
            self._poll_event.cancel()

    def upload_selected_files(self):
        target_folder = "/home/developer/Desktop/SC61850/File_Store"
        selected      = self.ids.file_chooser.selection
        if not selected:
            return
        duplicates = [
            os.path.basename(p) for p in selected
            if os.path.exists(os.path.join(target_folder, os.path.basename(p)))
        ]
        if duplicates:
            self.show_duplicate_popup(duplicates, selected, target_folder)
        else:
            self.do_upload(selected, target_folder)

    def show_duplicate_popup(self, duplicates, selected, target_folder):
        content = BoxLayout(orientation='vertical', padding=10, spacing=10)
        msg     = ("Duplicate files found:\n" + "\n".join(duplicates)
                   + "\n\nDo you want to overwrite?")
        content.add_widget(Label(text=msg))
        btn_layout = BoxLayout(size_hint_y=None, height='40dp', spacing=10)
        popup = Popup(title='Duplicate files', content=content,
                      size_hint=(0.7, 0.4), auto_dismiss=False)
        ow = Button(text='Overwrite')
        sk = Button(text='Skip')
        cl = Button(text='Cancel')
        ow.bind(on_press=lambda _: [popup.dismiss(),
                self.do_upload(selected, target_folder, skip_duplicates=False)])
        sk.bind(on_press=lambda _: [popup.dismiss(),
                self.do_upload(selected, target_folder, skip_duplicates=True)])
        cl.bind(on_press=popup.dismiss)
        btn_layout.add_widget(ow)
        btn_layout.add_widget(sk)
        btn_layout.add_widget(cl)
        content.add_widget(btn_layout)
        popup.open()

    def do_upload(self, selected, target_folder, skip_duplicates=False):
        content = BoxLayout(orientation='vertical', padding=10, spacing=10)
        self.progress_label = Label(text='Uploading...')
        self.progress_bar   = ProgressBar(max=len(selected), value=0)
        content.add_widget(self.progress_label)
        content.add_widget(self.progress_bar)
        self.progress_popup = Popup(
            title='Uploading', content=content,
            size_hint=(0.6, 0.3), auto_dismiss=False)
        self.progress_popup.open()
        self._upload_queue  = list(selected)
        self._upload_target = target_folder
        self._upload_skip   = skip_duplicates
        self._upload_done   = 0
        self._upload_errors = []
        Clock.schedule_interval(self._upload_step, 0)

    def _upload_step(self, dt):
        if not self._upload_queue:
            self.progress_popup.dismiss()
            self._show_result_popup()
            self.ids.file_chooser._update_files()
            return False
        src_path  = self._upload_queue.pop(0)
        filename  = os.path.basename(src_path)
        dest_path = os.path.join(self._upload_target, filename)
        try:
            if not (self._upload_skip and os.path.exists(dest_path)):
                shutil.copy2(src_path, dest_path)
        except Exception:
            self._upload_errors.append(filename)
        self._upload_done       += 1
        self.progress_bar.value  = self._upload_done
        self.progress_label.text = (
            f'Uploading: {filename} '
            f'({self._upload_done}/{int(self.progress_bar.max)})')

    def _show_result_popup(self):
        if self._upload_errors:
            msg = (f'Completed with {len(self._upload_errors)} error(s):\n'
                   + '\n'.join(self._upload_errors))
        else:
            msg = f'Upload completed  {self._upload_done} file(s)!'
        Popup(title='Result', content=Label(text=msg),
              size_hint=(0.5, 0.3)).open()

    def process_selected_file(self):
        selected = self.ids.file_chooser.selection
        if not selected:
            self._show_simple_popup("Error", "Please select a file first")
            return
        ALLOWED_EXT = ('.scd', '.cid', '.iid')
        json_dir    = "/home/developer/Desktop/SC61850/Json_File"
        os.makedirs(json_dir, exist_ok=True)
        success, errors = [], []
        for file_path in selected:
            if not file_path.lower().endswith(ALLOWED_EXT):
                errors.append(f"{os.path.basename(file_path)} (wrong format)")
                continue
            try:
                process_scd_file(file_path, json_dir)
                success.append(os.path.basename(file_path))
            except Exception as e:
                errors.append(f"{os.path.basename(file_path)}: {e}")
        msg = ""
        if success:
            msg += f"Success ({len(success)}):\n" + "\n".join(success)
        if errors:
            msg += f"\n\nFailed ({len(errors)}):\n" + "\n".join(errors)
        self._show_simple_popup("Result", msg.strip())
        self.manager.current = 'Login'

    def _show_simple_popup(self, title, msg):
        Popup(title=title, content=Label(text=msg),
              size_hint=(0.6, 0.3)).open()


# ══════════════════════════════════════════════════════════════════════════════
# SimulationConfirmPopup — ถาม simulation mode ก่อนกด Start
# ══════════════════════════════════════════════════════════════════════════════

class SimulationConfirmPopup(Popup):
    """
    เด้งขึ้นเมื่อกดปุ่ม START
    ให้ผู้ใช้เลือกว่าจะส่ง GOOSE แบบ Simulation Mode หรือ Normal Mode
    กด Confirm → เรียก on_confirm(simulation=True/False)
    กด Cancel  → ไม่ทำอะไร
    """

    def __init__(self, on_confirm, **kwargs):
        super().__init__(**kwargs)
        # ── หน้าตา popup ──────────────────────────────────────────────
        self.title        = "Confirm Publish"
        self.size_hint    = (0.55, 0.40)
        self.auto_dismiss = True

        self._on_confirm   = on_confirm
        self._sim_selected = False   # ค่าเริ่มต้น = Normal

        # layout หลัก
        root = BoxLayout(
            orientation = 'vertical',
            spacing     = dp(12),
            padding     = dp(16),
        )

        # ── คำอธิบาย ──────────────────────────────────────────────────
        root.add_widget(Label(
            text      = 'Select Publish Mode',
            font_size = dp(18),
            bold      = True,
            size_hint_y = None,
            height    = dp(32),
            color     = (1, 1, 1, 1),
        ))

        # ── ปุ่มเลือก Normal / Simulation ────────────────────────────
        mode_row = BoxLayout(
            size_hint_y = None,
            height      = dp(56),
            spacing     = dp(10),
        )

        self._btn_normal = Button(
            text             = 'Normal',
            font_size        = dp(16),
            background_color = (0.15, 0.55, 0.15, 1),
        )
        self._btn_sim = Button(
            text             = 'Simulation',
            font_size        = dp(16),
            background_color = (0.28, 0.28, 0.28, 1),
        )

        self._btn_normal.bind(on_press=self._select_normal)
        self._btn_sim.bind(on_press=self._select_simulation)

        mode_row.add_widget(self._btn_normal)
        mode_row.add_widget(self._btn_sim)
        root.add_widget(mode_row)

        # ── label แสดงคำอธิบาย mode ที่เลือก ─────────────────────────
        self._mode_lbl = Label(
            text        = '[color=aaaaaa]Normal: Send Real GOOSE (simulation=False)[/color]',
            markup      = True,
            font_size   = dp(13),
            size_hint_y = None,
            height      = dp(26),
        )
        root.add_widget(self._mode_lbl)

        # ── ปุ่ม Confirm / Cancel ──────────────────────────────────────
        btn_row = BoxLayout(
            size_hint_y = None,
            height      = dp(52),
            spacing     = dp(12),
        )
        btn_confirm = Button(
            text             = 'Confirm',
            font_size        = dp(16),
            background_color = (0.15, 0.50, 0.85, 1),  # น้ำเงิน
        )
        btn_cancel = Button(
            text             = 'Cancel',
            font_size        = dp(16),
            background_color = (0.45, 0.45, 0.45, 1),
        )
        btn_confirm.bind(on_press=self._on_confirm_pressed)
        btn_cancel.bind(on_press=self.dismiss)

        btn_row.add_widget(btn_confirm)
        btn_row.add_widget(btn_cancel)
        root.add_widget(btn_row)

        self.content = root

    # ── เลือก mode ────────────────────────────────────────────────────

    def _select_normal(self, *args):
        self._sim_selected = False
        # ปุ่ม Normal = เขียว, Simulation = เทา
        self._btn_normal.background_color = (0.15, 0.55, 0.15, 1)
        self._btn_sim.background_color    = (0.28, 0.28, 0.28, 1)
        self._btn_normal.text             = 'Normal'
        self._btn_sim.text                = 'Simulation'
        self._mode_lbl.text = '[color=aaaaaa]Normal: Send Real GOOSE (simulation=False)[/color]'

    def _select_simulation(self, *args):
        self._sim_selected = True
        # ปุ่ม Simulation = ส้ม, Normal = เทา
        self._btn_normal.background_color = (0.28, 0.28, 0.28, 1)
        self._btn_sim.background_color    = (0.75, 0.40, 0.05, 1)
        self._btn_normal.text             = 'Normal'
        self._btn_sim.text                = 'Simulation'
        self._mode_lbl.text = '[color=ffaa44]Simulation: Send GOOSE with bit Simulation=True[/color]'

    # ── กด Confirm ────────────────────────────────────────────────────

    def _on_confirm_pressed(self, *args):
        self.dismiss()
        # ส่งค่า simulation กลับไปยัง LoginScreen
        self._on_confirm(self._sim_selected)


# ══════════════════════════════════════════════════════════════════════════════
# MainApp
# ══════════════════════════════════════════════════════════════════════════════

class MainApp(App):
    def build(self):
        Factory.register('ConfigZone', cls=ConfigZone)
        return Builder.load_file('main_ui.kv')

    def on_start(self):
        # เมื่อ restore กลับมาจาก minimize → คืน fullscreen
        Window.bind(on_restore=self._on_restore)

    def _on_restore(self, *args):
        Window.fullscreen = 'auto'

    def minimize_window(self):
        Window.minimize()

    def exit_program(self):
        json_dir = "/home/developer/Desktop/SC61850/Json_File"
        try:
            for f in os.listdir(json_dir):
                if f.endswith('.json'):
                    os.remove(os.path.join(json_dir, f))
        except Exception as e:
            print(f"Error deleting json files: {e}")

        # cleanup CBMonitor ก่อน exit เพื่อไม่ให้ background thread ค้าง
        try:
            sm = self.root
            login = sm.get_screen('Login')
            if getattr(login, '_goose_manager', None):
                login._goose_manager.stop()
            if getattr(login, '_cb_monitor', None):
                login._cb_monitor.stop()
        except Exception as e:
            print(f"exit_program cleanup error: {e}")

        subprocess.run(['sudo', 'shutdown', '-h', 'now'])


if __name__ == '__main__':
    MainApp().run()