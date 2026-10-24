# Trade records (black box)

Written automatically by the Trade Desk GitHub Action into this `trade-records/` folder. Don't edit by hand.

- `journal/journal.json`: every order the auto-trader placed, with all details captured at decision time
- `excel/`: the weekly Excel journal (one row per order, columns A to AV). Rewritten whenever the week's orders change; a new file starts every Monday; only the newest 3 are kept
- `reports/YYYY/MM-Month/week-WW_<monday>_to_<sunday>.pdf`: weekly report, made Saturday 10:00 UTC, kept forever
- `reports/YYYY/MM-Month/monthly-report_YYYY-MM_Month.pdf`: monthly report on the month's last Saturday (title page, summary, final week)
- `state/`: trailing-stop peaks, report bookkeeping, supervisor results
- `index.json`: file list and storage size; the dashboard warns before 1 GB
