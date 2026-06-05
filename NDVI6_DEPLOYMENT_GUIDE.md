# NDVI6 Cloud Run Deployment Guide

**Last Updated:** February 13, 2026  
**Project:** gen-lang-client-0709485529  
**Project Number:** 258557095482  
**Service:** ndvi6-analysis-api  
**Region:** us-central1  
**Current Live URL:** `https://ndvi6-analysis-api-258557095482.us-central1.run.app`  
**Frontend:** Vercel → `lleo.earth` (auto-deploys from `main` branch of `LLEO2-MVP` repo)  

---

## ⚠️ CRITICAL LESSONS LEARNED (Read First!)

These are real problems we hit during deployment. Read before deploying:

### 1. `--allow-unauthenticated` Silently Fails in Cloud Build
Even though `cloudbuild.yaml` includes `--allow-unauthenticated`, Cloud Build may **silently ignore it** if the Cloud Build service account doesn't have `roles/run.admin`. The service deploys successfully but **requires authentication** (returns 403 to all requests).

**Fix:** Always run this AFTER deployment:
```powershell
gcloud run services add-iam-policy-binding ndvi6-analysis-api --region=us-central1 --member="allUsers" --role="roles/run.invoker"
```

### 2. Don't Use Synchronous `gcloud builds submit` in Terminals You Might Close
If the terminal is interrupted/closed during a synchronous build, the build gets **CANCELLED**. Use `--async`:
```powershell
gcloud builds submit --config cloudbuild.yaml --async
```
Then check status with:
```powershell
gcloud builds list --limit=3
gcloud builds describe BUILD_ID --format="value(status)"
```

### 3. Deleting & Recreating a Cloud Run Service Changes the URL
If you delete a Cloud Run service and recreate it, **the URL changes**. The old URL format was `ndvi6-analysis-api-wdkuwjzm3q-uc.a.run.app`, the new one is `ndvi6-analysis-api-258557095482.us-central1.run.app`. You must update:
- Frontend `index.ts` → `GCP_BACKEND_URL`
- Stripe webhook endpoint URL
- Any bookmarks or documentation

### 4. Cloud Run Multi-Instance State Loss
Cloud Run runs **8+ instances** (min-instances=8). Each instance has its own memory. If conversation state is only saved in-memory and not persisted to Supabase, the next request may hit a different instance and **lose all context**. This was fixed in the code — don't revert the `save_message()` and `apply_decision_to_state()` persistence fixes in `conversation_service.py`.

### 5. `min-instances` Resets to 0 When Service is Recreated
Even if `cloudbuild.yaml` specifies `--min-instances 8`, verify after deployment:
```powershell
gcloud run services describe ndvi6-analysis-api --region=us-central1 --format="value(spec.template.spec.containerConcurrency)"
```

### 6. `serviceUsageConsumer` Role is Required
Without `roles/serviceusage.serviceUsageConsumer` on the service account, Gemini API calls fail with permission errors. This role is easy to forget.

---

## What's NEW vs REUSED

| Resource | Status | Name |
|----------|--------|------|
| Cloud Run service | **NEW** | `ndvi6-analysis-api` |
| Docker repo | **NEW** | `ndvi6-docker` |
| Service account | **NEW** | `ndvi6-service-account` |
| Storage bucket | **REUSED** | `ndvi3-analysis-storage-bucket` |
| Cloud SQL | **REUSED** | `ndvi3-postgres-instance` / `ndvi3_analysis` / `ndvi3_user` |
| Supabase | **REUSED** | `quvbsftdyaxjqlwnjtxv.supabase.co` |
| All GCP Secrets | **REUSED** | (stripe-pro-price-id updated) |

---

## Current Cloud Run Configuration

| Setting | Value |
|---------|-------|
| Memory | 16Gi |
| CPU | 4 |
| Min instances | 8 |
| Max instances | 25 |
| Concurrency | 80 |
| Timeout | 3600s |
| Build machine | E2_HIGHCPU_8 |
| Build timeout | 1800s (30 min) |

---

## Pre-Flight: Verify Existing Secrets

