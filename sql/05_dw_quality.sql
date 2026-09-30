/* =============================================================================
   05_dw_quality.sql : warehouse layer, part 4 - data-quality audit (dw.dq_summary)
============================================================================= */
SET NOCOUNT ON;
GO

/* ---------------------------------------------------------------- data-quality audit */
DROP TABLE IF EXISTS dw.dq_summary;
CREATE TABLE dw.dq_summary (
    check_order  INT          NOT NULL,
    source_table VARCHAR(40)  NOT NULL,
    dq_check     VARCHAR(80)  NOT NULL,
    action_taken VARCHAR(80)  NOT NULL,
    rows_checked BIGINT       NOT NULL,
    rows_affected BIGINT      NOT NULL);

INSERT dw.dq_summary
SELECT 1, 'sap_isu.ADRC', 'ZIP code missing leading zero', 'Left-padded to 5 digits',
       COUNT(*), SUM(CAST(dq_zip_fixed AS INT)) FROM dw.dim_premise
UNION ALL
SELECT 2, 'sap_isu.ADRC', 'City name upper-case or trailing spaces', 'Replaced with canonical city from ZIP',
       COUNT(*), SUM(CAST(dq_city_fixed AS INT)) FROM dw.dim_premise
UNION ALL
SELECT 3, 'sap_isu.BUT000', 'Last name in all caps', 'Converted to proper case',
       COUNT(*), SUM(CAST(dq_name_fixed AS INT)) FROM dw.dim_customer
UNION ALL
SELECT 4, 'sap_isu.BUT000', 'Phone number in non-standard format', 'Normalized to 10 digits',
       COUNT(*), SUM(CAST(dq_phone_reformatted AS INT)) FROM dw.dim_customer
UNION ALL
SELECT 5, 'sap_isu.BUT000', 'Missing email address', 'Kept; reported as contactability gap',
       COUNT(*), SUM(1 - CAST(has_email AS INT)) FROM dw.dim_customer
UNION ALL
SELECT 6, 'sap_isu.DFKKZP', 'Duplicate payment rows from re-extract', 'Removed (kept first per PAYMENT_ID)',
       (SELECT COUNT_BIG(*) FROM sap_isu.DFKKZP),
       (SELECT COUNT_BIG(*) FROM sap_isu.DFKKZP) - (SELECT COUNT_BIG(*) FROM dw.fact_payment)
UNION ALL
SELECT 7, 'sap_isu.ERCH', 'Reversed billing documents', 'Excluded from revenue and AR (rebill carries amount)',
       COUNT_BIG(*), SUM(CAST(is_reversed AS INT)) FROM dw.fact_bill
UNION ALL
SELECT 8, 'ami.daily_reads', 'Missing interval reads', 'Flagged; excluded from usage averages',
       COUNT_BIG(*), SUM(CAST(is_missing AS INT)) FROM dw.fact_ami_daily
UNION ALL
SELECT 9, 'ami.daily_reads', 'Usage spike > 15x meter average', 'Flagged as outlier',
       COUNT_BIG(*), SUM(CAST(is_spike AS INT)) FROM dw.fact_ami_daily
UNION ALL
SELECT 10, 'crm.interactions', 'Contact not linked to a customer', 'Kept; reported as unidentified',
       COUNT_BIG(*), SUM(1 - CAST(is_identified AS INT)) FROM dw.fact_interaction
UNION ALL
SELECT 11, 'oms.outage_events', 'Outage still open at as-of date', 'Excluded from closed-event durations',
       COUNT(*), SUM(CAST(is_open AS INT)) FROM dw.fact_outage;
GO
