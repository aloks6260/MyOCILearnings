Steps to run it in OCI Cloud Shell:
1. Create a Confidential Application in IDCS (one-time setup)

In your Identity Domain console → Applications → Add Application → Confidential Application
Under "Client Configuration", enable "Configure this application as a client now"
Grant it the "Identity Domain Administrator" or at least "User Administrator" app role (needed to search/delete users)
Grant scope for the Identity Domain APIs (usually the default urn:opc:idm:__myscopes__ works with client_credentials grant)
Activate the app, then note the Client ID and Client Secret

2. Prepare your CSV
Must have a header row with a username column, e.g. users_to_delete.csv:
username
jdoe
asmith
bwayne

4. In OCI Cloud Shell
bash# Upload delete_idcs_users.py and users_to_delete.csv via Cloud Shell's upload button (⋮ menu)

pip3 install --user requests   # usually already available in Cloud Shell

4. Dry run first (no deletions happen)
bashpython3 delete_idcs_users.py \
  --csv users_to_delete.csv \
  --idcs-url https://idcs-xxxxxxxxxxxxxxxx.identity.oraclecloud.com \
  --client-id <YOUR_CLIENT_ID> \
  --client-secret <YOUR_CLIENT_SECRET>
Review the console output and delete_results.csv — it'll show DRY_RUN_WOULD_DELETE or NOT_FOUND for each user.

6. Actually delete, once you're confident
bashpython3 delete_idcs_users.py \
  --csv users_to_delete.csv \
  --idcs-url https://idcs-xxxxxxxxxxxxxxxx.identity.oraclecloud.com \
  --client-id <YOUR_CLIENT_ID> \
  --client-secret <YOUR_CLIENT_SECRET> \
  --confirm
Notes:

By default this does a permanent delete (forceDelete=true). Add --soft-delete if you want to deactivate instead of permanently removing.
--delay (default 0.3s) throttles calls between users to avoid hitting API rate limits — bump it up if you have a large list.
Results (per-user status: DELETED, NOT_FOUND, ERROR) get logged to delete_results.csv for an audit trail.
Avoid passing the client secret as a bare CLI arg on a shared system if you're security-conscious — you can instead hardcode it as an environment variable and read it with os.environ if you'd like; happy to adjust the script for that.


<img width="717" height="270" alt="image" src="https://github.com/user-attachments/assets/b1defbf3-2fbe-4939-99e7-3524ad3ce171" />
