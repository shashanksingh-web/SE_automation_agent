# Product Cohort — Postman auth

## Import

1. `Product Cohort — -sps-v1-fp.postman_collection.json`
2. `Product Cohort.postman_environment.json`
3. Select env **Product Cohort**

## Env setup (URLs + auth only)

| Key | Example |
|-----|---------|
| `PC_feature_url` | `https://saas-platform-service.api.dehat.net` |
| `PC_local_url` | `http://127.0.0.1:8000` |
| `go_admin_username` | your Go Admin email |
| `go_admin_password` | your password |
| `fp_session` | leave empty — filled by Sign in |

**Important:** paste credentials into **Current value**, not Initial only.

## Run order

1. **01 · Auth → Go Admin Sign in (save session)** — Send once
2. Console: `Saved fp_session = <uuid>`
3. **Auth probe — focus-product options** — should return 200
4. Step 1 → 2A → 2B → 3 (edit sample IDs in URL/body as needed)

Role required: `product_cohort_user`.

UI: `{PC_feature_url}/product-cohort`
