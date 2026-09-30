# Source Systems & Raw Zone Tables

Window: **2024-10-01 → 2026-09-30** (24 months). Large scale: 500K premises.
All data is synthetic. Tariff codes and towns are public reference data.

## Conventions in SAP extracts
- Column names are SAP technical names. `Z*` tables and columns are custom extracts or fields.
- Dates are `YYYYMMDD` integers. `0` means null and `99991231` means open-ended (e.g. an active contract's move-out date).
- Flags are `'X'` or blank.

## sap_isu: SAP IS-U / FI-CA

| Table | Grain | Key columns |
|---|---|---|
| `BUT000` | Business partner (customer) | PARTNER, TYPE (1 person / 2 org), BU_GROUP (RES/SMB/CI/GOV), names, CRDAT, SMTP_ADDR, TEL_NUMBER |
| `ADRC` | Service address | ADDRNUMBER, street, CITY1, POST_CODE1, COUNTY, GEO_LAT/LON |
| `EVBS` | Premise | VSTELLE, VBSART (SFH/MULTI/APT/COMMERCIAL/...), ADDRNUMBER |
| `FKKVKP` | Contract account | VKONT, GPART, EZAWE ('D' = autopay), ZAUTOPAY_DATE, ZEBILL(_DATE), ZBUDGET_BILL, ZLOW_INCOME, ZMED_CERT |
| `EANL` | Installation (one per premise per commodity) | ANLAGE, VSTELLE, SPARTE (01 elec / 02 gas), TARIFTYP, ABLEINH (meter read route), AKLASSE |
| `EVER` | Contract (customer × installation × tenure) | VERTRAG, ANLAGE, VKONTO, EINZDAT (move-in), AUSZDAT (move-out), ZSUPPLIER (BGS/BGSS or third-party) |
| `EQUI` | Meter | EQUNR, ANLAGE, HERST, ZMETER_TECH (AMI/AMR/MANUAL), INBDT |
| `ERCH` | Billing document, flattened with amounts (partitioned by bill month) | BELNR, VKONT, ABRVORG (01 periodic / 02 rebill / 03 final), BUDAT, BEGABRPE–ENDABRPE, FAEDN (due), STORNODAT (reversal), KWH, THERMS, charges, TOTAL_AMT |
| `DFKKZP` | Payment (partitioned by posting month) | PAYMENT_ID, VKONT, BELNR, BUDAT, BETRZ, PAY_CHANNEL (ACH/WEB/APP/IVR/CHK/AGT/ASST) |
| `ZINSTPLAN` | Payment arrangement | PLAN_ID, VKONT, PLAN_TYPE (DPA deferred payment / WTP winter termination program), STATUS |
| `ZSRVORD` | Service order | ORDER_ID, ORDER_TYPE, ERDAT/ERZEIT, SCHED_DATE, COMPL_DATE, STATUS, WORK_CENTER |

## sap_pm: SAP Plant Maintenance
| Table | Grain | Key columns |
|---|---|---|
| `AUFK` | Work order (partitioned by created month) | AUFNR, AUART (PM01 corrective / PM02 preventive / PM03 emergency / PM04 capital), KTEXT, ASSET_CLASS, TPLNR, ARBPL, PRIOK, ERDAT, GSTRP/GLTRP (plan), GETRI (actual finish), STAT (CRTD/REL/TECO/CLSD/DLFL), PLAN_COST, ACT_COST |

## Non-SAP feeds
| System/table | Grain | Notes |
|---|---|---|
| `ami/daily_reads` | Meter × day, sample of 25K electric AMI meters | KWH, PEAK_KW, READ_QUALITY (VALID/ESTIMATED/MISSING). ISO dates |
| `oms/outage_events` | Sustained outage event | EVENT_START, RESTORE_TIME, DEVICE_TYPE, CAUSE, CUSTOMERS_OUT, CUSTOMER_MINUTES |
| `crm/interactions` | Contact (partitioned by month) | CHANNEL, REASON, BP_ID, WAIT_SEC, HANDLE_SEC, FCR, CSAT |
| `weather/daily_weather` | Day | TAVG/TMAX/TMIN, PRECIP, MAX_GUST, HDD, CDD |
| `reference/towns`, `reference/rate_schedules` | Lookup | Division mapping, tariff descriptions |

## Built-in business patterns (things the reports should reveal)
- **Seasonality from weather**: summer cooling peaks (electric) and winter heating peaks (gas) driven by the daily weather feed.
- **BGS supply rate reset on 2025-06-01 (+18%)** and a smaller one in June 2026. High-bill complaints spike in the contact center right after.
- **Major storms** (three tropical remnants and five nor'easters): outage clusters, emergency PM03 work orders, and call-volume/ASA spikes all land on the same days.
- **Customer payment behaviour** (hidden segments): on-time payers, occasional late payers, chronic late payers, and customers in distress. These drive AR aging, arrangements and disconnects.
- **Winter termination moratorium (Nov 15 – Mar 15)**: residential disconnects shift to mid-March onward. Medical-certificate accounts are never disconnected.
- **Move-in/move-out churn**: final bills, vacancies (AMI usage drops to near zero), and new customers at the same premise.

## Injected data-quality issues (for the staging layer to fix)
| Where | Issue | Expected fix |
|---|---|---|
| ADRC.CITY1 | ~3% upper-case, ~2% trailing spaces | TRIM + proper-case / join on ZIP |
| ADRC.POST_CODE1 | ~1.5% lost leading zero (`7102`) | Left-pad to 5 digits |
| BUT000.NAME_LAST | ~2% upper-case | Proper-case |
| BUT000.TEL_NUMBER | 4 different formats, ~8% missing | Normalize to digits |
| BUT000.SMTP_ADDR | ~27% residential missing | Flag as contactability metric |
| DFKKZP | ~0.15% exact duplicate rows (re-extract) | De-duplicate on PAYMENT_ID |
| ERCH | Reversed bills (STORNODAT ≠ 0) plus rebills | Exclude reversed documents from revenue/AR |
| SAP dates | `0` and `99991231` sentinels | Convert to NULL / open-ended |
| ami/daily_reads | ~0.8% MISSING (null kWh), rare 40× spikes | Exclude / flag outliers |
| crm/interactions | ~8-25% null BP_ID (unauthenticated) | Keep, report as "unidentified" |
| oms/outage_events | Events still open at as-of date have no restore time | Treat as open, exclude from closed-event SAIDI |
