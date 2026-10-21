# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Parsing of the application configuration that OCUDU echoes at the top of its logs: log levels, node planes, cell
and AMF values, duplex mode and enabled pcaps.

The echo is logged by the CONFIG logger, with all values at debug level and only the non-default values at info level.
The same text format is used by the YAML configuration files, which parse_config() also accepts.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from . import events

# Log levels, from least to most verbose.
LEVELS = ("none", "error", "warning", "info", "debug")
# Level of the layers that neither their own option nor all_level set.
DEFAULT_LEVEL = "warning"
# Options that all_level does not set.
_NOT_FROM_ALL = ("lib",)

# Logger name to the log level option that controls it, without the "_level" suffix.
LOGGER_OPTIONS = {
    "MAC": "mac",
    "SCHED": "mac",
    "PHY": "phy",
    "RLC": "rlc",
    "PDCP": "pdcp",
    "SDAP": "sdap",
    "RRC": "rrc",
    "NGAP": "ngap",
    "XNAP": "xnap",
    "CU-CP": "cu",
    "CU-UP": "cu",
    "CU-CP-F1": "f1ap",
    "DU-F1": "f1ap",
    "CU-CP-E1": "e1ap",
    "CU-UP-E1": "e1ap",
    "CU-F1-U": "f1u",
    "DU-F1-U": "f1u",
    "DU": "du",
    "DU-MNG": "du",
    "GTPU": "gtpu",
    "SEC": "sec",
    "FAPI": "fapi",
    "E2AP": "e2ap",
}

# Top-level configuration sections that reveal each plane of the node.
_PLANE_SECTIONS = {
    "CU-CP": re.compile(r"(?m)^cu_cp:"),
    "CU-UP": re.compile(r"(?m)^cu_up:"),
    "DU": re.compile(r"(?m)^(?:cells|cell_cfg|ru_sdr|ru_ofh|du_high):"),
}
# Scalar options kept in AppConfig.values.
CELL_KEYS = ("pci", "band", "dl_arfcn", "channel_bandwidth_MHz", "common_scs", "nof_antennas_dl", "nof_antennas_ul")
VALUE_KEYS = (
    "gnb_id", "ran_node_name", "hex_max_size",
    *CELL_KEYS,
    "srate", "device_driver", "sync", "time_alignment_calibration",
    "enable_log", "enable_json", "autostart_stdout_metrics",
)
# Parents of the options of a cell, a list item being "-": cell_cfg, or an item of the cells list.
_CELL_PATHS = (("cell_cfg",), ("cells", "-"))
# Protocols of the "<proto>_enable: true" options of the pcap section.
PCAP_PROTOCOLS = ("ngap", "f1ap", "e1ap", "mac", "rlc", "n3", "f1u", "xnap", "e2ap_cu_cp", "e2ap_cu_up", "e2ap_du")
_SCALAR_RE = re.compile(r"(?P<key>[\w-]+)\s*:\s*(?P<value>.*?)\s*(?:#.*)?$")
_PCAP_RE = re.compile(r"^\s*(\w+)_enable\s*:\s*true\s*$", re.MULTILINE)
_DUPLEX_RE = re.compile(r"^\s*(?:-\s+)?duplex\s*:\s*(?P<mode>tdd|fdd)\b", re.MULTILINE)
# NR bands of TDD duplex, as OCUDU's band_helper lists them from TS 38.101-1 Table 5.2-1 and TS 38.101-2 Table 5.2-1.
# OCUDU sets a default TDD pattern for a band of these without one.
TDD_BANDS = frozenset({34, 38, 39, 40, 41, 46, 48, 50, 51, 53, 77, 78, 79, 90, 96, 101, 102, 104,
                       257, 258, 259, 260, 261, 262})
