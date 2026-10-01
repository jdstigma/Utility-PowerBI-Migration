#!/usr/bin/env python3
"""Generate the Power BI Project (PBIP) for the utility reports.

  powerbi/UtilityReporting.pbip                 <- open this in Power BI Desktop
  powerbi/UtilityReporting.SemanticModel/       <- TMDL: tables, relationships, DAX measures
  powerbi/UtilityReporting.Report/              <- PBIR: pages and visuals

Columns and types come from the actual Parquet files in powerbi_data/, so the
model always matches the exported data. Every table loads from GitHub through
the BaseUrl parameter (Web.Contents + Parquet.Document), so a refresh pulls the
latest published files, the same way the Excel reports pulled from SharePoint.

Once the report exists it is formatted in Power BI Desktop, so the script
protects it:
  python powerbi/build_pbip.py --model-only        # tables, relationships, measures only
  python powerbi/build_pbip.py --overwrite-report  # regenerate pages too (loses Desktop formatting)

--model-only replaces the whole semantic model folder. Make model changes
(measures, formats, relationships) here rather than in Desktop, or copy them
into MEASURES / RELATIONSHIPS first, or they will be overwritten.
"""
import hashlib
import json
import shutil
import uuid
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "powerbi_data"
OUT = Path(__file__).resolve().parent
NAME = "UtilityReporting"
BASE_URL = "https://raw.githubusercontent.com/jdstigma/Utility-PowerBI-Migration/main/powerbi_data/"
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric"

# display name -> parquet file stem
TABLES = {
    "Period": "dim_period",
    "Date": "date_daily",
    "Division": "division",
    "Revenue Residential": "revenue_residential_monthly",
    "Revenue Commercial": "revenue_commercial_monthly",
    "AR Aging": "ar_aging_monthly",
    "AR Top Accounts": "ar_top_accounts",
    "Payments": "payments_monthly",
    "Digital Adoption": "digital_adoption_monthly",
    "AMI Daily": "ami_daily_profile",
    "Outages": "reliability_events",
    "Work Orders": "work_orders_monthly",
    "Work Order Backlog": "work_order_backlog",
    "Contact Center": "contact_center_daily",
    "Collections Orders": "collections_orders_monthly",
    "Moratorium Compliance": "moratorium_compliance",
    "Payment Plans": "payment_plans_monthly",
    "Billing Exceptions": "billing_exceptions_monthly",
    "Rebills": "rebills_monthly",
    "Service Orders": "service_orders_monthly",
    "Customer 360": "customer_360",
    "Customer Bills": "customer_bills_recent",
    "Data Quality": "data_quality",
}

# (from table, from column) -> (to table, to column); all many-to-one, single direction
RELATIONSHIPS = [
    ("Date", "period_key", "Period", "period_key"),
    ("Revenue Residential", "period_key", "Period", "period_key"),
    ("Revenue Commercial", "period_key", "Period", "period_key"),
    ("Revenue Residential", "division", "Division", "division"),
    ("Revenue Commercial", "division", "Division", "division"),
    ("AR Aging", "month_end", "Period", "month_end"),
    ("AR Aging", "division", "Division", "division"),
    ("AR Top Accounts", "division", "Division", "division"),
    ("Payments", "payment_month", "Period", "month_start"),
    ("Payments", "division", "Division", "division"),
    ("Digital Adoption", "month_end", "Period", "month_end"),
    ("Digital Adoption", "division", "Division", "division"),
    ("AMI Daily", "read_date", "Date", "date"),
    ("AMI Daily", "division", "Division", "division"),
    ("Outages", "outage_date", "Date", "date"),
    ("Outages", "division", "Division", "division"),
    ("Work Orders", "created_month", "Period", "month_start"),
    ("Work Orders", "division", "Division", "division"),
    ("Work Order Backlog", "division", "Division", "division"),
    ("Contact Center", "interaction_date", "Date", "date"),
    ("Collections Orders", "created_month", "Period", "month_start"),
    ("Collections Orders", "division", "Division", "division"),
    ("Payment Plans", "created_month", "Period", "month_start"),
    ("Payment Plans", "division", "Division", "division"),
    ("Billing Exceptions", "bill_month", "Period", "month_start"),
    ("Billing Exceptions", "division", "Division", "division"),
    ("Rebills", "reversal_month", "Period", "month_start"),
    ("Service Orders", "created_month", "Period", "month_start"),
    ("Service Orders", "division", "Division", "division"),
    ("Customer 360", "division", "Division", "division"),
    ("Customer Bills", "customer_id", "Customer 360", "customer_id"),
]

USD0, USD2, PCT, INT, DEC1, DEC2 = r"\$#,0", r"\$#,0.00", "0.0%", "#,0", "#,0.0", "#,0.00"

# latest-snapshot helper for semi-additive month-end tables
def snapshot(table, expr, extra=""):
    f = f", {extra}" if extra else ""
    return (f"VAR me = MAX('{table}'[month_end]) RETURN "
            f"CALCULATE({expr}, '{table}'[month_end] = me{f})")


