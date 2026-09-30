/* =============================================================================
   06_rpt.sql : report layer (views at the grain each Power BI page needs)

   One section per report in docs/reports.md. These views are what gets exported
   to GitHub powerbi_data/ (the SharePoint replacement), so they are aggregated
   wherever the report does not need row-level detail.
============================================================================= */
IF SCHEMA_ID('rpt') IS NULL EXEC('CREATE SCHEMA rpt');
GO

/* 1. Revenue & Billing Summary ------------------------------------------------
   Split into two tables so commercial/industrial load (a few thousand very large
   accounts) doesn't flatten the residential trend. Both share period_key (YYYYMM)
   and division, and rpt.dim_period is the shared key table that lets Power BI plot
   them side by side, e.g. residential on the primary axis, C&I on the secondary. */
DROP VIEW IF EXISTS rpt.revenue_monthly;
GO

CREATE OR ALTER VIEW rpt.dim_period AS
SELECT DISTINCT
       YEAR(month_start) * 100 + MONTH(month_start) AS period_key,
       month_start, month_end, [year], [quarter], month_num, month_name, month_short, year_month
FROM dw.dim_date
WHERE is_in_window = 1;
GO

CREATE OR ALTER VIEW rpt.date_daily AS                       -- Power BI date table (daily facts)
SELECT [date], YEAR(month_start) * 100 + MONTH(month_start) AS period_key, month_start, year_month,
       [year], month_short, day_of_week, day_name, is_weekend, is_winter_moratorium, is_major_event_day,
       daily_saidi_min, temp_avg_f, temp_max_f, temp_min_f, precip_in, max_gust_mph,
       heating_degree_days, cooling_degree_days
FROM dw.dim_date
WHERE is_in_window = 1;
GO

CREATE OR ALTER VIEW rpt.division AS                         -- shared division dimension
SELECT division,
       SUM(customers_served)     AS customers_served,
       SUM(gas_customers_served) AS gas_customers_served,
       COUNT(*)                  AS towns
FROM dw.dim_geography
GROUP BY division;
GO

CREATE OR ALTER VIEW dw.v_revenue_monthly AS
SELECT YEAR(b.bill_month) * 100 + MONTH(b.bill_month)  AS period_key,
       b.bill_month,
       CASE WHEN c.segment_code IN ('RES', 'GOV') THEN 'Residential & Non-Commercial'
            ELSE 'Commercial & Industrial' END           AS customer_class,
       c.division, c.segment, b.elec_rate_code,
       ISNULL(b.gas_rate_code, 'None')                   AS gas_rate_code,
       b.elec_supplier,
       COUNT_BIG(*)                                      AS bills,
       COUNT(DISTINCT b.customer_id)                     AS customers_billed,
       SUM(b.kwh)                                        AS kwh,
       SUM(b.therms)                                     AS therms,
       SUM(b.elec_delivery_amt)                          AS elec_delivery_amt,
       SUM(b.elec_supply_amt)                            AS elec_supply_amt,
       SUM(b.gas_delivery_amt)                           AS gas_delivery_amt,
       SUM(b.gas_supply_amt)                             AS gas_supply_amt,
       SUM(b.tax_amt)                                    AS tax_amt,
       SUM(b.total_amt)                                  AS total_amt
FROM dw.fact_bill b
JOIN dw.dim_customer c ON c.customer_id = b.customer_id
WHERE b.is_reversed = 0
GROUP BY b.bill_month, c.segment_code, c.division, c.segment, b.elec_rate_code,
         ISNULL(b.gas_rate_code, 'None'), b.elec_supplier;
GO

CREATE OR ALTER VIEW rpt.revenue_residential_monthly AS      -- Residential + Government
SELECT * FROM dw.v_revenue_monthly WHERE customer_class = 'Residential & Non-Commercial';
GO

CREATE OR ALTER VIEW rpt.revenue_commercial_monthly AS       -- Small/Medium Business + C&I
SELECT * FROM dw.v_revenue_monthly WHERE customer_class = 'Commercial & Industrial';
GO

/* 2. Accounts Receivable Aging ------------------------------------------------ */
CREATE OR ALTER VIEW rpt.ar_aging_monthly AS
SELECT month_end, segment, division, aging_bucket, bucket_order, is_receivable, open_amt, open_bills, open_accounts
FROM dw.fact_ar_monthly;
GO

