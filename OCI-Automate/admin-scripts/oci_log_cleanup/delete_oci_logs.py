#!/usr/bin/env python3
import oci
import sys
import time

def delete_log_simple(logging_client, log_group_id, log_summary):
    log_id = log_summary.id
    log_name = log_summary.display_name
    
    try:
        print(f"Deleting '{log_name}' (OCID: {log_id})")
        
        # Delete WITHOUT if_match (optional parameter)
        response = logging_client.delete_log(
            log_group_id=log_group_id,
            log_id=log_id
            # No if_match needed - OCI Logging delete_log works without it
        )
        print(f"  ✓ Initiated (status: {response.status})")
        return True
        
    except oci.exceptions.ServiceError as e:
        print(f"  ✗ {e.code}: {e.message}")
        return False
    except Exception as e:
        print(f"  ✗ Error: {str(e)}")
        return False

def main(log_group_ocid):
    config = oci.config.from_file()
    logging_client = oci.logging.LoggingManagementClient(config)
    
    print("Listing logs...")
    response = logging_client.list_logs(log_group_id=log_group_ocid)
    logs = response.data
    
    if not logs:
        print("No logs found.")
        return
    
    print(f"Found {len(logs)} logs. Deleting...")
    success = 0
    
    for i, log_summary in enumerate(logs, 1):
        print(f"[{i}/{len(logs)}] ", end="")
        if delete_log_simple(logging_client, log_group_ocid, log_summary):
            success += 1
        time.sleep(0.5)  # Rate limiting
    
    print(f"\n✅ Complete: {success}/{len(logs)} deleted successfully.")
    print("Wait 1-2 min, then verify: oci logging log list --log-group-id " + log_group_ocid)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 delete_oci_logs_final.py <log-group-OCID>")
        sys.exit(1)
    main(sys.argv[1])
