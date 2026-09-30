#!/usr/bin/env python3
"""
Generate synthetic source-system extracts for a New Jersey combined electric and
gas utility, landed the way they would arrive in the data lake's raw zone.

Source systems simulated (folder under raw/):
  sap_isu   SAP IS-U / FI-CA: business partners, addresses, premises, contract
            accounts, installations, contracts, meters, billing, payments,
            installment plans, service orders
  sap_pm    SAP Plant Maintenance work orders
  ami       AMI head-end daily reads for a sample of electric meters
  oms       Outage Management System events
  crm       Contact center interactions
  weather   Daily weather feed (drives the load model)
  reference Towns/divisions and rate schedules

SAP extracts keep SAP-style column names, YYYYMMDD integer dates (0 = null,
99991231 = open-ended) and 'X'/'' flags, so the staging layer has real work to do.
A handful of data-quality problems are injected on purpose (see docs/source_systems.md).

Usage:
  python generate.py --scale large            # ~500K premises, ~40M+ rows
  python generate.py --scale small --out D:\\tmp\\lake

Output root defaults to %UTILITY_DATA_DIR% or C:\\Data\\utility-powerbi.
All people, accounts, addresses and amounts are fictional.
"""
import argparse
import json
import os
import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd

import reference as ref

START = np.datetime64("2024-10-01")
AS_OF = np.datetime64("2026-09-30")
MONTHS = 24
WX_START = np.datetime64("2024-09-01")
SAP_GO_LIVE = np.datetime64("2014-06-01")
OPEN_END = np.datetime64("9999-12-31")
DAY = np.timedelta64(1, "D")

SCALES = {  # premises, AMI sample meters
    "small": (20_000, 1_000),
    "medium": (100_000, 5_000),
    "large": (500_000, 25_000),
}

GZ = {"method": "gzip", "compresslevel": 5}
TS_FMT = "%Y-%m-%d %H:%M:%S"

rng: np.random.Generator = None
OUT: Path = None
manifest = {}


# ----------------------------------------------------------------------------- helpers
def days(n):
    return np.asarray(n).astype("timedelta64[D]")


def months(n):
    return np.asarray(n).astype("timedelta64[M]")


def sapdate(a):
    """datetime64 array -> int YYYYMMDD (NaT -> 0)."""
    a = np.asarray(a, dtype="datetime64[D]")
    nat = np.isnat(a)
    safe = np.where(nat, np.datetime64("2000-01-01"), a)
    y = safe.astype("datetime64[Y]").astype(np.int64) + 1970
    m = safe.astype("datetime64[M]").astype(np.int64) % 12 + 1
    d = (safe - safe.astype("datetime64[M]")).astype(np.int64) + 1
    out = y * 10000 + m * 100 + d
    out[nat] = 0
    return out


