#!/usr/bin/env python3
"""
OCI Volume Backup Cleanup Script (with throttling)

Adds:
- Configurable delay between delete calls to avoid TooManyRequests.
- OCI retry strategy with exponential backoff for throttling.
"""

import argparse
import logging
import sys
import time
from typing import List, Dict, Tuple
import oci
from oci.core import BlockstorageClient
from oci.core.models import BootVolumeBackup, VolumeBackup
from oci.config import from_file, validate_config
from oci.exceptions import ServiceError, ConfigFileNotFound

DEFAULT_CONFIG_FILE = "~/.oci/config"
DEFAULT_PROFILE = "DEFAULT"
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_level: str, log_file: str = None) -> logging.Logger:
    logger = logging.getLogger("oci_backup_cleanup")
    logger.setLevel(getattr(logging, log_level.upper()))
    # Remove old handlers if script is re-imported
    if logger.handlers:
        logger.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, log_level.upper()))
    console_formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

    return logger


def build_retry_strategy(logger: logging.Logger):
    """
    Build a retry strategy that specifically backs off on throttling (429)
    and common quota/limit errors using exponential backoff with jitter.
    """
    logger.info("Building custom retry strategy with exponential backoff for throttling")

    try:
        retry_strategy = oci.retry.RetryStrategyBuilder(
            max_attempts_check=True,
            max_attempts=8,
            total_elapsed_time_check=True,
            total_elapsed_time_seconds=600,
            service_error_check=True,
            service_error_retry_on_any_5xx=True,
            service_error_retry_config={
                400: ["QuotaExceeded", "LimitExceeded"],
                429: []
            },
            backoff_type=oci.retry.BACKOFF_FULL_JITTER_EQUAL_ON_THROTTLE_VALUE,
        ).get_retry_strategy()
        
        return retry_strategy
    except Exception as e:
        logger.warning(f"Could not build custom retry strategy: {e}. Using SDK defaults.")
        return None


def initialize_oci_client(
    config_file: str,
    profile: str,
    logger: logging.Logger
) -> Tuple[BlockstorageClient, dict]:
    try:
        logger.info(f"Loading OCI configuration from {config_file} (profile: {profile})")
        config = from_file(file_location=config_file, profile_name=profile)
        validate_config(config)
        logger.info(f"Configuration validated for tenancy: {config.get('tenancy')}")

        retry_strategy = build_retry_strategy(logger)

        if retry_strategy:
            block_storage_client = BlockstorageClient(
                config,
                retry_strategy=retry_strategy
            )
            logger.info("BlockstorageClient initialized with custom retry strategy")
        else:
            block_storage_client = BlockstorageClient(config)
            logger.info("BlockstorageClient initialized with default retry strategy")

        return block_storage_client, config

    except ConfigFileNotFound as e:
        logger.error(f"OCI config file not found: {e}")
        raise
    except Exception as e:
        logger.error(f"Failed to initialize OCI client: {e}")
        raise


def list_boot_volume_backups(
    client: BlockstorageClient,
    compartment_id: str,
    logger: logging.Logger
) -> List[BootVolumeBackup]:
    logger.info(f"Fetching boot volume backups from compartment: {compartment_id}")

    try:
        backups = oci.pagination.list_call_get_all_results(
            client.list_boot_volume_backups,
            compartment_id=compartment_id,
            sort_by="TIMECREATED",
            sort_order="ASC",
        ).data

        filtered = [
            b for b in backups
            if b.lifecycle_state in ["AVAILABLE", "CREATING"]
        ]
        logger.info(f"Found {len(filtered)} boot volume backups (eligible states)")
        return filtered

    except ServiceError as e:
        logger.error(f"Service error listing boot volume backups: {e.message}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error listing boot volume backups: {e}")
        raise


def list_block_volume_backups(
    client: BlockstorageClient,
    compartment_id: str,
    logger: logging.Logger
) -> List[VolumeBackup]:
    logger.info(f"Fetching block volume backups from compartment: {compartment_id}")

    try:
        backups = oci.pagination.list_call_get_all_results(
            client.list_volume_backups,
            compartment_id=compartment_id,
            sort_by="TIMECREATED",
            sort_order="ASC",
        ).data

        filtered = [
            b for b in backups
            if b.lifecycle_state in ["AVAILABLE", "CREATING"]
        ]
        logger.info(f"Found {len(filtered)} block volume backups (eligible states)")
        return filtered

    except ServiceError as e:
        logger.error(f"Service error listing block volume backups: {e.message}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error listing block volume backups: {e}")
        raise