Before deploying, verify all your secrets exist in GCP Secret Manager:

```powershell
# List all secrets
gcloud secrets list --project=gen-lang-client-0709485529
```

**All required secrets (verify each exists):**
```powershell
gcloud secrets versions access latest --secret=ndvi3-db-password
gcloud secrets versions access latest --secret=stripe-secret-key
gcloud secrets versions access latest --secret=stripe-pro-price-id
gcloud secrets versions access latest --secret=stripe-webhook-secret
gcloud secrets versions access latest --secret=openweather-api-key
gcloud secrets versions access latest --secret=google-places-key
gcloud secrets versions access latest --secret=supabase-service-role-key
gcloud secrets versions access latest --secret=resend-api-key
gcloud secrets versions access latest --secret=earth-engine-key
gcloud secrets versions access latest --secret=ndvi3-admin-key
gcloud secrets versions access latest --secret=google-earth-engine-credentials
gcloud secrets versions access latest --secret=gemini-api-key
```

If `gemini-api-key` does NOT exist, create it:
```powershell
# Replace YOUR_GEMINI_API_KEY with your actual key
echo YOUR_GEMINI_API_KEY | gcloud secrets create gemini-api-key --data-file=-
```

---

## Step 1: Set Project

```powershell
gcloud config set project gen-lang-client-0709485529
```

**Expected output:** `Updated property [core/project].`

---

## Step 2: Enable APIs (First-Time Only)

Most are already enabled from previous deployments. Safe to re-run:

```powershell
gcloud services enable run.googleapis.com sql-component.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com storage-component.googleapis.com iam.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com earthengine.googleapis.com
```

**Expected output:** `Operation "..." finished successfully.` (or "already enabled")

---

## Step 3: Create Artifact Registry Repo (First-Time Only)

```powershell
gcloud artifacts repositories create ndvi6-docker --repository-format=docker --location=us-central1 --description="Docker repository for NDVI6 API"
```

**Expected output:** `Created repository [ndvi6-docker].`  
**If error "already exists":** That's fine, skip.

---

## Step 4: Create Service Account (First-Time Only)

```powershell
gcloud iam service-accounts create ndvi6-service-account --description="Service account for NDVI6 Analysis API" --display-name="NDVI6 Service Account"
```

**Expected output:** `Created service account [ndvi6-service-account].`  
**If error "already exists":** That's fine, skip.

---

## Step 5: Grant IAM Roles (First-Time Only)

Run each command one at a time:

```powershell
gcloud projects add-iam-policy-binding gen-lang-client-0709485529 --member="serviceAccount:ndvi6-service-account@gen-lang-client-0709485529.iam.gserviceaccount.com" --role="roles/cloudsql.client"
```

```powershell
gcloud projects add-iam-policy-binding gen-lang-client-0709485529 --member="serviceAccount:ndvi6-service-account@gen-lang-client-0709485529.iam.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
```

```powershell
gcloud projects add-iam-policy-binding gen-lang-client-0709485529 --member="serviceAccount:ndvi6-service-account@gen-lang-client-0709485529.iam.gserviceaccount.com" --role="roles/storage.admin"
```

```powershell
gcloud projects add-iam-policy-binding gen-lang-client-0709485529 --member="serviceAccount:ndvi6-service-account@gen-lang-client-0709485529.iam.gserviceaccount.com" --role="roles/cloudtrace.agent"
```

```powershell
gcloud projects add-iam-policy-binding gen-lang-client-0709485529 --member="serviceAccount:ndvi6-service-account@gen-lang-client-0709485529.iam.gserviceaccount.com" --role="roles/logging.logWriter"
```

```powershell
gcloud projects add-iam-policy-binding gen-lang-client-0709485529 --member="serviceAccount:ndvi6-service-account@gen-lang-client-0709485529.iam.gserviceaccount.com" --role="roles/monitoring.metricWriter"
```

```powershell
gcloud projects add-iam-policy-binding gen-lang-client-0709485529 --member="serviceAccount:ndvi6-service-account@gen-lang-client-0709485529.iam.gserviceaccount.com" --role="roles/earthengine.writer"
```