_AMF_RE = re.compile(r"^(?P<indent>\s*)amf\s*:\s*(?:#.*)?$")
_KEY_VALUE_RE = re.compile(r"^\s*(?P<key>\w+)\s*:\s*(?P<value>[^#]*?)\s*(?:#.*)?$")
_LIST_ITEM_RE = re.compile(r"^\s*-\s*(?P<value>[^#]*?)\s*(?:#.*)?$")
_IPV4_RE = re.compile(r"\d+\.\d+\.\d+\.\d+")
_ECHO_HEADER_RE = re.compile(r"input configuration \((?P<scope>all values|only non-default values)\)", re.IGNORECASE)
_LEVEL_RE = re.compile(r"^\s+(?P<option>\w+)_level\s*:\s*(?P<level>\w+)")
# Lines searched for the echo header before giving up.
_MAX_HEADER_LINES = 1000


@dataclass
class AppConfig:
    """Log levels, planes and main values of an OCUDU application, from its configuration."""

    # Explicitly configured log levels, by option name without the "_level" suffix.
    levels: dict[str, str] = field(default_factory=dict)
    # Planes of the node: "CU-CP", "CU-UP" and "DU".
    planes: list[str] = field(default_factory=list)
    # Whether the configuration has all values, rather than only the non-default ones.
    complete: bool = True
    # Values of the options of VALUE_KEYS that are set, as text, the last one where an option repeats.
    values: dict[str, str] = field(default_factory=dict)
    # Protocols of PCAP_PROTOCOLS with an enabled pcap, in the order first seen.
    pcaps: list[str] = field(default_factory=list)
    # Whether the cell is TDD: by its TDD pattern or duplex option, else by its band, else None.
    tdd: bool | None = None
    # IPv4 addresses of the AMF and of the local end of the connection to it, and the AMF port.
    amf_addrs: list[str] = field(default_factory=list)
    amf_bind_addrs: list[str] = field(default_factory=list)
    amf_port: int | None = None

    def level(self, option: str) -> str:
        """Effective log level of an option, e.g. "mac": its own, else all_level's, else the OCUDU default."""
        if option in self.levels:
            return self.levels[option]
        inherited = self.levels.get("all") if option not in _NOT_FROM_ALL else None
        if option == "config":
            # The config logger supports none, info and debug only.
            return inherited if inherited in ("none", "info", "debug") else "none"
        return inherited or DEFAULT_LEVEL

    def logger_level(self, logger: str) -> str | None:
        """Effective log level of a logger, e.g. "SCHED", or None if no option controls it."""
        option = LOGGER_OPTIONS.get(logger)
        return self.level(option) if option else None

    def logs_at(self, logger: str, level: str) -> bool:
        """Returns whether a logger prints messages of the given level."""
        effective = self.logger_level(logger)
        return effective is not None and LEVELS.index(effective) >= LEVELS.index(level)

    @property
    def node_type(self) -> str | None:
        """Node type, e.g. "gNB", "DU" or "CU", or None when no plane is configured."""
        planes = set(self.planes)
        if "DU" in planes and planes & {"CU-CP", "CU-UP"}:
            return "gNB"
        if planes == {"CU-CP", "CU-UP"}:
            return "CU"
        return "+".join(self.planes) or None


def parse_config(text: str, complete: bool = True) -> AppConfig:
    """Parses a configuration in the YAML format. Repeated keys keep the last value, as in concatenated documents."""
    cfg = AppConfig(complete=complete)
    in_log = False
    for line in text.splitlines():
        if line and not line[0].isspace():
            in_log = line.startswith("log:")
            continue
        if in_log and (m := _LEVEL_RE.match(line)):
            cfg.levels[m.group("option")] = m.group("level")
    cfg.planes = [plane for plane, pattern in _PLANE_SECTIONS.items() if pattern.search(text)]
    for path, key, value in _scalars(text):
        # Cell options also name neighbour cells, e.g. the pci of the SIB3 neighbours of a cell, deeper.
        if key in VALUE_KEYS and (key not in CELL_KEYS or path in _CELL_PATHS):
            cfg.values[key] = value.strip('"')
    for proto in _PCAP_RE.findall(text):
        if proto in PCAP_PROTOCOLS and proto not in cfg.pcaps:
            cfg.pcaps.append(proto)
    if "tdd_ul_dl_cfg" in text:
        cfg.tdd = True
    elif m := _DUPLEX_RE.search(text):
        cfg.tdd = m.group("mode") == "tdd"
    elif cfg.values.get("band", "").isdigit():
        cfg.tdd = int(cfg.values["band"]) in TDD_BANDS
    _parse_amf(text, cfg)
    return cfg


