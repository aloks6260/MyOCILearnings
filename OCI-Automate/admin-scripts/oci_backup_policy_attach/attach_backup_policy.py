#!/usr/bin/env python3
"""
attach_backup_policy.py

Bulk-attach (and forcibly overwrite) a common OCI Volume Backup Policy across
a list of boot/block volumes supplied via CSV.

Behavior per volume:
    1. Look up any existing volume backup policy assignment.
    2. If it's already the target policy -> skip (no-op, idempotent).
    3. If it's a different policy -> delete the old assignment, then create
       a new assignment pointing at the target policy (forceful overwrite).
    4. If no assignment exists -> create a new assignment.

Rate limiting / resiliency:
    - Uses a custom OCI retry strategy with exponential backoff + full jitter,
      specifically retrying on HTTP 429 (TooManyRequests) and any 5xx.
    - Bounded thread pool (default 5 workers) instead of firing all requests
      at once, so you don't hammer the Block Storage service.
    - Small per-request throttle sleep as an extra safety margin on top of
      the SDK-level backoff.
    - Every volume's outcome (success/skipped/failed) is written to a CSV
      report so you have an audit trail and can re-run just the failures.

Usage:
    pip install oci

    python attach_backup_policy.py \\
        --csv volumes.csv \\
        --policy-id ocid1.volumebackuppolicy.oc1..xxxxxxxx \\
        --config-file ~/.oci/config \\
        --profile DEFAULT \\
        --workers 5 \\
        --dry-run

CSV input format (header optional, column name auto-detected):
    volume_id
    ocid1.bootvolume.oc1.ap-mumbai-1.xxxxxxxx
    ocid1.volume.oc1.ap-mumbai-1.yyyyyyyy
    ...

Recognized header names: volume_id, ocid, volume_ocid, id
If no recognizable header is present, the script assumes there is no header
at all and treats every row's first column as an OCID.
"""

import argparse
import csv
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import oci
from oci.exceptions import ServiceError
from oci.retry import RetryStrategyBuilder

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
LOG_FILE = f"backup_policy_attach_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom retry strategy: exponential backoff with full jitter, retries on
# 429 (throttling) and any 5xx transient service error. Adjust max_attempts /
# total_elapsed_time_seconds if you're pushing a very large tenancy-wide list.
# ---------------------------------------------------------------------------
def build_retry_strategy():
    return RetryStrategyBuilder(
        max_attempts_check=True,
        max_attempts=8,
        total_elapsed_time_check=True,
        total_elapsed_time_seconds=600,
        retry_max_wait_between_calls_seconds=30,
        retry_base_sleep_time_seconds=2,
        service_error_check=True,
        service_error_retry_on_any_5xx=True,
        service_error_retry_config={
            429: [],  # empty list = retry on ALL 429s regardless of error "code"
        },
        backoff_type=oci.retry.BACKOFF_FULL_JITTER_EQUAL_ON_THROTTLE_VALUE,
    ).get_retry_strategy()


RETRY_STRATEGY = build_retry_strategy()


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------
def read_ocids_from_csv(csv_path):
    """
    Reads volume OCIDs from a CSV file. Auto-detects a header column named
    one of: volume_id, ocid, volume_ocid, id. Falls back to column 0 if no
    recognizable header is present (and assumes there IS no header row in
    that case).
    """
    candidates = {"volume_id", "ocid", "volume_ocid", "id"}

    with open(csv_path, newline="") as f:
        rows = [r for r in csv.reader(f) if r]

    if not rows:
        raise ValueError("CSV file is empty")

    header = [c.strip().lower() for c in rows[0]]
    col_idx = 0
    data_rows = rows[1:]  # default: assume row 0 is a header

    matched = False
    for cand in candidates:
        if cand in header:
            col_idx = header.index(cand)
            matched = True
            break

    if not matched:
        # No recognizable header name found. If row 0 itself looks like an
        # OCID, there's no header at all -- include it as data.
        if rows[0][0].strip().lower().startswith("ocid1."):
            data_rows = rows

    ocids = []
    for row in data_rows:
        if col_idx >= len(row):
            continue
        val = row[col_idx].strip()
        if not val:
            continue
        if not val.startswith("ocid1."):
            log.warning(f"Skipping value that doesn't look like an OCID: {val}")
            continue
        ocids.append(val)

    # De-duplicate while preserving order
    seen = set()
    unique = []
    for o in ocids:
        if o not in seen:
            seen.add(o)
            unique.append(o)
    return unique


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def get_existing_assignment(blockstorage_client, volume_ocid):
    """
    Returns the existing VolumeBackupPolicyAssignment for a volume, or None
    if nothing is currently assigned. OCI only allows a single active
    assignment per asset, so the response list has at most one element.
    """
    resp = blockstorage_client.get_volume_backup_policy_asset_assignment(
        asset_id=volume_ocid,
        retry_strategy=RETRY_STRATEGY,
    )
    assignments = resp.data
    return assignments[0] if assignments else None


