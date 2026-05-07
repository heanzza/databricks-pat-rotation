# pat-rotation

Databricks Service Principal の Personal Access Token (PAT) を **Databricks Job で自動ローテーション** する Asset Bundle。

PAT を Databricks Secret Scope に保管し、新しい PAT が必要になる前 (例: 月次) に Job を実行することで、PAT の期限切れによる障害を防ぐ。

## 動作

```
Databricks Job (scheduled)
   │
   ├─① Secret Scope: pat-rotation
   │    ├─ oauth-client-id      ← SP の OAuth Client ID
   │    ├─ oauth-client-secret  ← SP の OAuth Secret
   │    ├─ workspace-host
   │    └─ current-pat          ← 新規発行 PAT がここに保存される
   │
   ├─② OAuth M2M で SP として認証
   │
   ├─③ SP 自身が新 PAT を発行 (lifetime=N日)
   │
   ├─④ 新 PAT で SQL Warehouse SELECT 1 → 動作検証
   │
   ├─⑤ Secret Scope の current-pat を更新
   │
   └─⑥ 同 SP の旧 PAT を全失効
```

失敗時は新 PAT を即失効してロールバックするので、**旧 PAT が残ったまま** になることはない。

## 構成

| ファイル | 内容 |
|---|---|
| `databricks.yml` | Asset Bundle (Job 定義 + 変数 + targets) |
| `notebooks/rotate_quicksight_pat.py` | 本番ローテーション Notebook |
| `notebooks/rotate_quicksight_pat_test.py` | スモークテスト Notebook (OAuth 不要) |

## 前提条件

- Databricks Workspace (SQL Warehouse あり)
- Databricks CLI (>= 0.220) + `databricks-cli` profile
- Workspace の Service Principal 作成権限
- Workspace の Secret Scope 作成権限
- Token Management / Warehouse の Permission 編集権限

## セットアップ

### 1. Service Principal を作成

```bash
databricks service-principals create \
  --json '{"displayName":"pat-rotation-sp"}' \
  --profile <your-profile>
```

レスポンスから次の 2 値を控える:
- `applicationId` (UUID 形式) → 以下 `<sp-app-id>` と表記
- `id` (workspace SP id) → 以下 `<sp-id>` と表記

### 2. SP の OAuth Secret を発行

> 出力される `secret` の値は **1 度きり** しか表示されない。直後に Step 3 で保存する。

```bash
databricks service-principal-secrets-proxy create <sp-id> \
  --profile <your-profile>
```

### 3. Secret Scope を作成し OAuth を保存

```bash
databricks secrets create-scope pat-rotation --profile <your-profile>

databricks secrets put-secret pat-rotation oauth-client-id \
  --string-value "<sp-app-id>" --profile <your-profile>

databricks secrets put-secret pat-rotation oauth-client-secret \
  --string-value "<oauth-secret-from-step-2>" --profile <your-profile>

databricks secrets put-secret pat-rotation workspace-host \
  --string-value "https://<workspace>.cloud.databricks.com" --profile <your-profile>
```

### 4. SP に Secret Scope の WRITE 権限を付与

```bash
databricks secrets put-acl pat-rotation <sp-app-id> WRITE \
  --profile <your-profile>
```

### 5. SP に Token / Warehouse 権限を付与

```bash
# Token CAN_USE
databricks token-management update-permissions --json "{
  \"access_control_list\":[
    {\"service_principal_name\":\"<sp-app-id>\",\"permission_level\":\"CAN_USE\"}
  ]
}" --profile <your-profile>

# Warehouse CAN_USE (該当 Warehouse の id を <warehouse-id> に置換)
databricks permissions update warehouses <warehouse-id> --json "{
  \"access_control_list\":[
    {\"service_principal_name\":\"<sp-app-id>\",\"permission_level\":\"CAN_USE\"}
  ]
}" --profile <your-profile>
```

### 6. Bundle のターゲットを設定

`databricks.yml` の `targets.example` セクションを編集 (またはコピーして別 target を作成):

```yaml
targets:
  example:
    mode: development
    default: true
    workspace:
      host: ${var.workspace_host}
    variables:
      workspace_host:        https://<workspace>.cloud.databricks.com
      warehouse_http_path:   /sql/1.0/warehouses/<warehouse-id>
      notification_email:    you@example.com
```

### 7. Deploy + テスト