def _scalars(text: str) -> Iterator[tuple[tuple[str, ...], str, str]]:
    """The scalar options of a YAML text, as (path, key, value), the path being the keys of their parents, "-" for a
    list item, in text order.
    """
    # Parents of the current line, as (indent, key or "-").
    stack: list[tuple[int, str]] = []
    for line in text.splitlines():
        rest = line.lstrip()
        if not rest or rest.startswith("#"):
            continue
        indent = len(line) - len(rest)
        if rest == "-" or rest.startswith(("- ", "-\t")):
            # A list may sit at the indent of its parent key.
            while stack and (stack[-1][0] > indent or (stack[-1][0] == indent and stack[-1][1] == "-")):
                stack.pop()
            stack.append((indent, "-"))
            item = rest[1:].lstrip()
            # A dash alone starts an item whose keys follow on the next lines.
            if not item:
                continue
            indent += len(rest) - len(item)
            rest = item
        while stack and stack[-1][0] >= indent:
            stack.pop()
        m = _SCALAR_RE.fullmatch(rest)
        if not m:
            continue
        if m.group("value"):
            yield tuple(name for _, name in stack), m.group("key"), m.group("value")
        else:
            stack.append((indent, m.group("key")))


def _parse_amf(text: str, cfg: AppConfig) -> None:
    """Sets the AMF addresses and port of the amf sections, wherever they are nested, e.g. under cu_cp."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _AMF_RE.match(lines[i])
        i += 1
        if not m:
            continue
        indent = len(m.group("indent"))
        key = None
        # The section ends at the first line indented as much as its header, and holds nested amf sections.
        while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
            line = lines[i]
            i += 1
            if km := _KEY_VALUE_RE.match(line):
                key, value = km.group("key"), km.group("value")
            elif (lm := _LIST_ITEM_RE.match(line)) and key is not None:
                value = lm.group("value")
            else:
                continue
            if key in ("addrs", "addr"):
                cfg.amf_addrs += [a for a in _IPV4_RE.findall(value) if a not in cfg.amf_addrs]
            elif key in ("bind_addrs", "bind_addr"):
                cfg.amf_bind_addrs += [a for a in _IPV4_RE.findall(value) if a not in cfg.amf_bind_addrs]
            elif key == "port" and value.isdigit() and cfg.amf_port is None:
                cfg.amf_port = int(value)


def is_echo_header(line: str) -> bool:
    """Whether a CONFIG log line is the header of the configuration echo, e.g. "Input configuration (all values):"."""
    return _ECHO_HEADER_RE.search(line) is not None


def extract_echo(lines: Iterable[str]) -> tuple[str, bool] | None:
    """Returns the configuration echoed in a log and whether it has all values, or None if the log has no echo.

    Stops reading at the end of the echo, or after the first lines when there is none.
    """
    body: list[str] = []
    complete = None
    for line_no, line in enumerate(lines):
        if complete is None:
            if "[CONFIG" in line and (m := _ECHO_HEADER_RE.search(line)):
                complete = m.group("scope") == "all values"
            elif line_no >= _MAX_HEADER_LINES:
                return None
            continue
        if events.is_entry_start(line):
            break
        body.append(line)
    if complete is None:
        return None
    return "".join(body), complete


def from_log(lines: Iterable[str]) -> AppConfig | None:
    """Parses the configuration echoed in a log, or returns None if the log has no echo."""
    echo = extract_echo(lines)
    return parse_config(*echo) if echo else None
