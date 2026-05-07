# Databricks notebook source
# MAGIC %md
# MAGIC # Rotate QuickSight PAT (Production / Databricks Secret Scope 版)
# MAGIC
# MAGIC Service Principal の PAT を月次でローテーションする本番 Notebook。
# MAGIC OAuth Secret と発行済 PAT は **Databricks Secret Scope** に保管する。
# MAGIC
# MAGIC ## 動作
# MAGIC 1. Secret Scope から SP の OAuth Client ID / Secret を取得
# MAGIC 2. OAuth M2M で Databricks SDK 認証 (= SP として動作)
# MAGIC 3. SP 自身の新 PAT を発行
# MAGIC 4. 新 PAT で SQL Warehouse に SELECT 1 → 動作検証
# MAGIC 5. Secret Scope の `pat` キーを更新
# MAGIC 6. 同 SP の旧 PAT を全失効
# MAGIC 7. 失敗時は新 PAT を即失効してロールバック

# COMMAND ----------

# MAGIC %pip install -q databricks-sdk databricks-sql-connector
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import os
from datetime import datetime, timezone

from databricks.sdk import WorkspaceClient
from databricks import sql as dbsql

# COMMAND ----------

dbutils.widgets.text("secret_scope",        "pat-rotation")
dbutils.widgets.text("oauth_id_key",        "oauth-client-id")
dbutils.widgets.text("oauth_secret_key",    "oauth-client-secret")
dbutils.widgets.text("host_key",            "workspace-host")
dbutils.widgets.text("pat_key",             "current-pat")
dbutils.widgets.text("warehouse_http_path", "/sql/1.0/warehouses/<warehouse-id>")
dbutils.widgets.text("lifetime_days",       "90")
dbutils.widgets.text("pat_scopes",          "sql")

SCOPE          = dbutils.widgets.get("secret_scope")
OAUTH_ID_KEY   = dbutils.widgets.get("oauth_id_key")
OAUTH_SECRET_KEY = dbutils.widgets.get("oauth_secret_key")
HOST_KEY       = dbutils.widgets.get("host_key")
PAT_KEY        = dbutils.widgets.get("pat_key")
HTTP_PATH      = dbutils.widgets.get("warehouse_http_path")
LIFETIME_DAYS  = int(dbutils.widgets.get("lifetime_days"))
PAT_SCOPES     = [s.strip() for s in dbutils.widgets.get("pat_scopes").split(",") if s.strip()]

print(f"scope:         {SCOPE}")
print(f"http_path:     {HTTP_PATH}")
print(f"lifetime_days: {LIFETIME_DAYS}")
print(f"pat_scopes:    {PAT_SCOPES or '(none → full SP permissions)'}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Secret Scope から OAuth → SDK 認証

# COMMAND ----------

host          = dbutils.secrets.get(scope=SCOPE, key=HOST_KEY)
client_id     = dbutils.secrets.get(scope=SCOPE, key=OAUTH_ID_KEY)
client_secret = dbutils.secrets.get(scope=SCOPE, key=OAUTH_SECRET_KEY)

os.environ["DATABRICKS_HOST"]          = host
os.environ["DATABRICKS_CLIENT_ID"]     = client_id
os.environ["DATABRICKS_CLIENT_SECRET"] = client_secret

w = WorkspaceClient()
me = w.current_user.me()
print(f"Authenticated as SP: {me.display_name} (id={me.id})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: SP 自身の新 PAT を発行

# COMMAND ----------

# Use raw REST so `scopes` is sent regardless of databricks-sdk version.
body = {
    "lifetime_seconds": LIFETIME_DAYS * 24 * 3600,
    "comment": f"Rotated {datetime.now(timezone.utc).isoformat()}",
}
if PAT_SCOPES:
    body["scopes"] = PAT_SCOPES
resp = w.api_client.do("POST", "/api/2.0/token/create", body=body)
new_id = resp["token_info"]["token_id"]
new_value = resp["token_value"]
print(f"Issued new PAT  token_id={new_id}  expires_in={LIFETIME_DAYS}d  scopes={PAT_SCOPES or 'ALL'}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: 検証 → Secret Scope 更新 → 旧 PAT 失効

# COMMAND ----------

try:
    # 3-1. 新 PAT で Warehouse に SELECT 1
    server_hostname = host.replace("https://", "").rstrip("/")
    with dbsql.connect(
        server_hostname=server_hostname,
        http_path=HTTP_PATH,
        access_token=new_value,
    ) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
    print("Verified new PAT against Warehouse")

    # 3-2. Secret Scope の PAT を更新
    w.secrets.put_secret(scope=SCOPE, key=PAT_KEY, string_value=new_value)
    print(f"Updated secret: {SCOPE}/{PAT_KEY}")

    # 3-3. 同 SP の旧 PAT を全失効
    revoked = 0
    for t in w.tokens.list():
        if t.token_id != new_id:
            w.tokens.delete(token_id=t.token_id)
            revoked += 1
    print(f"Revoked {revoked} old token(s)")

    print(f"OK rotated  new_id={new_id}  revoked={revoked}")

except Exception as e:
    print(f"FAILED: {e}")
    try:
        w.tokens.delete(token_id=new_id)
        print(f"Rolled back: revoked new token {new_id}")
    except Exception as cleanup_err:
        print(f"Rollback failed: {cleanup_err}")
    raise
finally:
    new_value = None

# COMMAND ----------

# MAGIC %md
# MAGIC ## 完了
# MAGIC 新 PAT は `pat-rotation/current-pat` から取得可能:
# MAGIC ```python
# MAGIC dbutils.secrets.get(scope="pat-rotation", key="current-pat")
# MAGIC ```
