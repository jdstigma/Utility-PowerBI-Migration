#!/usr/bin/env python3
"""Train the weather-driven outage model behind the Power BI "Outage Forecast" page.

Model: Poisson regression of daily outage events per division on that day's weather,
with log(customers served / 100K) as an offset, so the rate is "outages per 100K
customers" and divisions of different size share one set of weather effects.

  log E[events] = b0 + b1*gust + b2*max(gust-35,0) + b3*max(gust-50,0)
                  + b4*precip + b5*cdd + b6*hdd + b7*summer + log(customers/100K)

The hinge terms let the effect of wind steepen above 35 and 50 mph, the way tree and
conductor damage does. Customers interrupted = predicted events x the historical
customers-per-event for that wind band.

Validation: fit on Oct 2024 - Mar 2026, score Apr - Sep 2026 (held out), then refit on
all 24 months for the published coefficients.

Outputs (powerbi_data/, read by Power BI):
  outage_model_coefficients.parquet  term, coefficient, std_error, p_value
  outage_model_bands.parquet         wind bands: outages per 100K, customers & minutes per event
  outage_model_divisions.parquet     division centroid lat/lon, customers served, calm-day baseline
  outage_model_backtest.parquet      held-out days: actual vs predicted events and customers
  outage_model_metrics.parquet       back-test accuracy figures

Note: the history is synthetic, and the generator made outages rise with wind and rain.
The model recovers that by construction; the method is what transfers to real data.

  python forecast/train_outage_model.py
"""
import json
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
import pyodbc
import statsmodels.api as sm

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "powerbi_data"
CONN = ("Driver={ODBC Driver 18 for SQL Server};Server=(localdb)\\MSSQLLocalDB;Database=UtilityDW;"
        "Trusted_Connection=yes;Encrypt=no;")
TEST_START = pd.Timestamp("2026-04-01")
FEATURES = ["max_gust_mph", "gust_over_35", "gust_over_50", "precip_in", "cdd", "hdd", "summer"]
BANDS = [(0, 25, "Under 25 mph"), (25, 35, "25-35 mph"), (35, 45, "35-45 mph"), (45, 55, "45-55 mph"), (55, 999, "55+ mph")]
# risk = predicted outages / the division's calm-day baseline
RISK = [(0, 1.5, "Normal", 1), (1.5, 3, "Elevated", 2), (3, 8, "High", 3), (8, 1e9, "Severe", 4)]


def features(df):
    """Same feature definitions are used by the DAX columns in Power BI."""
    X = pd.DataFrame(index=df.index)
    X["max_gust_mph"] = df["max_gust_mph"]
    X["gust_over_35"] = (df["max_gust_mph"] - 35).clip(lower=0)
    X["gust_over_50"] = (df["max_gust_mph"] - 50).clip(lower=0)
    X["precip_in"] = df["precip_in"]
    X["cdd"] = df["cdd"]
    X["hdd"] = df["hdd"]
    X["summer"] = df["month"].isin([6, 7, 8]).astype(float)
    return sm.add_constant(X, has_constant="add").rename(columns={"const": "intercept"})


def load():
    cn = pyodbc.connect(CONN)
    q = """
        SELECT d.[date], g.division, g.customers_served,
               d.max_gust_mph, d.precip_in, d.cooling_degree_days AS cdd, d.heating_degree_days AS hdd,
               MONTH(d.[date]) AS month, d.is_major_event_day,
               COUNT(o.outage_id)                AS events,
               ISNULL(SUM(o.customers_out), 0)   AS customers_out,
               ISNULL(SUM(CAST(o.customer_minutes AS BIGINT)), 0) AS customer_minutes
        FROM dw.dim_date d
        CROSS JOIN (SELECT division, SUM(customers_served) AS customers_served,
                           AVG(latitude) AS lat, AVG(longitude) AS lon
                    FROM dw.dim_geography GROUP BY division) g
        LEFT JOIN dw.fact_outage o ON o.outage_date = d.[date] AND o.division = g.division
        WHERE d.is_in_window = 1
        GROUP BY d.[date], g.division, g.customers_served, d.max_gust_mph, d.precip_in,
                 d.cooling_degree_days, d.heating_degree_days, d.is_major_event_day"""
    import warnings
    warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")
    df = pd.read_sql(q, cn, parse_dates=["date"])
    geo = pd.read_sql("""SELECT division, SUM(customers_served) AS customers_served,
                                SUM(latitude * customers_served) / SUM(customers_served)  AS latitude,
                                SUM(longitude * customers_served) / SUM(customers_served) AS longitude
                         FROM dw.dim_geography GROUP BY division""", cn)
    return df.sort_values(["date", "division"]).reset_index(drop=True), geo


def fit(df):
    X = features(df)
    offset = np.log(df["customers_served"] / 100_000)
    return sm.GLM(df["events"], X, family=sm.families.Poisson(), offset=offset).fit()


def predict(model, df):
    return model.predict(features(df), offset=np.log(df["customers_served"] / 100_000))


def band_of(gust):
    for lo, hi, label in BANDS:
        if lo <= gust < hi:
            return label
    return BANDS[-1][2]


def main():
    df, geo = load()
    df["band"] = df["max_gust_mph"].map(band_of)

    # --- wind bands from the full history
    bands = []
    for lo, hi, label in BANDS:
        b = df[df["band"] == label]
        ev = b["events"].sum()
        bands.append({"band": label, "gust_min": lo, "gust_max": hi, "band_order": len(bands) + 1,
                      "division_days": len(b), "days": b["date"].nunique(),
                      "outages_per_100k": round(b["events"].sum() / (b["customers_served"].sum() / 100_000), 3) if len(b) else None,
                      "customers_per_event": round(b["customers_out"].sum() / ev, 1) if ev else None,
                      "minutes_per_event": round(b["customer_minutes"].sum() / b["customers_out"].sum(), 1) if ev else None})
    bands = pd.DataFrame(bands)
    # bands with no history borrow the next lower band's ratios
    bands[["customers_per_event", "minutes_per_event"]] = bands[["customers_per_event", "minutes_per_event"]].ffill()
    cpe = dict(zip(bands["band"], bands["customers_per_event"]))

    # --- back-test: train on the first 18 months, score the last 6
    train, test = df[df["date"] < TEST_START], df[df["date"] >= TEST_START].copy()
    m_bt = fit(train)
    test["predicted_events"] = predict(m_bt, test)
    test["predicted_customers_out"] = test["predicted_events"] * test["band"].map(cpe)
    daily = test.groupby("date")[["events", "predicted_events", "customers_out", "predicted_customers_out"]].sum()
    calm = test.assign(**{"max_gust_mph": 20.0, "precip_in": 0.0})
    test["baseline"] = predict(m_bt, calm)
    test["risk_ratio"] = test["predicted_events"] / test["baseline"]
    flagged = test["risk_ratio"] >= RISK[2][0]                      # High or Severe
    p95 = test["events"].quantile(0.95)
    big = test["events"] >= p95
    metrics = {
        "Held-out period": f"{TEST_START:%b %Y} - {test['date'].max():%b %Y}",
        "Division-days scored": len(test),
        "Mean absolute error (outages per division-day)": round(float((test["events"] - test["predicted_events"]).abs().mean()), 2),
        "Mean actual outages per division-day": round(float(test["events"].mean()), 2),
        "Correlation, daily totals (actual vs predicted)": round(float(daily["events"].corr(daily["predicted_events"])), 3),
        "Worst 5% division-days flagged High/Severe": round(float((flagged & big).sum() / big.sum()), 3),
        "High/Severe flags that were real bad days": round(float((flagged & big).sum() / max(flagged.sum(), 1)), 3),
        "Major event days in held-out period": int(test.loc[test["is_major_event_day"] == 1, "date"].nunique()),
    }

    # --- production model on all 24 months
    m = fit(df)
    coef = pd.DataFrame({"term": m.params.index, "coefficient": m.params.values,
                         "std_error": m.bse.values, "p_value": m.pvalues.values})
    calm_all = df.drop_duplicates("division").assign(max_gust_mph=20.0, precip_in=0.0, cdd=0.0, hdd=5.0, month=10)
    calm_all["baseline_outages"] = predict(m, calm_all)
    divs = geo.merge(calm_all[["division", "baseline_outages"]], on="division")
    metrics["Deviance explained (pseudo R-squared)"] = round(float(1 - m.deviance / m.null_deviance), 3)
    metrics["Training days"] = int(df["date"].nunique())

    backtest = test[["date", "division", "events", "predicted_events", "customers_out", "predicted_customers_out",
                     "max_gust_mph", "precip_in", "risk_ratio"]].rename(
        columns={"events": "actual_outages", "predicted_events": "predicted_outages",
                 "customers_out": "actual_customers_out"})
    backtest["date"] = backtest["date"].dt.date
    risk = pd.DataFrame(RISK, columns=["ratio_min", "ratio_max", "risk_level", "risk_order"])

    OUT.mkdir(exist_ok=True)
    coef.to_parquet(OUT / "outage_model_coefficients.parquet", index=False)
    bands.to_parquet(OUT / "outage_model_bands.parquet", index=False)
    divs.to_parquet(OUT / "outage_model_divisions.parquet", index=False)
    backtest.round(3).to_parquet(OUT / "outage_model_backtest.parquet", index=False)
    pd.DataFrame({"metric": list(metrics), "value": [str(v) for v in metrics.values()],
                  "metric_order": range(1, len(metrics) + 1)}).to_parquet(OUT / "outage_model_metrics.parquet", index=False)
    risk.to_parquet(OUT / "outage_model_risk_levels.parquet", index=False)

    print(m.summary().tables[1])
    print("\nwind bands:\n", bands.to_string(index=False))
    print("\ndivisions:\n", divs.round(4).to_string(index=False))
    print("\nback-test:"); [print(f"  {k}: {v}") for k, v in metrics.items()]
    return m, divs, cpe