def saptime(n, start_h=7, end_h=19):
    secs = rng.integers(start_h * 3600, end_h * 3600, n)
    return (secs // 3600) * 10000 + (secs % 3600 // 60) * 100 + secs % 60


def flag(mask):
    return np.where(mask, "X", "")


def lognorm(median, sigma, n):
    return median * np.exp(rng.normal(0, sigma, n))


def pick(labels, n, p=None):
    return np.asarray(labels, dtype=object)[rng.choice(len(labels), n, p=p)]


def month_key(d):
    return np.datetime_as_string(np.asarray(d, dtype="datetime64[M]"))


def write(df, system, table, partition=None, date_format=None):
    d = OUT / system / table
    if partition:
        d = d / f"year_month={partition}"
    d.mkdir(parents=True, exist_ok=True)
    df.to_csv(d / "part-0000.csv.gz", index=False, compression=GZ, date_format=date_format)
    manifest.setdefault(f"{system}/{table}", 0)
    manifest[f"{system}/{table}"] += len(df)


def write_partitioned(df, system, table, date_col_values, date_format=None):
    keys = month_key(date_col_values)
    for k in np.unique(keys):
        write(df[keys == k], system, table, k, date_format)


def log(msg, t0=[time.time()]):
    print(f"[{time.time() - t0[0]:7.1f}s] {msg}", flush=True)


# ----------------------------------------------------------------------------- weather
def make_weather():
    d = np.arange(WX_START, AS_OF + DAY, dtype="datetime64[D]")
    n = len(d)
    idx = pd.DatetimeIndex(d)
    doy, month = idx.dayofyear.values, idx.month.values
    anom = np.zeros(n)
    eps = rng.normal(0, 4.2, n)
    for i in range(1, n):
        anom[i] = 0.68 * anom[i - 1] + eps[i]
    tavg = 54.5 - 21.0 * np.cos(2 * np.pi * (doy - 20) / 365.25) + anom
    spread = rng.uniform(13, 21, n)
    precip = np.where(rng.random(n) < 0.31, rng.gamma(0.9, 0.42, n), 0.0)
    gust = np.clip(rng.normal(23, 6, n), 8, None)
    severity = np.zeros(n)

    inwin = d >= START
    thunder = inwin & np.isin(month, [6, 7, 8]) & (rng.random(n) < 0.12)
    severity[thunder] = 0.3
    gust[thunder] = rng.uniform(35, 48, thunder.sum())
    precip[thunder] += rng.uniform(0.5, 1.5, thunder.sum())

    tropical = np.nonzero(inwin & np.isin(month, [8, 9, 10]))[0]
    winter = np.nonzero(inwin & np.isin(month, [11, 12, 1, 2, 3]))[0]
    for day0 in np.concatenate([rng.choice(tropical, 3, replace=False), rng.choice(winter, 5, replace=False)]):
        s = rng.uniform(0.6, 1.0)
        for k in range(int(rng.integers(1, 3))):
            if day0 + k < n:
                severity[day0 + k] = max(severity[day0 + k], s * (1 - 0.4 * k))
                gust[day0 + k] = 45 + 25 * s * (1 - 0.3 * k)
                precip[day0 + k] += rng.uniform(1.2, 3.5)

    hdd = np.clip(65 - tavg, 0, None)
    cdd = np.clip(tavg - 65, 0, None)
    wx = pd.DataFrame({
        "STATION": "EWR", "OBS_DATE": d.astype("datetime64[s]"),
        "TAVG_F": tavg.round(1), "TMAX_F": (tavg + spread / 2).round(1), "TMIN_F": (tavg - spread / 2).round(1),
        "PRECIP_IN": precip.round(2), "MAX_GUST_MPH": gust.round(0).astype(int),
        "HDD": hdd.round(1), "CDD": cdd.round(1),
    })
    write(wx, "weather", "daily_weather", date_format="%Y-%m-%d")

    raw = np.vstack([
        1 + 0.045 * cdd + 0.004 * hdd,   # RES
        1 + 0.045 * cdd + 0.030 * hdd,   # RES_HEAT
        1 + 0.022 * cdd + 0.003 * hdd,   # COM
        1 + 0.006 * cdd,                 # IND
        0.12 + 0.065 * hdd,              # GAS_RES
        0.25 + 0.050 * hdd,              # GAS_COM
        0.60 + 0.020 * hdd,              # GAS_IND
    ])
    raw /= raw[:, inwin].mean(axis=1, keepdims=True)
    return d, severity, raw


# ----------------------------------------------------------------------------- main build
def build(n_prem, n_ami):
    global rng
    log(f"scale: {n_prem:,} premises, {n_ami:,} AMI sample meters -> {OUT}")

    wx_days, severity, daily_factor = make_weather()
    cumf = np.concatenate([np.zeros((daily_factor.shape[0], 1)), np.cumsum(daily_factor, axis=1)], axis=1)

    def di(d):
        return (np.asarray(d, dtype="datetime64[D]") - WX_START).astype(np.int64)

    def period_factor(prof, a, b):
        return (cumf[prof, b] - cumf[prof, a]) / np.maximum(b - a, 1)

    log("weather done")

    # ---------------------------------------------------------------- rates / towns
    rate_codes = list(ref.RATES)
    R = {c: i for i, c in enumerate(rate_codes)}
    r_prof = np.array([ref.PROFILES.index(ref.RATES[c][3]) for c in rate_codes])
    r_base = np.array([ref.RATES[c][4] for c in rate_codes], float)
    r_cust = np.array([ref.RATES[c][5] for c in rate_codes])
    r_deliv = np.array([ref.RATES[c][6] for c in rate_codes])
    r_supply = np.array([ref.RATES[c][7] for c in rate_codes])
    r_class = np.array([ref.RATES[c][1] for c in rate_codes], dtype=object)

    towns = ref.TOWNS
    t_city = np.array([t[0] for t in towns], dtype=object)
    t_county = np.array([t[1] for t in towns], dtype=object)
    t_zip = np.array([t[2] for t in towns], dtype=object)
    t_div = np.array([ref.DIVISIONS.index(t[3]) for t in towns])
    t_w = np.array([t[4] for t in towns], float)
    t_lat = np.array([t[5] for t in towns])
    t_lon = np.array([t[6] for t in towns])
    t_area = np.array([ref.AREA_CODES[t[1]] for t in towns], dtype=object)
    div_names = np.array(ref.DIVISIONS, dtype=object)
    div3 = np.array([x[:3].upper() for x in ref.DIVISIONS], dtype=object)

    write(pd.DataFrame({
        "CITY": t_city, "COUNTY": t_county, "ZIP": t_zip, "DIVISION": div_names[t_div],
        "LAT": t_lat, "LON": t_lon}), "reference", "towns")
    write(pd.DataFrame({
        "RATE_CODE": rate_codes, "COMMODITY": [ref.RATES[c][0] for c in rate_codes],
        "RATE_CLASS": r_class, "DESCRIPTION": [ref.RATES[c][2] for c in rate_codes],
        "CUSTOMER_CHARGE": r_cust, "DELIVERY_PER_UNIT": r_deliv, "SUPPLY_PER_UNIT": r_supply,
        "UNIT": ["kWh" if ref.RATES[c][0] == "ELEC" else "therm" for c in rate_codes]}), "reference", "rate_schedules")

    # ---------------------------------------------------------------- premises
    P = n_prem
    seg = rng.choice(4, P, p=[0.885, 0.095, 0.014, 0.006])
    town = rng.choice(len(towns), P, p=t_w / t_w.sum())
    div = t_div[town]

    elec = np.empty(P, dtype=np.int64)
    def assign(target, mask, codes, p):
        target[mask] = np.array([R[c] for c in codes])[rng.choice(len(codes), mask.sum(), p=p)]
    assign(elec, seg == 0, ["RS", "RHS", "RLM"], [0.85, 0.115, 0.035])
    assign(elec, seg == 1, ["GLP"], [1.0])
    assign(elec, seg == 2, ["LPL-S", "LPL-P", "HTS"], [0.72, 0.24, 0.04])
    assign(elec, seg == 3, ["GLP", "LPL-S"], [0.6, 0.4])

    gas_p = np.select([seg == 0, seg == 1, seg == 2], [np.array([0.78, 0.80, 0.72, 0.55])[div], 0.6, 0.45], 0.7)
    gas_p = np.where(elec == R["RHS"], 0.1, gas_p)
    has_gas = rng.random(P) < gas_p
    gas = np.full(P, -1, dtype=np.int64)
    assign(gas, has_gas & (seg == 0), ["RSG"], [1.0])
    assign(gas, has_gas & ((seg == 1) | (seg == 3)), ["GSG"], [1.0])
    assign(gas, has_gas & (seg == 2), ["LVG", "GSG"], [0.6, 0.4])

    ptype = np.empty(P, dtype=object)
    r0 = seg == 0
    ptype[r0] = pick(["SFH", "MULTI", "APT"], r0.sum(), [0.58, 0.22, 0.20])
    ptype[seg == 1] = "COMMERCIAL"
    ptype[seg == 2] = pick(["INDUSTRIAL", "COMMERCIAL"], (seg == 2).sum())
    ptype[seg == 3] = "GOVERNMENT"

    mult = np.exp(rng.normal(0, np.where(r0, 0.42, 0.60)))
    mult = np.where(ptype == "APT", mult * 0.7, mult)
    base_kwh = r_base[elec] * mult
    base_therm = np.where(gas >= 0, r_base[np.maximum(gas, 0)] * np.sqrt(mult) * np.exp(rng.normal(0, 0.3, P)), 0)
    cycle = rng.integers(0, 21, P)

    prem_id = 3000000000 + np.arange(P)
    addr_id = 8000000000 + np.arange(P)

    # addresses (with deliberate data-quality problems)
    city = t_city[town].copy()
    u = rng.random(P)
    city = np.where(u < 0.03, np.char.upper(city.astype(str)).astype(object), city)
    city = np.where((u >= 0.03) & (u < 0.05), (city.astype(str) + "  ").astype(object), city)
    zipc = t_zip[town].copy()
    strip = rng.random(P) < 0.015
    zipc[strip] = np.char.lstrip(zipc[strip].astype(str), "0").astype(object)
    unit = np.where(ptype == "APT", "APT " + pd.Series(rng.integers(1, 30, P)).astype(str).values
                    + pick(list("ABCDEF"), P), "")
    street = pick(ref.STREETS, P) + " " + pick(ref.STREET_SUFFIX, P, [0.3, 0.3, 0.12, 0.04, 0.05, 0.04, 0.06, 0.05, 0.04])
    write(pd.DataFrame({
        "ADDRNUMBER": addr_id, "HOUSE_NUM1": rng.integers(1, 2400, P), "HOUSE_NUM2": unit,
        "STREET": street, "CITY1": city, "POST_CODE1": zipc, "COUNTY": t_county[town],
        "REGION": "NJ", "COUNTRY": "US",
        "GEO_LAT": (t_lat[town] + rng.normal(0, 0.012, P)).round(5),
        "GEO_LON": (t_lon[town] + rng.normal(0, 0.015, P)).round(5)}), "sap_isu", "ADRC")
    write(pd.DataFrame({
        "VSTELLE": prem_id, "HAUS": 7000000000 + np.arange(P), "VBSART": ptype, "ADDRNUMBER": addr_id}),
        "sap_isu", "EVBS")

    # installations + meters
    g_idx = np.nonzero(gas >= 0)[0]
    anl_e = 4000000000 + np.arange(P)
    anl_g = np.full(P, -1, dtype=np.int64)
    anl_g[g_idx] = 4100000000 + np.arange(len(g_idx))
    mru = pd.Series(cycle + 1).map(lambda c: f"MR{c:02d}").values
    akl = {"RES": "RES", "COM": "COM", "IND": "IND"}
    eanl = pd.DataFrame({
        "ANLAGE": np.concatenate([anl_e, anl_g[g_idx]]),
        "VSTELLE": np.concatenate([prem_id, prem_id[g_idx]]),
        "SPARTE": ["01"] * P + ["02"] * len(g_idx),
        "TARIFTYP": np.concatenate([np.array(rate_codes, dtype=object)[elec], np.array(rate_codes, dtype=object)[gas[g_idx]]]),
        "ABLEINH": np.concatenate([mru, mru[g_idx]]),
        "AKLASSE": np.concatenate([r_class[elec], r_class[gas[g_idx]]]),
    })
    write(eanl, "sap_isu", "EANL")

    tech_e = pick(["AMI", "AMR", "MANUAL"], P, [0.96, 0.03, 0.01])
    tech_g = pick(["AMI", "AMR", "MANUAL"], len(g_idx), [0.82, 0.15, 0.03])
    inb_e = np.where(tech_e == "AMI",
                     np.datetime64("2021-03-01") + days(rng.integers(0, 1400, P)),
                     np.datetime64("2004-01-01") + days(rng.integers(0, 5800, P)))
    inb_g = np.datetime64("2008-01-01") + days(rng.integers(0, 6000, len(g_idx)))
    herst_e = pick(["Itron", "Landis+Gyr", "Aclara"], P, [0.45, 0.35, 0.20])
    herst_g = pick(["Honeywell Elster", "Itron", "Sensus"], len(g_idx), [0.5, 0.3, 0.2])
    eq_e = 6000000000 + np.arange(P)
    eq_g = 6100000000 + np.arange(len(g_idx))
    inb_all = np.concatenate([inb_e, inb_g])
    write(pd.DataFrame({
        "EQUNR": np.concatenate([eq_e, eq_g]),
        "ANLAGE": np.concatenate([anl_e, anl_g[g_idx]]),
        "SPARTE": ["01"] * P + ["02"] * len(g_idx),
        "HERST": np.concatenate([herst_e, herst_g]),
        "TYPBZ": np.concatenate([np.where(r0, "E-1PH-2S", "E-3PH-16S"), np.full(len(g_idx), "G-DIAPH-250")]),
        "ZMETER_TECH": np.concatenate([tech_e, tech_g]),
        "INBDT": sapdate(inb_all),
        "BAUJJ": inb_all.astype("datetime64[Y]").astype(int) + 1970 - rng.integers(0, 2, len(inb_all)),
    }), "sap_isu", "EQUI")
    manual_e = tech_e == "MANUAL"
    manual_g = np.zeros(P, bool)
    manual_g[g_idx] = tech_g == "MANUAL"
    log("premises, installations, meters done")

    # ---------------------------------------------------------------- occupancies (customer tenure at a premise)
    win_days = int((AS_OF - START) / DAY)
    moved = rng.random(P) < np.where(r0, 0.11, 0.04)
    o1_start = START - days(rng.integers(30, 365 * 30, P))
    o1_end = np.full(P, OPEN_END)
    o1_end[moved] = START + days(rng.integers(0, win_days, moved.sum()))
    gap = np.where(rng.random(P) < 0.7, 0, rng.integers(10, 90, P))
    m_idx = np.nonzero(moved)[0]
    o2_start = o1_end[m_idx] + days(gap[m_idx])
    keep2 = o2_start <= AS_OF
    m_idx, o2_start = m_idx[keep2], o2_start[keep2]

    occ_prem = np.concatenate([np.arange(P), m_idx])
    occ_start = np.concatenate([o1_start, o2_start])
    occ_end = np.concatenate([o1_end, np.full(len(m_idx), OPEN_END)])
    N = len(occ_prem)
    oseg = seg[occ_prem]
    ores = oseg == 0
    partner = 1000000000 + np.arange(N)
    vkont = 200000000000 + np.arange(N)

    pay_class = np.where(ores, rng.choice(4, N, p=[0.66, 0.21, 0.09, 0.04]),
                         rng.choice(4, N, p=[0.78, 0.16, 0.05, 0.01]))
    low_income = ores & (rng.random(N) < 0.06 + 0.12 * (pay_class >= 2))
    med_cert = ores & (rng.random(N) < 0.007)
    budget = ores & (rng.random(N) < 0.07)

    def enrollment(p):
        enrolled = rng.random(N) < p
        dt = AS_OF - days(np.minimum(rng.exponential(900, N), 4000).astype(int))
        dt = np.where(dt < occ_start, occ_start + days(rng.integers(0, 60, N)), dt)
        enrolled &= dt <= AS_OF
        return enrolled, np.where(enrolled, dt, np.datetime64("NaT", "D"))
    autopay, autopay_dt = enrollment(np.where(pay_class == 3, 0.08, np.where(ores, 0.30, 0.38)))
    ebill, ebill_dt = enrollment(np.where(ores, 0.45, 0.40))
    tps = rng.random(N) < np.array([0.14, 0.35, 0.65, 0.40])[oseg]

    # business partners
    first = pick(ref.FIRST_NAMES, N)
    last = pick(ref.LAST_NAMES, N)
    upper_last = rng.random(N) < 0.02
    last = np.where(upper_last, np.char.upper(last.astype(str)).astype(object), last)
    otown = town[occ_prem]
    org = np.empty(N, dtype=object)
    smb = oseg == 1
    org[smb] = pick(ref.BIZ_PREFIX, smb.sum()) + " " + pick(ref.SMB_TYPES, smb.sum()) + " " + pick(ref.BIZ_SUFFIX, smb.sum())
    ci = oseg == 2
    org[ci] = pick(ref.BIZ_PREFIX, ci.sum()) + " " + pick(ref.CI_TYPES, ci.sum()) + " " + pick(ref.BIZ_SUFFIX[:4], ci.sum())
    gv = np.nonzero(oseg == 3)[0]
    org[gv] = [ref.GOV_TYPES[k].format(c=t_city[otown[i]]) for i, k in zip(gv, rng.integers(0, len(ref.GOV_TYPES), len(gv)))]
    org = pd.Series(org).str.strip().values

    email = np.where(ores,
                     pd.Series(first).str.lower().values + "." + pd.Series(last).str.lower().values
                     + pd.Series(rng.integers(1, 99, N)).astype(str).values + "@" + pick(["example.com", "example.net", "example.org"], N),
                     "ap" + pd.Series(partner % 100000).astype(str).values + "@example.org")
    email = np.where(rng.random(N) < np.where(ores, 0.27, 0.10), "", email)
    area = t_area[otown]
    line = pd.Series(rng.integers(100, 200, N)).map(lambda x: f"{x:04d}").values
    fmt = rng.choice(4, N, p=[0.60, 0.25, 0.10, 0.05])
    phone = np.select([fmt == 0, fmt == 1, fmt == 2],
                      ["(" + area + ") 555-" + line, area + "-555-" + line, area + "555" + line],
                      "+1 " + area + " 555 " + line)
    phone = np.where(rng.random(N) < 0.08, "", phone)
    crdat = np.where(occ_start > SAP_GO_LIVE, occ_start - days(rng.integers(0, 21, N)), SAP_GO_LIVE)
    write(pd.DataFrame({
        "PARTNER": partner, "TYPE": np.where(ores, "1", "2"), "BU_GROUP": np.array(ref.SEGMENTS, dtype=object)[oseg],
        "NAME_FIRST": np.where(ores, first, ""), "NAME_LAST": np.where(ores, last, ""),
        "NAME_ORG1": np.where(ores, "", org), "CRDAT": sapdate(crdat),
        "SMTP_ADDR": email, "TEL_NUMBER": phone}), "sap_isu", "BUT000")
    write(pd.DataFrame({
        "VKONT": vkont, "GPART": partner, "EZAWE": np.where(autopay, "D", ""),
        "ZAUTOPAY_DATE": sapdate(autopay_dt), "ZEBILL": flag(ebill), "ZEBILL_DATE": sapdate(ebill_dt),
        "ZBUDGET_BILL": flag(budget), "ZLOW_INCOME": flag(low_income), "ZMED_CERT": flag(med_cert)}),
        "sap_isu", "FKKVKP")

    # contracts: one per occupancy per installation
    og = gas[occ_prem] >= 0
    ever = pd.DataFrame({
        "ANLAGE": np.concatenate([anl_e[occ_prem], anl_g[occ_prem][og]]),
        "VKONTO": np.concatenate([vkont, vkont[og]]),
        "EINZDAT": sapdate(np.concatenate([occ_start, occ_start[og]])),
        "AUSZDAT": sapdate(np.concatenate([occ_end, occ_end[og]])),
        "ZSUPPLIER": np.concatenate([np.where(tps, "TPS", "BGS"), np.where(rng.random(og.sum()) < 0.05, "TPS", "BGSS")]),
    })
    ever.insert(0, "VERTRAG", 5000000000 + np.arange(len(ever)))
    write(ever, "sap_isu", "EVER")
    log(f"occupancies: {N:,} business partners / contract accounts, {len(ever):,} contracts")

    # ---------------------------------------------------------------- billing + payments
    month0 = START.astype("datetime64[M]")
    mstarts = (month0 + months(np.arange(-1, MONTHS + 1))).astype("datetime64[D]")
    offsets = np.round(np.arange(21) * 1.35).astype(int)
    bill_dates = mstarts[:, None] + days(offsets)[None, :]           # row k = month START-1+k

    ocyc = cycle[occ_prem]
    # final bills for customers who moved out inside the window
    ended = (occ_end >= START) & (occ_end <= AS_OF)
    k_e = np.zeros(N, dtype=np.int64)
    k_e[ended] = (occ_end[ended].astype("datetime64[M]") - month0).astype(np.int64) + 1
    bd_e = bill_dates[k_e, ocyc]
    last_bd = np.where(bd_e <= occ_end, bd_e, bill_dates[np.maximum(k_e - 1, 0), ocyc])
    final_ps = np.maximum(last_bd, occ_start)
    final_ok = ended & (occ_end > final_ps)
    final_m = np.where(final_ok, k_e - 1, -1)

    e_supply_step = [(np.datetime64("2025-06-01"), 1.18), (np.datetime64("2026-06-01"), 1.03)]
    g_supply_step = [(np.datetime64("2025-11-01"), 1.06)]

    def step_mult(dates, steps):
        m = np.ones(len(dates))
        for dt, f in steps:
            m *= np.where(dates >= dt, f, 1.0)
        return m

    def calc_bills(o, ps, pe, abrvorg):
        n = len(o)
        p = occ_prem[o]
        nd = (pe - ps).astype(np.int64)
        a, b = di(ps), di(pe)
        prorate = nd / 30.4
        e = elec[p]
        kwh = base_kwh[p] / 30.4 * nd * period_factor(r_prof[e], a, b) * np.exp(rng.normal(0, 0.10, n))
        est_e = rng.random(n) < np.where(manual_e[p], 0.25, 0.012)
        kwh = np.round(np.where(est_e, kwh * rng.uniform(0.8, 1.2, n), kwh))
        e_del = r_cust[e] * prorate + kwh * r_deliv[e]
        e_sup = kwh * r_supply[e] * step_mult(pe, e_supply_step)

        g = gas[p]
        hg = g >= 0
        gi = np.maximum(g, 0)
        th = base_therm[p] / 30.4 * nd * period_factor(r_prof[gi], a, b) * np.exp(rng.normal(0, 0.12, n))
        est_g = hg & (rng.random(n) < np.where(manual_g[p], 0.30, 0.03))
        th = np.where(hg, np.round(np.where(est_g, th * rng.uniform(0.8, 1.2, n), th), 1), 0.0)
        g_del = np.where(hg, r_cust[gi] * prorate + th * r_deliv[gi], 0.0)
        g_sup = np.where(hg, th * r_supply[gi] * step_mult(pe, g_supply_step), 0.0)

        e_del, e_sup, g_del, g_sup = (np.round(x, 2) for x in (e_del, e_sup, g_del, g_sup))
        tax = np.round((e_del + e_sup + g_del + g_sup) * 0.06625, 2)
        return pd.DataFrame({
            "_o": o, "_bd": pe,
            "VKONT": vkont[o], "GPART": partner[o], "VSTELLE": prem_id[p],
            "ABRVORG": abrvorg, "BUDAT": pe, "BEGABRPE": ps, "ENDABRPE": pe,
            "FAEDN": pe + days(20), "STORNODAT": np.datetime64("NaT", "D"),
            "ELEC_TARIF": np.array(rate_codes, dtype=object)[e], "KWH": kwh, "ELEC_EST": flag(est_e),
            "ELEC_SUPPLIER": np.where(tps[o], "TPS", "BGS"),
            "GAS_TARIF": np.where(hg, np.array(rate_codes, dtype=object)[gi], ""), "THERMS": th, "GAS_EST": flag(est_g),
            "ELEC_DELIV_AMT": e_del, "ELEC_SUPPLY_AMT": e_sup, "GAS_DELIV_AMT": g_del, "GAS_SUPPLY_AMT": g_sup,
            "TAX_AMT": tax, "TOTAL_AMT": np.round(e_del + e_sup + g_del + g_sup + tax, 2), "WAERS": "USD",
        })

    money_cols = ["ELEC_DELIV_AMT", "ELEC_SUPPLY_AMT", "GAS_DELIV_AMT", "GAS_SUPPLY_AMT", "TAX_AMT", "TOTAL_AMT"]
    pay_parts = []
    channels = np.array(["ACH", "WEB", "APP", "IVR", "CHK", "AGT", "ASST"], dtype=object)
    chan_p = np.array([
        [0, 0.48, 0.20, 0.12, 0.14, 0.06],   # A
        [0, 0.40, 0.18, 0.15, 0.14, 0.13],   # B
        [0, 0.30, 0.15, 0.18, 0.10, 0.27],   # C
        [0, 0.20, 0.10, 0.20, 0.07, 0.43],   # D
    ])
    lag_lo = np.array([3, 10, 20, 25])
    lag_hi = np.array([22, 46, 76, 111])

    def make_payments(fr):
        o = fr["_o"].values
        n = len(o)
        bd = fr["BUDAT"].values.astype("datetime64[D]")
        cls = pay_class[o]
        auto = autopay[o] & (autopay_dt[o].astype("datetime64[D]") <= bd)
        pays = rng.random(n) < np.where(auto, 0.998, np.array([0.995, 0.975, 0.88, 0.62])[cls])
        lag = np.where(auto, 20, rng.integers(lag_lo[cls], lag_hi[cls]))
        partial = ~auto & (rng.random(n) < np.array([0.0, 0.03, 0.25, 0.45])[cls])
        amt = fr["TOTAL_AMT"].values * np.where(partial, rng.uniform(0.3, 0.9, n), 1.0)
        chan = np.zeros(n, dtype=np.int64)
        cum = chan_p.cumsum(axis=1)
        r = rng.random(n)
        chan[~auto] = (r[~auto, None] > cum[cls[~auto]]).sum(axis=1).clip(0, 5)
        pdate = bd + days(lag)
        ok = pays & (pdate <= AS_OF) & (amt > 0)
        parts = [pd.DataFrame({"VKONT": vkont[o][ok], "BELNR": fr["BELNR"].values[ok], "BUDAT": pdate[ok],
                               "BETRZ": np.round(amt[ok], 2), "PAY_CHANNEL": channels[chan[ok]]})]
        # energy-assistance credits (USF / LIHEAP) for low-income customers
        asst = low_income[o] & (rng.random(n) < 0.15)
        adate = bd + days(rng.integers(5, 40, n))
        asst &= adate <= AS_OF
        parts.append(pd.DataFrame({"VKONT": vkont[o][asst], "BELNR": fr["BELNR"].values[asst], "BUDAT": adate[asst],
                                   "BETRZ": np.round(fr["TOTAL_AMT"].values[asst] * rng.uniform(0.2, 0.6, asst.sum()), 2),
                                   "PAY_CHANNEL": "ASST"}))
        pay_parts.append(pd.concat(parts, ignore_index=True))

    next_belnr = 900000000000
    carry = {}
    n_bills = 0
    for m in range(MONTHS):
        bd = bill_dates[m + 1][ocyc]
        prev = bill_dates[m][ocyc]
        act = (occ_start < bd) & (occ_end >= bd)
        o = np.nonzero(act)[0]
        frames = [calc_bills(o, np.maximum(prev[o], occ_start[o]), bd[o], "01")]
        fo = np.nonzero(final_m == m)[0]
        if len(fo):
            frames.append(calc_bills(fo, final_ps[fo], occ_end[fo], "03"))
        fr = pd.concat(frames, ignore_index=True)
        fr.insert(0, "BELNR", next_belnr + np.arange(len(fr)))
        next_belnr += len(fr)

        # reversals (storno) + corrected rebills
        rev = rng.random(len(fr)) < 0.003
        storno = fr["BUDAT"].values.astype("datetime64[D]") + days(rng.integers(5, 21, len(fr)))
        rev &= storno <= AS_OF
        if rev.any():
            fr.loc[rev, "STORNODAT"] = storno[rev]
            rb = fr[rev].copy()
            f = rng.uniform(0.85, 1.02, len(rb))
            rb["BELNR"] = next_belnr + np.arange(len(rb))
            next_belnr += len(rb)
            rb["ABRVORG"] = "02"
            rb["BUDAT"] = storno[rev]
            rb["FAEDN"] = storno[rev] + days(20)
            rb["STORNODAT"] = np.datetime64("NaT", "D")
            rb["ELEC_EST"] = ""
            rb["GAS_EST"] = ""
            rb["KWH"] = np.round(rb["KWH"] * f)
            rb["THERMS"] = np.round(rb["THERMS"] * f, 1)
            for c in money_cols:
                rb[c] = np.round(rb[c] * f, 2)
            rbm = (storno[rev].astype("datetime64[M]") - month0).astype(np.int64)
            for mm in np.unique(rbm):
                carry.setdefault(int(mm), []).append(rb[rbm == mm])
        fr = pd.concat([fr] + carry.pop(m, []), ignore_index=True)

        make_payments(fr[fr["STORNODAT"].isna()])
        out = fr.drop(columns=["_o", "_bd"])
        for c in ["BUDAT", "BEGABRPE", "ENDABRPE", "FAEDN", "STORNODAT"]:
            out[c] = sapdate(out[c].values)
        write(out, "sap_isu", "ERCH", str(np.datetime_as_string(month0 + months(m))))
        n_bills += len(out)
    log(f"billing done: {n_bills:,} billing documents")

    pay = pd.concat(pay_parts, ignore_index=True)
    pay_parts.clear()
    pay.insert(0, "PAYMENT_ID", 700000000000 + np.arange(len(pay)))
    dup = pay.sample(frac=0.0015, random_state=1)              # re-extracted duplicate rows
    pay = pd.concat([pay, dup]).sort_values("PAYMENT_ID", kind="stable")
    pdates = pay["BUDAT"].values.astype("datetime64[D]")
    pay["BUDAT"] = sapdate(pdates)
    write_partitioned(pay.reset_index(drop=True), "sap_isu", "DFKKZP", pdates)
    log(f"payments done: {len(pay):,} rows")
    del pay, dup

    # ---------------------------------------------------------------- installment plans
    cand = np.nonzero((pay_class >= 2) & (occ_end > START))[0]
    pl = cand[rng.random(len(cand)) < 0.45]
    lo = np.maximum(occ_start[pl], START)
    hi = np.minimum(occ_end[pl], AS_OF)
    created = lo + days((rng.random(len(pl)) * ((hi - lo) / DAY)).astype(int))
    cm = pd.DatetimeIndex(created).month.values
    cd = pd.DatetimeIndex(created).day.values
    winter = ((cm == 11) & (cd >= 15)) | np.isin(cm, [12, 1, 2]) | ((cm == 3) & (cd <= 15))
    ptype_pl = np.where(winter & low_income[pl] & (rng.random(len(pl)) < 0.6), "WTP", "DPA")
    n_inst = rng.choice([3, 4, 6, 9, 12], len(pl))
    total = np.round(lognorm(650, 0.7, len(pl)) * np.where(ores[pl], 1, 4), 2)
    ends = created + days(n_inst * 30)
    r = rng.random(len(pl))
    status = np.where(ends < AS_OF, np.select([r < 0.55, r < 0.90], ["COMP", "DFLT"], "CANC"),
                      np.where(r < 0.8, "ACTV", "DFLT"))
    write(pd.DataFrame({
        "PLAN_ID": 1100000000 + np.arange(len(pl)), "VKONT": vkont[pl], "PLAN_TYPE": ptype_pl,
        "ERDAT": sapdate(created), "TOTAL_AMT": total,
        "DOWN_PAYMENT": np.round(total * rng.uniform(0.1, 0.25, len(pl)), 2),
        "NUM_INSTALLMENTS": n_inst, "STATUS": status}), "sap_isu", "ZINSTPLAN")
    log(f"installment plans: {len(pl):,}")

    # ---------------------------------------------------------------- service orders
    so = []

    def add_orders(otype, occ_idx, created, sched, compl, wc, cancel_rate=0.0):
        n = len(occ_idx)
        compl = np.asarray(compl, dtype="datetime64[D]").copy()
        status = np.where(compl > AS_OF, "OPEN", "COMP").astype(object)
        canc = rng.random(n) < cancel_rate
        status[canc] = "CANC"
        compl[(status != "COMP")] = np.datetime64("NaT", "D")
        so.append(pd.DataFrame({
            "ORDER_TYPE": otype, "VSTELLE": prem_id[occ_prem[occ_idx]], "VKONT": vkont[occ_idx],
            "ERDAT": np.asarray(created, dtype="datetime64[D]"), "ERZEIT": saptime(n),
            "SCHED_DATE": np.asarray(sched, dtype="datetime64[D]"), "COMPL_DATE": compl, "STATUS": status,
            "WORK_CENTER": wc}))

    mi = np.nonzero(occ_start >= START)[0]
    add_orders("MOVE_IN", mi, occ_start[mi] - days(rng.integers(1, 22, len(mi))), occ_start[mi],
               occ_start[mi] + days(rng.integers(0, 2, len(mi))), "CS-BACKOFFICE")
    mo = np.nonzero(ended)[0]
    add_orders("MOVE_OUT", mo, occ_end[mo] - days(rng.integers(1, 22, len(mo))), occ_end[mo],
               occ_end[mo] + days(rng.integers(0, 2, len(mo))), "CS-BACKOFFICE")

    dc_c = np.nonzero(((pay_class == 3) & (rng.random(N) < 0.45) | (pay_class == 2) & (rng.random(N) < 0.05))
                      & ~med_cert & (occ_end > START + days(90)))[0]
    lo = np.maximum(occ_start[dc_c], START) + days(60)
    hi = np.minimum(occ_end[dc_c], AS_OF)
    okr = hi > lo
    dc_c, lo, hi = dc_c[okr], lo[okr], hi[okr]
    dd = lo + days((rng.random(len(dc_c)) * ((hi - lo) / DAY)).astype(int))
    dm = pd.DatetimeIndex(dd).month.values
    dday = pd.DatetimeIndex(dd).day.values
    morat = ores[dc_c] & (((dm == 11) & (dday >= 15)) | np.isin(dm, [12, 1, 2]) | ((dm == 3) & (dday <= 15)))
    yr = pd.DatetimeIndex(dd).year.values + np.where(dm >= 11, 1, 0)
    shifted = pd.to_datetime(pd.DataFrame({"year": yr, "month": 3, "day": 16})).values.astype("datetime64[D]") + days(rng.integers(0, 45, len(dd)))
    dd = np.where(morat, shifted, dd)
    keep = dd <= AS_OF
    dc_c, dd = dc_c[keep], dd[keep]
    dcompl = dd + days(rng.integers(0, 4, len(dd)))
    add_orders("DISC_NP", dc_c, dd - days(rng.integers(10, 16, len(dd))), dd, dcompl, "FLD-COLLECT", 0.08)
    rc = rng.random(len(dc_c)) < 0.70
    rc_created = dcompl[rc] + days(rng.integers(0, 11, rc.sum()))
    keep = rc_created <= AS_OF
    rco = dc_c[rc][keep]
    rc_created = rc_created[keep]
    add_orders("RECONNECT", rco, rc_created, rc_created, rc_created + days(rng.integers(0, 3, len(rco))), "FLD-COLLECT")

    def random_orders(otype, frac, median_days, wc, cancel_rate=0.03):
        idx = np.nonzero(rng.random(N) < frac)[0]
        lo = np.maximum(occ_start[idx], START)
        hi = np.minimum(occ_end[idx], AS_OF)
        ok = hi > lo
        idx, lo, hi = idx[ok], lo[ok], hi[ok]
        c = lo + days((rng.random(len(idx)) * ((hi - lo) / DAY)).astype(int))
        s = c + days(rng.integers(1, 8, len(idx)))
        add_orders(otype, idx, c, s, s + days(lognorm(median_days, 0.6, len(idx)).astype(int)), wc, cancel_rate)

    random_orders("METER_EXCH", 0.03, 8, "FLD-METER")
    random_orders("HIGH_BILL_INV", 0.015, 5, "FLD-METER")
    random_orders("NEW_SVC", 0.01, 45, "ENG-NEWBUS", 0.06)
    random_orders("GAS_LEAK_INV", 0.006, 0, "GAS-EMERG", 0.0)
    so = pd.concat(so, ignore_index=True).sort_values(["ERDAT", "ERZEIT"], kind="stable").reset_index(drop=True)
    so.insert(0, "ORDER_ID", 1200000000 + np.arange(len(so)))
    for c in ["ERDAT", "SCHED_DATE", "COMPL_DATE"]:
        so[c] = sapdate(so[c].values)
    write(so, "sap_isu", "ZSRVORD")
    log(f"service orders: {len(so):,}")
    del so

    # ---------------------------------------------------------------- SAP PM work orders
    W = int(P * 0.55)
    win = np.nonzero(wx_days >= START)[0]
    wd = (wx_days[win].astype(np.int64) + 3) % 7
    wwt = np.where(wd < 5, 1.0, 0.25) * (1 + 6 * severity[win])
    wdays = wx_days[win][rng.choice(len(win), W, p=wwt / wwt.sum())]
    sev_w = severity[di(wdays)]
    auart = np.where(sev_w > 0.5, pick(["PM03", "PM01", "PM02", "PM04"], W, [0.55, 0.35, 0.05, 0.05]),
                     pick(["PM01", "PM02", "PM03", "PM04"], W, [0.38, 0.40, 0.08, 0.14]))
    is_gas = rng.random(W) < 0.35
    e_assets = ["Pole", "Overhead Conductor", "Underground Cable", "Distribution Transformer",
                "Substation Equipment", "Streetlight", "Electric Meter"]
    g_assets = ["Gas Main", "Gas Service Line", "Regulator Station", "Gas Meter", "Valve"]
    asset = np.where(is_gas, pick(g_assets, W, [0.35, 0.3, 0.1, 0.15, 0.1]),
                     pick(e_assets, W, [0.22, 0.2, 0.14, 0.18, 0.08, 0.1, 0.08]))
    act_text = {"PM01": "Repair", "PM02": "Inspect", "PM03": "Emergency repair", "PM04": "Replace"}
    ktext = pd.Series(auart).map(act_text).values + " " + asset
    ktext = np.where((auart == "PM04") & (asset == "Gas Main"), "Cast iron main replacement", ktext)
    ktext = np.where((auart == "PM02") & (asset == "Overhead Conductor") & (rng.random(W) < 0.5), "Vegetation management", ktext)
    prio = np.select([auart == "PM03", auart == "PM01"],
                     ["1", pick(["2", "3"], W, [0.4, 0.6])], pick(["3", "4"], W, [0.5, 0.5]))
    lead = np.where(auart == "PM03", 0, rng.integers(2, 30, W))
    pdur_med = pd.Series(auart).map({"PM01": 12, "PM02": 30, "PM03": 1, "PM04": 90}).values
    pdur = np.maximum(lognorm(1, 0.5, W) * pdur_med, 1).astype(int)
    gstrp = wdays + days(lead)
    gltrp = gstrp + days(pdur)
    actual = gstrp + days(np.maximum(pdur * lognorm(1.1, 0.4, W), 0).astype(int))
    prem_w = rng.integers(0, P, W)
    wdiv = div[prem_w]
    stat = np.where(gstrp > AS_OF, "CRTD", np.where(actual > AS_OF, "REL",
                    np.where((actual + days(30) < AS_OF) & (rng.random(W) < 0.9), "CLSD", "TECO"))).astype(object)
    stat[rng.random(W) < 0.015] = "DLFL"
    cost_med = pd.Series(auart).map({"PM01": 2800, "PM02": 900, "PM03": 6500, "PM04": 38000}).values
    cost_med = np.where(asset == "Gas Main", cost_med * 4, cost_med)
    plan_cost = np.round(lognorm(1, 0.6, W) * cost_med, 2)
    progress = np.select([np.isin(stat, ["TECO", "CLSD"]), stat == "REL"], [1.0, rng.uniform(0.1, 0.8, W)], 0.0)
    act_cost = np.round(plan_cost * lognorm(1.05, 0.25, W) * progress, 2)
    crew = np.where(is_gas, "GS", pick(["OH", "UG", "SUB", "MTR", "VEG"], W, [0.45, 0.2, 0.1, 0.1, 0.15]))
    aufk = pd.DataFrame({
        "AUFNR": 400000000000 + np.arange(W), "AUART": auart, "KTEXT": ktext, "ASSET_CLASS": asset,
        "TPLNR": np.where(is_gas, "G-", "E-") + div3[wdiv] + "-" + pd.Series(rng.integers(1000, 9999, W)).astype(str).values,
        "ARBPL": div3[wdiv] + "-" + crew + pd.Series(rng.integers(1, 12, W)).map(lambda x: f"{x:02d}").values,
        "PRIOK": prio, "ERDAT": sapdate(wdays), "GSTRP": sapdate(gstrp), "GLTRP": sapdate(gltrp),
        "GETRI": sapdate(np.where(np.isin(stat, ["TECO", "CLSD"]), actual, np.datetime64("NaT", "D"))),
        "STAT": stat, "PLAN_COST": plan_cost, "ACT_COST": act_cost, "ZDIVISION": div_names[wdiv],
        "ZMUNI": t_city[town[prem_w]]})
    write_partitioned(aufk, "sap_pm", "AUFK", wdays)
    log(f"work orders: {W:,}")
    del aufk

    # ---------------------------------------------------------------- OMS outages
    scale = P / 500_000
    season = np.where(np.isin(pd.DatetimeIndex(wx_days[win]).month.values, [6, 7, 8]), 1.3, 1.0)
    lam = 16 * scale * season * (1 + 15 * severity[win] ** 2)
    cnt = rng.poisson(lam)
    od = np.repeat(wx_days[win], cnt)
    E = len(od)
    osev = severity[di(od)]
    storm = osev >= 0.25
    devices = np.array(["Service", "Transformer", "Fuse", "Recloser", "Breaker"], dtype=object)
    dev = np.where(storm, rng.choice(5, E, p=[0.25, 0.30, 0.28, 0.12, 0.05]),
                   rng.choice(5, E, p=[0.38, 0.32, 0.22, 0.06, 0.02]))
    med = np.array([1.3, 7, 45, 300, 1200])[dev]
    cust = np.maximum(np.round(lognorm(1, 0.5, E) * med), 1).astype(int)
    dur = np.where(storm, lognorm(1, 0.9, E) * (90 + 450 * osev), lognorm(70, 0.7, E))
    dur = np.maximum(dur, 6).astype(int)
    ostart = od.astype("datetime64[s]") + np.asarray(rng.integers(0, 86400, E)).astype("timedelta64[s]")
    orest = ostart + (dur * 60).astype("timedelta64[s]")
    still_out = orest > (AS_OF + DAY).astype("datetime64[s]")
    causes_n = ["Tree", "Equipment Failure", "Animal", "Weather - Lightning", "Weather - Wind", "Vehicle",
                "Underground Fault", "Unknown", "Overload"]
    causes_s = ["Tree", "Weather - Wind", "Weather - Lightning", "Equipment Failure", "Unknown"]
    cause = np.where(storm, pick(causes_s, E, [0.45, 0.30, 0.08, 0.08, 0.09]),
                     pick(causes_n, E, [0.22, 0.28, 0.12, 0.06, 0.05, 0.06, 0.09, 0.08, 0.04]))
    otown = town[rng.integers(0, P, E)]
    oms = pd.DataFrame({
        "EVENT_ID": 900000000 + np.arange(E), "EVENT_START": ostart,
        "RESTORE_TIME": np.where(still_out, np.datetime64("NaT", "D"), orest),
        "DIVISION": div_names[t_div[otown]], "MUNICIPALITY": t_city[otown],
        "CIRCUIT_ID": div3[t_div[otown]] + "-" + pd.Series(otown * 100 + rng.integers(0, 60, E) + 1000).astype(str).values,
        "DEVICE_TYPE": devices[dev], "CAUSE": cause, "CUSTOMERS_OUT": cust,
        "CUSTOMER_MINUTES": np.where(still_out, np.nan, cust * dur)})
    oms["CUSTOMER_MINUTES"] = oms["CUSTOMER_MINUTES"].astype("Int64")
    write(oms, "oms", "outage_events", date_format=TS_FMT)
    log(f"outage events: {E:,}")

    # ---------------------------------------------------------------- CRM interactions
    C = int(P * 5)
    wdts = wx_days[win]
    m_ = pd.DatetimeIndex(wdts).month.values
    y_ = pd.DatetimeIndex(wdts).year.values
    wk = np.array([1.15, 1.05, 1.0, 1.0, 0.95, 0.35, 0.15])[(wdts.astype(np.int64) + 3) % 7]
    ratebump = np.where((y_ == 2025) & np.isin(m_, [6, 7]), 0.12, np.where((y_ == 2026) & np.isin(m_, [6, 7]), 0.06, 0))
    winter_hb = np.where(np.isin(m_, [1, 2]), 0.05, 0)
    sev_d = severity[win]
    dw = wk * (1 + 3 * sev_d) * (1 + ratebump + winter_hb)
    ci = rng.choice(len(win), C, p=dw / dw.sum())
    cday = wdts[ci]
    reasons_base = ["Billing Inquiry", "Payment / Payment Arrangement", "Start / Stop / Transfer Service",
                    "Account Update", "Meter Issue", "Gas Odor", "Energy Efficiency Programs",
                    "Paperless / Autopay Enrollment", "Collections / Disconnect Notice", "Other"]
    reason = pick(reasons_base, C, [0.26, 0.18, 0.16, 0.09, 0.05, 0.03, 0.05, 0.06, 0.08, 0.04])
    p_out = 0.05 + 0.5 * sev_d[ci]
    p_hb = 0.05 + ratebump[ci] + winter_hb[ci]
    r = rng.random(C)
    reason = np.where(r < p_out, "Outage Report", np.where(r < p_out + p_hb, "High Bill Complaint", reason))
    chans = ["Phone - Agent", "Phone - IVR", "Chat", "Web Self-Service", "Mobile App", "Email"]
    channel = pick(chans, C, [0.42, 0.14, 0.12, 0.18, 0.09, 0.05])
    channel = np.where(reason == "Outage Report", pick(chans[:2] + chans[3:5], C, [0.2, 0.4, 0.3, 0.1]), channel)
    channel = np.where(reason == "Gas Odor", "Phone - Agent", channel)
    agent = np.isin(channel, ["Phone - Agent", "Chat", "Email"])
    is_mon = ((cday.astype(np.int64) + 3) % 7) == 0
    wait = np.select([channel == "Phone - Agent", channel == "Chat"],
                     [rng.exponential(75 * (1 + 5 * sev_d[ci]) * np.where(is_mon, 1.25, 1.0)), rng.exponential(40, C)], 0)
    wait = np.where(reason == "Gas Odor", rng.exponential(15, C), wait).astype(int)
    h_med = pd.Series(reason).map({
        "Billing Inquiry": 360, "Payment / Payment Arrangement": 300, "Start / Stop / Transfer Service": 480,
        "Account Update": 240, "Meter Issue": 420, "Gas Odor": 180, "Energy Efficiency Programs": 400,
        "Paperless / Autopay Enrollment": 200, "Collections / Disconnect Notice": 480, "Other": 300,
        "Outage Report": 150, "High Bill Complaint": 540}).values
    handle = np.where(agent, lognorm(1, 0.45, C) * h_med, lognorm(110, 0.5, C)).astype(int)
    fcr_p = pd.Series(reason).map({"High Bill Complaint": 0.55, "Collections / Disconnect Notice": 0.60,
                                   "Meter Issue": 0.62}).fillna(0.76).values
    fcr = rng.random(C) < np.where(agent, fcr_p, 0.85)
    transferred = (channel == "Phone - Agent") & (rng.random(C) < 0.09)
    responded = rng.random(C) < 0.12
    csat = np.clip(np.round(rng.normal(3.9 + 0.5 * fcr - 0.7 * (wait > 300), 0.9)), 1, 5)
    agent_no = rng.integers(0, 420, C)
    site = np.array(["Newark CC", "Hamilton CC", "Remote"], dtype=object)[agent_no % 3]

    bp = np.full(C, -1, dtype=np.int64)
    todo = np.arange(C)
    for _ in range(6):
        cand_o = rng.integers(0, N, len(todo))
        ok = (occ_start[cand_o] <= cday[todo]) & (occ_end[cand_o] >= cday[todo])
        bp[todo[ok]] = partner[cand_o[ok]]
        todo = todo[~ok]
        if not len(todo):
            break
    anon = rng.random(C) < np.where((reason == "Outage Report") & ~agent, 0.25, 0.08)
    bp[anon] = -1
    secs = np.where(agent, np.clip(rng.normal(13 * 3600, 3.2 * 3600, C), 7 * 3600, 21 * 3600 - 1),
                    rng.integers(0, 86400, C)).astype(np.int64)
    cts = cday.astype("datetime64[s]") + secs.astype("timedelta64[s]")
    order = np.argsort(cts, kind="stable")
    crm = pd.DataFrame({
        "INTERACTION_ID": 5500000000 + np.arange(C), "CREATED_TS": cts[order], "CHANNEL": channel[order],
        "REASON": reason[order], "BP_ID": pd.array(np.where(bp < 0, None, bp)[order], dtype="Int64"),
        "WAIT_SEC": wait[order], "HANDLE_SEC": handle[order], "FCR": np.where(fcr, "Y", "N")[order],
        "TRANSFERRED": np.where(transferred, "Y", "N")[order],
        "CSAT": pd.array(np.where(responded, csat, np.nan)[order], dtype="Int64"),
        "AGENT_ID": np.where(agent, "AG" + pd.Series(1000 + agent_no).astype(str).values, "")[order],
        "SITE": np.where(agent, site, "")[order]})
    write_partitioned(crm, "crm", "interactions", cday[order], date_format=TS_FMT)
    log(f"contact center interactions: {C:,}")
    del crm

    # ---------------------------------------------------------------- AMI daily reads (sample)
    cands = np.nonzero((seg <= 1) & (tech_e == "AMI"))[0]
    ami_p = np.sort(rng.choice(cands, min(n_ami, len(cands)), replace=False))
    # vacancy between tenants
    vac_s = np.full(P, OPEN_END)
    vac_e = np.full(P, OPEN_END)
    vac_s[moved] = o1_end[moved]
    vac_e[m_idx] = o2_start
    wk_res = np.array([1.0, 1.0, 1.0, 1.0, 1.0, 1.08, 1.08])
    wk_com = np.array([1.1, 1.1, 1.1, 1.1, 1.1, 0.55, 0.55])
    n_ami_rows = 0
    for m in range(MONTHS):
        ms = (month0 + months(m)).astype("datetime64[D]")
        me = min((month0 + months(m + 1)).astype("datetime64[D]"), AS_OF + DAY)
        dd = np.arange(ms, me, dtype="datetime64[D]")
        D = len(dd)
        pr = np.repeat(ami_p, D)
        dt = np.tile(dd, len(ami_p))
        wdx = (dt.astype(np.int64) + 3) % 7
        f = daily_factor[r_prof[elec[pr]], di(dt)] * np.where(seg[pr] == 0, wk_res[wdx], wk_com[wdx])
        kwh = base_kwh[pr] / 30.4 * f * np.exp(rng.normal(0, 0.18, len(pr)))
        kwh = np.where((dt > vac_s[pr]) & (dt < vac_e[pr]), kwh * 0.04, kwh)
        q = rng.random(len(pr))
        quality = np.select([q < 0.008, q < 0.023], ["MISSING", "ESTIMATED"], "VALID").astype(object)
        kwh = np.where(q > 0.9998, kwh * 40, kwh)
        kwh = np.where(quality == "MISSING", np.nan, np.round(kwh, 3))
        peak = np.round(kwh / 24 * rng.uniform(2.0, 3.6, len(pr)), 3)
        ami = pd.DataFrame({"METER_ID": eq_e[pr], "READ_DATE": dt.astype("datetime64[s]"),
                            "KWH": kwh, "PEAK_KW": peak, "READ_QUALITY": quality})
        write(ami, "ami", "daily_reads", str(np.datetime_as_string(month0 + months(m))), date_format="%Y-%m-%d")
        n_ami_rows += len(ami)
    log(f"AMI daily reads: {n_ami_rows:,}")


def main():
    global rng, OUT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scale", choices=SCALES, default="large")
    ap.add_argument("--seed", type=int, default=20260930)
    ap.add_argument("--out", default=os.environ.get("UTILITY_DATA_DIR", r"C:\Data\utility-powerbi"))
    args = ap.parse_args()

    OUT = Path(args.out) / "raw"
    if OUT.exists():
        if not (OUT / "_manifest.json").exists() and any(OUT.iterdir()):
            raise SystemExit(f"{OUT} exists and was not created by this generator; refusing to overwrite.")
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    rng = np.random.default_rng(args.seed)

    build(*SCALES[args.scale])
    (OUT / "_manifest.json").write_text(json.dumps({
        "scale": args.scale, "seed": args.seed, "window_start": str(START), "as_of": str(AS_OF),
        "row_counts": manifest}, indent=2))
    total = sum(manifest.values())
    size = sum(f.stat().st_size for f in OUT.rglob("*.gz")) / 1e6
    log(f"done: {total:,} rows across {len(manifest)} tables, {size:,.0f} MB gzipped")


if __name__ == "__main__":
    main()