def yoy(measure):
    return ("VAR firstKey = MINX(ALL('Period'), 'Period'[period_key]) "
            "VAR keys = FILTER(VALUES('Period'[period_key]), 'Period'[period_key] - 100 >= firstKey) "
            f"VAR cur = CALCULATE([{measure}], TREATAS(keys, 'Period'[period_key])) "
            f"VAR py = CALCULATE([{measure}], REMOVEFILTERS('Period'), "
            "TREATAS(SELECTCOLUMNS(keys, \"k\", 'Period'[period_key] - 100), 'Period'[period_key])) "
            "RETURN IF(NOT ISBLANK(py), DIVIDE(cur - py, py))")


# table -> [(measure name, DAX, format, display folder)]
MEASURES = {
    "Revenue Residential": [
        ("Residential Revenue", "SUM('Revenue Residential'[total_amt])", USD0, "Revenue"),
        ("Residential kWh", "SUM('Revenue Residential'[kwh])", INT, "Usage"),
        ("Residential Therms", "SUM('Revenue Residential'[therms])", INT, "Usage"),
        ("Residential Bills", "SUM('Revenue Residential'[bills])", INT, "Revenue"),
        ("Avg Residential Bill", "DIVIDE([Residential Revenue], [Residential Bills])", USD2, "Revenue"),
        ("Residential Revenue YoY %", yoy("Residential Revenue"), PCT, "Revenue"),
        ("Total Revenue", "[Residential Revenue] + [C&I Revenue]", USD0, "Revenue"),
        ("Residential Share %", "DIVIDE([Residential Revenue], [Total Revenue])", PCT, "Revenue"),
    ],
    "Revenue Commercial": [
        ("C&I Revenue", "SUM('Revenue Commercial'[total_amt])", USD0, "Revenue"),
        ("C&I kWh", "SUM('Revenue Commercial'[kwh])", INT, "Usage"),
        ("C&I Therms", "SUM('Revenue Commercial'[therms])", INT, "Usage"),
        ("C&I Bills", "SUM('Revenue Commercial'[bills])", INT, "Revenue"),
        ("Avg C&I Bill", "DIVIDE([C&I Revenue], [C&I Bills])", USD2, "Revenue"),
        ("C&I Revenue YoY %", yoy("C&I Revenue"), PCT, "Revenue"),
    ],
    "AR Aging": [
        ("AR Balance", snapshot("AR Aging", "SUM('AR Aging'[open_amt])", "'AR Aging'[is_receivable] = TRUE()"), USD0, "AR"),
        ("AR Past Due", snapshot("AR Aging", "SUM('AR Aging'[open_amt])",
                                 "'AR Aging'[is_receivable] = TRUE(), 'AR Aging'[bucket_order] > 1"), USD0, "AR"),
        ("AR % Past Due", "DIVIDE([AR Past Due], [AR Balance])", PCT, "AR"),
        ("AR 91-180 Days", snapshot("AR Aging", "SUM('AR Aging'[open_amt])", "'AR Aging'[bucket_order] = 5"), USD0, "AR"),
        ("Written Off to Date", snapshot("AR Aging", "SUM('AR Aging'[open_amt])", "'AR Aging'[is_receivable] = FALSE()"), USD0, "AR"),
    ],
    "AR Top Accounts": [
        ("Top Accounts Past Due", "SUM('AR Top Accounts'[past_due_amt])", USD0, "AR"),
    ],
    "Payments": [
        ("Payments $", "SUM(Payments[amount])", USD0, "Payments"),
        ("Payment Count", "SUM(Payments[payments])", INT, "Payments"),
        ("Digital Payment %", "DIVIDE(CALCULATE([Payments $], Payments[channel] IN {\"Autopay (ACH)\", \"Online\", \"Mobile App\"}), [Payments $])", PCT, "Payments"),
        ("Energy Assistance $", "CALCULATE([Payments $], Payments[channel] = \"Energy Assistance\")", USD0, "Payments"),
    ],
    "Digital Adoption": [
        ("Active Customers", snapshot("Digital Adoption", "SUM('Digital Adoption'[active_customers])"), INT, "Adoption"),
        ("Autopay %", "DIVIDE(" + snapshot("Digital Adoption", "SUM('Digital Adoption'[autopay_customers])") + ", [Active Customers])", PCT, "Adoption"),
        ("Paperless %", "DIVIDE(" + snapshot("Digital Adoption", "SUM('Digital Adoption'[paperless_customers])") + ", [Active Customers])", PCT, "Adoption"),
    ],
    "AMI Daily": [
        ("Avg Daily kWh per Meter", "DIVIDE(SUM('AMI Daily'[kwh_valid]), SUM('AMI Daily'[reads_valid]))", DEC1, "AMI"),
        ("Read Success %", "DIVIDE(SUM('AMI Daily'[meters_reporting]), SUM('AMI Daily'[meters_expected]))", "0.00%", "AMI"),
        ("Estimated Read %", "DIVIDE(SUM('AMI Daily'[reads_estimated]), SUM('AMI Daily'[meters_expected]))", "0.00%", "AMI"),
        ("Spike Reads", "SUM('AMI Daily'[reads_spike])", INT, "AMI"),
    ],
    "Date": [
        ("Avg Temp (F)", "AVERAGE('Date'[temp_avg_f])", DEC1, "Weather"),
        ("Cooling Degree Days", "SUM('Date'[cooling_degree_days])", INT, "Weather"),
        ("Heating Degree Days", "SUM('Date'[heating_degree_days])", INT, "Weather"),
        ("Major Event Days", "CALCULATE(COUNTROWS('Date'), 'Date'[is_major_event_day] = TRUE()) + 0", INT, "Reliability"),
    ],
    "Division": [
        ("Customers Served", "SUM(Division[customers_served])", INT, "Reliability"),
    ],
    "Outages": [
        ("Outage Events", "COUNTROWS(Outages)", INT, "Reliability"),
        ("Customer Minutes", "SUM(Outages[customer_minutes])", INT, "Reliability"),
        ("Customer Interruptions", "SUM(Outages[customers_out])", INT, "Reliability"),
        ("SAIDI (min)", "DIVIDE([Customer Minutes], [Customers Served])", DEC1, "Reliability"),
        ("SAIFI", "DIVIDE([Customer Interruptions], [Customers Served])", DEC2, "Reliability"),
        ("CAIDI (min)", "DIVIDE([Customer Minutes], [Customer Interruptions])", DEC1, "Reliability"),
        ("SAIDI excl. MED (min)", "CALCULATE([SAIDI (min)], Outages[is_major_event_day] = FALSE())", DEC1, "Reliability"),
        ("SAIFI excl. MED", "CALCULATE([SAIFI], Outages[is_major_event_day] = FALSE())", DEC2, "Reliability"),
    ],
    "Work Orders": [
        ("Work Orders Created", "SUM('Work Orders'[work_orders])", INT, "Work Orders"),
        ("On-Time Completion %", "DIVIDE(SUM('Work Orders'[finished_on_time]), SUM('Work Orders'[finished]))", PCT, "Work Orders"),
        ("Planned Cost", "SUM('Work Orders'[planned_cost])", USD0, "Work Orders"),
        ("Actual Cost", "SUM('Work Orders'[actual_cost])", USD0, "Work Orders"),
    ],
    "Work Order Backlog": [
        ("Open Backlog", "COUNTROWS('Work Order Backlog')", INT, "Work Orders"),
        ("Overdue Work Orders", "CALCULATE(COUNTROWS('Work Order Backlog'), 'Work Order Backlog'[is_overdue] = TRUE()) + 0", INT, "Work Orders"),
        ("Avg Backlog Age (days)", "AVERAGE('Work Order Backlog'[open_age_days])", DEC1, "Work Orders"),
    ],
    "Contact Center": [
        ("Total Contacts", "SUM('Contact Center'[contacts])", INT, "Contact Center"),
        ("ASA (sec)", "DIVIDE(CALCULATE(SUM('Contact Center'[wait_sec_total]), 'Contact Center'[channel] = \"Phone - Agent\"), SUM('Contact Center'[agent_calls]))", DEC1, "Contact Center"),
        ("Service Level (30s) %", "DIVIDE(SUM('Contact Center'[answered_within_30s]), SUM('Contact Center'[agent_calls]))", PCT, "Contact Center"),
        ("AHT (min)", "DIVIDE(CALCULATE(SUM('Contact Center'[handle_sec_total]), 'Contact Center'[is_assisted] = TRUE()), CALCULATE(SUM('Contact Center'[contacts]), 'Contact Center'[is_assisted] = TRUE())) / 60", DEC1, "Contact Center"),
        ("FCR %", "DIVIDE(SUM('Contact Center'[resolved_first_contact]), [Total Contacts])", PCT, "Contact Center"),
        ("CSAT (avg)", "DIVIDE(SUM('Contact Center'[csat_total]), SUM('Contact Center'[csat_responses]))", DEC2, "Contact Center"),
        ("Self-Service %", "DIVIDE(CALCULATE([Total Contacts], 'Contact Center'[is_assisted] = FALSE()), [Total Contacts])", PCT, "Contact Center"),
    ],
    "Collections Orders": [
        ("Disconnects", "CALCULATE(SUM('Collections Orders'[orders]), 'Collections Orders'[order_type] = \"Disconnect for Non-Pay\", 'Collections Orders'[status] = \"Completed\")", INT, "Collections"),
        ("Reconnects", "CALCULATE(SUM('Collections Orders'[orders]), 'Collections Orders'[order_type] = \"Reconnect\", 'Collections Orders'[status] = \"Completed\")", INT, "Collections"),
        ("Reconnect Rate %", "DIVIDE([Reconnects], [Disconnects])", PCT, "Collections"),
        ("Reconnect Within 1 Day %", "DIVIDE(CALCULATE(SUM('Collections Orders'[completed_within_1_day]), 'Collections Orders'[order_type] = \"Reconnect\", 'Collections Orders'[status] = \"Completed\"), [Reconnects])", PCT, "Collections"),
    ],
    "Moratorium Compliance": [
        ("Moratorium Exceptions", "CALCULATE(SUM('Moratorium Compliance'[disconnects_completed]), 'Moratorium Compliance'[is_winter_moratorium] = TRUE(), 'Moratorium Compliance'[segment] = \"Residential\") + 0", INT, "Collections"),
        ("Medical Cert Disconnects", "CALCULATE(SUM('Moratorium Compliance'[disconnects_completed]), 'Moratorium Compliance'[has_medical_cert] = TRUE()) + 0", INT, "Collections"),
    ],
    "Payment Plans": [
        ("Payment Plans Created", "SUM('Payment Plans'[plans])", INT, "Collections"),
        ("Plan Default %", "DIVIDE(CALCULATE(SUM('Payment Plans'[plans]), 'Payment Plans'[status] = \"Defaulted\"), CALCULATE(SUM('Payment Plans'[plans]), 'Payment Plans'[status] IN {\"Completed\", \"Defaulted\"}))", PCT, "Collections"),
    ],
    "Billing Exceptions": [
        ("Bills Reviewed", "SUM('Billing Exceptions'[bills])", INT, "Exceptions"),
        ("Estimated Bill %", "DIVIDE(SUM('Billing Exceptions'[elec_estimated]), [Bills Reviewed])", "0.00%", "Exceptions"),
        ("Consecutive Estimates", "SUM('Billing Exceptions'[consecutive_estimates])", INT, "Exceptions"),
        ("Zero-Usage Bills", "SUM('Billing Exceptions'[zero_usage_bills])", INT, "Exceptions"),
        ("High-Variance Bill %", "DIVIDE(SUM('Billing Exceptions'[high_variance_bills]), [Bills Reviewed])", PCT, "Exceptions"),
    ],
    "Rebills": [
        ("Reversed Bills", "SUM(Rebills[reversed_bills])", INT, "Exceptions"),
        ("Reversed $", "SUM(Rebills[reversed_amt])", USD0, "Exceptions"),
    ],
    "Service Orders": [
        ("Service Orders Created", "SUM('Service Orders'[orders])", INT, "Service Orders"),
        ("Avg Cycle Days", "DIVIDE(SUM('Service Orders'[cycle_days_total]), SUM('Service Orders'[completed]))", DEC1, "Service Orders"),
        ("Completion %", "DIVIDE(SUM('Service Orders'[completed]), [Service Orders Created])", PCT, "Service Orders"),
        ("Open Service Orders", "SUM('Service Orders'[open_orders])", INT, "Service Orders"),
    ],
    "Customer 360": [
        ("Customers", "COUNTROWS('Customer 360')", INT, "Customers"),
        ("Open Balance", "SUM('Customer 360'[open_amt])", USD2, "Customers"),
        ("Past Due Balance", "SUM('Customer 360'[past_due_amt])", USD2, "Customers"),
    ],
    "Customer Bills": [
        ("Billed (Last 3 Mo)", "SUM('Customer Bills'[total_amt])", USD2, "Customers"),
        ("Paid (Last 3 Mo)", "SUM('Customer Bills'[paid_amt])", USD2, "Customers"),
    ],
    "Data Quality": [
        ("Rows Affected", "SUM('Data Quality'[rows_affected])", INT, "Data Quality"),
    ],
}