def forecast_check(m, divs, cpe):
    """Score today's live Open-Meteo forecast in Python, to cross-check the Power BI page."""
    q = ("https://api.open-meteo.com/v1/forecast?daily=wind_gusts_10m_max,precipitation_sum,"
         "temperature_2m_max,temperature_2m_min&wind_speed_unit=mph&precipitation_unit=inch"
         "&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days=16"
         f"&latitude={','.join(f'{x:.4f}' for x in divs['latitude'])}"
         f"&longitude={','.join(f'{x:.4f}' for x in divs['longitude'])}")
    resp = json.load(urllib.request.urlopen(q, timeout=30))
    rows = []
    for (_, d), r in zip(divs.iterrows(), resp):
        dd = r["daily"]
        for i, day in enumerate(dd["time"]):
            tavg = (dd["temperature_2m_max"][i] + dd["temperature_2m_min"][i]) / 2
            rows.append({"division": d["division"], "date": day, "customers_served": d["customers_served"],
                         "max_gust_mph": dd["wind_gusts_10m_max"][i], "precip_in": dd["precipitation_sum"][i] or 0.0,
                         "cdd": max(tavg - 65, 0), "hdd": max(65 - tavg, 0), "month": int(day[5:7]),
                         "baseline": d["baseline_outages"]})
    f = pd.DataFrame(rows)
    f["predicted_outages"] = predict(m, f)
    f["predicted_customers_out"] = f["predicted_outages"] * f["max_gust_mph"].map(band_of).map(cpe)
    f["risk_ratio"] = f["predicted_outages"] / f["baseline"]
    print("\nlive forecast check (first 7 days, all divisions):")
    s = f[f["date"] <= sorted(f["date"].unique())[6]]
    print(f"  predicted outages: {s['predicted_outages'].sum():.1f}   predicted customers out: {s['predicted_customers_out'].sum():,.0f}"
          f"   max gust: {s['max_gust_mph'].max():.1f} mph   peak risk ratio: {s['risk_ratio'].max():.2f}")
    print(f.groupby("date")[["max_gust_mph", "predicted_outages"]].agg({"max_gust_mph": "max", "predicted_outages": "sum"}).round(2).head(7).to_string())


if __name__ == "__main__":
    forecast_check(*main())
