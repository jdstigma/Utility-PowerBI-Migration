/* =============================================================================
   01_stg.sql : staging layer (views over the landing schemas)

   Landing schemas (sap_isu, sap_pm, ami, oms, crm, weather, reference) hold the
   curated lake data exactly as downloaded. These views apply the business rules
   and data-quality fixes. Nothing here stores data.

   DQ fixes applied (see docs/source_systems.md):
     - ZIP codes that lost their leading zero      -> left-padded to 5 digits
     - City names upper-cased / trailing spaces    -> canonical city from ZIP lookup
     - Upper-cased last names                      -> proper case
     - Phone numbers in 4 formats                  -> 10 digits
     - Duplicate payment rows (re-extract)         -> de-duplicated on PAYMENT_ID
     - SAP 'X'/blank flags, 9999-12-31 open dates  -> BIT / NULL
     - AMI missing reads and meter spikes          -> flagged, not deleted
============================================================================= */
IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg');
GO

CREATE OR ALTER VIEW stg.customer AS
SELECT
    b.PARTNER                                   AS customer_id,
    a.VKONT                                     AS account_id,
    CASE b.TYPE WHEN '1' THEN 'Person' ELSE 'Organization' END AS customer_type,
    b.BU_GROUP                                  AS segment_code,
    CASE b.BU_GROUP WHEN 'RES' THEN 'Residential' WHEN 'SMB' THEN 'Small/Medium Business'
                    WHEN 'CI' THEN 'Commercial & Industrial' WHEN 'GOV' THEN 'Government' END AS segment,
    CASE WHEN b.TYPE = '1'
         THEN b.NAME_FIRST + ' ' +
              CASE WHEN b.NAME_LAST COLLATE Latin1_General_CS_AS = UPPER(b.NAME_LAST) AND LEN(b.NAME_LAST) > 1
                   THEN UPPER(LEFT(b.NAME_LAST, 1)) + LOWER(SUBSTRING(b.NAME_LAST, 2, 100))
                   ELSE b.NAME_LAST END
         ELSE b.NAME_ORG1 END                   AS customer_name,
    b.CRDAT                                     AS created_date,
    NULLIF(b.SMTP_ADDR, '')                     AS email,
    p.phone10                                   AS phone,
    CAST(IIF(NULLIF(b.SMTP_ADDR, '') IS NULL, 0, 1) AS BIT) AS has_email,
    CAST(IIF(p.phone10 IS NULL, 0, 1) AS BIT)   AS has_phone,
    CAST(IIF(a.EZAWE = 'D', 1, 0) AS BIT)       AS is_autopay,
    a.ZAUTOPAY_DATE                             AS autopay_date,
    CAST(IIF(a.ZEBILL = 'X', 1, 0) AS BIT)      AS is_paperless,
    a.ZEBILL_DATE                               AS paperless_date,
    CAST(IIF(a.ZBUDGET_BILL = 'X', 1, 0) AS BIT) AS is_budget_billing,
    CAST(IIF(a.ZLOW_INCOME = 'X', 1, 0) AS BIT) AS is_low_income,
    CAST(IIF(a.ZMED_CERT = 'X', 1, 0) AS BIT)   AS has_medical_cert,
    CAST(IIF(b.NAME_LAST COLLATE Latin1_General_CS_AS = UPPER(b.NAME_LAST) AND LEN(b.NAME_LAST) > 1, 1, 0) AS BIT) AS dq_name_fixed,
    CAST(IIF(b.TEL_NUMBER IS NOT NULL AND b.TEL_NUMBER NOT LIKE '([0-9][0-9][0-9]) [0-9][0-9][0-9]-[0-9][0-9][0-9][0-9]', 1, 0) AS BIT) AS dq_phone_reformatted
FROM sap_isu.BUT000 b
JOIN sap_isu.FKKVKP a ON a.GPART = b.PARTNER
CROSS APPLY (SELECT REPLACE(TRANSLATE(ISNULL(b.TEL_NUMBER, ''), '()-+', '    '), ' ', '') AS digits) d
CROSS APPLY (SELECT CASE WHEN LEN(d.digits) = 11 AND LEFT(d.digits, 1) = '1' THEN RIGHT(d.digits, 10)
                         WHEN LEN(d.digits) = 10 THEN d.digits END AS phone10) p;
GO