SORT_BY = {("AR Aging", "aging_bucket"): "bucket_order", ("Date", "day_name"): "day_of_week",
           ("Period", "year_month"): "period_key", ("Period", "month_name"): "month_num"}

KEY_WORDS = ("_key", "_id", "year", "month_num", "day_of_week", "priority", "bucket_order", "check_order",
             "quarter", "latitude", "longitude")


def guid(*parts):
    return str(uuid.UUID(hashlib.md5("|".join(parts).encode()).hexdigest()))


def q(name):  # TMDL object name
    return name if name.replace("_", "").isalnum() else "'" + name.replace("'", "''") + "'"


def arrow_types(t):
    s = str(t)
    if s.startswith(("int", "uint")):
        return "int64", "Int64.Type"
    if s.startswith(("double", "float")):
        return "double", "type number"
    if s.startswith("decimal"):
        return "decimal", "Currency.Type"
    if s == "bool":
        return "boolean", "type logical"
    if s.startswith("date"):
        return "dateTime", "type date"
    if s.startswith("timestamp"):
        return "dateTime", "type datetime"
    return "string", "type text"


# ----------------------------------------------------------------------------- semantic model (TMDL)
def write_model():
    sm = OUT / f"{NAME}.SemanticModel"
    d = sm / "definition"
    (d / "tables").mkdir(parents=True)
    (sm / "definition.pbism").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.2", "settings": {}}, indent=2))
    (d / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1601\n\n")
    (d / "model.tmdl").write_text(
        "model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
        "\tsourceQueryCulture: en-US\n\tdataAccessOptions\n\t\tlegacyRedirects\n\t\treturnErrorValuesAsNull\n\n"
        "annotation PBI_QueryOrder = " + json.dumps(["BaseUrl"] + list(TABLES)) + "\n\n"
        + "".join(f"ref table {q(t)}\n" for t in TABLES) + "\n")
    (d / "expressions.tmdl").write_text(
        f'expression BaseUrl = "{BASE_URL}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n'
        f"\tlineageTag: {guid('BaseUrl')}\n\n\tannotation PBI_ResultType = Text\n\n")

    for table, stem in TABLES.items():
        schema = pq.read_schema(DATA / f"{stem}.parquet")
        lines = [f"table {q(table)}", f"\tlineageTag: {guid(table)}"]
        if table == "Date":
            lines.append("\tdataCategory: Time")
        lines.append("")
        for m, dax, fmt, folder in MEASURES.get(table, []):
            lines += [f"\tmeasure {q(m)} = {dax}", f"\t\tformatString: {fmt}", f"\t\tdisplayFolder: {folder}",
                      f"\t\tlineageTag: {guid(table, m)}", ""]
        types = []
        for f in schema:
            dt, mt = arrow_types(f.type)
            types.append(f'{{"{f.name}", {mt}}}')
            key = any(k in f.name for k in KEY_WORDS)
            summarize = "sum" if dt in ("int64", "double", "decimal") and not key else "none"
            lines += [f"\tcolumn {q(f.name)}", f"\t\tdataType: {dt}"]
            if table == "Date" and f.name == "date":
                lines.append("\t\tisKey")
            if mt == "type date":
                lines.append("\t\tformatString: Short Date")
            elif mt == "type datetime":
                lines.append("\t\tformatString: General Date")
            elif dt == "decimal":
                lines.append(f"\t\tformatString: {USD2}")
            elif dt == "int64":
                lines.append("\t\tformatString: 0" if key else f"\t\tformatString: {INT}")
            lines += [f"\t\tlineageTag: {guid(table, f.name)}", f"\t\tsummarizeBy: {summarize}",
                      f"\t\tsourceColumn: {f.name}", "", "\t\tannotation SummarizationSetBy = Automatic", ""]
        m_expr = [
            "let",
            f'    Source = Parquet.Document(Binary.Buffer(Web.Contents(BaseUrl, [RelativePath = "{stem}.parquet"]))),',
            f"    Typed = Table.TransformColumnTypes(Source, {{{', '.join(types)}}})",
            "in",
            "    Typed",
        ]
        lines += [f"\tpartition {q(table)} = m", "\t\tmode: import", "\t\tsource ="]
        lines += ["\t\t\t\t" + x for x in m_expr]
        lines += ["", "\tannotation PBI_ResultType = Table", ""]
        (d / "tables" / f"{table}.tmdl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    rel = []
    for ft, fc, tt, tc in RELATIONSHIPS:
        rel += [f"relationship {guid('rel', ft, fc, tt, tc)}",
                f"\tfromColumn: {q(ft)}.{q(fc)}", f"\ttoColumn: {q(tt)}.{q(tc)}", ""]
    (d / "relationships.tmdl").write_text("\n".join(rel) + "\n")


# ----------------------------------------------------------------------------- report (PBIR)
def col(t, c):
    return {"Column": {"Expression": {"SourceRef": {"Entity": t}}, "Property": c}}


def mea(t, m):
    return {"Measure": {"Expression": {"SourceRef": {"Entity": t}}, "Property": m}}


MEASURE_HOME = {m: t for t, ms in MEASURES.items() for m, *_ in ms}


def F(ref):
    """'Table[column]' -> column field; '[Measure]' -> measure field."""
    if ref.startswith("["):
        m = ref[1:-1]
        return mea(MEASURE_HOME[m], m), f"{MEASURE_HOME[m]}.{m}", m
    t, c = ref[:-1].split("[")
    return col(t, c), f"{t}.{c}", c


def lit(s):
    return {"expr": {"Literal": {"Value": "'" + s.replace("'", "''") + "'"}}}


def visual(vtype, x, y, w, h, roles, title=None, sort=None, z=0):
    query = {"queryState": {}}
    for role, refs in roles.items():
        query["queryState"][role] = {"projections": [
            dict(zip(("field", "queryRef", "nativeQueryRef"), F(r))) for r in refs]}
    if sort:
        ref, direction = sort
        query["sortDefinition"] = {"sort": [{"field": F(ref)[0], "direction": direction}], "isDefaultSort": False}
    v = {"visualType": vtype, "query": query, "drillFilterOtherVisuals": True}
    if title:
        v["visualContainerObjects"] = {"title": [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}},
                                                                 "text": lit(title)}}]}
    return {"x": x, "y": y, "z": z, "width": w, "height": h, "visual": v}


