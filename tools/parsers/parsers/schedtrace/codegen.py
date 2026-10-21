# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Generates _generated.py, the decoders of CellEvent buffers compiled from the schemas of fbs/: one function per
table and struct, with the vtable slots, struct formats, enums and defaults of their fields resolved. root() decodes
as flatbuf.Decoder does, several times faster, and root_grid() only the fields of GRID_FIELDS, faster still.

Run it after changing fbs/ or this file:

    python -m parsers.schedtrace.codegen

or to copy the schemas of an OCUDU tree into fbs/ first, of its working tree or of a
revision:

    python -m parsers.schedtrace.codegen --ocudu ~/srs/ocudu [--rev origin/dev]

The generated module holds the file format version of the schemas, FORMAT_VERSION, and the hash of its sources,
SCHEMA_HASH, which a test checks against source_hash().
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path
from typing import Any

from . import schema as fbs
from .schema import SCALARS, Schema, Struct, Table

HERE = Path(__file__).parent
FBS_DIR = HERE / "fbs"
OUTPUT = HERE / "_generated.py"
# Directory of the schemas in an OCUDU tree.
OCUDU_FBS_DIR = "lib/scheduler/trace/fbs"
# Single-value enums of the schemas that hold the major and minor file format version.
VERSION_ENUMS = ("FileFormatVersionMajor", "FileFormatVersionMinor")
# Names of the scalar unpackers of the generated module, by struct format character.
_FORMAT_NAMES = {"?": "bool", "b": "i8", "B": "u8", "h": "i16", "H": "u16", "i": "i32", "I": "u32", "q": "i64", "Q": "u64",
                 "f": "f32", "d": "f64"}

# Fields that root_grid() keeps, by table: those that grid.allocations(), ue_metrics and harq read, the timestamp,
# slot and latency of an event, and the type of each input. Tables not listed keep all their fields.
GRID_FIELDS: dict[str, tuple[str, ...]] = {
    "CellSlotEvent": ("slot_tx", "latency_us", "inputs", "decision"),
    "SlotDecision": ("dl_pdcchs", "ul_pdcchs", "sib_pdschs", "rar_pdschs", "paging_pdschs", "ue_pdschs", "puschs",
                     "pucchs", "ssbs", "csi_rs", "prachs", "failed_attempts"),
    "Pdsch": ("rnti", "rbs", "sym_start", "sym_length", "codewords", "harq_id", "ue_index", "nof_retxs", "k1"),
    "Pusch": ("rnti", "rbs", "sym_start", "sym_length", "rv_index", "nof_layers", "harq_id", "tb_size_bytes", "ue_index",
              "nof_retxs", "k2"),
    "Pucch": ("rnti", "res_idx", "nof_bits_harq_ack", "nof_bits_sr", "nof_bits_csi1"),
    "SibPdsch": ("is_sib1", "rbs", "sym_start", "sym_length"),
    "RarPdsch": ("rnti", "rbs", "sym_start", "sym_length", "grants"),
    "PagingPdsch": ("rbs", "sym_start", "sym_length", "paging_ues"),
    "RachIndication": (),
    "HarqAckEvent": ("slot_rx", "ue_idx", "rnti", "harq_id", "status", "tbs"),
    "SrEvent": ("ue_idx", "rnti"),
    "CsiReportEvent": ("ue_idx", "rnti", "rsrp_dbm", "ri", "first_tb_wb_cqi"),
}

_PRELUDE = '''
import struct
from typing import Any

_unpack_from = struct.unpack_from
_u16 = struct.Struct("<H").unpack_from
_u32 = struct.Struct("<I").unpack_from
_i32 = struct.Struct("<i").unpack_from


def _string(buf, at):
    length = _u32(buf, at)[0]
    return bytes(buf[at + 4 : at + 4 + length]).decode("utf-8", "replace")


def _strings(buf, at):
    return [_string(buf, e + _u32(buf, e)[0]) for e in range(at + 4, at + 4 + 4 * _u32(buf, at)[0], 4)]


def _scalars(buf, at, fmt):
    return list(_unpack_from("<%d%s" % (_u32(buf, at)[0], fmt), buf, at + 4))


def _enums(buf, at, fmt, names):
    return [names.get(v, v) for v in _unpack_from("<%d%s" % (_u32(buf, at)[0], fmt), buf, at + 4)]


def _structs(buf, at, size, iter_unpack, build):
    return [build(v) for v in iter_unpack(buf[at + 4 : at + 4 + size * _u32(buf, at)[0]])]


def _tables(buf, at, table):
    return [table(buf, e + _u32(buf, e)[0]) for e in range(at + 4, at + 4 + 4 * _u32(buf, at)[0], 4)]


def _union(buf, at, kind, members):
    member = members.get(kind)
    return member(buf, at + _u32(buf, at)[0]) if member else {"type": kind}


def _union_vector(buf, types_at, values_at, members):
    types_at += _u32(buf, types_at)[0]
    values_at += _u32(buf, values_at)[0] + 4
    out = []
    for i in range(_u32(buf, types_at)[0]):
        kind = buf[types_at + 4 + i]
        out.append(_union(buf, values_at + 4 * i, kind, members) if kind else None)
    return out
'''