CREATE OR ALTER VIEW stg.premise AS
SELECT
    e.VSTELLE                                   AS premise_id,
    e.VBSART                                    AS premise_type,
    CONCAT(r.HOUSE_NUM1, ' ', r.STREET, IIF(r.HOUSE_NUM2 IS NULL, '', ' ' + r.HOUSE_NUM2)) AS address_line,
    t.CITY                                      AS city,
    t.ZIP                                       AS zip,
    t.COUNTY                                    AS county,
    t.DIVISION                                  AS division,
    r.GEO_LAT                                   AS latitude,
    r.GEO_LON                                   AS longitude,
    CAST(IIF(LEN(r.POST_CODE1) < 5, 1, 0) AS BIT)       AS dq_zip_fixed,
    -- the '|' sentinel makes trailing spaces count (SQL Server ignores them in = / <>)
    CAST(IIF(r.CITY1 + '|' COLLATE Latin1_General_CS_AS <> t.CITY + '|', 1, 0) AS BIT) AS dq_city_fixed
FROM sap_isu.EVBS e
JOIN sap_isu.ADRC r       ON r.ADDRNUMBER = e.ADDRNUMBER
JOIN reference.towns t    ON t.ZIP = RIGHT('00000' + TRIM(r.POST_CODE1), 5);
GO

CREATE OR ALTER VIEW stg.installation AS
SELECT
    i.ANLAGE                                    AS installation_id,
    i.VSTELLE                                   AS premise_id,
    CASE i.SPARTE WHEN '01' THEN 'Electric' WHEN '02' THEN 'Gas' END AS commodity,
    i.TARIFTYP                                  AS rate_code,
    i.ABLEINH                                   AS read_route,
    i.AKLASSE                                   AS rate_class,
    m.EQUNR                                     AS meter_id,
    m.HERST                                     AS meter_manufacturer,
    m.ZMETER_TECH                               AS meter_technology,
    m.INBDT                                     AS meter_install_date
FROM sap_isu.EANL i
LEFT JOIN sap_isu.EQUI m ON m.ANLAGE = i.ANLAGE;
GO

CREATE OR ALTER VIEW stg.contract AS
SELECT
    c.VERTRAG                                   AS contract_id,
    c.ANLAGE                                    AS installation_id,
    c.VKONTO                                    AS account_id,
    c.EINZDAT                                   AS move_in_date,
    NULLIF(c.AUSZDAT, '9999-12-31')             AS move_out_date,
    CAST(IIF(c.AUSZDAT = '9999-12-31', 1, 0) AS BIT) AS is_active,
    CASE c.ZSUPPLIER WHEN 'TPS' THEN 'Third-Party Supplier' ELSE 'Utility Default Supply' END AS supplier
FROM sap_isu.EVER c;
GO

CREATE OR ALTER VIEW stg.bill AS
SELECT
    h.BELNR                                     AS bill_id,
    h.VKONT                                     AS account_id,
    h.GPART                                     AS customer_id,
    h.VSTELLE                                   AS premise_id,
    CASE h.ABRVORG WHEN '01' THEN 'Regular' WHEN '02' THEN 'Rebill' WHEN '03' THEN 'Final' END AS bill_type,
    h.BUDAT                                     AS bill_date,
    h.BEGABRPE                                  AS period_start,
    h.ENDABRPE                                  AS period_end,
    DATEDIFF(DAY, h.BEGABRPE, h.ENDABRPE)       AS billing_days,
    h.FAEDN                                     AS due_date,
    h.STORNODAT                                 AS reversal_date,
    CAST(IIF(h.STORNODAT IS NULL, 0, 1) AS BIT) AS is_reversed,
    h.ELEC_TARIF                                AS elec_rate_code,
    h.KWH                                       AS kwh,
    CAST(IIF(h.ELEC_EST = 'X', 1, 0) AS BIT)    AS elec_estimated,
    CASE h.ELEC_SUPPLIER WHEN 'TPS' THEN 'Third-Party Supplier' ELSE 'Utility Default Supply' END AS elec_supplier,
    NULLIF(h.GAS_TARIF, '')                     AS gas_rate_code,
    h.THERMS                                    AS therms,
    CAST(IIF(h.GAS_EST = 'X', 1, 0) AS BIT)     AS gas_estimated,
    h.ELEC_DELIV_AMT                            AS elec_delivery_amt,
    h.ELEC_SUPPLY_AMT                           AS elec_supply_amt,
    h.GAS_DELIV_AMT                             AS gas_delivery_amt,
    h.GAS_SUPPLY_AMT                            AS gas_supply_amt,
    h.TAX_AMT                                   AS tax_amt,
    h.TOTAL_AMT                                 AS total_amt