def cards(measures, y=70, h=90):
    w = (1240 - 10 * (len(measures) - 1)) / len(measures)
    return [visual("card", 20 + i * (w + 10), y, w, h, {"Values": [m]}) for i, m in enumerate(measures)]


def _lit(v):
    return {"expr": {"Literal": {"Value": v}}}


def slicer(ref, x, w, title, kind="dropdown", search=False):
    """Slicer row format: 82px tall at the top of the page, title and hover header hidden.
    kind='between' is a range slider (numeric fields); 'dropdown' is single-select with Select all."""
    v = visual("slicer", x, 0, w, 82.33, {"Values": [ref]})
    if kind == "between":
        objs = {"data": [{"properties": {"mode": _lit("'Between'")}}],
                "slider": [{"properties": {"show": _lit("true")}}],
                "header": [{"properties": {"show": _lit("false")}}]}
    else:
        objs = {"data": [{"properties": {"mode": _lit("'Dropdown'")}}],
                "selection": [{"properties": {"selectAllCheckboxEnabled": _lit("true"), "singleSelect": _lit("true")}}],
                "header": [{"properties": {"show": _lit("true"), "text": _lit(f"'{title}'")}}]}
        if search:
            objs["general"] = [{"properties": {"selfFilterEnabled": _lit("true")}}]
    v["visual"]["objects"] = objs
    v["visual"]["visualContainerObjects"] = {
        "title": [{"properties": {"show": _lit("false"), "text": _lit(f"'{title}'")}}],
        "visualHeader": [{"properties": {"show": _lit("false")}}]}
    return v