```bash
# Validate
databricks bundle validate -t example --profile <your-profile>

# Deploy (2 つの Job が作成される)
databricks bundle deploy -t example --profile <your-profile>

# まずスモークテスト (OAuth/Warehouse 接続が動くか)
databricks bundle run rotate_quicksight_pat_test -t example --profile <your-profile>

# 本番ローテーション (実際に PAT が発行され、Secret Scope に保存される)
databricks bundle run rotate_pat -t example --profile <your-profile>
```

### 8. スケジュール有効化

動作確認後、`databricks.yml` で `schedule_pause_status` を `UNPAUSED` に変更:

```yaml
variables:
  ...
  schedule_pause_status: UNPAUSED
```

または CLI 一発で:
```bash
databricks bundle deploy -t example \
  --var="schedule_pause_status=UNPAUSED" \
  --profile <your-profile>
```

## 利用方法

ローテーション後の PAT を任意の Notebook / Job から取得:

```python
pat = dbutils.secrets.get(scope="pat-rotation", key="current-pat")
```

## 設定可能な変数 (一覧)

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `workspace_host` | (必須) | `https://<workspace>.cloud.databricks.com` |
| `warehouse_http_path` | (必須) | `/sql/1.0/warehouses/<id>` |
| `notification_email` | (必須) | Job 失敗時の通知先 |
| `secret_scope` | `pat-rotation` | Secret Scope 名 |
| `oauth_id_key` | `oauth-client-id` | Scope 内の OAuth Client ID キー |
| `oauth_secret_key` | `oauth-client-secret` | Scope 内の OAuth Secret キー |
| `host_key` | `workspace-host` | Scope 内の host キー |
| `pat_key` | `current-pat` | 発行 PAT を保存するキー |
| `lifetime_days` | `90` | PAT 有効期限 (日) |
| `pat_scopes` | `sql` | 発行する PAT の API scope (カンマ区切り)。最小権限。空文字 = SP フル権限。`authentication` は付けない |
| `schedule_cron` | `0 0 17 1 * ?` | Quartz cron — 毎月1日 17:00 UTC |
| `schedule_pause_status` | `PAUSED` | 初期は PAUSED、確認後 UNPAUSED |

## トラブルシューティング

| 症状 | 原因 | 対処 |
|---|---|---|
| `PermissionDenied: secret-scopes.secrets/put` | SP に Secret Scope WRITE 権限なし | Step 4 を実行 |
| `403 PERMISSION_DENIED` (Token API) | SP に Token CAN_USE 権限なし | Step 5 (token-management) を実行 |
| `Could not connect to Warehouse` | SP に Warehouse CAN_USE 権限なし | Step 5 (warehouses) を実行 |
| `error downloading Terraform: openpgp: key expired` | CLI の Terraform 自動 DL 失敗 | `brew install hashicorp/tap/terraform` 後、`DATABRICKS_TF_EXEC_PATH=$(which terraform)` を export |

## 設計メモ

- 新 PAT 発行 → 動作検証 → Secret Scope 更新 → 旧 PAT 失効、の順序で **失敗時の旧 PAT 残存** を保証
- 検証 / 書き込みで失敗した場合は新 PAT を即失効してロールバック
- `w.tokens.list()` は SDK 認証中の SP の PAT のみを返すので、誤って他 SP の PAT を消す事故は起きない
- PAT lifetime 90 日 / 月次ローテーションで常に 60 日以上のバッファあり

## AWS Secrets Manager 連携 (将来用)

QuickSight 等の外部 BI ツールが AWS Secrets Manager 経由で PAT を読む場合、Notebook の Step ⑤ の直後に boto3 で AWS 側にも書き込む処理を追加する。詳細は次の追加要件:

1. AWS Account に IAM Role + Secrets Manager Secret を作成
2. Databricks Unity Catalog で Service Credential を登録 (上記 IAM Role を assume)
3. Notebook 末尾に下記を追加:
   ```python
   import boto3, json
   sm = boto3.client("secretsmanager", region_name="<region>")
   sm.put_secret_value(
       SecretId="<secret-arn>",
       SecretString=json.dumps({"username":"token","password":new_value}),
   )
   ```

## 参考リンク

- [Databricks Service Principal OAuth (M2M)](https://docs.databricks.com/dev-tools/auth/oauth-m2m.html)
- [Token management API](https://docs.databricks.com/api/workspace/tokenmanagement)
- [Asset Bundles](https://docs.databricks.com/dev-tools/bundles/index.html)

## License

MIT
