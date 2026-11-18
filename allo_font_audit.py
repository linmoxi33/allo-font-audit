#!/usr/bin/env python3
"""allo-font-audit: audit a shipped font release against its declared contract.

Pure standard library. Reads SFNT (OTF/TTF) tables directly: name, OS/2, head,
hhea, hmtx, maxp, cmap, fvar, GSUB/GPOS. It does not render glyphs and does not
need the network.

Exit codes: 0 = no findings, 1 = findings, 2 = usage or input error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys

VERSION = "0.1.0"
FONT_SUFFIXES = (".otf", ".ttf", ".woff2")
PARSED_SUFFIXES = (".otf", ".ttf")


class AuditError(Exception):
    """Input cannot be audited (exit 2)."""


def u16(d, o):
    return struct.unpack(">H", d[o:o + 2])[0]


def i16(d, o):
    return struct.unpack(">h", d[o:o + 2])[0]


def u32(d, o):
    return struct.unpack(">I", d[o:o + 4])[0]


def fixed(d, o):
    return struct.unpack(">i", d[o:o + 4])[0] / 65536.0


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def decode_name(raw, platform):
    if platform == 3:
        return raw.decode("utf-16-be", "replace")
    if platform == 0:
        return raw.decode("utf-16-be", "replace")
    return raw.decode("latin-1", "replace")


def pick_name(records, name_id):
    # Prefer Windows English, then any Windows, then Unicode, then Mac.
    for want in ((3, 0x409), (3, None), (0, None), (1, 0)):
        for plat, enc, lang, nid, text in records:
            if nid != name_id:
                continue
            if want[0] is not None and plat != want[0]:
                continue
            if want[1] is not None and lang != want[1]:
                continue
            if text:
                return text
    return None


def parse_cmap(d, off):
    """Return {codepoint: glyph_id} from the best Unicode cmap subtable."""
    if off + 4 > len(d):
        return {}
    n = u16(d, off + 2)
    best = None
    for i in range(n):
        rec = off + 4 + 8 * i
        pid, eid, sub = u16(d, rec), u16(d, rec + 2), u32(d, rec + 4)
        if (pid, eid) in ((3, 10), (0, 4), (0, 6)):
            best = off + sub
            break
        if best is None and (pid, eid) in ((3, 1), (0, 3), (0, 1), (0, 0)):
            best = off + sub
    if best is None or best + 2 > len(d):
        return {}
    fmt = u16(d, best)
    out = {}
    if fmt == 4:
        seg = u16(d, best + 6) // 2
        ends = best + 14
        starts = ends + 2 * seg + 2
        deltas = starts + 2 * seg
        ranges = deltas + 2 * seg
        for s in range(seg):
            end = u16(d, ends + 2 * s)
            start = u16(d, starts + 2 * s)
            delta = i16(d, deltas + 2 * s)
            ro = u16(d, ranges + 2 * s)
            if end < start or end - start > 0xFFFF:
                continue
            for cp in range(start, min(end, 0xFFFF) + 1):
                if ro == 0:
                    gid = (cp + delta) & 0xFFFF
                else:
                    addr = ranges + 2 * s + ro + 2 * (cp - start)
                    if addr + 2 > len(d):
                        continue
                    gid = u16(d, addr)
                    if gid:
                        gid = (gid + delta) & 0xFFFF
                if gid:
                    out[cp] = gid
    elif fmt == 12:
        groups = u32(d, best + 12)
        for g in range(groups):
            p = best + 16 + 12 * g
            if p + 12 > len(d):
                break
            start, end, gid = u32(d, p), u32(d, p + 4), u32(d, p + 8)
            if end < start or end - start > 0x20000:
                continue
            for cp in range(start, end + 1):
                out[cp] = gid + (cp - start)
    return out


def parse_features(d, tables, tag):
    if tag not in tables:
        return set()
    off, ln = tables[tag]
    if ln < 10:
        return set()
    major = u16(d, off)
    if major != 1:
        return set()
    feat_off = u16(d, off + 6)
    p = off + feat_off
    if p + 2 > off + ln:
        return set()
    count = u16(d, p)
    out = set()
    for i in range(count):
        q = p + 2 + 6 * i
        if q + 4 > off + ln:
            break
        out.add(d[q:q + 4].decode("latin-1"))
    return out


class Font:
    def __init__(self, path, data):
        self.path = path
        self.data = data
        self.tables = {}
        if data[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
            raise AuditError(f"{path}: not an SFNT font")
        n = u16(data, 4)
        for i in range(n):
            p = 12 + 16 * i
            if p + 16 > len(data):
                raise AuditError(f"{path}: truncated table directory")
            tag = data[p:p + 4].decode("latin-1")
            off, ln = u32(data, p + 8), u32(data, p + 12)
            if off + ln > len(data):
                raise AuditError(f"{path}: table {tag} out of bounds")
            self.tables[tag] = (off, ln)
        self.names = self._names()
        self.family = pick_name(self.names, 16) or pick_name(self.names, 1)
        self.subfamily = pick_name(self.names, 17) or pick_name(self.names, 2)
        self.fullname = pick_name(self.names, 4)
        self.psname = pick_name(self.names, 6)
        head = self.tables.get("head")
        self.units = u16(data, head[0] + 18) if head else 0
        self.italic = bool(u16(data, head[0] + 44) & 0x02) if head else False
        os2 = self.tables.get("OS/2")
        self.weight = u16(data, os2[0] + 4) if os2 else 0
        self.fs_selection = u16(data, os2[0] + 62) if os2 and os2[1] >= 64 else 0
        if self.fs_selection & 0x01:
            self.italic = True
        self.typo_asc = i16(data, os2[0] + 68) if os2 and os2[1] >= 74 else None
        self.typo_desc = i16(data, os2[0] + 70) if os2 and os2[1] >= 74 else None
        self.typo_gap = i16(data, os2[0] + 72) if os2 and os2[1] >= 74 else None
        self.win_asc = u16(data, os2[0] + 74) if os2 and os2[1] >= 76 else None
        self.win_desc = u16(data, os2[0] + 76) if os2 and os2[1] >= 78 else None
        hhea = self.tables.get("hhea")
        self.asc = i16(data, hhea[0] + 4) if hhea else None
        self.desc = i16(data, hhea[0] + 6) if hhea else None
        self.gap = i16(data, hhea[0] + 8) if hhea else None
        self.num_hmetrics = u16(data, hhea[0] + 34) if hhea else 0
        maxp = self.tables.get("maxp")
        self.num_glyphs = u16(data, maxp[0] + 4) if maxp else 0
        hmtx = self.tables.get("hmtx")
        self.advances = []
        if hmtx and self.num_hmetrics:
            for gid in range(self.num_hmetrics):
                p = hmtx[0] + 4 * gid
                if p + 2 <= len(data):
                    self.advances.append(u16(data, p))
            if self.advances and self.num_glyphs > self.num_hmetrics:
                self.advances.extend([self.advances[-1]] * (self.num_glyphs - self.num_hmetrics))
        self.cmap = parse_cmap(data, self.tables["cmap"][0]) if "cmap" in self.tables else {}
        self.axes = self._axes()
        self.features = parse_features(data, self.tables, "GSUB") | parse_features(data, self.tables, "GPOS")
        self.instances = self._instances()

    def _names(self):
        if "name" not in self.tables:
            return []
        d = self.data
        off, ln = self.tables["name"]
        if ln < 6:
            return []
        count, string_off = u16(d, off + 2), u16(d, off + 4)
        recs = []
        for i in range(count):
            p = off + 6 + 12 * i
            if p + 12 > off + ln:
                break
            pid, eid, lid, nid, slen, soff = struct.unpack(">HHHHHH", d[p:p + 12])
            start = off + string_off + soff
            if start + slen > off + ln:
                continue
            recs.append((pid, eid, lid, nid, decode_name(d[start:start + slen], pid)))
        return recs

    def _axes(self):
        if "fvar" not in self.tables:
            return []
        d = self.data
        o, ln = self.tables["fvar"]
        if ln < 16:
            return []
        version, axoff, _, axcnt, axsz, instcnt, instsz = struct.unpack(">IHHHHHH", d[o:o + 16])
        if version != 0x10000 or axsz < 20:
            return []
        out = []
        for i in range(axcnt):
            p = o + axoff + i * axsz
            if p + 20 > o + ln:
                break
            tag, mn, dv, mx, flags, nid = struct.unpack(">4siiiHH", d[p:p + 20])
            out.append({"tag": tag.decode("latin-1"), "min": mn / 65536.0,
                        "default": dv / 65536.0, "max": mx / 65536.0,
                        "name_id": nid})
        return out

    def _instances(self):
        if "fvar" not in self.tables:
            return []
        d = self.data
        o, ln = self.tables["fvar"]
        if ln < 16:
            return []
        _, axoff, _, axcnt, axsz, instcnt, instsz = struct.unpack(">IHHHHHH", d[o:o + 16])
        if instsz < 4 + 4 * axcnt:
            return []
        out = []
        for i in range(instcnt):
            p = o + axoff + axcnt * axsz + i * instsz
            if p + 4 + 4 * axcnt > o + ln:
                break
            nid = u16(d, p)
            coords = [fixed(d, p + 4 + 4 * j) for j in range(axcnt)]
            out.append({"name_id": nid, "coords": coords})
        return out

    def advance(self, cp):
        gid = self.cmap.get(cp)
        if gid is None or gid >= len(self.advances):
            return None
        return self.advances[gid]

    def vertical(self):
        return {"hhea": [self.asc, self.desc, self.gap],
                "typo": [self.typo_asc, self.typo_desc, self.typo_gap],
                "win": [self.win_asc, self.win_desc]}

    def summary(self):
        return {"path": self.path, "family": self.family, "subfamily": self.subfamily,
                "fullname": self.fullname, "weight": self.weight, "italic": self.italic,
                "glyphs": self.num_glyphs, "units_per_em": self.units,
                "axes": [a["tag"] for a in self.axes], "features": sorted(self.features),
                "cmap_size": len(self.cmap)}


def discover_fonts(root):
    found = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in ("node_modules", ".git"))
        for name in sorted(files):
            if name.lower().endswith(FONT_SUFFIXES):
                full = os.path.join(base, name)
                found.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(found)


def load_contract(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            c = json.load(fh)
    except OSError as e:
        raise AuditError(f"cannot read contract: {e}")
    except json.JSONDecodeError as e:
        raise AuditError(f"contract is not valid JSON: {e}")
    for key in ("family", "variable_family", "variable_file", "axes", "statics"):
        if key not in c:
            raise AuditError(f"contract missing key: {key}")
    return c


def load_sums(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        raise AuditError(f"cannot read SHA256SUMS: {e}")
    out = {}
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise AuditError(f"SHA256SUMS line {n} is malformed")
        out[parts[1].lstrip("*").strip()] = parts[0].lower()
    if not out:
        raise AuditError("SHA256SUMS is empty")
    return out


def finding(code, message, files=None, detail=None, severity="error"):
    return {"code": code, "severity": severity, "message": message,
            "files": files or [], "detail": detail or {}}


def run_audit(root, contract_path, sums_path=None):
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise AuditError(f"root is not a directory: {root}")
    contract = load_contract(contract_path)
    if sums_path is None:
        sums_path = os.path.join(root, "fonts", "SHA256SUMS")
        if not os.path.exists(sums_path):
            sums_path = os.path.join(root, "SHA256SUMS")
    sums = load_sums(sums_path)
    findings = []
    fonts = {}
    all_files = discover_fonts(root)
    parsed_files = [p for p in all_files if p.lower().endswith(PARSED_SUFFIXES)]
    input_hasher = hashlib.sha256()
    for rel in sorted(set(all_files) | set(sums)):
        full = os.path.join(root, rel)
        if os.path.isfile(full):
            input_hasher.update(rel.encode() + b"\0" + sha256_file(full).encode() + b"\n")
    input_sha = input_hasher.hexdigest()

    # 1. SHA256SUMS integrity.
    listed = set(sums)
    for rel, want in sorted(sums.items()):
        full = os.path.join(root, rel)
        if not os.path.isfile(full):
            findings.append(finding("SUMS_MISSING", f"listed file is absent: {rel}", [rel]))
            continue
        got = sha256_file(full)
        if got != want:
            findings.append(finding("SUMS_MISMATCH", f"sha256 mismatch: {rel}",
                                    [rel], {"expected": want, "actual": got}))
    for rel in all_files:
        if rel not in listed:
            findings.append(finding("SUMS_UNLISTED", f"font file is not in SHA256SUMS: {rel}", [rel]))
    for rel in sorted(listed - set(all_files)):
        if not os.path.exists(os.path.join(root, rel)):
            continue
        if rel.lower().endswith(FONT_SUFFIXES):
            findings.append(finding("SUMS_UNLISTED", f"listed font is not discoverable: {rel}", [rel]))

    # 2. Parse every OTF/TTF.
    for rel in parsed_files:
        try:
            with open(os.path.join(root, rel), "rb") as fh:
                fonts[rel] = Font(rel, fh.read())
        except AuditError as e:
            findings.append(finding("PARSE", str(e), [rel]))
        except Exception as e:  # malformed tables should be a finding, not a crash
            findings.append(finding("PARSE", f"{rel}: {type(e).__name__}: {e}", [rel]))

    var_rel = contract["variable_file"]
    var = fonts.get(var_rel)
    if var is None:
        findings.append(finding("VAR_MISSING", f"variable font is absent: {var_rel}", [var_rel]))
    else:
        if var.family != contract["variable_family"]:
            findings.append(finding("NAME_FAMILY",
                                    f"variable family is {var.family!r}, contract says {contract['variable_family']!r}",
                                    [var_rel]))
        want_axes = contract["axes"]
        got = {a["tag"]: a for a in var.axes}
        for tag, spec in want_axes.items():
            if tag not in got:
                findings.append(finding("AXIS_MISSING", f"variable font has no {tag} axis", [var_rel],
                                        {"expected": spec}))
                continue
            a = got[tag]
            for key in ("min", "default", "max"):
                if key in spec and abs(a[key] - spec[key]) > 1e-6:
                    findings.append(finding("AXIS_RANGE",
                                            f"{tag}.{key} is {a[key]:g}, contract says {spec[key]:g}",
                                            [var_rel], {"axis": tag, "field": key,
                                                        "expected": spec[key], "actual": a[key]}))

    # 3. Static fonts: presence, family, weight, italic, features.
    statics = contract["statics"]
    static_fonts = []
    for item in statics:
        rel = item["file"]
        f = fonts.get(rel)
        if f is None:
            if os.path.exists(os.path.join(root, rel)):
                continue  # already reported as PARSE
            findings.append(finding("STATIC_MISSING", f"static font is absent: {rel}", [rel]))
            continue
        static_fonts.append(f)
        want_family = item.get("family", contract["family"])
        if f.family != want_family:
            findings.append(finding("NAME_FAMILY",
                                    f"family is {f.family!r}, contract says {want_family!r}", [rel]))
        if "weight" in item and f.weight != item["weight"]:
            findings.append(finding("WEIGHT",
                                    f"OS/2 usWeightClass is {f.weight}, contract says {item['weight']}",
                                    [rel], {"expected": item["weight"], "actual": f.weight}))
        want_italic = bool(item.get("italic", False))
        if f.italic != want_italic:
            findings.append(finding("ITALIC",
                                    f"italic flag is {f.italic}, contract says {want_italic}", [rel]))
        expected_suffix = item.get("suffix")
        if expected_suffix and not os.path.basename(rel).startswith(expected_suffix):
            findings.append(finding("FILENAME",
                                    f"file name does not start with {expected_suffix!r}", [rel]))
        for feat in contract.get("features", []):
            if feat not in f.features:
                findings.append(finding("FEATURE_MISSING",
                                        f"OpenType feature {feat} is absent", [rel], {"feature": feat}))

    # 4. Cross-static invariants: family naming, vertical metrics, cmap, features.
    if static_fonts:
        fams = sorted({f.family for f in static_fonts})
        vert = {}
        for f in static_fonts:
            key = json.dumps(f.vertical(), sort_keys=True)
            vert.setdefault(key, []).append(f.path)
        if len(vert) > 1:
            findings.append(finding("VERTICAL_DIFF",
                                    "static fonts disagree on vertical metrics",
                                    [p for p in sorted(f.path for f in static_fonts)],
                                    {"variants": [{"metrics": json.loads(k), "files": v}
                                                  for k, v in sorted(vert.items())]}))
        base = static_fonts[0]
        base_set = set(base.cmap)
        for f in static_fonts[1:]:
            missing = sorted(base_set - set(f.cmap))
            extra = sorted(set(f.cmap) - base_set)
            if missing or extra:
                findings.append(finding("CMAP_DIFF",
                                        f"{f.path} cmap differs from {base.path}",
                                        [base.path, f.path],
                                        {"missing": missing[:40], "extra": extra[:40],
                                         "missing_count": len(missing), "extra_count": len(extra)}))
        feat_sets = {f.path: f.features for f in static_fonts + ([var] if var else [])}
        common = set.intersection(*feat_sets.values()) if feat_sets else set()
        for path, feats in sorted(feat_sets.items()):
            missing = sorted(common - feats) if common else []
            if missing:
                findings.append(finding("FEATURE_DIFF", f"{path} lacks family-wide features",
                                        [path], {"missing": missing}))
        missing_family = sorted((set.union(*feat_sets.values()) if feat_sets else set()) - common)
        if missing_family and len(feat_sets) > 1:
            findings.append(finding("FEATURE_DIFF",
                                    "OpenType feature coverage is not identical across the release",
                                    sorted(feat_sets), {"not_shared": missing_family},
                                    severity="warning"))

    # 5. The README claim: statics are the variable font at opsz 14 / ROND 50 / wght 400.
    regular = None
    for f in static_fonts:
        if f.weight == 400 and not f.italic:
            regular = f
            break
    if regular is not None and var is not None:
        diffs = []
        for cp in sorted(set(regular.cmap) & set(var.cmap)):
            a, b = regular.advance(cp), var.advance(cp)
            if a is not None and b is not None and a != b:
                diffs.append({"codepoint": f"U+{cp:04X}", "static": a, "variable": b})
        if diffs:
            findings.append(finding("ADVANCE_DIFF",
                                    f"{regular.path} advances differ from the variable default instance",
                                    [regular.path, var_rel],
                                    {"count": len(diffs), "sample": diffs[:12]}))

    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = sum(1 for f in findings if f["severity"] == "warning")
    return {
        "tool": "allo-font-audit", "version": VERSION, "root": root,
        "input_sha256": input_sha, "contract": os.path.abspath(contract_path),
        "sums": os.path.abspath(sums_path),
        "summary": {"font_files": len(all_files), "parsed": len(fonts),
                    "statics": len(static_fonts), "errors": errors,
                    "warnings": warnings, "findings": len(findings)},
        "findings": findings,
        "fonts": [fonts[p].summary() for p in sorted(fonts)],
    }


def render_text(report):
    s = report["summary"]
    lines = [f"allo-font-audit {report['version']} root={report['root']}",
             f"fonts={s['font_files']} parsed={s['parsed']} statics={s['statics']} "
             f"errors={s['errors']} warnings={s['warnings']}",
             f"input_sha256={report['input_sha256']}"]
    for f in report["findings"]:
        lines.append(f"[{f['severity']}] {f['code']}: {f['message']}")
        for path in f["files"][:6]:
            lines.append(f"    {path}")
        if f["code"] in ("ADVANCE_DIFF", "CMAP_DIFF", "VERTICAL_DIFF"):
            lines.append("    " + json.dumps(f["detail"], sort_keys=True)[:400])
    if not report["findings"]:
        lines.append("OK: no findings")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="allo-font-audit",
                                 description="Audit a shipped font release against its contract.")
    ap.add_argument("root", help="release root (contains fonts/)")
    ap.add_argument("--contract", required=True, help="contract JSON")
    ap.add_argument("--sums", help="SHA256SUMS path (default: <root>/fonts/SHA256SUMS)")
    ap.add_argument("--json", action="store_true", help="emit JSON on stdout")
    ap.add_argument("--output", help="also write the report to this path")
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args(argv)
    try:
        report = run_audit(args.root, args.contract, args.sums)
    except AuditError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, sort_keys=True) if args.json else render_text(report)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(text)
    return 1 if any(x["severity"] == "error" for x in report["findings"]) else 0


if __name__ == "__main__":
    sys.exit(main())