```powershell
gcloud projects add-iam-policy-binding gen-lang-client-0709485529 --member="serviceAccount:ndvi6-service-account@gen-lang-client-0709485529.iam.gserviceaccount.com" --role="roles/aiplatform.user"
```

```powershell
gcloud projects add-iam-policy-binding gen-lang-client-0709485529 --member="serviceAccount:ndvi6-service-account@gen-lang-client-0709485529.iam.gserviceaccount.com" --role="roles/serviceusage.serviceUsageConsumer"
```

**Also grant Cloud Build SA the run.admin role** (needed for `--allow-unauthenticated` to work):
```powershell
gcloud projects add-iam-policy-binding gen-lang-client-0709485529 --member="serviceAccount:258557095482@cloudbuild.gserviceaccount.com" --role="roles/run.admin"
```

```powershell
gcloud projects add-iam-policy-binding gen-lang-client-0709485529 --member="serviceAccount:258557095482@cloudbuild.gserviceaccount.com" --role="roles/artifactregistry.writer"
```

**Expected output:** Each command prints the updated IAM policy (long JSON). That's normal.

---

## Step 6: Update Stripe Price ID Secret (If Changed)

Current active price: `price_1T04cyG0juUNsGnR3QbsXguf` ($1/month Pro plan)

```powershell
echo price_1T04cyG0juUNsGnR3QbsXguf | gcloud secrets versions add stripe-pro-price-id --data-file=-
```

**Expected output:** `Created version [X] of the secret [stripe-pro-price-id].`

---

## Step 7: NO New Bucket or Postgres Needed

We reuse existing infrastructure:
- ✅ **Bucket:** `ndvi3-analysis-storage-bucket` (no password — IAM-controlled)
- ✅ **Postgres:** `ndvi3-postgres-instance` / DB: `ndvi3_analysis` / User: `ndvi3_user`
- ✅ **Password:** stored in secret `ndvi3-db-password`

**Nothing to do here.** Skip to Step 8.

---

## Step 8: Build & Deploy with Cloud Build

Navigate to the backend folder and submit:

```powershell
cd C:\Saeid2\agents_SA\8_GeoLLM_test_Supabase\satellite-backend-2
```

### Option A: Async build (RECOMMENDED — won't cancel if terminal closes)
```powershell
gcloud builds submit --config cloudbuild.yaml --async
```
Then monitor with:
```powershell
gcloud builds list --limit=3
# When you see the build ID:
gcloud builds describe BUILD_ID --format="value(status)"
# Watch logs:
gcloud builds log BUILD_ID --stream
```

### Option B: Synchronous build (blocks terminal — will cancel if you close it!)
```powershell
gcloud builds submit --config cloudbuild.yaml
```

⏳ **This takes 5-10 minutes.** Watch the output for `SUCCESS`.

**Expected output (at the end):**
```
DONE
------------------------------------------------------------------------------------
ID          CREATE_TIME                DURATION  SOURCE    STATUS
xxxxxxxx    2026-02-12T...             Xm Xs     ...       SUCCESS
```

### Common Errors at This Step:

| Error | Fix |
|-------|-----|
| `denied: Permission "artifactregistry.repositories.uploadArtifacts"` | Cloud Build SA needs Artifact Registry Writer role (see Step 5) |
| `Secret version not found` | A secret doesn't exist. Check Pre-Flight section above. |
| `Dockerfile not found` | Make sure you're in `satellite-backend-2/` directory |
| `Build timeout` | The 1800s (30min) timeout should be enough. If not, increase in cloudbuild.yaml |
| `Build CANCELLED` | You closed the terminal during sync build. Use `--async` next time. Re-submit. |

---

## Step 9: Grant Public Access (⚠️ CRITICAL — DO THIS EVERY TIME)

**This step is required after EVERY deployment**, especially if the service was newly created or recreated. Even if `cloudbuild.yaml` has `--allow-unauthenticated`, it may silently fail.