def group_backups_by_volume(
    backups: List,
    backup_type: str,
    logger: logging.Logger
) -> Dict[str, List]:
    grouped = {}
    for backup in backups:
        volume_id = backup.boot_volume_id if backup_type == "boot" else backup.volume_id
        grouped.setdefault(volume_id, []).append(backup)

    logger.info(f"Grouped {len(backups)} {backup_type} backups into {len(grouped)} volume groups")
    return grouped


def delete_old_boot_volume_backups(
    client: BlockstorageClient,
    backups_by_volume: Dict[str, List[BootVolumeBackup]],
    keep_count: int,
    dry_run: bool,
    sleep_between: float,
    logger: logging.Logger
) -> Tuple[int, int]:
    deleted_count = 0
    error_count = 0

    logger.info(
        f"Deleting old boot volume backups (keep={keep_count}, "
        f"sleep_between={sleep_between}s, dry_run={dry_run})"
    )

    for volume_id, backups in backups_by_volume.items():
        sorted_backups = sorted(backups, key=lambda b: b.time_created)
        total_backups = len(sorted_backups)
        to_delete_count = max(0, total_backups - keep_count)

        logger.info(
            f"Boot volume {volume_id}: total={total_backups}, "
            f"to_delete={to_delete_count}, keep={keep_count}"
        )

        for i, backup in enumerate(sorted_backups):
            if i < to_delete_count:
                msg = (
                    f"{'[DRY RUN] Would delete' if dry_run else 'Deleting'} "
                    f"boot backup {backup.display_name} "
                    f"(ID={backup.id}, Created={backup.time_created})"
                )
                logger.info(msg)

                if not dry_run:
                    try:
                        client.delete_boot_volume_backup(backup.id)
                        deleted_count += 1
                        logger.debug(f"Delete request sent for boot backup {backup.id}")
                    except ServiceError as e:
                        logger.error(
                            f"Failed to delete boot backup {backup.id}: "
                            f"{e.message} (status={e.status})"
                        )
                        error_count += 1
                    except Exception as e:
                        logger.error(f"Unexpected error deleting boot backup {backup.id}: {e}")
                        error_count += 1

                    if sleep_between > 0:
                        logger.debug(f"Sleeping {sleep_between}s to avoid throttling")
                        time.sleep(sleep_between)
                else:
                    deleted_count += 1
            else:
                logger.debug(
                    f"Keeping boot backup {backup.display_name} "
                    f"(ID={backup.id}, Created={backup.time_created})"
                )

    return deleted_count, error_count


def delete_old_block_volume_backups(
    client: BlockstorageClient,
    backups_by_volume: Dict[str, List[VolumeBackup]],
    keep_count: int,
    dry_run: bool,
    sleep_between: float,
    logger: logging.Logger
) -> Tuple[int, int]:
    deleted_count = 0
    error_count = 0

    logger.info(
        f"Deleting old block volume backups (keep={keep_count}, "
        f"sleep_between={sleep_between}s, dry_run={dry_run})"
    )

    for volume_id, backups in backups_by_volume.items():
        sorted_backups = sorted(backups, key=lambda b: b.time_created)
        total_backups = len(sorted_backups)
        to_delete_count = max(0, total_backups - keep_count)

        logger.info(
            f"Block volume {volume_id}: total={total_backups}, "
            f"to_delete={to_delete_count}, keep={keep_count}"
        )

        for i, backup in enumerate(sorted_backups):
            if i < to_delete_count:
                msg = (
                    f"{'[DRY RUN] Would delete' if dry_run else 'Deleting'} "
                    f"block backup {backup.display_name} "
                    f"(ID={backup.id}, Created={backup.time_created})"
                )
                logger.info(msg)

                if not dry_run:
                    try:
                        client.delete_volume_backup(backup.id)
                        deleted_count += 1
                        logger.debug(f"Delete request sent for block backup {backup.id}")
                    except ServiceError as e:
                        logger.error(
                            f"Failed to delete block backup {backup.id}: "
                            f"{e.message} (status={e.status})"
                        )
                        error_count += 1
                    except Exception as e:
                        logger.error(f"Unexpected error deleting block backup {backup.id}: {e}")
                        error_count += 1

                    if sleep_between > 0:
                        logger.debug(f"Sleeping {sleep_between}s to avoid throttling")
                        time.sleep(sleep_between)
                else:
                    deleted_count += 1
            else:
                logger.debug(
                    f"Keeping block backup {backup.display_name} "
                    f"(ID={backup.id}, Created={backup.time_created})"
                )

    return deleted_count, error_count


def main():
    parser = argparse.ArgumentParser(
        description="OCI Volume Backup Cleanup Script with throttling",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run, keep 3, no actual deletion
  %(prog)s -c ocid1.compartment.oc1..xxxxx -k 3 --dry-run

  # Keep 5 backups and sleep 0.5s between delete calls
  %(prog)s -c ocid1.compartment.oc1..xxxxx -k 5 --sleep-between 0.5

  # Process only boot backups with debug logging
  %(prog)s -c ocid1.compartment.oc1..xxxxx -k 3 --boot-only --sleep-between 1 --log-level DEBUG
        """
    )

    parser.add_argument(
        "--compartment-id",
        "-c",
        required=True,
        help="OCID of the compartment to scan for backups",
    )
    parser.add_argument(
        "--keep",
        "-k",
        type=int,
        required=True,
        help="Number of most recent backups to keep per volume",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_FILE,
        help=f"Path to OCI config file (default: {DEFAULT_CONFIG_FILE})",
    )
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        help=f"Profile name in OCI config file (default: {DEFAULT_PROFILE})",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Simulate deletion without actually deleting backups",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "--log-file",
        help="Optional path to log file",
    )
    parser.add_argument(
        "--boot-only",
        action="store_true",
        help="Process only boot volume backups",
    )
    parser.add_argument(
        "--block-only",
        action="store_true",
        help="Process only block volume backups",
    )
    parser.add_argument(
        "--sleep-between",
        type=float,
        default=0.5,
        help="Seconds to sleep between each delete API call (default: 0.5)",
    )

    args = parser.parse_args()

    if args.boot_only and args.block_only:
        parser.error("Cannot specify both --boot-only and --block-only")
    if args.keep < 0:
        parser.error("--keep must be a non-negative integer")
    if args.sleep_between < 0:
        parser.error("--sleep-between must be >= 0")

    logger = setup_logging(args.log_level, args.log_file)

    logger.info("=" * 80)
    logger.info("OCI Volume Backup Cleanup Script (with throttling) Started")
    logger.info("=" * 80)
    logger.info(f"Compartment ID: {args.compartment_id}")
    logger.info(f"Keep Count: {args.keep}")
    logger.info(f"Dry Run: {args.dry_run}")
    logger.info(f"Config File: {args.config}")
    logger.info(f"Profile: {args.profile}")
    logger.info(f"Sleep Between Deletes: {args.sleep_between}s")

    process_boot = not args.block_only
    process_block = not args.boot_only
    logger.info(f"Process Boot Backups: {process_boot}")
    logger.info(f"Process Block Backups: {process_block}")

    try:
        client, _ = initialize_oci_client(args.config, args.profile, logger)

        total_deleted = 0
        total_errors = 0

        if process_boot:
            logger.info("=" * 80)
            logger.info("PROCESSING BOOT VOLUME BACKUPS")
            logger.info("=" * 80)
            boot_backups = list_boot_volume_backups(client, args.compartment_id, logger)
            if boot_backups:
                grouped = group_backups_by_volume(boot_backups, "boot", logger)
                d, e = delete_old_boot_volume_backups(
                    client,
                    grouped,
                    args.keep,
                    args.dry_run,
                    args.sleep_between,
                    logger,
                )
                total_deleted += d
                total_errors += e
            else:
                logger.info("No boot volume backups found")

        if process_block:
            logger.info("=" * 80)
            logger.info("PROCESSING BLOCK VOLUME BACKUPS")
            logger.info("=" * 80)
            block_backups = list_block_volume_backups(client, args.compartment_id, logger)
            if block_backups:
                grouped = group_backups_by_volume(block_backups, "block", logger)
                d, e = delete_old_block_volume_backups(
                    client,
                    grouped,
                    args.keep,
                    args.dry_run,
                    args.sleep_between,
                    logger,
                )
                total_deleted += d
                total_errors += e
            else:
                logger.info("No block volume backups found")

        logger.info("=" * 80)
        logger.info("EXECUTION SUMMARY")
        logger.info("=" * 80)
        if args.dry_run:
            logger.info(f"[DRY RUN] Would delete {total_deleted} backups")
        else:
            logger.info(f"Deleted {total_deleted} backups")

        if total_errors:
            logger.warning(f"Encountered {total_errors} errors during execution")

        logger.info("Script execution completed")
        sys.exit(1 if total_errors else 0)

    except KeyboardInterrupt:
        logger.warning("Interrupted by user (Ctrl+C)")
        sys.exit(130)
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