FROM sap_isu.ERCH h;
GO

CREATE OR ALTER VIEW stg.payment AS
SELECT payment_id, account_id, bill_id, payment_date, amount, channel_code, channel, dup_rows
FROM (
    SELECT
        z.PAYMENT_ID  AS payment_id,
        z.VKONT       AS account_id,
        z.BELNR       AS bill_id,
        z.BUDAT       AS payment_date,
        z.BETRZ       AS amount,
        z.PAY_CHANNEL AS channel_code,
        CASE z.PAY_CHANNEL WHEN 'ACH' THEN 'Autopay (ACH)' WHEN 'WEB' THEN 'Online'
             WHEN 'APP' THEN 'Mobile App' WHEN 'IVR' THEN 'Phone (IVR)' WHEN 'CHK' THEN 'Mail (Check)'
             WHEN 'AGT' THEN 'Authorized Pay Agent' WHEN 'ASST' THEN 'Energy Assistance' END AS channel,
        COUNT(*) OVER (PARTITION BY z.PAYMENT_ID)                            AS dup_rows,
        ROW_NUMBER() OVER (PARTITION BY z.PAYMENT_ID ORDER BY z.PAYMENT_ID)  AS rn
    FROM sap_isu.DFKKZP z
) x
WHERE rn = 1;
GO

CREATE OR ALTER VIEW stg.payment_plan AS
SELECT
    PLAN_ID          AS plan_id,
    VKONT            AS account_id,
    CASE PLAN_TYPE WHEN 'WTP' THEN 'Winter Termination Program' ELSE 'Deferred Payment Arrangement' END AS plan_type,
    ERDAT            AS created_date,
    TOTAL_AMT        AS total_amt,
    DOWN_PAYMENT     AS down_payment,
    NUM_INSTALLMENTS AS num_installments,
    CASE STATUS WHEN 'ACTV' THEN 'Active' WHEN 'COMP' THEN 'Completed'
                WHEN 'DFLT' THEN 'Defaulted' WHEN 'CANC' THEN 'Cancelled' END AS status
FROM sap_isu.ZINSTPLAN;
GO

CREATE OR ALTER VIEW stg.service_order AS
SELECT
    o.ORDER_ID                                  AS order_id,
    o.ORDER_TYPE                                AS order_type_code,
    CASE o.ORDER_TYPE WHEN 'MOVE_IN' THEN 'Move In' WHEN 'MOVE_OUT' THEN 'Move Out'
         WHEN 'DISC_NP' THEN 'Disconnect for Non-Pay' WHEN 'RECONNECT' THEN 'Reconnect'
         WHEN 'METER_EXCH' THEN 'Meter Exchange' WHEN 'HIGH_BILL_INV' THEN 'High Bill Investigation'
         WHEN 'NEW_SVC' THEN 'New Service Connection' WHEN 'GAS_LEAK_INV' THEN 'Gas Leak Investigation' END AS order_type,
    o.VSTELLE                                   AS premise_id,
    o.VKONT                                     AS account_id,
    DATEADD(SECOND, DATEDIFF(SECOND, '00:00:00', o.ERZEIT), CAST(o.ERDAT AS DATETIME2(0))) AS created_at,
    o.ERDAT                                     AS created_date,
    o.SCHED_DATE                                AS scheduled_date,
    o.COMPL_DATE                                AS completed_date,
    CASE o.STATUS WHEN 'COMP' THEN 'Completed' WHEN 'OPEN' THEN 'Open' WHEN 'CANC' THEN 'Cancelled' END AS status,
    o.WORK_CENTER                               AS work_center,
    DATEDIFF(DAY, o.ERDAT, o.COMPL_DATE)        AS cycle_days
FROM sap_isu.ZSRVORD o;
GO

