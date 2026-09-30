/* =============================================================================
   03_dw_ar.sql : warehouse layer, part 2 - AR aging ledger (month-end snapshots)
============================================================================= */
SET NOCOUNT ON;
GO

/* ---------------------------------------------------------------- AR aging, rebuilt at every month end
   A bill is open at month-end ME if it was billed on/before ME and its cumulative
   payments through ME are less than the bill total. Reversed bills are excluded
   (their rebills carry the balance). Aging bucket = days past due at ME.
   Write-off policy: a balance more than 180 days past due is charged off to bad
   debt. It leaves receivables (is_receivable = 0) and shows in the 'Written Off'
   bucket as the cumulative amount charged off to date; later recoveries still
   reduce it because payments keep applying to the bill.                         */
DECLARE @as_of DATE = (SELECT CAST(value AS DATE) FROM etl.config WHERE setting = 'as_of_date');

DROP TABLE IF EXISTS #pay_cum;
SELECT bill_id, me, SUM(amt) OVER (PARTITION BY bill_id ORDER BY me ROWS UNBOUNDED PRECEDING) AS cum_paid
INTO #pay_cum
FROM (SELECT bill_id, EOMONTH(payment_date) AS me, SUM(amount) AS amt
      FROM dw.fact_payment GROUP BY bill_id, EOMONTH(payment_date)) x;
CREATE CLUSTERED INDEX IX_pay_cum ON #pay_cum (bill_id, me);

DROP TABLE IF EXISTS #bill;
SELECT b.bill_id, b.customer_id, b.bill_date, b.due_date, b.total_amt, fp.paid_me
INTO #bill
FROM dw.fact_bill b
LEFT JOIN (SELECT pc.bill_id, MIN(pc.me) AS paid_me
           FROM #pay_cum pc JOIN dw.fact_bill fb ON fb.bill_id = pc.bill_id
           WHERE pc.cum_paid >= fb.total_amt - 0.005
           GROUP BY pc.bill_id) fp ON fp.bill_id = b.bill_id
WHERE b.is_reversed = 0 AND b.total_amt > 0;

DROP TABLE IF EXISTS #me;
SELECT DISTINCT month_end AS me INTO #me FROM dw.dim_date WHERE is_in_window = 1;

DROP TABLE IF EXISTS #open;
SELECT m.me, b.bill_id, b.customer_id, b.due_date,
       b.total_amt - ISNULL(pc.cum_paid, 0) AS open_amt,
       DATEDIFF(DAY, b.due_date, m.me)      AS days_past_due
INTO #open
FROM #bill b
JOIN #me m ON m.me >= EOMONTH(b.bill_date) AND m.me < ISNULL(b.paid_me, '9999-12-31')
OUTER APPLY (SELECT TOP (1) p.cum_paid FROM #pay_cum p
             WHERE p.bill_id = b.bill_id AND p.me <= m.me ORDER BY p.me DESC) pc;

DROP TABLE IF EXISTS dw.fact_ar_monthly;
SELECT o.me AS month_end, c.segment, c.division, a.aging_bucket, a.bucket_order,
       CAST(IIF(a.bucket_order < 9, 1, 0) AS BIT) AS is_receivable,
       SUM(o.open_amt) AS open_amt, COUNT(*) AS open_bills, COUNT(DISTINCT o.customer_id) AS open_accounts
INTO dw.fact_ar_monthly
FROM #open o
JOIN dw.dim_customer c ON c.customer_id = o.customer_id
CROSS APPLY (SELECT CASE WHEN o.days_past_due > 180 THEN 'Written Off'
                         WHEN o.days_past_due <= 0 THEN 'Current'
                         WHEN o.days_past_due <= 30 THEN '1-30'
                         WHEN o.days_past_due <= 60 THEN '31-60'
                         WHEN o.days_past_due <= 90 THEN '61-90' ELSE '91-180' END AS aging_bucket,
                    CASE WHEN o.days_past_due > 180 THEN 9
                         WHEN o.days_past_due <= 0 THEN 1 WHEN o.days_past_due <= 30 THEN 2
                         WHEN o.days_past_due <= 60 THEN 3 WHEN o.days_past_due <= 90 THEN 4 ELSE 5 END AS bucket_order) a
GROUP BY o.me, c.segment, c.division, a.aging_bucket, a.bucket_order;

DROP TABLE IF EXISTS dw.fact_ar_customer;
SELECT o.customer_id,
       SUM(IIF(o.days_past_due <= 180, o.open_amt, 0))                  AS open_amt,
       SUM(IIF(o.days_past_due BETWEEN 1 AND 180, o.open_amt, 0))       AS past_due_amt,
       SUM(IIF(o.days_past_due <= 0, o.open_amt, 0))                    AS current_amt,
       SUM(IIF(o.days_past_due BETWEEN 1 AND 30, o.open_amt, 0))        AS past_due_1_30,
       SUM(IIF(o.days_past_due BETWEEN 31 AND 60, o.open_amt, 0))       AS past_due_31_60,
       SUM(IIF(o.days_past_due BETWEEN 61 AND 90, o.open_amt, 0))       AS past_due_61_90,
       SUM(IIF(o.days_past_due BETWEEN 91 AND 180, o.open_amt, 0))      AS past_due_91_180,
       SUM(IIF(o.days_past_due > 180, o.open_amt, 0))                   AS written_off_amt,
       MAX(IIF(o.days_past_due <= 180, o.days_past_due, NULL))          AS oldest_days_past_due,
       SUM(IIF(o.days_past_due <= 180, 1, 0))                           AS open_bills
INTO dw.fact_ar_customer
FROM #open o
WHERE o.me = @as_of
GROUP BY o.customer_id;
ALTER TABLE dw.fact_ar_customer ALTER COLUMN customer_id BIGINT NOT NULL;
ALTER TABLE dw.fact_ar_customer ADD CONSTRAINT PK_fact_ar_customer PRIMARY KEY (customer_id);
DROP TABLE #pay_cum, #bill, #me, #open;
GO

