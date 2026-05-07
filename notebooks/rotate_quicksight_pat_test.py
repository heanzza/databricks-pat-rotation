# Databricks notebook source
# MAGIC %md
# MAGIC # Rotate QuickSight PAT (TEST mode)
# MAGIC
# MAGIC AWS Secrets Manager / SP OAuth が未整備でも実行できるテスト版。
# MAGIC Job 実行ユーザー (= Notebook 実行者) のコンテキストで以下を検証する:
# MAGIC
# MAGIC 1. SDK 認証 (Job クラスタの実行ユーザー)
# MAGIC 2. PAT 発行 (`w.tokens.create`)
# MAGIC 3. 新 PAT で SQL Warehouse に `SELECT 1` (動作検証)
# MAGIC 4. 自分自身の PAT を一覧表示
# MAGIC 5. このテストで作った PAT を **必ず削除** (掃除)
# MAGIC
# MAGIC AWS 連携は **行わない**。本番版に進む前のスモークテスト用。

# COMMAND ----------

# MAGIC %pip install -q databricks-sdk databricks-sql-connector
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import os
from datetime import datetime, timezone

from databricks.sdk import WorkspaceClient
from databricks import sql as dbsql

# COMMAND ----------

dbutils.widgets.text("warehouse_http_path", "/sql/1.0/warehouses/<warehouse-id>")
dbutils.widgets.text("workspace_host",      "https://<workspace>.cloud.databricks.com")
dbutils.widgets.text("test_lifetime_seconds", "600")  # 10分: 削除失敗時の保険
dbutils.widgets.text("pat_scopes",          "sql")

HTTP_PATH = dbutils.widgets.get("warehouse_http_path")
HOST      = dbutils.widgets.get("workspace_host")
LIFETIME  = int(dbutils.widgets.get("test_lifetime_seconds"))
PAT_SCOPES = [s.strip() for s in dbutils.widgets.get("pat_scopes").split(",") if s.strip()]

print(f"workspace_host: {HOST}")
print(f"http_path:      {HTTP_PATH}")
print(f"test lifetime:  {LIFETIME}s")
print(f"pat_scopes:     {PAT_SCOPES or '(none → caller full perms)'}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: SDK 認証 (Notebook 実行者として)

# COMMAND ----------

# Job クラスタ上では、Job 実行者の認証が自動的に使われる
w = WorkspaceClient()
me = w.current_user.me()
print(f"Authenticated as: {me.display_name} (id={me.id})")
print(f"Email: {me.emails[0].value if me.emails else 'N/A'}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: テスト用 PAT を発行

# COMMAND ----------

# Use raw REST so `scopes` is sent regardless of databricks-sdk version.
body = {
    "lifetime_seconds": LIFETIME,
    "comment": f"TEST PAT rotation smoke test {datetime.now(timezone.utc).isoformat()}",
}
if PAT_SCOPES:
    body["scopes"] = PAT_SCOPES
resp = w.api_client.do("POST", "/api/2.0/token/create", body=body)
new_id = resp["token_info"]["token_id"]
new_value = resp["token_value"]
print(f"Issued test PAT  token_id={new_id}  expires_in={LIFETIME}s  scopes={PAT_SCOPES or 'ALL'}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: 新 PAT で SQL Warehouse に SELECT 1

# COMMAND ----------

verification_ok = False
try:
    host = HOST.replace("https://", "").rstrip("/")
    with dbsql.connect(
        server_hostname=host,
        http_path=HTTP_PATH,
        access_token=new_value,
    ) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 AS smoke_test")
        row = cur.fetchone()
        print(f"Warehouse returned: {row}")
        verification_ok = True
    print("Verified new PAT against Warehouse")
except Exception as e:
    print(f"Verification FAILED: {e}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: 自分自身の PAT 一覧
# MAGIC `w.tokens.list()` は呼び出し元自身の PAT のみ返す。
# MAGIC （他人の PAT に影響しないことを確認）

# COMMAND ----------

own_tokens = list(w.tokens.list())
print(f"Own tokens count: {len(own_tokens)}")
for t in own_tokens:
    marker = "← NEW (this run)" if t.token_id == new_id else ""
    print(f"  token_id={t.token_id}  comment={t.comment[:60]!r:65}  {marker}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5: テストで作った PAT を必ず削除

# COMMAND ----------

try:
    w.tokens.delete(token_id=new_id)
    print(f"Cleaned up test PAT: {new_id}")
except Exception as e:
    print(f"WARNING: cleanup failed for {new_id}: {e}")
    print("Token will auto-expire in lifetime_seconds.")

# トークン値をメモリから消す
new_value = None

# COMMAND ----------

# MAGIC %md
# MAGIC ## 結果

# COMMAND ----------

if not verification_ok:
    raise RuntimeError("Smoke test failed: warehouse SELECT 1 did not succeed")

print("=" * 60)
print("SMOKE TEST PASSED")
print("=" * 60)
print("Validated:")
print("  - SDK auth on Job cluster")
print("  - Token create / list / delete")
print("  - Token usable against SQL Warehouse")
print("Next step: configure AWS Secrets Manager + SP OAuth and run the prod notebook.")
