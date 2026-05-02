from lxml import etree
import json
import os

NS = {'scl': 'http://www.iec.ch/61850/2003/SCL'}


def safe_int(val, default=0):
    """แปลง int อย่างปลอดภัย ไม่ crash ถ้าค่าไม่ใช่ตัวเลข"""
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def process_scd_file(file_path, output_dir_json):
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    file_ext  = os.path.splitext(file_path)[1].lower()
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    json_path = os.path.join(output_dir_json, f"{base_name}.json")
    os.makedirs(output_dir_json, exist_ok=True)

    try:
        tree = etree.parse(file_path)
    except etree.XMLSyntaxError as e:
        raise ValueError(f"Invalid XML in {file_path}: {e}")
    root = tree.getroot()

    # ══════════════════════════════════════════════════════════════════
    # 1. DataTypeTemplates
    # ══════════════════════════════════════════════════════════════════

    do_type_map = {}   # { dot_id: { da_name: {'bType':..,'type':..,'fc':..} } }
    sdo_map     = {}   # { dot_id: { sdo_name: child_dot_id } }
    for dotype in root.findall('.//scl:DOType', NS):
        dt_id = dotype.get('id')
        do_type_map[dt_id] = {
            da.get('name'): {
                'bType': da.get('bType', ''),
                'type' : da.get('type', ''),
                'fc'   : da.get('fc', ''),
            }
            for da in dotype.findall('scl:DA', NS)
        }
        sdo_map[dt_id] = {
            sdo.get('name'): sdo.get('type')
            for sdo in dotype.findall('scl:SDO', NS)
        }

    da_type_map = {}   # { dat_id: { bda_name: {'bType':..,'type':..} } }
    for datype in root.findall('.//scl:DAType', NS):
        dat_id = datype.get('id')
        da_type_map[dat_id] = {
            bda.get('name'): {
                'bType': bda.get('bType', ''),
                'type' : bda.get('type', ''),
            }
            for bda in datype.findall('scl:BDA', NS)
        }

    ln_type_map = {}   # { lnt_id: { do_name: dot_id } }
    for lntype in root.findall('.//scl:LNodeType', NS):
        lt_id = lntype.get('id')
        ln_type_map[lt_id] = {
            do.get('name'): do.get('type')
            for do in lntype.findall('scl:DO', NS)
        }

    # ══════════════════════════════════════════════════════════════════
    # 2. Helpers
    # ══════════════════════════════════════════════════════════════════

    def normalize_mac(mac_str):
        return mac_str.replace('-', ':') if mac_str else '01:0C:CD:01:00:00'

    def parse_appid(appid_str):
        """APPID ใน SCL spec เป็น hex เสมอ (IEC 61850-6)"""
        if not appid_str:
            return '0000', 0
        try:
            s = appid_str.strip()
            if s.lower().startswith('0x'):
                val = int(s, 16)
            else:
                try:
                    val = int(s, 16)
                except ValueError:
                    val = int(s, 10)
            return f'{val:04X}', val
        except (ValueError, TypeError):
            return '0000', 0

    def parse_vlan_id(vlan_str):
        """รองรับ hex ไม่มี 0x prefix เช่น '00A', '001'"""
        if not vlan_str:
            return 0
        s = vlan_str.strip()
        try:
            if s.lower().startswith('0x'):
                return int(s, 16)
            try:
                return int(s, 16)
            except ValueError:
                return int(s, 10)
        except (ValueError, TypeError):
            return 0

    def flatten_datype(dat_id, visited=None):
        """
        Drill DAType แบบ recursive จนเจอ primitive bType
        - BDA เดียว Struct → drill ต่อ
        - BDA เดียว primitive → คืนทันที
        - BDA หลายตัว ทุกตัวเป็น Struct ชนิดเดียวกัน → drill ต่อ
        - BDA หลายตัว mixed → คืน 'Struct'
        """
        if not dat_id or dat_id not in da_type_map:
            return dat_id or 'UNKNOWN'
        visited = visited or set()
        if dat_id in visited:
            return dat_id
        visited.add(dat_id)

        bdas = list(da_type_map[dat_id].values())
        if len(bdas) == 1:
            b = bdas[0]
            if b.get('bType') and b['bType'] != 'Struct':
                return b['bType']
            if b.get('bType') == 'Struct' and b.get('type'):
                return flatten_datype(b['type'], visited)
        elif len(bdas) > 1:
            btypes = set(b.get('bType', '') for b in bdas)
            types  = set(b.get('type', '')  for b in bdas)
            if btypes == {'Struct'} and len(types) == 1 and '' not in types:
                child_type = next(iter(types))
                return flatten_datype(child_type, visited)
        return 'Struct'

    def resolve_final_type(btype, typ):
        """
        ถ้า btype เป็น Struct และมี type → flatten DAType
        ถ้า btype เป็น primitive → คืนเลย
        """
        if not btype:
            return typ or 'UNKNOWN'
        if btype != 'Struct':
            return btype
        if typ and typ in da_type_map:
            return flatten_datype(typ)
        return typ or btype or 'UNKNOWN'

    def resolve_da_type(dot_id, da_name, da_name_path=None):
        """
        Resolve DA type แบบ recursive รองรับ:
        1. DA ตรงๆ ใน DOType
        2. DA เป็น Struct → drill ลึกใน DAType (Fix 2.2/2.3)
        3. SDO path → child DOType
        4. daName อ้างถึง BDA ใน DAType ลึกหลายชั้น (Fix 2.2/2.3)
        """
        if not dot_id or dot_id not in do_type_map:
            return 'UNKNOWN'

        parts = da_name_path if da_name_path else (da_name.split('.') if da_name else [])
        if not parts:
            return 'UNKNOWN'

        # ── Case A: DA ตรงๆ อยู่ใน DOType ──────────────────────────────
        da_info = do_type_map[dot_id].get(parts[0])
        if da_info is not None:
            btype = da_info.get('bType', '')
            typ   = da_info.get('type', '')
            if len(parts) == 1:
                return resolve_final_type(btype, typ)
            # nested → drill เข้า DAType ตาม path ที่เหลือ
            if btype == 'Struct' and typ and typ in da_type_map:
                return _drill_datype(typ, parts[1:])
            return resolve_final_type(btype, typ)

        # ── Case B: SDO path (เช่น phsA, phsB ใน WYE/DEL) ──────────────
        sdo_children = sdo_map.get(dot_id, {})
        if parts[0] in sdo_children:
            child_dot_id = sdo_children[parts[0]]
            return resolve_da_type(child_dot_id, None, parts[1:] if len(parts) > 1 else [])

        # ── Case C: DA ไม่อยู่ใน DOType ตรงๆ → drill ลงใน DAType ทุกชั้น ──
        # Fix 2.2/2.3: ค้นหา parts[0] ใน DAType ลึกหลายชั้น
        result = _search_da_in_datype_deep(dot_id, parts[0])
        if result != 'UNKNOWN':
            return result

        return 'UNKNOWN'

    def _drill_datype(dat_id, remaining_parts, visited=None):
        """
        Drill ลึกใน DAType ตาม path ที่เหลือ
        เช่น remaining_parts=['f'] ใน DAType ที่มี BDA:f(FLOAT32)
        """
        if not dat_id or dat_id not in da_type_map:
            return 'UNKNOWN'
        visited = visited or set()
        if dat_id in visited:
            return 'UNKNOWN'
        visited.add(dat_id)

        if not remaining_parts:
            return flatten_datype(dat_id, set(visited))

        bda_info = da_type_map[dat_id].get(remaining_parts[0])
        if bda_info is not None:
            bda_btype = bda_info.get('bType', '')
            bda_type  = bda_info.get('type', '')
            if len(remaining_parts) == 1:
                return resolve_final_type(bda_btype, bda_type)
            if bda_btype == 'Struct' and bda_type and bda_type in da_type_map:
                return _drill_datype(bda_type, remaining_parts[1:], visited)
            return resolve_final_type(bda_btype, bda_type)

        # ไม่เจอใน BDA ตรงๆ → ลอง drill เข้า BDA(Struct) ทุกตัวต่อ
        for bda_val in da_type_map[dat_id].values():
            if bda_val.get('bType') == 'Struct' and bda_val.get('type'):
                result = _drill_datype(bda_val['type'], remaining_parts, visited)
                if result != 'UNKNOWN':
                    return result
        return 'UNKNOWN'

    def _search_da_in_datype_deep(dot_id, da_name_target, depth=0):
        """
        Fix 2.2/2.3: ค้นหา da_name_target ใน DAType ทุกชั้นของ DOType
        เช่น DOType → DA:mag(Struct) → DAType → BDA:f(FLOAT32)
             DOType → DA:rangeC(Struct) → DAType → BDA:hhLim(Struct) → DAType → BDA:f
        """
        if depth > 4:   # ป้องกัน infinite loop
            return 'UNKNOWN'

        for da_info in do_type_map.get(dot_id, {}).values():
            c_btype = da_info.get('bType', '')
            c_type  = da_info.get('type', '')
            if c_btype != 'Struct' or not c_type or c_type not in da_type_map:
                continue

            # ค้นหา da_name_target ใน DAType นี้ (ชั้น 1)
            bda_info = da_type_map[c_type].get(da_name_target)
            if bda_info is not None:
                return resolve_final_type(bda_info.get('bType', ''), bda_info.get('type', ''))

            # ยังไม่เจอ → drill ลงใน BDA(Struct) ต่อ (ชั้น 2+)
            for bda_val in da_type_map[c_type].values():
                if bda_val.get('bType') == 'Struct' and bda_val.get('type'):
                    inner_type = bda_val['type']
                    if inner_type in da_type_map:
                        deep = da_type_map[inner_type].get(da_name_target)
                        if deep is not None:
                            return resolve_final_type(deep.get('bType', ''), deep.get('type', ''))

        return 'UNKNOWN'

    # ══════════════════════════════════════════════════════════════════
    # 3. Communication section
    # ══════════════════════════════════════════════════════════════════
    comm_map     = {}
    time_map     = {}
    smv_comm_map = {}
    ip_map       = {}

    if file_ext == '.iid':
        print("⚠️  .iid file: No Communication section, network params will use defaults")
    else:
        for cap in root.findall('.//scl:ConnectedAP', NS):
            ied_n = cap.get('iedName')
            comm_map.setdefault(ied_n, {})
            time_map.setdefault(ied_n, {})
            smv_comm_map.setdefault(ied_n, {})

            if ied_n not in ip_map:
                addr_el = cap.find('scl:Address', NS)
                if addr_el is not None:
                    p_map = {p.get('type'): p.text for p in addr_el.findall('scl:P', NS)}
                    ip_map[ied_n] = p_map.get('IP', '')

            for gse in cap.findall('scl:GSE', NS):
                cb_name = gse.get('cbName')
                ld_inst = gse.get('ldInst')
                addr    = {p.get('type'): p.text for p in gse.findall('.//scl:P', NS)}
                if 'MAC-Address' in addr:
                    addr['MAC-Address'] = normalize_mac(addr['MAC-Address'])
                comm_map[ied_n][(ld_inst, cb_name)] = addr
                mt = gse.find('scl:MinTime', NS)
                mx = gse.find('scl:MaxTime', NS)
                time_map[ied_n][(ld_inst, cb_name)] = (
                    safe_int(mt.text, 2)    if mt is not None else 2,
                    safe_int(mx.text, 1000) if mx is not None else 1000,
                )

            for smv in cap.findall('scl:SMV', NS):
                cb_name = smv.get('cbName')
                ld_inst = smv.get('ldInst')
                addr    = {p.get('type'): p.text for p in smv.findall('.//scl:P', NS)}
                if 'MAC-Address' in addr:
                    addr['MAC-Address'] = normalize_mac(addr['MAC-Address'])
                smv_comm_map[ied_n][(ld_inst, cb_name)] = addr

    # ══════════════════════════════════════════════════════════════════
    # Global LN map — สร้างก่อน main loop เพื่อรองรับ cross-LD FCDA
    # { (ied_name, ld_inst, full_ln): lnType }
    # ══════════════════════════════════════════════════════════════════
    global_ln_map  = {}
    global_dai_map = {}

    # Fix 1.5: สร้าง reverse index เพื่อค้นหา LN ข้าม LD
    # { (ied_name, full_ln): [(ld_inst, lnType), ...] }
    global_ln_reverse = {}

    for ied in root.findall('.//scl:IED', NS):
        ied_name = ied.get('name')
        for ld in ied.findall('.//scl:LDevice', NS):
            ld_inst = ld.get('inst')
            for node in ld.findall('scl:LN0', NS) + ld.findall('scl:LN', NS):
                prefix   = node.get('prefix', '')
                ln_class = node.get('lnClass', '')
                ln_inst  = node.get('inst', '')
                full_ln  = f"{prefix}{ln_class}{ln_inst}"
                lntype   = node.get('lnType')
                global_ln_map[(ied_name, ld_inst, full_ln)] = lntype

                # Fix 1.5: เก็บ reverse index ไว้ fallback
                key = (ied_name, full_ln)
                global_ln_reverse.setdefault(key, [])
                global_ln_reverse[key].append((ld_inst, lntype))

                for doi in node.findall('.//scl:DOI', NS):
                    do_name = doi.get('name')
                    for dai in doi.findall('.//scl:DAI', NS):
                        val_el = dai.find('.//scl:Val', NS)
                        global_dai_map[
                            (ied_name, ld_inst, full_ln, do_name, dai.get('name'))
                        ] = val_el.text if val_el is not None else None

    def get_lntype(ied_name, fcda_ld, target_ln):
        """
        Fix 1.5: หา lnType ของ LN
        1. ลองหาจาก LD ที่ FCDA ระบุ (fcda_ld) ก่อน
        2. ถ้าไม่เจอ → ค้นหาทุก LD ใน IED เดียวกัน (cross-LD fallback)
        """
        lntype = global_ln_map.get((ied_name, fcda_ld, target_ln))
        if lntype is not None:
            return lntype

        # Fallback: ค้นหาทุก LD
        candidates = global_ln_reverse.get((ied_name, target_ln), [])
        if candidates:
            return candidates[0][1]   # คืน lnType แรกที่เจอ
        return None

    # ══════════════════════════════════════════════════════════════════
    # 4. Main Parse
    # ══════════════════════════════════════════════════════════════════
    ied_json_structure = {}

    for ied in root.findall('.//scl:IED', NS):
        ied_name = ied.get('name')
        ied_json_structure[ied_name] = {
            "Communication": {
                "IP"   : ip_map.get(ied_name, ''),
                "GOOSE": [],
                "SV"   : [],
            },
            "LDevices": {}
        }

        for ld in ied.findall('.//scl:LDevice', NS):
            ld_inst = ld.get('inst')

            ln0 = ld.find('scl:LN0', NS)
            if ln0 is None:
                continue

            ied_json_structure[ied_name]["LDevices"].setdefault(
                ld_inst, {"DataSets": {}, "LNs": {}})

            lns_store = ied_json_structure[ied_name]["LDevices"][ld_inst]["LNs"]

            lns_store.setdefault("LLN0", {
                "lnClass": "LLN0",
                "inst"   : "",
                "prefix" : "",
                "lnType" : ln0.get('lnType', ''),
                "desc"   : ln0.get('desc', ''),
                "DOs"    : {}
            })

            for ln_el in ld.findall('scl:LN', NS):
                prefix   = ln_el.get('prefix', '')
                ln_class = ln_el.get('lnClass', '')
                ln_inst  = ln_el.get('inst', '')
                full_ln  = f"{prefix}{ln_class}{ln_inst}"
                lntype   = ln_el.get('lnType', '')

                lns_store.setdefault(full_ln, {
                    "lnClass": ln_class,
                    "inst"   : ln_inst,
                    "prefix" : prefix,
                    "lnType" : lntype,
                    "desc"   : ln_el.get('desc', ''),
                    "DOs"    : {}
                })

                for doi in ln_el.findall('.//scl:DOI', NS):
                    do_name = doi.get('name')
                    lns_store[full_ln]["DOs"].setdefault(do_name, {"Attributes": {}})

                    # Fix 2.2/2.3: หา dot_id เพื่อ resolve Type ใน DOI/DAI ด้วย
                    dot_id_for_doi = ln_type_map.get(lntype, {}).get(do_name)

                    for dai in doi.findall('.//scl:DAI', NS):
                        val_el  = dai.find('.//scl:Val', NS)
                        da_name = dai.get('name')

                        # Resolve Type จาก DataTypeTemplates
                        da_type_resolved = resolve_da_type(dot_id_for_doi, da_name)

                        lns_store[full_ln]["DOs"][do_name]["Attributes"][da_name] = {
                            "Type"        : da_type_resolved.upper() if da_type_resolved else '',
                            "InitialValue": val_el.text if val_el is not None else None,
                        }

            # ── GOOSE Controls ─────────────────────────────────────
            for gse_cb in ln0.findall('scl:GSEControl', NS):
                cb_name              = gse_cb.get('name')
                extra                = comm_map.get(ied_name, {}).get((ld_inst, cb_name), {})
                appid_hex, appid_int = parse_appid(extra.get('APPID', '0000'))
                min_t, max_t         = time_map.get(ied_name, {}).get((ld_inst, cb_name), (2, 1000))

                ied_json_structure[ied_name]["Communication"]["GOOSE"].append({
                    "CBName"       : cb_name,
                    "LDInst"       : ld_inst,
                    "GoCBRef"      : f"{ied_name}{ld_inst}/LLN0$GO${cb_name}",
                    "DataSet"      : gse_cb.get('datSet'),
                    "DataSetRef"   : f"{ied_name}{ld_inst}/LLN0${gse_cb.get('datSet')}",
                    "GoID"         : gse_cb.get('appID'),
                    "APPID_hex"    : appid_hex,
                    "APPID_int"    : appid_int,
                    "MAC"          : extra.get('MAC-Address', '01:0C:CD:01:00:00'),
                    "VLAN-ID"      : parse_vlan_id(extra.get('VLAN-ID', '0')),
                    "VLAN-Priority": safe_int(extra.get('VLAN-PRIORITY', '4'), 4),
                    "ConfRev"      : safe_int(gse_cb.get('confRev', 1), 1),
                    "MinTime"      : min_t,
                    "MaxTime"      : max_t,
                })

            # ── SV Controls ────────────────────────────────────────
            for sv_cb in ln0.findall('scl:SampledValueControl', NS):
                cb_name              = sv_cb.get('name')
                extra                = smv_comm_map.get(ied_name, {}).get((ld_inst, cb_name), {})
                appid_hex, appid_int = parse_appid(extra.get('APPID', '4000'))

                ied_json_structure[ied_name]["Communication"]["SV"].append({
                    "CBName"       : cb_name,
                    "LDInst"       : ld_inst,
                    "SvCBRef"      : f"{ied_name}{ld_inst}/LLN0$MS${cb_name}",
                    "DataSet"      : sv_cb.get('datSet'),
                    "SmvID"        : sv_cb.get('smvID'),
                    "APPID_hex"    : appid_hex,
                    "APPID_int"    : appid_int,
                    "MAC"          : extra.get('MAC-Address', '01:0C:CD:04:00:00'),
                    "VLAN-ID"      : parse_vlan_id(extra.get('VLAN-ID', '0')),
                    "VLAN-Priority": safe_int(extra.get('VLAN-PRIORITY', '4'), 4),
                    "ConfRev"      : safe_int(sv_cb.get('confRev', 1), 1),
                    "SmpRate"      : sv_cb.get('smpRate', ''),
                    "SmpMod"       : sv_cb.get('smpMod', ''),
                    "NofASDU"      : safe_int(sv_cb.get('nofASDU', 1), 1),
                })

            # ── DataSets ───────────────────────────────────────────
            for datset in ln0.findall('scl:DataSet', NS):
                datset_name = datset.get('name')
                ied_json_structure[ied_name]["LDevices"][ld_inst]["DataSets"] \
                    .setdefault(datset_name, [])

                seen_entries = set()
                for fcda in datset.findall('scl:FCDA', NS):
                    do_name_raw = fcda.get('doName', '')
                    da_name_raw = fcda.get('daName')
                    prefix      = fcda.get('prefix', '')
                    ln_class    = fcda.get('lnClass', '')
                    ln_inst_f   = fcda.get('lnInst', '')
                    fcda_ld     = fcda.get('ldInst', ld_inst)
                    target_ln   = f"{prefix}{ln_class}{ln_inst_f}"
                    fc          = fcda.get('fc')

                    # doName อาจมี dot เช่น "PPV.phsAB" → DO=PPV, sdo_path=['phsAB']
                    do_parts = do_name_raw.split('.') if do_name_raw else []
                    do_name  = do_parts[0] if do_parts else ''
                    sdo_path = do_parts[1:] if len(do_parts) > 1 else []

                    # Fix 1.5: ใช้ get_lntype แทน global_ln_map.get ตรงๆ
                    lnt_id = get_lntype(ied_name, fcda_ld, target_ln)
                    dot_id = ln_type_map.get(lnt_id, {}).get(do_name)

                    # Resolve dot_id ผ่าน SDO path ถ้ามี
                    resolved_dot_id = dot_id
                    for sdo_part in sdo_path:
                        if resolved_dot_id is None:
                            break
                        resolved_dot_id = sdo_map.get(resolved_dot_id, {}).get(sdo_part)

                    if da_name_raw:
                        # มี daName ระบุชัดเจน → ใช้เลย
                        da_list = [da_name_raw]
                    elif resolved_dot_id:
                        # Fix 1.4: ไม่มี daName → expand DA ทั้งหมดที่ FC ตรง
                        all_das = do_type_map.get(resolved_dot_id, {})
                        direct_das = [
                            da_n for da_n, da_i in all_das.items()
                            if da_i.get('fc') == fc
                        ]
                        if direct_das:
                            da_list = direct_das
                        else:
                            # ลอง expand ผ่าน SDO ของ resolved_dot_id
                            sdos = sdo_map.get(resolved_dot_id, {})
                            da_list = []
                            for sdo_n, child_dot_id in sdos.items():
                                child_das = [
                                    da_n for da_n, da_i
                                    in do_type_map.get(child_dot_id, {}).items()
                                    if da_i.get('fc') == fc
                                ]
                                for cda in child_das:
                                    da_list.append(f"{sdo_n}.{cda}")
                            if not da_list:
                                da_list = ['stVal', 'q', 't']
                    else:
                        da_list = ['stVal', 'q', 't']

                    for da_item in da_list:
                        entry_key = (fcda_ld, target_ln, do_name_raw, da_item, fc)
                        if entry_key in seen_entries:
                            continue
                        seen_entries.add(entry_key)

                        da_type  = resolve_da_type(resolved_dot_id, da_item)
                        init_val = global_dai_map.get(
                            (ied_name, fcda_ld, target_ln, do_name, da_item))

                        entry = {
                            "LDInst"      : fcda_ld,
                            "LN"          : target_ln,
                            "DO"          : do_name_raw,
                            "DA"          : da_item,
                            "Type"        : da_type,
                            "FC"          : fc,
                            "InitialValue": init_val,
                        }
                        ied_json_structure[ied_name]["LDevices"][ld_inst] \
                            ["DataSets"][datset_name].append(entry)

                        lns = ied_json_structure[ied_name]["LDevices"] \
                                .setdefault(fcda_ld, {"DataSets": {}, "LNs": {}})["LNs"]

                        if target_ln not in lns:
                            lns[target_ln] = {"lnType": lnt_id or "", "DOs": {}}

                        lns[target_ln]["DOs"].setdefault(do_name_raw, {"Attributes": {}})
                        lns[target_ln]["DOs"][do_name_raw]["Attributes"][da_item] = {
                            "Type"        : da_type.upper(),
                            "InitialValue": init_val,
                        }

            # ── ExtRef / Inputs ────────────────────────────────────
            extref_list = []
            inputs_el = ln0.find('scl:Inputs', NS)
            if inputs_el is not None:
                for ext in inputs_el.findall('scl:ExtRef', NS):
                    extref_list.append({
                        "IedName"    : ext.get('iedName', ''),
                        "LDInst"     : ext.get('ldInst', ''),
                        "Prefix"     : ext.get('prefix', ''),
                        "LNClass"    : ext.get('lnClass', ''),
                        "LNInst"     : ext.get('lnInst', ''),
                        "DOName"     : ext.get('doName', ''),
                        "DAName"     : ext.get('daName', ''),
                        "FC"         : ext.get('fc', ''),
                        "ServiceType": ext.get('serviceType', ''),
                        "SrcLDInst"  : ext.get('srcLDInst', ''),
                        "SrcCBName"  : ext.get('srcCBName', ''),
                    })

            for ln in ld.findall('scl:LN', NS):
                ln_inputs = ln.find('scl:Inputs', NS)
                if ln_inputs is None:
                    continue
                prefix   = ln.get('prefix', '')
                ln_class = ln.get('lnClass', '')
                ln_inst  = ln.get('inst', '')
                full_ln  = f"{prefix}{ln_class}{ln_inst}"
                for ext in ln_inputs.findall('scl:ExtRef', NS):
                    extref_list.append({
                        "SubscriberLN": full_ln,
                        "IedName"    : ext.get('iedName', ''),
                        "LDInst"     : ext.get('ldInst', ''),
                        "Prefix"     : ext.get('prefix', ''),
                        "LNClass"    : ext.get('lnClass', ''),
                        "LNInst"     : ext.get('lnInst', ''),
                        "DOName"     : ext.get('doName', ''),
                        "DAName"     : ext.get('daName', ''),
                        "FC"         : ext.get('fc', ''),
                        "ServiceType": ext.get('serviceType', ''),
                        "SrcLDInst"  : ext.get('srcLDInst', ''),
                        "SrcCBName"  : ext.get('srcCBName', ''),
                    })

            if extref_list:
                ied_json_structure[ied_name]["LDevices"][ld_inst]["ExtRefs"] = extref_list

    # ══════════════════════════════════════════════════════════════════
    # 5. Save JSON
    # ══════════════════════════════════════════════════════════════════
    try:
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(ied_json_structure, f, indent=4, ensure_ascii=False)
        print(f"✅ JSON saved → {json_path}")
    except OSError as e:
        raise OSError(f"Failed to write JSON to {json_path}: {e}")

    return json_path