/* =============================================================================
   04_dw_ops.sql : warehouse layer, part 3 - reliability, AMI and operational facts
============================================================================= */
SET NOCOUNT ON;
GO

/* ---------------------------------------------------------------- outages + IEEE 1366 major event days
   Daily SAIDI = customer-minutes interrupted / customers served.
   T_MED = exp(alpha + 2.5 * beta), alpha/beta = mean/stdev of ln(daily SAIDI) over
   non-zero days. IEEE uses the prior 5 years; the demo has 2, so it uses the whole window. */
DECLARE @served FLOAT = (SELECT SUM(customers_served) FROM dw.dim_geography);
WITH daily AS (
    SELECT outage_date, SUM(CAST(customer_minutes AS FLOAT)) / @served AS saidi
    FROM stg.outage WHERE customer_minutes > 0 GROUP BY outage_date
), stats AS (SELECT AVG(LOG(saidi)) AS alpha, STDEV(LOG(saidi)) AS beta FROM daily)
UPDATE d SET daily_saidi_min = x.saidi,
             is_major_event_day = IIF(x.saidi > EXP(s.alpha + 2.5 * s.beta), 1, 0)
FROM dw.dim_date d
JOIN daily x ON x.outage_date = d.[date]
CROSS JOIN stats s;

DROP TABLE IF EXISTS dw.fact_outage;
SELECT o.*, d.is_major_event_day
INTO dw.fact_outage
FROM stg.outage o JOIN dw.dim_date d ON d.[date] = o.outage_date;
ALTER TABLE dw.fact_outage ALTER COLUMN outage_id BIGINT NOT NULL;
ALTER TABLE dw.fact_outage ADD CONSTRAINT PK_fact_outage PRIMARY KEY (outage_id);
GO

/* ---------------------------------------------------------------- AMI daily reads */
DROP TABLE IF EXISTS #meter_avg;
SELECT meter_id, AVG(kwh) AS avg_kwh INTO #meter_avg FROM stg.ami_read GROUP BY meter_id;

DROP TABLE IF EXISTS dw.fact_ami_daily;
SELECT r.meter_id, p.premise_id, r.read_date, r.kwh, r.peak_kw, r.read_quality, r.is_missing,
       CAST(IIF(r.kwh > 15 * m.avg_kwh, 1, 0) AS BIT) AS is_spike
INTO dw.fact_ami_daily
FROM stg.ami_read r
JOIN #meter_avg m    ON m.meter_id = r.meter_id
JOIN dw.dim_premise p ON p.elec_meter_id = r.meter_id;
CREATE CLUSTERED COLUMNSTORE INDEX CCI_fact_ami_daily ON dw.fact_ami_daily;
DROP TABLE #meter_avg;
GO

/* ---------------------------------------------------------------- operational facts */
DECLARE @as_of DATE = (SELECT CAST(value AS DATE) FROM etl.config WHERE setting = 'as_of_date');

DROP TABLE IF EXISTS dw.fact_work_order;
SELECT w.*,
       IIF(w.is_open = 1, DATEDIFF(DAY, w.created_date, @as_of), NULL)                AS open_age_days,
       DATEDIFF(DAY, w.planned_finish, w.actual_finish)                               AS finish_variance_days,
       CAST(IIF(w.actual_finish <= w.planned_finish, 1, 0) AS BIT)                    AS finished_on_time,
       CAST(IIF(w.is_open = 1 AND w.planned_finish < @as_of, 1, 0) AS BIT)            AS is_overdue
INTO dw.fact_work_order
FROM stg.work_order w;
CREATE CLUSTERED COLUMNSTORE INDEX CCI_fact_work_order ON dw.fact_work_order;

DROP TABLE IF EXISTS dw.fact_service_order;
SELECT s.*, p.division, p.city,
       IIF(s.status = 'Open', DATEDIFF(DAY, s.created_date, @as_of), NULL) AS open_age_days
INTO dw.fact_service_order
FROM stg.service_order s
JOIN dw.dim_premise p ON p.premise_id = s.premise_id;
CREATE CLUSTERED COLUMNSTORE INDEX CCI_fact_service_order ON dw.fact_service_order;

DROP TABLE IF EXISTS dw.fact_interaction;
SELECT * INTO dw.fact_interaction FROM stg.interaction;
CREATE CLUSTERED COLUMNSTORE INDEX CCI_fact_interaction ON dw.fact_interaction;

DROP TABLE IF EXISTS dw.fact_payment_plan;
SELECT pp.*, c.customer_id, c.segment, c.division, c.is_low_income
INTO dw.fact_payment_plan
FROM stg.payment_plan pp
JOIN dw.dim_customer c ON c.account_id = pp.account_id;
GO

