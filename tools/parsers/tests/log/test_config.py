# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import unittest

from parsers.log import config

# Head of a real log with the configuration echo, shortened.
LOG = """\
2026-08-07T08:46:25.252000 [GNB     ] [I] Built in RelWithDebInfo mode using commit 86cd6fb on branch master
2026-08-07T08:46:25.253965 [CONFIG  ] [D] gNB input configuration (all values):
log:
  filename: /var/log/retina/2026-08-07_08-46-25/gnb.log
  all_level: info
  config_level: debug
  mac_level: debug
  rlc_level: warning
cu_cp:
  max_nof_dus: 6
cell_cfg:
  pci: 1
2026-08-07T08:46:25.300000 [CONFIG  ] [I] Worker pool "main_pool" instantiated with #workers=5
""".splitlines(keepends=True)


class ConfigTest(unittest.TestCase):
    def test_from_log(self):
        cfg = config.from_log(LOG)
        self.assertEqual(cfg.levels, {"all": "info", "config": "debug", "mac": "debug", "rlc": "warning"})
        self.assertTrue(cfg.complete)
        self.assertEqual((cfg.planes, cfg.node_type), (["CU-CP", "DU"], "gNB"))

    def test_levels(self):
        cfg = config.from_log(LOG)
        self.assertEqual((cfg.logger_level("SCHED"), cfg.logger_level("RLC"), cfg.logger_level("RRC")), ("debug", "warning", "info"))
        self.assertIsNone(cfg.logger_level("METRICS"))
        self.assertTrue(cfg.logs_at("SCHED", "debug"))
        self.assertFalse(cfg.logs_at("RRC", "debug"))
        self.assertFalse(cfg.logs_at("RLC", "info"))

    def test_defaults(self):
        cfg = config.parse_config("log:\n  filename: gnb.log\n", complete=False)
        self.assertEqual((cfg.level("mac"), cfg.level("config")), ("warning", "none"))
        self.assertFalse(cfg.complete)
        self.assertIsNone(cfg.node_type)

    def test_levels_inherited_from_all_level(self):
        cfg = config.parse_config("log:\n  all_level: debug\n  rrc_level: info\n")
        self.assertEqual([cfg.level(o) for o in ("rrc", "ngap", "config", "lib")], ["info", "debug", "debug", "warning"])
        cfg = config.parse_config("log:\n  all_level: error\n")
        self.assertEqual([cfg.level(o) for o in ("cu", "config")], ["error", "none"])
        self.assertFalse(cfg.logs_at("CU-CP", "info"))

    def test_last_value_wins(self):
        text = "log:\n  all_level: info\ncu_cp:\n  x: 1\nlog:\n  all_level: warning\n"
        self.assertEqual(config.parse_config(text).levels, {"all": "warning"})

    def test_only_log_section(self):
        self.assertEqual(config.parse_config("cu_cp:\n  some_level: 3\n").levels, {})

    def test_non_default_echo(self):
        log = [LOG[0], LOG[1].replace("[D]", "[I]").replace("all values", "only non-default values"), "log:\n", "  mac_level: info\n"]
        cfg = config.from_log(log)
        self.assertFalse(cfg.complete)
        self.assertEqual((cfg.level("mac"), cfg.level("rrc")), ("info", "warning"))

    def test_no_echo(self):
        self.assertIsNone(config.from_log([LOG[0], LOG[-1]]))

    def test_echo_headers(self):
        for header in ("Input configuration (all values):", "DU input configuration (only non-default values):"):
            cfg = config.from_log([LOG[0], f"2026-08-07T08:46:25.253965 [CONFIG  ] [D] {header}\n", *LOG[2:]])
            self.assertEqual(cfg.levels["mac"], "debug", header)

    def test_amf_of_an_echo(self):
        # The echo writes address lists in block style, and other sections have ports and addresses too.
        echo = """remote_control:
  port: 8001
cu_cp:
  amf:
    no_core: false
    amf:
      addrs:
        - 172.116.30.81
      port: 38412
      bind_addrs:
        - 172.116.30.51
      bind_interface: auto
  max_nof_dus: 6
e2:
  addrs:
    - 10.0.0.9
  port: 36421
"""
        cfg = config.parse_config(echo)
        self.assertEqual((cfg.amf_addrs, cfg.amf_bind_addrs, cfg.amf_port), (["172.116.30.81"], ["172.116.30.51"], 38412))


if __name__ == "__main__":
    unittest.main()

    def test_values(self):
        cfg = config.parse_config(YAML)
        self.assertEqual(cfg.values, {"gnb_id": "411", "pci": "2", "band": "78", "common_scs": "30"})
        self.assertEqual(cfg.pcaps, ["ngap", "f1ap"])
        self.assertTrue(cfg.tdd)
        self.assertEqual((cfg.amf_addrs, cfg.amf_bind_addrs, cfg.amf_port), (["10.0.0.1"], ["10.0.0.2"], 38412))

    def test_duplex(self):
        # A TDD band without a TDD pattern gets OCUDU's default one.
        self.assertTrue(config.parse_config("cell_cfg:\n  band: 78\n").tdd)
        self.assertFalse(config.parse_config("cell_cfg:\n  band: 3\n").tdd)
        self.assertTrue(config.parse_config("cell_cfg:\n  band: 3\n  tdd_ul_dl_cfg:\n    nof_dl_slots: 7\n").tdd)
        self.assertFalse(config.parse_config("ru:\n  duplex: fdd\ncell_cfg:\n  band: 78\n").tdd)
        # The band of a neighbour cell is not the cell's.
        self.assertIsNone(config.parse_config("cell_cfg:\n  dl_arfcn: 368500\n  ncells:\n    - band: 78\n").tdd)

    def test_values_of_list_items(self):
        cfg = config.parse_config("cell_cfg:\n  pci: 7\ncells:\n  - pci: 1\n    band: 78\n  - pci: 2\n")
        self.assertEqual((cfg.values["pci"], cfg.values["band"]), ("2", "78"))

    def test_values_of_items_of_a_dash_alone(self):
        cfg = config.parse_config("cells:\n  -\n    pci: 7\n    band: 3\n")
        self.assertEqual((cfg.values["pci"], cfg.values["band"]), ("7", "3"))

    def test_cell_values_of_neighbours_are_left_out(self):
        # The SIB3 neighbours of a configuration file, and the NTN neighbours of the echo, deeper in the cell.
        cfg = config.parse_config(
            "cell_cfg:\n  pci: 1\n  sib:\n    sib3:\n      intra_freq_neigh_cell_list:\n        - pci: 2\n")
        self.assertEqual(cfg.values["pci"], "1")
        cfg = config.parse_config(
            "cells:\n- pci: 3\n  ntn:\n    ncells:\n      - sat_ref: 1\n        pci: 42\n  band: 78\n")
        self.assertEqual((cfg.values["pci"], cfg.values["band"]), ("3", "78"))


# A configuration file, shortened, with a repeated option.
YAML = """gnb_id: 411
cu_cp:
  amf:
    addrs: 10.0.0.1
    port: 38412
    bind_addrs: 10.0.0.2
cell_cfg:
  pci: 1
  band: 78
  common_scs: 30
  tdd_ul_dl_cfg:
    dl_ul_tx_period: 10
pcap:
  ngap_enable: true
  f1ap_enable: true
  mac_enable: false
cell_cfg:
  pci: 2
"""