CREATE OR ALTER VIEW rpt.ar_top_accounts AS
SELECT TOP (2000)
       c.customer_id, c.account_id, c.customer_name, c.segment, c.division, c.city,
       c.is_low_income, c.has_medical_cert, c.is_active,
       a.open_amt, a.past_due_amt, a.past_due_1_30, a.past_due_31_60, a.past_due_61_90, a.past_due_91_180,
       a.written_off_amt,
       a.oldest_days_past_due, a.open_bills
FROM dw.fact_ar_customer a
JOIN dw.dim_customer c ON c.customer_id = a.customer_id
ORDER BY a.past_due_amt DESC;
GO

/* 3. Payments & Digital Adoption ---------------------------------------------- */
CREATE OR ALTER VIEW rpt.payments_monthly AS
SELECT p.payment_month, p.channel, c.segment, c.division,
       COUNT_BIG(*) AS payments, SUM(p.amount) AS amount
FROM dw.fact_payment p
JOIN dw.dim_customer c ON c.customer_id = p.customer_id
GROUP BY p.payment_month, p.channel, c.segment, c.division;
GO

CREATE OR ALTER VIEW rpt.digital_adoption_monthly AS
SELECT d.month_end, c.segment, c.division,
       COUNT(*)                                                                   AS active_customers,
       SUM(IIF(c.is_autopay = 1 AND c.autopay_date <= d.month_end, 1, 0))          AS autopay_customers,
       SUM(IIF(c.is_paperless = 1 AND c.paperless_date <= d.month_end, 1, 0))      AS paperless_customers
FROM (SELECT DISTINCT month_end FROM dw.dim_date WHERE is_in_window = 1) d
JOIN dw.dim_customer c
  ON c.move_in_date <= d.month_end AND (c.move_out_date IS NULL OR c.move_out_date > d.month_end)
GROUP BY d.month_end, c.segment, c.division;
GO

/* 4. Usage & AMI Load Profile -------------------------------------------------- */
CREATE OR ALTER VIEW rpt.ami_daily_profile AS
SELECT a.read_date, p.division, p.elec_rate_code,
       COUNT(*)                                            AS meters_expected,
       SUM(IIF(a.is_missing = 0, 1, 0))                    AS meters_reporting,
       SUM(IIF(a.read_quality = 'ESTIMATED', 1, 0))        AS reads_estimated,
       SUM(IIF(a.is_spike = 1, 1, 0))                      AS reads_spike,
       SUM(IIF(a.is_missing = 0 AND a.is_spike = 0, a.kwh, 0)) AS kwh_valid,
       SUM(IIF(a.is_missing = 0 AND a.is_spike = 0, 1, 0))     AS reads_valid,
       MAX(IIF(a.is_spike = 0, a.peak_kw, NULL))           AS max_peak_kw
FROM dw.fact_ami_daily a
JOIN dw.dim_premise p ON p.premise_id = a.premise_id
GROUP BY a.read_date, p.division, p.elec_rate_code;
GO

/* 5. Outage & Reliability ------------------------------------------------------ */
CREATE OR ALTER VIEW rpt.reliability_events AS
SELECT o.outage_id, o.outage_start, o.restore_time, o.outage_date, o.duration_min, o.is_open,
       o.division, o.municipality, o.circuit_id, o.device_type, o.cause,
       o.customers_out, o.customer_minutes, o.is_major_event_day
FROM dw.fact_outage o;
GO

