# Amarisoft UE Config Reference

## File: amarisoft_ue.cfg

The config the Amarisoft UE simulator (`lteue`) was started with. **JSON5** —
supports `//` and `/* */` comments and trailing commas, so a plain JSON parser
will reject it. Unlike OCUDU's `ocudu_gnb.yml` it is a **single document** (no
concatenation, no duplicate-key overrides) — what you read is what applied.

The simulator does not echo an effective config into `ue.log` the way the gNB
does; this file *is* the source of truth for what the UE was told to do.

### Reading shortcut

```bash
ls -lh amarisoft_ue.cfg
wc -l amarisoft_ue.cfg        # ~100–165 lines, safe to read in full
```

## Top-level structure

| Key | Purpose |
|---|---|
| `log_options` | ue.log verbosity (see § Field reference) |
| `log_filename` | absolute path the UE writes `ue.log` to |
| `com_addr` | remote-API / WebSocket bind (e.g. `0.0.0.0:9002`); cosmetic for analysis |
| `license_server` | Amarisoft license endpoint (masked in stored configs) |
| `rf_driver` | radio/sample transport — for OCUDU runs this is the ZMQ-style link to the gNB |
| `tx_gain` / `rx_gain` / `tx_time_offset` | RF front-end trims (0 in simulated runs) |
| `cell_groups[]` | the cell(s) the UE can camp on / hand over between |
| `caps` | UE capability-set id |
| `ue_list[]` | the simulated UE(s): identity, credentials, and the event script |

## Field reference

### `log_options`

Comma-separated `layer.key=value` list controlling what lands in `ue.log` — the
UE-side analog of OCUDU's `log.<layer>_level`. Common keys:

| Token | Effect |
|---|---|
| `all.level=debug` | base verbosity for every layer (`debug` ⇒ full per-message trace) |
| `all.level=warn` | warnings only (Retina): no PHY PRACH, NAS state or RRC message records; count from `metrics.json` |
| `all.max_size=0` | hex-dump bytes per PDU; `0` = none |
| `time.us=1` | microsecond record times (`HH:MM:SS.uuuuuu`) |
| `<layer>.level=info` | per-layer override (e.g. `ip.level=info`) |
| `phy.dci_size`/`phy.csi`/`phy.cch`/`phy.cell_meas=1` | extra PHY detail (DCI sizes, CSI, control channels, cell measurements) |
| `rrc.max_size=N` / `nas.key=1` / `rrc.key=1` | RRC dump size; log NAS/RRC security keys |

If an expected line is missing from `ue.log`, check whether `log_options` raised
that layer (see `log-format.md` § Per-layer format for what each layer emits).

### `rf_driver`

| Field | Notes |
|---|---|
| `name` | `"ocudu"` for OCUDU-driven tests (ZMQ-style sample transport); `"zmq"`/`"uhd"` etc. otherwise |
| `tx_port0..N` / `rx_port0..N` | per-antenna-port TCP endpoints; `rx_port*` points at the **gNB IP** (e.g. `tcp://172.20.0.4:31000`) |
| `args` | driver args, e.g. `id=ue,base_srate=122880000` |
| `dl_sample_bits` / `ul_sample_bits` | sample width (16) |
| `sync` | clock sync source — `"none"` in simulated runs |

A `name: "ocudu"` (or `zmq`) driver with `tx_port*/rx_port*` means **sample
transport over TCP to a co-located gNB** — UE and gNB share a host and therefore
a clock (see the `correlate` type's cross-correlation model, § Clocks).

### `cell_groups[].cells[]`

One entry per cell; **more than one `rf_port` ⇒ a multi-cell, handover-capable
setup**. Group-level flags: `multi_ue` (one RF stream shared by all simulated
UEs), `channel_sim` / `delay_sim` (enable RF channel + propagation-delay
modelling — present in mobility/fading runs, absent ⇒ ideal channel),
`pdcch_decode_opt[_threshold]` (drop weak PDCCH candidates to cut false positives
and CPU).

| Field | Notes |
|---|---|
| `rf_port` | antenna-port index; cell ↔ `rf_driver` port |
| `band` | NR band (e.g. 3, 78) |
| `bandwidth` | channel bandwidth in MHz (5, 50, 100) |
| `dl_nr_arfcn` / `ssb_nr_arfcn` | DL + SSB ARFCN |
| `subcarrier_spacing` / `ssb_subcarrier_spacing` | numerology SCS in kHz (15, 30) |
| `n_antenna_dl` / `n_antenna_ul` | MIMO layout |
| `sample_rate` | sample rate in MHz (122.88) |
| `global_timing_advance` | `-1` = auto |
| `position` / `ref_signal_power` / `ul_power_attenuation` / `antenna` | mobility/channel-sim runs only: cell geometry and power |

### `ue_list[]`

| Field | Notes |
|---|---|
| `ue_count` | number of UEs simulated from this block; `1` = single-UE, `>1` = multi-UE |
| `imsi` | base IMSI; with `ue_count > 1` the simulator allocates a contiguous block from here |
| `imeisv` | optional device identity |
| `sim_algo` / `K` | SIM auth algorithm (`"xor"` test mode) + key |
| `apn` / `attach_pdn_type` | PDN APN and type (`ipv4`) |
| `as_release` | AS release (15; **16 is required for conditional HO**) |
| `ue_category` | `"nr"` |
| `spec_tolerance` | relax strict spec conformance |
| `sim_events_loop_count` / `sim_events_loop_delay` | repeat the event list N times, delay s between loops — drives attach/detach **churn** runs |
| `channel` / `max_distance` / `bounce` / `position` / `speed` / `direction` | mobility runs only: UE trajectory and channel type (`awgn`) |
| `sim_events[]` | the timed scenario script (below) |

### `sim_events[]`

The per-UE scenario: a list of timed actions, each with `start_time` (seconds
from UE start) and an `event`. This is the script behind the lifecycle you see in
`ue.log`/`stdout.log`.

| `event` | Extra fields | Meaning |
|---|---|---|
| `power_on` / `power_off` | — | UE attach / detach |
| `quit` | — | simulator exit |
| `cbr_send` / `cbr_recv` | `dst_addr`, `payload_len`, `bit_rate`, `end_time` | constant-bit-rate UL/DL traffic window |
| `ping` | `dst_addr`, `payload_len`, `delay`, `end_time` | periodic ping |

## Quick checks

```bash
# Cell parameters (band / BW / ARFCN / SCS), one block per cell
grep -E "band:|bandwidth:|dl_nr_arfcn:|subcarrier_spacing:" amarisoft_ue.cfg

# How many UEs, and is it a churn loop?
grep -E "ue_count:|sim_events_loop_count:|sim_events_loop_delay:" amarisoft_ue.cfg

# RF backend + the gNB endpoint it talks to
grep -E "name:|rx_port0:|args:" amarisoft_ue.cfg

# Multi-cell (handover-capable)?  >1 => yes
grep -c "rf_port:" amarisoft_ue.cfg

# Mobility / channel modelling enabled?
grep -E "channel_sim:|delay_sim:|speed:|position:" amarisoft_ue.cfg

# Scenario timeline
grep -E "event:|start_time:" amarisoft_ue.cfg
```