def source_hash() -> str:
    """Hash of the sources of the generated decoder: the schemas of fbs/, this file and the schema parser, without their
    license headers, which the header annotator rewrites.
    """
    h = hashlib.sha256()
    for path in [*sorted(FBS_DIR.glob("*.fbs")), Path(__file__), Path(fbs.__file__)]:
        h.update(path.name.encode() + b"\0")
        for line in path.read_text().splitlines():
            if "SPDX-" not in line:
                h.update(line.encode() + b"\n")
    return h.hexdigest()


def generate(sch: Schema, source: str) -> str:
    """The source of the decoder module of a schema, with source as its SCHEMA_HASH."""
    return _Generator(sch).module(source)


class _Generator:
    def __init__(self, sch: Schema):
        self.s = sch
        # struct format characters of the scalar unpackers the module defines.
        self.formats: set[str] = set()
        # Prefix of the function names of the decoder being generated.
        self.prefix = ""
        # Fields that the decoder being generated keeps, by table, all of them for tables not in it.
        self.keep: dict[str, tuple[str, ...]] = {}

    def module(self, source: str) -> str:
        if self.s.root_type is None:
            raise ValueError("No root type.")
        body: list[str] = []
        for st in self.s.structs.values():
            body += self._struct(st)
        unions: list[str] = []
        for prefix, keep in (("", {}), ("grid_", GRID_FIELDS)):
            self.prefix, self.keep = prefix, keep
            tables, union_lines = self._decoder()
            body += tables
            unions += union_lines
        consts = [f"_ENUM_{e.name} = {e.names!r}" for e in self.s.enums.values()]
        consts += [f'_s_{_FORMAT_NAMES[c]} = struct.Struct("<{c}").unpack_from' for c in sorted(self.formats)]
        for st in self.s.structs.values():
            consts.append(f'_STRUCT_{st.name} = struct.Struct("<{self._format(st)}")')
        # Unpackers of the vtable offsets of n fields, by n.
        max_slots = max((f.slot + 1 for t in self.s.tables.values() for f in t.fields), default=0)
        consts.append(f'_VTABLES = [struct.Struct("<%dH" % n).unpack_from for n in range({max_slots + 1})]')
        header = "\n".join(line for line in Path(__file__).read_text().splitlines()[:2])
        return "\n".join([
            header,
            "",
            '"""Decoder of CellEvent buffers, generated by codegen.py from the schemas of fbs/. Do not edit: run',
            "python -m parsers.schedtrace.codegen instead.",
            '"""',
            "",
            "# ruff: noqa",
            "# fmt: off",
            _PRELUDE,
            f'SCHEMA_HASH = "{source}"',
            f"FORMAT_VERSION = {format_version(self.s)}",
            "",
            *consts,
            "",
            *body,
            "",
            *unions,
            "",
            "",
            "def root(buf: bytes | memoryview) -> dict[str, Any]:",
            '    """The CellEvent of a buffer, as flatbuf.Decoder.root() gives it."""',
            f"    return _table_{self.s.root_type}(buf, _u32(buf, 0)[0])",
            "",
            "",
            "def root_grid(buf: bytes | memoryview) -> dict[str, Any]:",
            '    """The CellEvent of a buffer, as root() gives it, with only the fields of GRID_FIELDS of codegen.py."""',
            f"    return _grid_table_{self.s.root_type}(buf, _u32(buf, 0)[0])",
            "",
        ])

    def _fields(self, t: Table) -> list[fbs.Field]:
        """The fields of a table that the decoder being generated keeps."""
        keep = self.keep.get(t.name)
        return t.fields if keep is None else [f for f in t.fields if f.name in keep]

    def _decoder(self) -> tuple[list[str], list[str]]:
        """The functions of the tables of the decoder being generated, those reachable from the root type through the
        fields it keeps, and the dicts of the members of its unions.
        """
        for name, keep in self.keep.items():
            if name not in self.s.tables:
                raise ValueError(f"No table {name}.")
            if missing := set(keep) - {f.name for f in self.s.tables[name].fields}:
                raise ValueError(f"No fields {sorted(missing)} in table {name}.")
        tables, members, unions = {self.s.root_type}, set(), set()
        pending = [self.s.root_type]
        while pending:
            for f in self._fields(self.s.tables[pending.pop()]):
                tname = f.type.name
                if tname in self.s.unions and tname not in unions:
                    unions.add(tname)
                    new = set(self.s.unions[tname].members.values()) - members - tables
                    members.update(self.s.unions[tname].members.values())
                    pending += new
                elif tname in self.s.tables and tname not in tables:
                    tables.add(tname)
                    if tname not in members:
                        pending.append(tname)
        body: list[str] = []
        for t in self.s.tables.values():
            if t.name in tables:
                body += self._table(t, None)
            if t.name in members:
                body += self._table(t, t.name)
        union_lines = [
            f"{self._name('members', u.name)} = {{"
            + ", ".join(f"{k}: {self._name('member', m)}" for k, m in u.members.items())
            + "}"
            for u in self.s.unions.values()
            if u.name in unions
        ]
        return body, union_lines

    def _name(self, kind: str, name: str) -> str:
        """The name of the function of a table ("table") or union member ("member"), or of the dict of the members of
        a union ("members"), in the decoder being generated.
        """
        return f"_{self.prefix}{kind}_{name}"

    def _scalar(self, type_name: str) -> str | None:
        """The struct format character of a scalar or enum type, else None."""
        scalar = fbs.scalar_of(type_name, self.s)
        return SCALARS[scalar][0] if scalar is not None else None

    def _unpacker(self, fmt: str) -> str:
        self.formats.add(fmt)
        return f"_s_{_FORMAT_NAMES[fmt]}"

    def _format(self, st: Struct) -> str:
        """The struct format of a struct, its fields flattened, padding as "x"."""
        out, end = [], 0
        for f, offset in zip(st.fields, st.offsets):
            if offset > end:
                out.append(f"{offset - end}x")
            if (fmt := self._scalar(f.type.name)) is not None:
                out.append(fmt)
                end = offset + SCALARS[fbs.scalar_of(f.type.name, self.s)][1]
            else:
                inner = self.s.structs[f.type.name]
                out.append(self._format(inner))
                end = offset + inner.size
        if st.size > end:
            out.append(f"{st.size - end}x")
        return "".join(out)

    def _struct_value(self, st: Struct, index: int) -> tuple[str, int]:
        """A dict expression of a struct from the tuple v of its flattened fields, from index, and the next index."""
        items = []
        for f in st.fields:
            if self._scalar(f.type.name) is not None:
                expr = f"v[{index}]"
                if f.type.name in self.s.enums:
                    expr = f"_ENUM_{f.type.name}.get(v[{index}], v[{index}])"
                index += 1
            else:
                expr, index = self._struct_value(self.s.structs[f.type.name], index)
            items.append(f'"{f.name}": {expr}')
        return "{" + ", ".join(items) + "}", index

    def _struct(self, st: Struct) -> list[str]:
        value, _ = self._struct_value(st, 0)
        return [
            "",
            "",
            f"def _build_{st.name}(v):",
            f"    return {value}",
            "",
            "",
            f"def _struct_{st.name}(buf, at):",
            f"    return _build_{st.name}(_STRUCT_{st.name}.unpack_from(buf, at))",
        ]

    def _default(self, default: Any, type_name: str) -> str:
        """The value of an absent scalar field, as flatbuf.Decoder gives it."""
        if default is None:
            return "None"
        if fbs.scalar_of(type_name, self.s) == "bool":
            return repr(bool(default))
        enum = self.s.enums.get(type_name)
        return repr(enum.names.get(default, default) if enum else default)

    def _table(self, t: Table, member: str | None) -> list[str]:
        """The function of a table, or of a union member, which adds its "type"."""
        fields = self._fields(t)
        nof_slots = max((f.slot + 1 for f in fields), default=0)
        name = self._name("member", t.name) if member else self._name("table", t.name)
        lines = ["", "", f"def {name}(buf, pos):"]
        if nof_slots:
            lines += [
                "    vt = pos - _i32(buf, pos)[0]",
                "    n = (_u16(buf, vt)[0] - 4) >> 1",
                # A newer writer may have more fields, an older one fewer, which are absent.
                f"    vo = _VTABLES[{nof_slots}](buf, vt + 4) if n >= {nof_slots} else _VTABLES[n](buf, vt + 4) + (0,) * ({nof_slots} - n)",
            ]
        items = [f'"type": "{member}"'] if member else []
        for i, f in enumerate(fields):
            var = f"v{i}"
            lines += self._field(f, var)
            items.append(f'"{f.name}": {var}')
        lines.append("    return {")
        lines += [f"        {item}," for item in items]
        lines.append("    }")
        return lines

    def _field(self, f: fbs.Field, var: str) -> list[str]:
        """Statements that decode a field of a table into var, given its vtable offsets vo."""
        tname = f.type.name
        lines = [f"    o = vo[{f.slot}]"]
        if tname in self.s.unions:
            lines.append(f"    t = vo[{f.slot - 1}]")
            if f.type.vector:
                lines.append(f"    {var} = _union_vector(buf, pos + t, pos + o, {self._name('members', tname)}) if t and o else None")
            else:
                lines += [
                    "    kind = buf[pos + t] if t else 0",
                    "    if o and kind:",
                    f"        member = {self._name('members', tname)}.get(kind)",
                    f'        {var} = member(buf, pos + o + _u32(buf, pos + o)[0]) if member else {{"type": kind}}',
                    "    else:",
                    f"        {var} = None",
                ]
            return lines
        if f.type.vector:
            lines.append(f"    {var} = {self._vector(tname)} if o else None")
            return lines
        if (fmt := self._scalar(tname)) is not None:
            default = self._default(f.default, tname)
            if tname in self.s.enums:
                value = f"_ENUM_{tname}.get(x := {self._unpacker(fmt)}(buf, pos + o)[0], x)"
            elif fmt == "B":
                value = "buf[pos + o]"
            else:
                value = f"{self._unpacker(fmt)}(buf, pos + o)[0]"
            lines.append(f"    {var} = {value} if o else {default}")
        elif tname in self.s.structs:
            lines.append(f"    {var} = _struct_{tname}(buf, pos + o) if o else None")
        elif tname == "string":
            lines.append(f"    {var} = _string(buf, pos + o + _u32(buf, pos + o)[0]) if o else None")
        else:
            lines.append(f"    {var} = {self._name('table', tname)}(buf, pos + o + _u32(buf, pos + o)[0]) if o else None")
        return lines

    def _vector(self, tname: str) -> str:
        """An expression of a vector field, at its offset o from pos."""
        at = "pos + o + _u32(buf, pos + o)[0]"
        if (fmt := self._scalar(tname)) is not None:
            if tname in self.s.enums:
                return f'_enums(buf, {at}, "{fmt}", _ENUM_{tname})'
            return f'_scalars(buf, {at}, "{fmt}")'
        if tname in self.s.structs:
            st = self.s.structs[tname]
            return f"_structs(buf, {at}, {st.size}, _STRUCT_{tname}.iter_unpack, _build_{tname})"
        if tname == "string":
            return f"_strings(buf, {at})"
        return f"_tables(buf, {at}, {self._name('table', tname)})"