```powershell
gcloud run services add-iam-policy-binding ndvi6-analysis-api --region=us-central1 --member="allUsers" --role="roles/run.invoker"
```

**Expected output:** Updated IAM policy printed.

**How to verify it worked:**
```powershell
curl https://ndvi6-analysis-api-258557095482.us-central1.run.app/health
```
If you get `{"status":"healthy",...}` → ✅ public access works.  
If you get `403 Forbidden` → ❌ re-run the IAM binding command above.

---

## Step 10: Get & Verify Your Cloud Run URL

```powershell
gcloud run services describe ndvi6-analysis-api --region=us-central1 --format="value(status.url)"
```

**Current URL (as of Feb 13, 2026):**
```
https://ndvi6-analysis-api-258557095482.us-central1.run.app
```

⚠️ **If the URL changed** (happens when you delete and recreate the service), you must update:
1. Frontend `index.ts` (Step 13)
2. Stripe webhook (Step 14)

---

## Step 11: Health Check & Verify Configuration

```powershell
# Basic health check
curl https://ndvi6-analysis-api-258557095482.us-central1.run.app/health
```

**Expected output:** `{"status":"healthy",...}` or similar JSON.

**Verify the service configuration is correct:**
```powershell
gcloud run services describe ndvi6-analysis-api --region=us-central1 --format="yaml(spec.template.spec)"
```

**Check min-instances didn't reset to 0:**
```powershell
gcloud run services describe ndvi6-analysis-api --region=us-central1 --format="value(spec.template.metadata.annotations)"
```
Look for `autoscaling.knative.dev/minScale: '8'`. If it says `0`, update manually:
```powershell
gcloud run services update ndvi6-analysis-api --region=us-central1 --min-instances=8
```

---

## Step 12: Set Up Warmup Scheduler (First-Time Only)

```powershell
gcloud scheduler jobs create http ndvi6-warmup --schedule="*/5 * * * *" --http-method=GET --uri="https://ndvi6-analysis-api-258557095482.us-central1.run.app/health" --oidc-service-account-email=ndvi6-service-account@gen-lang-client-0709485529.iam.gserviceaccount.com --oidc-token-audience="https://ndvi6-analysis-api-258557095482.us-central1.run.app"
```

**Expected output:** `Created job [ndvi6-warmup].`  
**If error "already exists":** Update it instead:
```powershell
gcloud scheduler jobs update http ndvi6-warmup --schedule="*/5 * * * *" --http-method=GET --uri="https://ndvi6-analysis-api-258557095482.us-central1.run.app/health" --oidc-service-account-email=ndvi6-service-account@gen-lang-client-0709485529.iam.gserviceaccount.com --oidc-token-audience="https://ndvi6-analysis-api-258557095482.us-central1.run.app"
```

---

## Step 13: Update Frontend Backend URL

Edit `Satellite-frontend-2/src/types/index.ts`:

```typescript
// ⚡ SWITCH BACKEND: Comment/uncomment ONE of these lines
//export const GCP_BACKEND_URL = 'http://localhost:8080';                                          // LOCAL
//export const GCP_BACKEND_URL = 'https://ndvi5-analysis-api-258557095482.us-central1.run.app';    // CLOUD (ndvi5 - old)
export const GCP_BACKEND_URL = 'https://ndvi6-analysis-api-258557095482.us-central1.run.app';      // CLOUD (ndvi6 - current)
```

---

## Step 14: Update Stripe Webhook

After deployment, Stripe needs to know your new backend URL:

1. Go to [Stripe Dashboard → Developers → Webhooks](https://dashboard.stripe.com/webhooks)
2. Click **+ Add endpoint** (or edit existing ndvi6 endpoint)
3. **Endpoint URL:** `https://ndvi6-analysis-api-258557095482.us-central1.run.app/api/stripe/webhook`
4. **Select events:**
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.payment_succeeded`
   - `invoice.payment_failed`
5. Click **Add endpoint**
6. Copy the **Signing secret** (`whsec_...`)
7. Update in GCP Secret Manager:

```powershell
echo whsec_YOUR_NEW_SIGNING_SECRET | gcloud secrets versions add stripe-webhook-secret --data-file=-
```

8. **Redeploy** to pick up the new webhook secret:
```powershell
cd C:\Saeid2\agents_SA\8_GeoLLM_test_Supabase\satellite-backend-2
gcloud builds submit --config cloudbuild.yaml --async
```

⚠️ **Don't forget Step 9 again** after this redeploy!

---

## Step 15: Deploy Frontend to Vercel (Production)

The frontend auto-deploys to **lleo.earth** when you push to the `main` branch of the `LLEO2-MVP` GitHub repo.

```powershell
cd C:\Saeid2\agents_SA\8_GeoLLM_test_Supabase\Satellite-frontend-2
git add -A
git commit -m "Update backend URL to ndvi6"
git push origin main
```

**Vercel will automatically:**
- Detect the push to `main`
- Build the React app
- Deploy to `lleo.earth` and `lleo-mvp.vercel.app`
- Takes ~1-2 minutes

**Verify:** Visit https://lleo.earth after a couple minutes.

---

## Quick Redeploy Checklist (Code Changes Only)

When you've made code changes and just need to redeploy (no new infrastructure):

```powershell
# 1. Navigate to backend
cd C:\Saeid2\agents_SA\8_GeoLLM_test_Supabase\satellite-backend-2

# 2. Build & deploy (async so it won't cancel)
gcloud builds submit --config cloudbuild.yaml --async

# 3. Check build status
gcloud builds list --limit=1

# 4. When build shows SUCCESS, grant public access (EVERY TIME!)
gcloud run services add-iam-policy-binding ndvi6-analysis-api --region=us-central1 --member="allUsers" --role="roles/run.invoker"

# 5. Verify health
curl https://ndvi6-analysis-api-258557095482.us-central1.run.app/health
```

---

## Troubleshooting

### Service returns 403 Forbidden
**Cause:** `--allow-unauthenticated` was silently ignored during Cloud Build.  
**Fix:**
```powershell
gcloud run services add-iam-policy-binding ndvi6-analysis-api --region=us-central1 --member="allUsers" --role="roles/run.invoker"
```

### Build keeps getting CANCELLED
**Cause:** Terminal was closed/interrupted during synchronous build.  
**Fix:** Use `--async` flag:
```powershell
gcloud builds submit --config cloudbuild.yaml --async
```

### Conversation context lost after clarification questions
**Cause:** Multi-instance Cloud Run — state not persisted to Supabase.  
**Fix:** Ensure `conversation_service.py` has these persistence calls:
- `save_message()` must call `state.add_message()` AND `conversation_mgr.update_conversation()`
- `apply_decision_to_state()` must call `conversation_mgr.update_conversation()` at the end

### Cloud Run URL changed after service recreation
**Cause:** Deleting and recreating a Cloud Run service generates a new URL.  
**Fix:** Update `Satellite-frontend-2/src/types/index.ts` with new URL, push to GitHub.

### Gemini API permission errors
**Cause:** Missing `serviceUsageConsumer` role on service account.  
**Fix:**
```powershell
gcloud projects add-iam-policy-binding gen-lang-client-0709485529 --member="serviceAccount:ndvi6-service-account@gen-lang-client-0709485529.iam.gserviceaccount.com" --role="roles/serviceusage.serviceUsageConsumer"
```

### Check Cloud Run Logs
```powershell
gcloud run services logs read ndvi6-analysis-api --region=us-central1 --limit=50
```

### Check if service is running
```powershell
gcloud run services describe ndvi6-analysis-api --region=us-central1
```

### Check Cloud Build history
```powershell
gcloud builds list --limit=5
```

### View a specific build's logs
```powershell
gcloud builds log BUILD_ID
```

### Stream live build logs
```powershell
gcloud builds log BUILD_ID --stream
```

---

## Quick Reference: All Secrets

| Secret Name | What It Is | Source |
|-------------|-----------|--------|
| `ndvi3-db-password` | PostgreSQL password | Existing (ndvi3 setup) |
| `earth-engine-key` | Earth Engine service account JSON | Existing |
| `ndvi3-admin-key` | Admin API key | Existing |
| `openweather-api-key` | OpenWeather API key | Existing |
| `google-places-key` | Google Places API key | Existing |
| `supabase-service-role-key` | Supabase service role key | Existing |
| `stripe-secret-key` | Stripe secret key (sk_live_...) | Existing |
| `stripe-webhook-secret` | Stripe webhook signing secret | **Update in Step 14** |
| `stripe-pro-price-id` | Stripe price ID for Pro plan | `price_1T04cyG0juUNsGnR3QbsXguf` |
| `resend-api-key` | Resend email API key | Existing |
| `google-earth-engine-credentials` | EE credentials JSON | Existing |
| `gemini-api-key` | Gemini API key | Verify exists (Pre-Flight) |

## Quick Reference: Environment Variables (not secrets)

| Env Var | Value |
|---------|-------|
| `GOOGLE_CLOUD_PROJECT` | `gen-lang-client-0709485529` |
| `STORAGE_BUCKET_NAME` | `ndvi3-analysis-storage-bucket` |
| `REGION` | `us-central1` |
| `DB_USER` | `ndvi3_user` |
| `DB_NAME` | `ndvi3_analysis` |
| `SUPABASE_URL` | `https://quvbsftdyaxjqlwnjtxv.supabase.co` |
| `PYTHONUNBUFFERED` | `1` |

## Quick Reference: Service Account IAM Roles

| Role | Why Needed |
|------|-----------|
| `roles/cloudsql.client` | Connect to Cloud SQL |
| `roles/secretmanager.secretAccessor` | Read secrets |
| `roles/storage.admin` | Upload maps/charts to GCS bucket |
| `roles/cloudtrace.agent` | Cloud Trace telemetry |
| `roles/logging.logWriter` | Write logs |
| `roles/monitoring.metricWriter` | Write metrics |
| `roles/earthengine.writer` | Earth Engine API access |
| `roles/aiplatform.user` | Vertex AI / Gemini access |
| `roles/serviceusage.serviceUsageConsumer` | Required for Gemini API calls |

## Quick Reference: Cloud Build SA Roles

| Role | Why Needed |
|------|-----------|
| `roles/run.admin` | Deploy to Cloud Run + set `--allow-unauthenticated` |
| `roles/artifactregistry.writer` | Push Docker images to Artifact Registry |

## Quick Reference: Key Files

| File | Purpose |
|------|---------|
| `satellite-backend-2/cloudbuild.yaml` | Cloud Build config (image, deploy, secrets, env vars) |
| `satellite-backend-2/Dockerfile` | Docker build (Python 3.11-slim, uvicorn) |
| `satellite-backend-2/main.py` | FastAPI app entry point |
| `satellite-backend-2/requirements.txt` | Python dependencies |
| `satellite-backend-2/lleo/` | LLEO v2 modular package (services, routers, agents) |
| `Satellite-frontend-2/src/types/index.ts` | Backend URL config (`GCP_BACKEND_URL`) |

## Quick Reference: URLs

| What | URL |
|------|-----|
| Backend (Cloud Run) | `https://ndvi6-analysis-api-258557095482.us-central1.run.app` |
| Frontend (Vercel) | `https://lleo.earth` |
| Frontend (Vercel preview) | `https://lleo-mvp.vercel.app` |
| Supabase | `https://quvbsftdyaxjqlwnjtxv.supabase.co` |
| Stripe Dashboard | `https://dashboard.stripe.com` |
| GCP Console | `https://console.cloud.google.com/run?project=gen-lang-client-0709485529` |
| GitHub (Frontend) | `https://github.com/saeedashraf/LLEO2-MVP` |

## Local Development

To run the backend locally:
```powershell
Push-Location C:\Saeid2\agents_SA\8_GeoLLM_test_Supabase\satellite-backend-2
venv\Scripts\python.exe run.py
```

Then switch the frontend to use localhost:
```typescript
// In Satellite-frontend-2/src/types/index.ts:
export const GCP_BACKEND_URL = 'http://localhost:8080';    // LOCAL
```
