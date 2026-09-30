/* =============================================================================
   02_dw_core.sql : warehouse layer, part 1 - dimensions + bill/payment facts

   Dimensions : dim_date (with weather + winter moratorium + major event days),
                dim_geography, dim_rate, dim_premise, dim_customer
   Facts      : fact_bill, fact_payment, fact_ar_monthly, fact_ar_customer,
                fact_ami_daily, fact_outage, fact_work_order, fact_service_order,
                fact_interaction, fact_payment_plan
   Audit      : dw.dq_summary (what the staging layer fixed)

   Re-runnable: every table is dropped and rebuilt. Large facts use clustered
   columnstore indexes. As-of date lives in etl.config.
============================================================================= */
SET NOCOUNT ON;
IF SCHEMA_ID('dw') IS NULL EXEC('CREATE SCHEMA dw');
GO

DROP TABLE IF EXISTS etl.config;
CREATE TABLE etl.config (setting SYSNAME PRIMARY KEY, value NVARCHAR(100) NOT NULL);
INSERT etl.config VALUES ('as_of_date', '2026-09-30'), ('window_start', '2024-10-01');
GO

/* ---------------------------------------------------------------- dim_date */
DROP TABLE IF EXISTS dw.dim_date;
DECLARE @start DATE = '2014-01-01', @end DATE = '2026-12-31';
WITH n AS (
    SELECT TOP (DATEDIFF(DAY, @start, @end) + 1) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS i
    FROM sys.all_objects a CROSS JOIN sys.all_objects b
), d AS (SELECT DATEADD(DAY, i, @start) AS [date] FROM n)
SELECT
    d.[date],
    YEAR(d.[date])                                   AS [year],
    DATEPART(QUARTER, d.[date])                      AS [quarter],
    MONTH(d.[date])                                  AS month_num,
    DATENAME(MONTH, d.[date])                        AS month_name,
    LEFT(DATENAME(MONTH, d.[date]), 3)               AS month_short,
    CONVERT(CHAR(7), d.[date], 126)                  AS year_month,
    DATEFROMPARTS(YEAR(d.[date]), MONTH(d.[date]), 1) AS month_start,
    EOMONTH(d.[date])                                AS month_end,
    DATEDIFF(DAY, '19000101', d.[date]) % 7 + 1      AS day_of_week,      -- 1 = Monday
    DATENAME(WEEKDAY, d.[date])                      AS day_name,
    CAST(IIF(DATEDIFF(DAY, '19000101', d.[date]) % 7 >= 5, 1, 0) AS BIT) AS is_weekend,
    CAST(IIF((MONTH(d.[date]) = 11 AND DAY(d.[date]) >= 15) OR MONTH(d.[date]) IN (12, 1, 2)
             OR (MONTH(d.[date]) = 3 AND DAY(d.[date]) <= 15), 1, 0) AS BIT) AS is_winter_moratorium,
    CAST(IIF(d.[date] BETWEEN '2024-10-01' AND '2026-09-30', 1, 0) AS BIT) AS is_in_window,
    w.temp_avg_f, w.temp_max_f, w.temp_min_f, w.precip_in, w.max_gust_mph,
    w.heating_degree_days, w.cooling_degree_days,
    CAST(0 AS BIT)                                   AS is_major_event_day,
    CAST(NULL AS DECIMAL(10,4))                      AS daily_saidi_min
INTO dw.dim_date
FROM d LEFT JOIN stg.weather w ON w.weather_date = d.[date];
ALTER TABLE dw.dim_date ALTER COLUMN [date] DATE NOT NULL;
ALTER TABLE dw.dim_date ADD CONSTRAINT PK_dim_date PRIMARY KEY ([date]);
GO

/* ---------------------------------------------------------------- dim_rate / dim_premise / dim_geography */
DROP TABLE IF EXISTS dw.dim_rate;
SELECT * INTO dw.dim_rate FROM stg.rate;
ALTER TABLE dw.dim_rate ALTER COLUMN rate_code VARCHAR(10) NOT NULL;
ALTER TABLE dw.dim_rate ADD CONSTRAINT PK_dim_rate PRIMARY KEY (rate_code);

DROP TABLE IF EXISTS dw.dim_premise;
SELECT p.premise_id, p.premise_type, p.address_line, p.city, p.zip, p.county, p.division, p.latitude, p.longitude,
       e.rate_code AS elec_rate_code, e.read_route, e.meter_id AS elec_meter_id,
       e.meter_manufacturer AS elec_meter_manufacturer, e.meter_technology AS elec_meter_technology,
       CAST(IIF(g.installation_id IS NULL, 0, 1) AS BIT) AS has_gas,
       g.rate_code AS gas_rate_code, g.meter_technology AS gas_meter_technology,
       p.dq_zip_fixed, p.dq_city_fixed
