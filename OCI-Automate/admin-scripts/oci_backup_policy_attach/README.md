How it works:

CSV input — auto-detects a header column (volume_id, ocid, volume_ocid, or id); falls back gracefully if there's no header at all. Works for both boot and block volume OCIDs since they use the same backup policy assignment API.
Forceful overwrite logic, per volume:

Fetches the existing assignment via get_volume_backup_policy_asset_assignment.
If it already matches your target policy → skips (idempotent, safe to re-run).
If it's a different policy → deletes the old assignment, then creates the new one.
If nothing is assigned → just creates the new one.


Rate limiting / resiliency:

Custom OCI retry strategy with exponential backoff + full jitter, specifically retrying on 429 (throttling) and any 5xx.
Bounded ThreadPoolExecutor (default 5 workers) instead of blasting every request at once.
A small per-request throttle sleep as an extra safety margin.


Auditability — every outcome (success/skipped/failed/dry-run) is written to a timestamped CSV report plus a log file, so you can confirm tenancy-wide consistency afterward or re-run just the failures.

Usage:
bashpip install oci

python attach_backup_policy.py \
    --csv volumes.csv \
    --policy-id ocid1.volumebackuppolicy.oc1..xxxxxxxx \
    --config-file ~/.oci/config \
    --profile DEFAULT \
    --workers 5 \
    --dry-run
Run once with --dry-run first to preview exactly what would change, then drop the flag to execute. A couple of things worth knowing before you run it at scale:
