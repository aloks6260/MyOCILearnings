#!/usr/bin/env python3
"""
delete_idcs_users.py

Bulk-delete OCI IDCS / OCI IAM Identity Domain users listed in a CSV file.

The CSV must have a header row with a column named "username"
(case-insensitive), e.g.:

    username
    jdoe
    asmith
    bwayne

For each username, the script:
  1. Looks the user up via SCIM filter (GET /admin/v1/Users?filter=userName eq "...")
  2. Deletes the user (DELETE /admin/v1/Users/{id})
  3. Logs the result to console and to a results CSV (delete_results.csv)

Auth: OAuth2 client-credentials grant against your IDCS/Identity Domain's
confidential-application client ID/secret.

SAFETY:
  - Defaults to DRY-RUN (no deletes happen) unless you pass --confirm.
  - Always test with --dry-run first and review the printed list.
"""

import argparse
import base64
import csv
import json
import sys
import time
from datetime import datetime
from typing import List, Optional

import requests

# ---------------------------------------------------------------------------
# Configuration - fill these in or pass as environment variables / CLI args
# ---------------------------------------------------------------------------
# Example IDCS_URL: https://idcs-xxxxxxxxxxxxxxxx.identity.oraclecloud.com
# (For newer "Identity Domains" it may look like:
#  https://idcs-xxxxxxxxxxxxxxxx.identity.oraclecloud.com  as well)


def get_access_token(idcs_url: str, client_id: str, client_secret: str) -> str:
    """Obtain an OAuth2 access token using the client_credentials grant."""
    token_url = f"{idcs_url.rstrip('/')}/oauth2/v1/token"
    basic = base64.urlsafe_b64encode(
        f"{client_id}:{client_secret}".encode("utf-8")
    ).decode("ascii")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Authorization": f"Basic {basic}",
        "Accept": "application/json",
    }
    data = "grant_type=client_credentials&scope=urn:opc:idm:__myscopes__"

    resp = requests.post(token_url, headers=headers, data=data, timeout=30)
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in response: {resp.text}")
    return token


def find_user_id(idcs_url: str, token: str, username: str) -> Optional[str]:
    """Look up a user's SCIM id by exact userName match. Returns None if not found."""
    url = f"{idcs_url.rstrip('/')}/admin/v1/Users"
    headers = {"Authorization": f"Bearer {token}"}
    # Escape any double quotes in the username defensively
    safe_username = username.replace('"', '\\"')
    params = {
        "filter": f'userName eq "{safe_username}"',
        "attributes": "id,userName",
    }
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    resources = body.get("Resources", [])
    if not resources:
        return None
    return resources[0].get("id")


def delete_user(idcs_url: str, token: str, user_id: str, force: bool = True) -> None:
    """Delete a user by SCIM id. force=True permanently deletes rather than
    just deactivating (maps to ?forceDelete=true)."""
    url = f"{idcs_url.rstrip('/')}/admin/v1/Users/{user_id}"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"forceDelete": "true"} if force else {}
    resp = requests.delete(url, headers=headers, params=params, timeout=30)
    # IDCS returns 204 No Content on success
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")


def read_usernames(csv_path: str) -> List[str]:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # Find the "username" column case-insensitively
        col = None
        for fieldname in reader.fieldnames or []:
            if fieldname.strip().lower() == "username":
                col = fieldname
                break
        if not col:
            raise ValueError(
                f"No 'username' column found in {csv_path}. "
                f"Found columns: {reader.fieldnames}"
            )
        usernames = [row[col].strip() for row in reader if row.get(col, "").strip()]
    return usernames


def main():
    parser = argparse.ArgumentParser(description="Bulk delete OCI IDCS users from a CSV file.")
    parser.add_argument("--csv", required=True, help="Path to CSV file with a 'username' column")
    parser.add_argument("--idcs-url", required=True, help="Base IDCS/Identity Domain URL, e.g. https://idcs-xxxx.identity.oraclecloud.com")
    parser.add_argument("--client-id", required=True, help="OAuth2 confidential app client ID")
    parser.add_argument("--client-secret", required=True, help="OAuth2 confidential app client secret")
    parser.add_argument("--confirm", action="store_true", help="Actually perform deletions. Without this flag, runs in dry-run mode.")
    parser.add_argument("--soft-delete", action="store_true", help="Deactivate/soft-delete instead of permanently deleting (omits forceDelete=true).")
    parser.add_argument("--delay", type=float, default=0.3, help="Seconds to sleep between API calls (default 0.3, to avoid rate limits)")
    parser.add_argument("--results-csv", default="delete_results.csv", help="Path to write results log")
    args = parser.parse_args()

    dry_run = not args.confirm

    print(f"{'DRY RUN - ' if dry_run else ''}Loading usernames from {args.csv} ...")
    usernames = read_usernames(args.csv)
    print(f"Found {len(usernames)} username(s) to process.")

    if dry_run:
        print("\n*** DRY RUN MODE: no users will be deleted. Re-run with --confirm to apply. ***\n")

    print("Requesting access token ...")
    token = get_access_token(args.idcs_url, args.client_id, args.client_secret)
    print("Token acquired.\n")

    results = []
    for i, username in enumerate(usernames, start=1):
        print(f"[{i}/{len(usernames)}] Processing '{username}' ...")
        row = {"username": username, "timestamp": datetime.utcnow().isoformat() + "Z"}
        try:
            user_id = find_user_id(args.idcs_url, token, username)
            if not user_id:
                print(f"    NOT FOUND - skipping")
                row.update({"status": "NOT_FOUND", "detail": ""})
                results.append(row)
                continue

            row["user_id"] = user_id

            if dry_run:
                print(f"    Would delete user_id={user_id}")
                row.update({"status": "DRY_RUN_WOULD_DELETE", "detail": ""})
            else:
                delete_user(args.idcs_url, token, user_id, force=not args.soft_delete)
                print(f"    DELETED (id={user_id})")
                row.update({"status": "DELETED", "detail": ""})

        except requests.HTTPError as e:
            print(f"    ERROR: {e}")
            row.update({"status": "ERROR", "detail": str(e)})
        except Exception as e:
            print(f"    ERROR: {e}")
            row.update({"status": "ERROR", "detail": str(e)})

        results.append(row)
        time.sleep(args.delay)

    # Write results log
    fieldnames = ["timestamp", "username", "user_id", "status", "detail"]
    with open(args.results_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow({k: row.get(k, "") for k in fieldnames})

    print(f"\nDone. Results written to {args.results_csv}")

    # Summary
    from collections import Counter
    counts = Counter(r["status"] for r in results)
    print("Summary:", dict(counts))


if __name__ == "__main__":
    main()
