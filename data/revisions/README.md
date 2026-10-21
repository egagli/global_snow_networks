# Revision log

Upstream sources revise data they have already published: NRCS corrects
SNOTEL records after QC, CDEC adjusts SWE, NVE reprocesses whole series.
The nightly refresh re-downloads every station's full record, so those
revisions reach the station CSVs — and this log records what changed, so a
value in `data/stations/` that differs from last week's is never a mystery
(DESIGN.md §6.6, issue #24).

One file per month of detection, `YYYY-MM.csv`, appended by the nightly
refresh:

| column | meaning |
| --- | --- |
| `detected_utc` | when the refresh saw the change (ISO 8601, UTC) |
| `code`, `client` | the station, as in `all_snow_stations.geojson` |
| `date` | the observation date that changed |
| `variable` | `wteq_cm` or `snwd_cm`; `rows` for a `refused_shrink` row |
| `old_value`, `new_value` | the CSV cell before and after; empty = missing |
| `kind` | `revised`, `filled`, `removed`, or `refused_shrink` |

- `revised`: a value changed to a different value (beyond 1e-6 cm).
- `filled`: a missing value (or a date the CSV did not have) gained one.
- `removed`: a value became missing, or its date disappeared.
- `refused_shrink`: the replacement would have lost history and was
  refused by the shrink guard; the old CSV was kept. `old_value` /
  `new_value` are the populated-row counts and `date` is the old record
  start. Rerun with `--allow-shrink <code>` once the loss is confirmed real.

Only dates up to the old file's last date are compared; days appended
beyond it are new observations, not revisions.
