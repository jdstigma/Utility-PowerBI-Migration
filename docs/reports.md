# Report Catalog: Excel → Power BI

These are the ten proposed reports that replace the current Excel workbooks. Each one names the assumed Excel pain point, what Power BI improves, the core measures, and the source tables. Customer 360 is a shared drill-through page that every report can jump to.

| # | Report | Audience | Core measures | Sources |
|---|---|---|---|---|
| 1 | **Revenue & Billing Summary** | Finance, Rates | Billed revenue (delivery / supply / tax), kWh, therms, avg bill, YoY, revenue per customer by rate class & division. **Residential & non-commercial and commercial & industrial are separate tables** sharing `period_key`, plotted on a dual y-axis so C&I doesn't hide the residential trend. | ERCH, EANL, reference |
| 2 | **Accounts Receivable Aging** | Credit & Collections, Finance | Open AR, Current / 1-30 / 31-60 / 61-90 / 91-180 buckets, 180-day write-offs, % past due, DSO, top arrears accounts | ERCH, DFKKZP, FKKVKP |
| 3 | **Payments & Digital Adoption** | Customer Ops, Digital | Payments by channel, autopay and paperless enrollment trend, cost-to-serve by channel, assistance (USF/LIHEAP) credits | DFKKZP, FKKVKP |
| 4 | **Usage & AMI Load Profile** | Load Research, Energy Efficiency | Daily kWh vs temperature (CDD/HDD), weekday/weekend shape, top-usage premises, read quality % | AMI daily_reads, weather, EQUI |
| 5 | **Outage & Reliability** | Electric Operations, Regulatory | SAIDI, SAIFI, CAIDI (IEEE 1366, with and without Major Event Days), outages by cause/device/circuit, worst-performing circuits | OMS, premises |
| 6 | **Work Order Backlog** | Asset Management, Field Ops | Open backlog by age & priority, planned vs actual cost, schedule adherence, emergency vs planned mix | AUFK |
| 7 | **Contact Center Performance** | Customer Care | Volume, ASA (avg speed of answer), AHT, FCR, CSAT, contact reasons, storm and rate-change spikes | CRM interactions |
| 8 | **Credit & Collections Actions** | Credit & Collections, Regulatory | Disconnects for non-pay, reconnect rate & time, payment arrangements (DPA/WTP) and default rate, winter-moratorium compliance, medical-certificate protections | ZSRVORD, ZINSTPLAN, FKKVKP |
| 9 | **Meter-to-Cash Exceptions** | Billing Ops | Estimated bills %, consecutive estimates, zero-usage bills on active accounts, high-bill variance (>50% vs prior), rebills/reversals | ERCH, EQUI |
| 10 | **Service Order Activity** | Field Ops, Customer Ops | Move-ins/outs, new service connections, meter exchanges, cycle time vs target, open order aging | ZSRVORD |
| – | **Customer 360** (drill-through) | Everyone | Account profile, bill history, payments, contacts, orders, outages in their town | all |

## What Power BI improves over the Excel versions

- **One semantic model.** Every measure (past due, SAIDI, AHT...) is defined once in DAX and reused. Today each workbook defines its own.
- **Scheduled refresh** replaces copy/paste refresh. In the demo the model reads the GitHub files. In production it would connect to SQL through an on-prem gateway, which removes the Excel/SharePoint hop entirely.
- **Drill-through and cross-filtering**: division → town → circuit → account, all in one click path.
- **Row-level security** by division, so one report serves every region.
- **Data-quality surfacing**: Report 9 plus a DQ page show the issues the staging layer caught (duplicate payments, bad ZIPs, missing reads) instead of hiding them.
- **Regulatory-grade definitions**: reliability indices follow IEEE 1366, with Major Event Days flagged by the 2.5-beta method.