def slicers():
    return [slicer("Division[division]", 1044.26, 215.44, "Division"),
            slicer("Period[year]", 0, 212.69, "Year", kind="between")]


PAGES = [
    ("Revenue & Billing", slicers() + cards(["[Residential Revenue]", "[C&I Revenue]", "[Avg Residential Bill]",
                                             "[Residential Revenue YoY %]", "[C&I Revenue YoY %]"]) + [
        visual("lineClusteredColumnComboChart", 20, 175, 820, 525,
               {"Category": ["Period[year_month]"], "Y": ["[Residential Revenue]"], "Y2": ["[C&I Revenue]"]},
               "Residential (columns, left axis) vs C&I (line, right axis) revenue by month",
               sort=("Period[year_month]", "Ascending")),
        visual("clusteredBarChart", 850, 175, 410, 255, {"Category": ["Revenue Residential[elec_rate_code]"],
               "Y": ["[Residential Revenue]"]}, "Residential revenue by electric rate"),
        visual("clusteredBarChart", 850, 445, 410, 255, {"Category": ["Revenue Commercial[elec_rate_code]"],
               "Y": ["[C&I Revenue]"]}, "C&I revenue by electric rate"),
    ]),
    ("AR Aging", slicers() + cards(["[AR Balance]", "[AR Past Due]", "[AR % Past Due]", "[AR 91-180 Days]",
                                    "[Written Off to Date]"]) + [
        visual("columnChart", 20, 175, 700, 525, {"Category": ["Period[year_month]"], "Series": ["AR Aging[aging_bucket]"],
               "Y": ["[AR Balance]"]}, "Month-end receivables by aging bucket", sort=("Period[year_month]", "Ascending")),
        visual("tableEx", 730, 175, 530, 525, {"Values": ["AR Top Accounts[customer_name]", "AR Top Accounts[segment]",
               "AR Top Accounts[division]", "AR Top Accounts[past_due_amt]", "AR Top Accounts[oldest_days_past_due]"]},
               "Top past-due accounts", sort=("AR Top Accounts[past_due_amt]", "Descending")),
    ]),
    ("Payments & Digital", slicers() + cards(["[Payments $]", "[Payment Count]", "[Digital Payment %]",
                                              "[Autopay %]", "[Paperless %]"]) + [
        visual("lineChart", 20, 175, 620, 525, {"Category": ["Period[year_month]"], "Y": ["[Autopay %]", "[Paperless %]"]},
               "Autopay and paperless adoption", sort=("Period[year_month]", "Ascending")),
        visual("clusteredBarChart", 650, 175, 610, 525, {"Category": ["Payments[channel]"], "Y": ["[Payments $]"]},
               "Payments by channel", sort=("[Payments $]", "Descending")),
    ]),
    ("Usage & AMI", slicers() + cards(["[Avg Daily kWh per Meter]", "[Read Success %]", "[Estimated Read %]",
                                       "[Spike Reads]", "[Avg Temp (F)]"]) + [
        visual("lineClusteredColumnComboChart", 20, 175, 1240, 330,
               {"Category": ["Date[date]"], "Y": ["[Avg Daily kWh per Meter]"], "Y2": ["[Avg Temp (F)]"]},
               "Daily kWh per meter (columns) vs temperature (line)", sort=("Date[date]", "Ascending")),
        visual("clusteredColumnChart", 20, 515, 610, 185, {"Category": ["Date[day_name]"], "Y": ["[Avg Daily kWh per Meter]"]},
               "Usage by day of week", sort=("Date[day_name]", "Ascending")),
        visual("clusteredBarChart", 640, 515, 620, 185, {"Category": ["AMI Daily[elec_rate_code]"],
               "Y": ["[Avg Daily kWh per Meter]"]}, "Usage by rate"),
    ]),
    ("Outage & Reliability", slicers() + cards(["[SAIDI excl. MED (min)]", "[SAIFI excl. MED]", "[CAIDI (min)]",
                                                "[SAIDI (min)]", "[Major Event Days]"]) + [
        visual("clusteredColumnChart", 20, 175, 610, 260, {"Category": ["Outages[division]"],
               "Y": ["[SAIDI excl. MED (min)]", "[SAIDI (min)]"]}, "SAIDI by division, with and without major events"),
        visual("clusteredBarChart", 640, 175, 620, 260, {"Category": ["Outages[cause]"], "Y": ["[Customer Minutes]"]},
               "Customer minutes by cause", sort=("[Customer Minutes]", "Descending")),
        visual("tableEx", 20, 445, 1240, 255, {"Values": ["Outages[circuit_id]", "Outages[division]",
               "[Outage Events]", "[Customer Interruptions]", "[Customer Minutes]"]},
               "Worst-performing circuits", sort=("[Customer Minutes]", "Descending")),
    ]),
    ("Work Order Backlog", slicers() + cards(["[Open Backlog]", "[Overdue Work Orders]", "[Avg Backlog Age (days)]",
                                              "[On-Time Completion %]", "[Actual Cost]"]) + [
        visual("columnChart", 20, 175, 700, 525, {"Category": ["Period[year_month]"], "Series": ["Work Orders[order_type]"],
               "Y": ["[Work Orders Created]"]}, "Work orders created by type", sort=("Period[year_month]", "Ascending")),
        visual("clusteredBarChart", 730, 175, 530, 525, {"Category": ["Work Order Backlog[asset_class]"],
               "Y": ["[Open Backlog]", "[Overdue Work Orders]"]}, "Open backlog by asset class",
               sort=("[Open Backlog]", "Descending")),
    ]),
    ("Contact Center", slicers() + cards(["[Total Contacts]", "[ASA (sec)]", "[Service Level (30s) %]", "[AHT (min)]",
                                          "[FCR %]", "[CSAT (avg)]"]) + [
        visual("lineChart", 20, 175, 1240, 260, {"Category": ["Date[date]"], "Y": ["[Total Contacts]"]},
               "Daily contacts (storm and rate-change spikes)", sort=("Date[date]", "Ascending")),
        visual("clusteredBarChart", 20, 445, 610, 255, {"Category": ["Contact Center[reason]"], "Y": ["[Total Contacts]"]},
               "Contacts by reason", sort=("[Total Contacts]", "Descending")),
        visual("clusteredBarChart", 640, 445, 620, 255, {"Category": ["Contact Center[channel]"],
               "Y": ["[Total Contacts]"]}, "Contacts by channel", sort=("[Total Contacts]", "Descending")),
    ]),
    ("Credit & Collections", slicers() + cards(["[Disconnects]", "[Reconnect Rate %]", "[Reconnect Within 1 Day %]",
                                                "[Moratorium Exceptions]", "[Medical Cert Disconnects]",
                                                "[Plan Default %]"]) + [
        visual("clusteredColumnChart", 20, 175, 820, 525, {"Category": ["Period[year_month]"],
               "Y": ["[Disconnects]", "[Reconnects]"]}, "Disconnects and reconnects (winter moratorium Nov 15 - Mar 15)",
               sort=("Period[year_month]", "Ascending")),
        visual("clusteredBarChart", 850, 175, 410, 525, {"Category": ["Payment Plans[plan_type]"],
               "Y": ["[Payment Plans Created]"], "Series": ["Payment Plans[status]"]}, "Payment arrangements by status"),
    ]),
    ("Meter-to-Cash Exceptions", slicers() + cards(["[Bills Reviewed]", "[Estimated Bill %]", "[Consecutive Estimates]",
                                                    "[Zero-Usage Bills]", "[High-Variance Bill %]", "[Reversed Bills]"]) + [
        visual("lineChart", 20, 175, 820, 525, {"Category": ["Period[year_month]"],
               "Y": ["[Estimated Bill %]", "[High-Variance Bill %]"]}, "Exception rates by bill month",
               sort=("Period[year_month]", "Ascending")),
        visual("tableEx", 850, 175, 410, 525, {"Values": ["Billing Exceptions[elec_rate_code]", "[Bills Reviewed]",
               "[Estimated Bill %]", "[High-Variance Bill %]"]}, "Exceptions by rate"),
    ]),
    ("Service Orders", slicers() + cards(["[Service Orders Created]", "[Completion %]", "[Avg Cycle Days]",
                                          "[Open Service Orders]"]) + [
        visual("clusteredBarChart", 20, 175, 610, 525, {"Category": ["Service Orders[order_type]"],
               "Y": ["[Avg Cycle Days]"]}, "Average cycle time (days) by order type", sort=("[Avg Cycle Days]", "Descending")),
        visual("columnChart", 640, 175, 620, 525, {"Category": ["Period[year_month]"], "Series": ["Service Orders[order_type]"],
               "Y": ["[Service Orders Created]"]}, "Service orders created", sort=("Period[year_month]", "Ascending")),
    ]),
    ("Customer 360", [
        slicer("Customer 360[customer_name]", 224.69, 400, "Customer", search=True),
        slicer("Customer 360[customer_id]", 636.69, 220, "Customer ID", search=True),
        ] + slicers() + cards(["[Customers]", "[Open Balance]", "[Past Due Balance]", "[Billed (Last 3 Mo)]",
                               "[Paid (Last 3 Mo)]"]) + [
        visual("tableEx", 20, 175, 1240, 255, {"Values": ["Customer 360[customer_id]", "Customer 360[customer_name]",
               "Customer 360[segment]", "Customer 360[address_line]", "Customer 360[city]", "Customer 360[elec_rate_code]",
               "Customer 360[is_autopay]", "Customer 360[is_low_income]", "Customer 360[open_amt]",
               "Customer 360[past_due_amt]"]}, "Customer profile", sort=("Customer 360[past_due_amt]", "Descending")),
        visual("tableEx", 20, 440, 1240, 260, {"Values": ["Customer Bills[bill_date]", "Customer Bills[bill_type]",
               "Customer Bills[kwh]", "Customer Bills[therms]", "Customer Bills[total_amt]", "Customer Bills[paid_amt]",
               "Customer Bills[due_date]"]}, "Recent bills", sort=("Customer Bills[bill_date]", "Descending")),
    ]),
    ("Data Quality", [
        visual("tableEx", 20, 20, 1240, 420, {"Values": ["Data Quality[source_table]", "Data Quality[dq_check]",
               "Data Quality[action_taken]", "Data Quality[rows_checked]", "Data Quality[rows_affected]",
               "Data Quality[pct_affected]"]}, "Data-quality issues found and fixed in the staging layer",
               sort=("Data Quality[check_order]", "Ascending")),
        visual("clusteredBarChart", 20, 450, 1240, 250, {"Category": ["Data Quality[dq_check]"], "Y": ["[Rows Affected]"]},
               "Rows affected by check", sort=("[Rows Affected]", "Descending")),
    ]),
]


