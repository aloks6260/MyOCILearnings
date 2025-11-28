This script can be used to run through a list of all Boot and Block Volumes in a given compartment and delete the backups with different options -

Here are some concrete command-line examples for the throttled script.

**Basic usage**
Dry run, see what would be deleted (no actual deletion), keep 3 backups per volume:

python oci_backup_cleanup.py -c ocid1.compartment.oc1..xxxx -k 3 --dry-run

**Actually delete, keep 5 newest backups per volume:**

python oci_backup_cleanup.py -c ocid1.compartment.oc1..xxxx -k 5

**Passing config file and profile
By default, it uses ~/.oci/config and profile DEFAULT. To override:​**

Custom config file, default profile:

python oci_backup_cleanup.py -c ocid1.compartment.oc1..xxxx -k 5 --config /home/ubuntu/.oci/prod_config

**Custom config file and custom profile:**

python oci_backup_cleanup.py -c ocid1.compartment.oc1..xxxx -k 5 --config /home/ubuntu/.oci/prod_config --profile PROD

The config file must be a standard OCI CLI-style file with entries like tenancy, user, fingerprint, key_file, and region.​

**Controlling throttling (sleep and retries)**
Keep 4 backups, sleep 1 second between each delete call:

python oci_backup_cleanup.py -c ocid1.compartment.oc1..xxxx -k 4 --sleep-between 1

**More aggressive throttling, sleep 2 seconds, and enable debug logging:**

python oci_backup_cleanup.py -c ocid1.compartment.oc1..xxxx -k 2 --sleep-between 2 --log-level DEBUG

**Limiting to boot or block backups**
Only boot volume backups:

python oci_backup_cleanup.py -c ocid1.compartment.oc1..xxxx -k 3 --boot-only

Only block volume backups:

python oci_backup_cleanup.py -c ocid1.compartment.oc1..xxxx -k 3 --block-only

**With the log file
Write detailed logs to a file:**

python oci_backup_cleanup.py -c ocid1.compartment.oc1..xxxx -k 3 --sleep-between 0.5 --log-level INFO --log-file /var/log/oci_backup_cleanup.log​

