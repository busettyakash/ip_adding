import re
from datetime import datetime
import pytz
import os
import sys
import time
from azure.identity import ClientSecretCredential
from azure.mgmt.cosmosdb import CosmosDBManagementClient
from azure.mgmt.cosmosdb.models import IpAddressOrRange, DatabaseAccountUpdateParameters

# ====== CONFIGURATION from Pipeline Variables ====== #
TENANT_ID = os.getenv("TENANT_ID")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
SUBSCRIPTION_ID = os.getenv("SUBSCRIPTION_ID")
RESOURCE_GROUP = os.getenv("RESOURCE_GROUP")
COSMOS_ACCOUNT = os.getenv("COSMOS_ACCOUNT")
NEW_IPS = os.getenv("NEW_IPS")      # Space-separated list of IPs
BRANCH_NAME = os.getenv("BRANCH")   # Optional: branch param from pipeline

# ====== FUNCTIONS ====== #
def validate_ip(ip: str) -> bool:
    pattern = re.compile(
        r'^((25[0-5]|2[0-4]\d|1?\d{1,2})\.){3}(25[0-5]|2[0-4]\d|1?\d{1,2})$'
    )
    return bool(pattern.match(ip))

def write_log(status: str, ip: str, details: str = ""):
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S")
    branch_info = f"[Branch: {BRANCH_NAME}]" if BRANCH_NAME else ""
    with open("added_ips.txt", "a", encoding="utf-8") as logfile:
        logfile.write(f"{now} - {status}: {ip} {details} {branch_info}\n")

def pipeline_log(level: str, ip: str, message: str):
    """
    Writes a log entry in Azure DevOps UI format.
    Levels: error | warning
    (no 'info' type in Azure DevOps)
    """
    if level not in ["error", "warning"]:
        print(f"[INFO] {message} [IP: {ip}]", flush=True)
    else:
        print(f"##vso[task.logissue type={level};]{message} [IP: {ip}]", flush=True)

# ====== MAIN ====== #
def main():
    print("========== Cosmos DB IP Update Script ==========", flush=True)

    if not NEW_IPS:
        pipeline_log("error", "None", "No IP addresses provided")
        write_log("FAILED", "None", "No IPs provided")
        sys.exit(1)

    ip_list = [ip.strip() for ip in NEW_IPS.split() if ip.strip()]
    if not ip_list:
        pipeline_log("error", "None", "Provided IP string is empty after parsing")
        write_log("FAILED", "None", "No valid IPs parsed")
        sys.exit(1)

    print(f"[DEBUG] NEW_IPS: {' '.join(ip_list)}", flush=True)
    print(f"[DEBUG] Cosmos Account: {COSMOS_ACCOUNT} | Resource Group: {RESOURCE_GROUP}", flush=True)

    invalid_ips, valid_ips = [], []
    for ip in ip_list:
        if not validate_ip(ip):
            pipeline_log("error", ip, "Invalid IP address")
            write_log("INVALID", ip, "Not added")
            invalid_ips.append(ip)
        else:
            valid_ips.append(ip)

    if not valid_ips:
        print("[FAILED] No valid IPs found. Exiting with error.", flush=True)
        sys.exit(1)

    # Authenticate
    try:
        credential = ClientSecretCredential(
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET
        )
        cosmos_client = CosmosDBManagementClient(credential, SUBSCRIPTION_ID)
    except Exception as e:
        for ip in valid_ips:
            pipeline_log("error", ip, f"Authentication failed – {e}")
            write_log("FAILED", ip, f"Auth error – {e}")
        sys.exit(1)

    added_ips, duplicate_ips = [], []

    try:
        account = cosmos_client.database_accounts.get(RESOURCE_GROUP, COSMOS_ACCOUNT)
        current_rules = account.ip_rules or []
        existing_ips = [rule.ip_address_or_range for rule in current_rules]

        new_ip_rules = []
        for ip in valid_ips:
            if ip in existing_ips:
                pipeline_log("warning", ip, "Duplicate IP – already exists")
                write_log("DUPLICATE", ip, "Already present")
                duplicate_ips.append(ip)
                continue

            print(f"[ACTION] Queued for adding: {ip}", flush=True)
            new_ip_rules.append(IpAddressOrRange(ip_address_or_range=ip))
            write_log("PENDING", ip, "Queued for update")

        if not new_ip_rules:
            print("[INFO] No new IPs to add. Exiting successfully.", flush=True)
            print(f"[SUMMARY] Valid: {len(valid_ips)} | Invalid: {len(invalid_ips)} | "
                  f"Duplicate: {len(duplicate_ips)} | Added: 0", flush=True)
            sys.exit(0)

        updated_rules = current_rules + new_ip_rules

        print(f"[ACTION] Updating Cosmos DB with {len(new_ip_rules)} new IP(s)...", flush=True)
        update_params = DatabaseAccountUpdateParameters(
            location=account.location,
            locations=account.locations,
            is_virtual_network_filter_enabled=True,
            ip_rules=updated_rules
        )

        poller = cosmos_client.database_accounts.begin_update(
            RESOURCE_GROUP, COSMOS_ACCOUNT, update_params
        )

        while not poller.done():
            print("[INFO] Cosmos DB update still in progress...", flush=True)
            time.sleep(60)

        poller.result()
        print("[SUCCESS] All valid IPs added successfully.", flush=True)

        for ip_rule in new_ip_rules:
            print(f"[INFO] Successfully added [IP: {ip_rule.ip_address_or_range}]", flush=True)
            added_ips.append(ip_rule.ip_address_or_range)
            write_log("SUCCESS", ip_rule.ip_address_or_range, "Added")

        print(f"[SUMMARY] Valid: {len(valid_ips)} | Invalid: {len(invalid_ips)} | "
              f"Duplicate: {len(duplicate_ips)} | Added: {len(added_ips)}", flush=True)

        sys.exit(0)

    except Exception as e:
        for ip in valid_ips:
            pipeline_log("error", ip, f"Update failed – {e}")
            write_log("FAILED", ip, f"Update error – {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()