def write_report():
    rp = OUT / f"{NAME}.Report"
    d = rp / "definition"
    (d / "pages").mkdir(parents=True)
    (rp / "definition.pbir").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0", "datasetReference": {"byPath": {"path": f"../{NAME}.SemanticModel"}}}, indent=2))
    (d / "version.json").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/report/definition/versionMetadata/1.0.0/schema.json", "version": "2.0.0"}, indent=2))
    (d / "report.json").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/report/definition/report/3.3.0/schema.json",
        "themeCollection": {}}, indent=2))
    order = []
    for i, (title, visuals) in enumerate(PAGES, 1):
        pid = f"p{i:02d}_{hashlib.md5(title.encode()).hexdigest()[:8]}"
        order.append(pid)
        pd_ = d / "pages" / pid
        (pd_ / "visuals").mkdir(parents=True)
        (pd_ / "page.json").write_text(json.dumps({
            "$schema": f"{SCHEMA}/item/report/definition/page/2.1.0/schema.json",
            "name": pid, "displayName": title, "displayOption": "FitToPage", "height": 720, "width": 1280}, indent=2))
        for j, v in enumerate(visuals):
            vid = hashlib.md5(f"{pid}{j}".encode()).hexdigest()[:20]
            (pd_ / "visuals" / vid).mkdir()
            (pd_ / "visuals" / vid / "visual.json").write_text(json.dumps({
                "$schema": f"{SCHEMA}/item/report/definition/visualContainer/2.12.0/schema.json",
                "name": vid,
                "position": {"x": round(v["x"], 2), "y": v["y"], "z": j, "width": round(v["width"], 2),
                             "height": v["height"], "tabOrder": j},
                "visual": v["visual"]}, indent=2))
    (d / "pages" / "pages.json").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/report/definition/pagesMetadata/1.1.0/schema.json",
        "pageOrder": order, "activePageName": order[0]}, indent=2))