/* 6. Work Order Backlog -------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.work_orders_monthly AS
SELECT DATEFROMPARTS(YEAR(created_date), MONTH(created_date), 1) AS created_month,
       order_type, commodity, asset_class, division, priority, status,
       COUNT_BIG(*)                                AS work_orders,
       SUM(CAST(finished_on_time AS INT))          AS finished_on_time,
       SUM(IIF(actual_finish IS NOT NULL, 1, 0))   AS finished,
       SUM(planned_cost)                           AS planned_cost,
       SUM(actual_cost)                            AS actual_cost
FROM dw.fact_work_order
WHERE status <> 'Deleted'
GROUP BY DATEFROMPARTS(YEAR(created_date), MONTH(created_date), 1),
         order_type, commodity, asset_class, division, priority, status;
GO

CREATE OR ALTER VIEW rpt.work_order_backlog AS
SELECT work_order_id, order_type, description, asset_class, commodity, functional_location, work_center,
       priority, created_date, planned_start, planned_finish, status, planned_cost, actual_cost,
       division, municipality, open_age_days, is_overdue
FROM dw.fact_work_order
WHERE is_open = 1;
GO

/* 7. Contact Center Performance ------------------------------------------------ */
CREATE OR ALTER VIEW rpt.contact_center_daily AS
SELECT interaction_date, channel, reason, site, is_assisted,
       COUNT_BIG(*)                                        AS contacts,
       SUM(CAST(is_identified AS INT))                     AS identified_contacts,
       SUM(CAST(wait_sec AS BIGINT))                       AS wait_sec_total,
       SUM(IIF(channel = 'Phone - Agent' AND wait_sec <= 30, 1, 0)) AS answered_within_30s,
       SUM(IIF(channel = 'Phone - Agent', 1, 0))           AS agent_calls,
       SUM(CAST(handle_sec AS BIGINT))                     AS handle_sec_total,
       SUM(CAST(first_contact_resolved AS INT))            AS resolved_first_contact,
       SUM(CAST(transferred AS INT))                       AS transferred,
       COUNT(csat)                                         AS csat_responses,
       SUM(CAST(csat AS INT))                              AS csat_total,
       SUM(IIF(csat >= 4, 1, 0))                           AS csat_satisfied
FROM dw.fact_interaction
GROUP BY interaction_date, channel, reason, site, is_assisted;
GO

/* 8. Credit & Collections Actions ---------------------------------------------- */
CREATE OR ALTER VIEW rpt.collections_orders_monthly AS
SELECT DATEFROMPARTS(YEAR(s.created_date), MONTH(s.created_date), 1) AS created_month,
       s.order_type, s.status, s.division, c.segment, c.is_low_income,
       COUNT_BIG(*)                                        AS orders,
       SUM(IIF(s.cycle_days <= 1, 1, 0))                   AS completed_within_1_day,
       AVG(CAST(s.cycle_days AS FLOAT))                    AS avg_cycle_days
FROM dw.fact_service_order s
JOIN dw.dim_customer c ON c.account_id = s.account_id
WHERE s.order_type_code IN ('DISC_NP', 'RECONNECT')
GROUP BY DATEFROMPARTS(YEAR(s.created_date), MONTH(s.created_date), 1),
         s.order_type, s.status, s.division, c.segment, c.is_low_income;
GO

CREATE OR ALTER VIEW rpt.moratorium_compliance AS
SELECT d.is_winter_moratorium, c.segment, c.has_medical_cert,
       COUNT_BIG(*) AS disconnects_completed
FROM dw.fact_service_order s
JOIN dw.dim_customer c ON c.account_id = s.account_id
JOIN dw.dim_date d     ON d.[date] = s.completed_date
WHERE s.order_type_code = 'DISC_NP' AND s.status = 'Completed'
GROUP BY d.is_winter_moratorium, c.segment, c.has_medical_cert;
GO

CREATE OR ALTER VIEW rpt.payment_plans_monthly AS
SELECT DATEFROMPARTS(YEAR(created_date), MONTH(created_date), 1) AS created_month,
       plan_type, status, segment, division, is_low_income,
       COUNT_BIG(*) AS plans, SUM(total_amt) AS total_amt, SUM(down_payment) AS down_payment,
       AVG(CAST(num_installments AS FLOAT)) AS avg_installments
FROM dw.fact_payment_plan
GROUP BY DATEFROMPARTS(YEAR(created_date), MONTH(created_date), 1), plan_type, status, segment, division, is_low_income;
GO

/* 9. Meter-to-Cash Exceptions -------------------------------------------------- */
CREATE OR ALTER VIEW rpt.billing_exceptions_monthly AS
WITH b AS (
    SELECT fb.bill_month, fb.customer_id, fb.elec_rate_code, fb.bill_type, fb.billing_days,
           fb.kwh, fb.elec_estimated, fb.gas_estimated, fb.gas_rate_code,
           LAG(fb.kwh)            OVER (PARTITION BY fb.customer_id ORDER BY fb.bill_date) AS prior_kwh,
           LAG(fb.elec_estimated) OVER (PARTITION BY fb.customer_id ORDER BY fb.bill_date) AS prior_estimated
    FROM dw.fact_bill fb
    WHERE fb.is_reversed = 0 AND fb.bill_type <> 'Rebill'
)
SELECT b.bill_month, c.division, c.segment, b.elec_rate_code,
       COUNT_BIG(*)                                                              AS bills,
       SUM(CAST(b.elec_estimated AS INT))                                        AS elec_estimated,
       SUM(CAST(b.gas_estimated AS INT))                                         AS gas_estimated,
       SUM(IIF(b.elec_estimated = 1 AND b.prior_estimated = 1, 1, 0))            AS consecutive_estimates,
       SUM(IIF(b.kwh = 0 AND b.bill_type = 'Regular' AND b.billing_days >= 20, 1, 0)) AS zero_usage_bills,
       SUM(IIF(b.prior_kwh > 0 AND ABS(b.kwh - b.prior_kwh) / b.prior_kwh > 0.5, 1, 0)) AS high_variance_bills