CREATE OR ALTER VIEW stg.work_order AS
SELECT
    w.AUFNR                                     AS work_order_id,
    w.AUART                                     AS order_type_code,
    CASE w.AUART WHEN 'PM01' THEN 'Corrective' WHEN 'PM02' THEN 'Preventive'
                 WHEN 'PM03' THEN 'Emergency'  WHEN 'PM04' THEN 'Capital Replacement' END AS order_type,
    w.KTEXT                                     AS description,
    w.ASSET_CLASS                               AS asset_class,
    CASE WHEN w.TPLNR LIKE 'G-%' THEN 'Gas' ELSE 'Electric' END AS commodity,
    w.TPLNR                                     AS functional_location,
    w.ARBPL                                     AS work_center,
    CAST(w.PRIOK AS TINYINT)                    AS priority,
    w.ERDAT                                     AS created_date,
    w.GSTRP                                     AS planned_start,
    w.GLTRP                                     AS planned_finish,
    w.GETRI                                     AS actual_finish,
    CASE w.STAT WHEN 'CRTD' THEN 'Created' WHEN 'REL' THEN 'Released' WHEN 'TECO' THEN 'Technically Complete'
                WHEN 'CLSD' THEN 'Closed' WHEN 'DLFL' THEN 'Deleted' END AS status,
    CAST(IIF(w.STAT IN ('CRTD', 'REL'), 1, 0) AS BIT) AS is_open,
    w.PLAN_COST                                 AS planned_cost,
    w.ACT_COST                                  AS actual_cost,
    w.ZDIVISION                                 AS division,
    w.ZMUNI                                     AS municipality
FROM sap_pm.AUFK w;
GO

CREATE OR ALTER VIEW stg.outage AS
SELECT
    EVENT_ID                                    AS outage_id,
    EVENT_START                                 AS outage_start,
    RESTORE_TIME                                AS restore_time,
    CAST(EVENT_START AS DATE)                   AS outage_date,
    DATEDIFF(MINUTE, EVENT_START, RESTORE_TIME) AS duration_min,
    CAST(IIF(RESTORE_TIME IS NULL, 1, 0) AS BIT) AS is_open,
    DIVISION                                    AS division,
    MUNICIPALITY                                AS municipality,
    CIRCUIT_ID                                  AS circuit_id,
    DEVICE_TYPE                                 AS device_type,
    CAUSE                                       AS cause,
    CUSTOMERS_OUT                               AS customers_out,
    CUSTOMER_MINUTES                            AS customer_minutes
FROM oms.outage_events;
GO

CREATE OR ALTER VIEW stg.interaction AS
SELECT
    INTERACTION_ID                              AS interaction_id,
    CREATED_TS                                  AS created_at,
    CAST(CREATED_TS AS DATE)                    AS interaction_date,
    DATEPART(HOUR, CREATED_TS)                  AS interaction_hour,
    CHANNEL                                     AS channel,
    CAST(IIF(CHANNEL IN ('Phone - Agent', 'Chat', 'Email'), 1, 0) AS BIT) AS is_assisted,
    REASON                                      AS reason,
    BP_ID                                       AS customer_id,
    CAST(IIF(BP_ID IS NULL, 0, 1) AS BIT)       AS is_identified,
    WAIT_SEC                                    AS wait_sec,
    HANDLE_SEC                                  AS handle_sec,
    CAST(IIF(FCR = 'Y', 1, 0) AS BIT)           AS first_contact_resolved,
    CAST(IIF(TRANSFERRED = 'Y', 1, 0) AS BIT)   AS transferred,
    CAST(CSAT AS TINYINT)                       AS csat,
    NULLIF(AGENT_ID, '')                        AS agent_id,
    NULLIF(SITE, '')                            AS site
FROM crm.interactions;
GO

CREATE OR ALTER VIEW stg.ami_read AS
SELECT
    METER_ID                                    AS meter_id,
    READ_DATE                                   AS read_date,
    KWH                                         AS kwh,
    PEAK_KW                                     AS peak_kw,
    READ_QUALITY                                AS read_quality,
    CAST(IIF(READ_QUALITY = 'MISSING', 1, 0) AS BIT) AS is_missing
FROM ami.daily_reads;
GO

CREATE OR ALTER VIEW stg.weather AS
SELECT OBS_DATE AS weather_date, TAVG_F AS temp_avg_f, TMAX_F AS temp_max_f, TMIN_F AS temp_min_f,
       PRECIP_IN AS precip_in, MAX_GUST_MPH AS max_gust_mph, HDD AS heating_degree_days, CDD AS cooling_degree_days
FROM weather.daily_weather;
GO

CREATE OR ALTER VIEW stg.rate AS
SELECT RATE_CODE AS rate_code, COMMODITY AS commodity,
       CASE RATE_CLASS WHEN 'RES' THEN 'Residential' WHEN 'COM' THEN 'Commercial' WHEN 'IND' THEN 'Industrial' END AS rate_class,
       DESCRIPTION AS rate_description, UNIT AS unit,
       CUSTOMER_CHARGE AS customer_charge, DELIVERY_PER_UNIT AS delivery_per_unit, SUPPLY_PER_UNIT AS supply_per_unit
FROM reference.rate_schedules;
GO