def check_names():
    """Measure names must be unique model-wide and must not match a column in their table (case-insensitive)."""
    seen = {}
    for table, ms in MEASURES.items():
        cols = {f.name.lower() for f in pq.read_schema(DATA / f"{TABLES[table]}.parquet")}
        for m, *_ in ms:
            if m.lower() in cols:
                raise SystemExit(f"measure '{m}' collides with a column in '{table}'")
            if m.lower() in seen:
                raise SystemExit(f"measure '{m}' defined in both '{seen[m.lower()]}' and '{table}'")
            seen[m.lower()] = table


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Generate the Power BI project.",
                                 epilog="The report is edited in Power BI Desktop once it exists, so a full "
                                        "rebuild will not overwrite it without --overwrite-report.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--model-only", action="store_true",
                      help="rebuild only the semantic model (tables, relationships, measures); "
                           "leave the report pages and their formatting untouched")
    mode.add_argument("--overwrite-report", action="store_true",
                      help="rebuild the report pages too, discarding any formatting done in Power BI Desktop")
    args = ap.parse_args()

    check_names()
    model_dir, report_dir = OUT / f"{NAME}.SemanticModel", OUT / f"{NAME}.Report"
    build_report = not args.model_only
    if build_report and report_dir.exists() and not args.overwrite_report:
        raise SystemExit(f"{report_dir.name} already exists and may contain formatting edited in Power BI Desktop.\n"
                         "  Rebuild just the model:        python powerbi/build_pbip.py --model-only\n"
                         "  Replace the report as well:    python powerbi/build_pbip.py --overwrite-report")
    if args.model_only and not report_dir.exists():
        raise SystemExit(f"{report_dir.name} does not exist yet; run without --model-only first.")

    if model_dir.exists():
        shutil.rmtree(model_dir)
    write_model()
    if build_report:
        if report_dir.exists():
            shutil.rmtree(report_dir)
        write_report()
        (OUT / f"{NAME}.pbip").write_text(json.dumps({
            "$schema": f"{SCHEMA}/pbip/pbipProperties/1.0.0/schema.json",
            "version": "1.0", "artifacts": [{"report": {"path": f"{NAME}.Report"}}],
            "settings": {"enableAutoRecovery": True}}, indent=2))
    n_m = sum(len(v) for v in MEASURES.values())
    what = (f"{len(PAGES)} pages, {sum(len(v) for _, v in PAGES)} visuals" if build_report
            else "report left untouched")
    print(f"wrote {NAME}: {len(TABLES)} tables, {len(RELATIONSHIPS)} relationships, {n_m} measures; {what}")


if __name__ == "__main__":
    main()