INTO dw.dim_premise
FROM stg.premise p
JOIN stg.installation e      ON e.premise_id = p.premise_id AND e.commodity = 'Electric'
LEFT JOIN stg.installation g ON g.premise_id = p.premise_id AND g.commodity = 'Gas';
ALTER TABLE dw.dim_premise ALTER COLUMN premise_id BIGINT NOT NULL;
ALTER TABLE dw.dim_premise ADD CONSTRAINT PK_dim_premise PRIMARY KEY (premise_id);
CREATE INDEX IX_dim_premise_meter ON dw.dim_premise (elec_meter_id);

DROP TABLE IF EXISTS dw.dim_geography;
SELECT t.CITY AS city, t.ZIP AS zip, t.COUNTY AS county, t.DIVISION AS division,
       t.LAT AS latitude, t.LON AS longitude,
       (SELECT COUNT(*) FROM dw.dim_premise p WHERE p.city = t.CITY) AS customers_served,
       (SELECT COUNT(*) FROM dw.dim_premise p WHERE p.city = t.CITY AND p.has_gas = 1) AS gas_customers_served
INTO dw.dim_geography
FROM reference.towns t;
ALTER TABLE dw.dim_geography ALTER COLUMN city VARCHAR(30) NOT NULL;
ALTER TABLE dw.dim_geography ADD CONSTRAINT PK_dim_geography PRIMARY KEY (city);
GO

/* ---------------------------------------------------------------- dim_customer (customer = contract account, 1:1) */
DROP TABLE IF EXISTS dw.dim_customer;
DECLARE @as_of DATE = (SELECT CAST(value AS DATE) FROM etl.config WHERE setting = 'as_of_date');
SELECT c.customer_id, c.account_id, c.customer_name, c.customer_type, c.segment_code, c.segment,
       c.created_date, c.email, c.phone, c.has_email, c.has_phone,
       c.is_autopay, c.autopay_date, c.is_paperless, c.paperless_date,
       c.is_budget_billing, c.is_low_income, c.has_medical_cert,
       k.premise_id, p.city, p.division,
       k.move_in_date, k.move_out_date, k.is_active, k.supplier AS elec_supplier,
       CAST(DATEDIFF(DAY, k.move_in_date, ISNULL(k.move_out_date, @as_of)) / 365.25 AS DECIMAL(5,1)) AS tenure_years,
       c.dq_name_fixed, c.dq_phone_reformatted
INTO dw.dim_customer
FROM stg.customer c
JOIN (SELECT ct.account_id, i.premise_id, ct.move_in_date, ct.move_out_date, ct.is_active, ct.supplier
      FROM stg.contract ct
      JOIN stg.installation i ON i.installation_id = ct.installation_id AND i.commodity = 'Electric') k
  ON k.account_id = c.account_id
JOIN dw.dim_premise p ON p.premise_id = k.premise_id;
ALTER TABLE dw.dim_customer ALTER COLUMN customer_id BIGINT NOT NULL;
ALTER TABLE dw.dim_customer ADD CONSTRAINT PK_dim_customer PRIMARY KEY (customer_id);
CREATE UNIQUE INDEX UX_dim_customer_account ON dw.dim_customer (account_id) INCLUDE (segment, division);
GO

/* ---------------------------------------------------------------- fact_bill / fact_payment */
DROP TABLE IF EXISTS dw.fact_bill;
SELECT b.*, DATEFROMPARTS(YEAR(b.bill_date), MONTH(b.bill_date), 1) AS bill_month
INTO dw.fact_bill
FROM stg.bill b;
CREATE CLUSTERED COLUMNSTORE INDEX CCI_fact_bill ON dw.fact_bill;

DROP TABLE IF EXISTS dw.fact_payment;
SELECT p.payment_id, c.customer_id, p.account_id, p.bill_id, p.payment_date,
       DATEFROMPARTS(YEAR(p.payment_date), MONTH(p.payment_date), 1) AS payment_month,
       p.amount, p.channel_code, p.channel
INTO dw.fact_payment
FROM stg.payment p
JOIN dw.dim_customer c ON c.account_id = p.account_id;
CREATE CLUSTERED COLUMNSTORE INDEX CCI_fact_payment ON dw.fact_payment;
GO