FROM b
JOIN dw.dim_customer c ON c.customer_id = b.customer_id
GROUP BY b.bill_month, c.division, c.segment, b.elec_rate_code;
GO

CREATE OR ALTER VIEW rpt.rebills_monthly AS
SELECT DATEFROMPARTS(YEAR(reversal_date), MONTH(reversal_date), 1) AS reversal_month,
       COUNT_BIG(*) AS reversed_bills, SUM(total_amt) AS reversed_amt
FROM dw.fact_bill
WHERE is_reversed = 1
GROUP BY DATEFROMPARTS(YEAR(reversal_date), MONTH(reversal_date), 1);
GO

/* 10. Service Order Activity --------------------------------------------------- */
CREATE OR ALTER VIEW rpt.service_orders_monthly AS
SELECT DATEFROMPARTS(YEAR(created_date), MONTH(created_date), 1) AS created_month,
       order_type, status, division, work_center,
       COUNT_BIG(*)                               AS orders,
       SUM(IIF(status = 'Completed', 1, 0))       AS completed,
       SUM(IIF(status = 'Completed', cycle_days, 0)) AS cycle_days_total,
       SUM(IIF(status = 'Open', 1, 0))            AS open_orders,
       MAX(open_age_days)                         AS max_open_age_days
FROM dw.fact_service_order
GROUP BY DATEFROMPARTS(YEAR(created_date), MONTH(created_date), 1), order_type, status, division, work_center;
GO

/* Customer 360 (drill-through) --------------------------------------------------- */
CREATE OR ALTER VIEW rpt.customer_360 AS
SELECT c.customer_id, c.account_id, c.customer_name, c.customer_type, c.segment, c.division, c.city,
       p.address_line, p.zip, p.latitude, p.longitude, p.premise_type, p.elec_rate_code, p.gas_rate_code,
       p.elec_meter_technology, c.elec_supplier,
       c.move_in_date, c.move_out_date, c.is_active, c.tenure_years,
       c.has_email, c.has_phone, c.is_autopay, c.is_paperless, c.is_budget_billing,
       c.is_low_income, c.has_medical_cert,
       ISNULL(a.open_amt, 0)        AS open_amt,
       ISNULL(a.past_due_amt, 0)    AS past_due_amt,
       ISNULL(a.written_off_amt, 0) AS written_off_amt,
       a.oldest_days_past_due
FROM dw.dim_customer c
JOIN dw.dim_premise p          ON p.premise_id = c.premise_id
LEFT JOIN dw.fact_ar_customer a ON a.customer_id = c.customer_id;
GO

CREATE OR ALTER VIEW rpt.customer_bills_recent AS          -- last 3 months of bills for drill-through
SELECT b.bill_id, b.customer_id, b.bill_date, b.due_date, b.period_start, b.period_end, b.bill_type,
       b.is_reversed, b.kwh, b.therms, b.elec_estimated, b.gas_estimated, b.total_amt,
       ISNULL(p.paid_amt, 0) AS paid_amt
FROM dw.fact_bill b
LEFT JOIN (SELECT bill_id, SUM(amount) AS paid_amt FROM dw.fact_payment GROUP BY bill_id) p ON p.bill_id = b.bill_id
WHERE b.bill_date > DATEADD(MONTH, -3, (SELECT CAST(value AS DATE) FROM etl.config WHERE setting = 'as_of_date'));
GO

/* Data-quality page ------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.data_quality AS
SELECT check_order, source_table, dq_check, action_taken, rows_checked, rows_affected,
       CAST(rows_affected * 100.0 / NULLIF(rows_checked, 0) AS DECIMAL(6,2)) AS pct_affected
FROM dw.dq_summary;
GO