def process_volume(blockstorage_client, volume_ocid, policy_ocid, dry_run=False):
    """
    Ensures `volume_ocid` ends up assigned to `policy_ocid`, forcefully
    overwriting any different pre-existing assignment. Returns a dict
    suitable for the CSV report.
    """
    result = {
        "volume_id": volume_ocid,
        "status": "unknown",
        "previous_policy_id": "",
        "detail": "",
    }

    try:
        existing = get_existing_assignment(blockstorage_client, volume_ocid)

        if existing:
            result["previous_policy_id"] = existing.policy_id
            if existing.policy_id == policy_ocid:
                result["status"] = "skipped"
                result["detail"] = "Already assigned to target policy"
                log.info(f"[SKIP] {volume_ocid} already on target policy")
                return result

            if dry_run:
                log.info(
                    f"[DRY-RUN] Would delete assignment {existing.id} "
                    f"(policy {existing.policy_id}) on {volume_ocid}"
                )
            else:
                blockstorage_client.delete_volume_backup_policy_assignment(
                    policy_assignment_id=existing.id,
                    retry_strategy=RETRY_STRATEGY,
                )
                log.info(
                    f"[DELETE] Removed old assignment on {volume_ocid} "
                    f"(was policy {existing.policy_id})"
                )

        if dry_run:
            log.info(f"[DRY-RUN] Would assign policy {policy_ocid} to {volume_ocid}")
            result["status"] = "dry-run"
            result["detail"] = "Would create new assignment"
            return result

        create_details = oci.core.models.CreateVolumeBackupPolicyAssignmentDetails(
            asset_id=volume_ocid,
            policy_id=policy_ocid,
        )
        blockstorage_client.create_volume_backup_policy_assignment(
            create_volume_backup_policy_assignment_details=create_details,
            retry_strategy=RETRY_STRATEGY,
        )
        result["status"] = "success"
        result["detail"] = "Assigned to target policy"
        log.info(f"[OK] {volume_ocid} -> {policy_ocid}")

    except ServiceError as se:
        result["status"] = "failed"
        result["detail"] = f"ServiceError {se.status}: {se.code} - {se.message}"
        log.error(f"[FAIL] {volume_ocid}: {result['detail']}")
    except Exception as e:  # noqa: BLE001 - want to capture and report any failure
        result["status"] = "failed"
        result["detail"] = str(e)
        log.error(f"[FAIL] {volume_ocid}: {e}")

    return result


def process_with_throttle(client, ocid, policy_id, dry_run, throttle_seconds):
    time.sleep(throttle_seconds)  # simple client-side pacing on top of SDK backoff
    return process_volume(client, ocid, policy_id, dry_run)


def write_report(results, report_path):
    fieldnames = ["volume_id", "status", "previous_policy_id", "detail"]
    with open(report_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    log.info(f"Report written to {report_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Bulk attach/forcefully overwrite an OCI Volume Backup Policy "
        "across boot/block volumes listed in a CSV."
    )
    parser.add_argument("--csv", required=True, help="Path to input CSV containing volume OCIDs")
    parser.add_argument("--policy-id", required=True, help="OCID of the common Volume Backup Policy to attach")
    parser.add_argument("--config-file", default="~/.oci/config", help="Path to OCI config file")
    parser.add_argument("--profile", default="DEFAULT", help="Profile name in the OCI config file")
    parser.add_argument(
        "--workers", type=int, default=5,
        help="Concurrent worker threads. Keep modest (3-10) to respect service limits. Default 5.",
    )
    parser.add_argument(
        "--throttle", type=float, default=0.2,
        help="Seconds to sleep before each request, per worker (default 0.2s).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without making them")
    parser.add_argument("--report", default=None, help="Path to output CSV report (default: auto-generated)")
    parser.add_argument(
        "--instance-principal", action="store_true",
        help="Use Instance Principal auth instead of a config file (e.g. running from an OCI compute instance)",
    )
    args = parser.parse_args()

    # ---- Auth / client setup ----
    if args.instance_principal:
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        blockstorage_client = oci.core.BlockstorageClient(config={}, signer=signer)
    else:
        config = oci.config.from_file(file_location=args.config_file, profile_name=args.profile)
        oci.config.validate_config(config)
        blockstorage_client = oci.core.BlockstorageClient(config)

    # ---- Load volumes ----
    try:
        volume_ocids = read_ocids_from_csv(args.csv)
    except Exception as e:
        log.error(f"Failed to read CSV: {e}")
        sys.exit(1)

    if not volume_ocids:
        log.error("No valid volume OCIDs found in CSV. Exiting.")
        sys.exit(1)

    log.info(f"Loaded {len(volume_ocids)} unique volume OCID(s) from {args.csv}")
    log.info(f"Target backup policy: {args.policy_id}")
    if args.dry_run:
        log.info("Running in DRY-RUN mode - no changes will be made.")

    # ---- Process with bounded concurrency + manual throttle ----
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                process_with_throttle,
                blockstorage_client,
                ocid,
                args.policy_id,
                args.dry_run,
                args.throttle,
            ): ocid
            for ocid in volume_ocids
        }
        for future in as_completed(futures):
            results.append(future.result())

    # ---- Summary ----
    success = sum(1 for r in results if r["status"] == "success")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    dry = sum(1 for r in results if r["status"] == "dry-run")
    failed = sum(1 for r in results if r["status"] == "failed")

    log.info("=" * 60)
    log.info(
        f"SUMMARY: total={len(results)} success={success} skipped={skipped} "
        f"dry_run={dry} failed={failed}"
    )
    log.info("=" * 60)

    report_path = args.report or f"backup_policy_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    write_report(results, report_path)

    if failed:
        log.warning(f"{failed} volume(s) failed. Check the report/log ({LOG_FILE}) for details.")
        sys.exit(2)


if __name__ == "__main__":
    main()