def format_version(sch: Schema) -> tuple[int, int]:
    """The file format version of a schema, (major, minor), from the Value of its VERSION_ENUMS."""
    out = []
    for name in VERSION_ENUMS:
        enum = sch.enums.get(name)
        values = [v for v, n in (enum.names.items() if enum else ()) if n == "Value"]
        if not values:
            raise ValueError(f"The schemas lack the file format version: enum {name} with a Value.")
        out.append(values[0])
    return out[0], out[1]


def sync(ocudu: Path, rev: str | None = None) -> None:
    """Replaces the schemas of fbs/ with those of an OCUDU tree, of a revision or its working tree."""
    if rev is None:
        files = {p.name: p.read_bytes() for p in (ocudu / OCUDU_FBS_DIR).glob("*.fbs")}
    else:
        listing = _git(ocudu, "ls-tree", "--name-only", f"{rev}:{OCUDU_FBS_DIR}").decode().split()
        # Read all before writing any, so that a failed read leaves fbs/ as it was.
        files = {n: _git(ocudu, "show", f"{rev}:{OCUDU_FBS_DIR}/{n}") for n in listing if n.endswith(".fbs")}
    if not files:
        raise SystemExit(f"No schemas in {ocudu / OCUDU_FBS_DIR}{f' at {rev}' if rev else ''}.")
    for old in FBS_DIR.glob("*.fbs"):
        old.unlink()
    for name, content in files.items():
        (FBS_DIR / name).write_bytes(content)
    print(f"Copied {len(files)} schemas into {FBS_DIR}.")


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


def main() -> None:
    ap = argparse.ArgumentParser(description="Generates the schedtrace decoder from the schemas of fbs/.")
    ap.add_argument("--ocudu", type=Path, help="OCUDU tree to copy the schemas from first")
    ap.add_argument("--rev", help="revision of the OCUDU tree to copy from, instead of its working tree")
    args = ap.parse_args()
    if args.rev and not args.ocudu:
        ap.error("--rev needs --ocudu")
    if args.ocudu:
        sync(args.ocudu.expanduser(), args.rev)
    OUTPUT.write_text(generate(fbs.load(FBS_DIR / "cell_event.fbs"), source_hash()))
    print(f"Wrote {OUTPUT}.")


if __name__ == "__main__":
    main()
