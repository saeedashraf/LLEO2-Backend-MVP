"""
LLEO v2 — Stripe billing & email endpoints.

POST /api/stripe/webhook
POST /api/billing/create-checkout-session
POST /api/billing/create-portal-session
POST /api/billing/cancel-subscription
POST /api/billing/reactivate-subscription
POST /api/email/welcome
POST /api/email/password-reset
GET  /credits/{user_id}
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request

from ..stripe_handler import stripe_handler
from ..supabase_client import supabase_client

router = APIRouter(tags=["billing"])


# ── POST /api/stripe/webhook ─────────────────────────────────────────────────

@router.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    """Stripe webhook — handles subscription and payment events."""
    try:
        payload = await request.body()
        sig_header = request.headers.get("stripe-signature")

        if not sig_header:
            raise HTTPException(status_code=400, detail="Missing stripe-signature header")

        event = stripe_handler.verify_webhook(payload, sig_header)
        if not event:
            raise HTTPException(status_code=400, detail="Invalid webhook signature")

        event_type = event["type"]
        event_data = event["data"]["object"]
        print(f"📨 Stripe webhook: {event_type}")

        handlers = {
            "customer.subscription.created": stripe_handler.handle_subscription_created,
            "customer.subscription.updated": stripe_handler.handle_subscription_updated,
            "customer.subscription.deleted": stripe_handler.handle_subscription_deleted,
            "invoice.payment_succeeded": stripe_handler.handle_payment_succeeded,
            "invoice.payment_failed": stripe_handler.handle_payment_failed,
        }
        handler = handlers.get(event_type)
        if handler:
            handler(event_data)
        else:
            print(f"ℹ️  Unhandled event type: {event_type}")

        return {"status": "success", "event_type": event_type}

    except HTTPException:
        raise
    except Exception as exc:
        print(f"❌ Webhook error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ── POST /api/billing/create-checkout-session ─────────────────────────────────

@router.post("/api/billing/create-checkout-session")
async def create_checkout_session(
    user_email: str = Form(...),
    user_id: str = Form(...),
):
    """Create Stripe Checkout session for Pro subscription."""
    try:
        checkout_url = stripe_handler.create_checkout_session(user_email, user_id)
        return {"checkout_url": checkout_url}
    except ValueError as exc:
        # User-facing validation (e.g. already subscribed)
        print(f"⚠️ Checkout blocked for {user_email}: {exc}")
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        # Server-side config or Stripe API errors (already logged inside handler)
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        print(f"❌ Checkout session unexpected error for {user_email}: {exc}")
        raise HTTPException(status_code=500, detail=f"Could not start checkout: {exc}")


# ── POST /api/billing/create-portal-session ──────────────────────────────────

@router.post("/api/billing/create-portal-session")
async def create_portal_session(user_email: str = Form(...)):
    """Create Stripe Customer Portal session for subscription management."""
    try:
        portal_url = stripe_handler.create_customer_portal_session(user_email)
        if not portal_url:
            raise HTTPException(status_code=404, detail="Customer not found")
        return {"portal_url": portal_url}
    except HTTPException:
        raise
    except Exception as exc:
        print(f"❌ Portal session error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ── GET /credits/{user_id} ───────────────────────────────────────────────────

@router.post("/api/billing/cancel-subscription")
async def cancel_subscription(user_email: str = Form(...)):
    """Cancel the user's active subscription at end of billing period."""
    try:
        result = stripe_handler.cancel_subscription(user_email)
        if not result["success"]:
            raise HTTPException(status_code=400, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception as exc:
        print(f"❌ Cancel subscription error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/api/billing/reactivate-subscription")
async def reactivate_subscription(user_email: str = Form(...)):
    """Reactivate a subscription that was set to cancel at period end."""
    try:
        result = stripe_handler.reactivate_subscription(user_email)
        if not result["success"]:
            raise HTTPException(status_code=400, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception as exc:
        print(f"❌ Reactivate subscription error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/credits/{user_id}")
async def get_user_credits(user_id: str):
    """Return the user's remaining credits from Supabase."""
    try:
        resp = (
            supabase_client.client
            .table("user_credits")
            .select("*")
            .eq("user_id", user_id)
            .single()
            .execute()
        )
        if resp.data:
            return resp.data
        return {"user_id": user_id, "credits": 0}
    except Exception as exc:
        print(f"⚠️  Credits lookup: {exc}")
        return {"user_id": user_id, "credits": 0}


# ── GET /api/billing/invoices ────────────────────────────────────────────────

@router.get("/api/billing/invoices")
async def get_invoices(user_email: str):
    """Fetch Stripe invoices for a user."""
    try:
        import stripe
        customers = stripe.Customer.list(email=user_email, limit=1)
        if not customers.data:
            return {"invoices": []}

        customer = customers.data[0]
        invoices = stripe.Invoice.list(customer=customer.id, limit=20)

        result = []
        for inv in invoices.data:
            result.append({
                "id": inv.id,
                "status": inv.status,
                "amount": inv.amount_paid / 100 if inv.amount_paid else 0,
                "currency": (inv.currency or "usd").upper(),
                "created": inv.created,
                "hosted_invoice_url": getattr(inv, 'hosted_invoice_url', None),
                "invoice_pdf": getattr(inv, 'invoice_pdf', None),
            })

        return {"invoices": result}

    except Exception as exc:
        print(f"❌ Invoice fetch error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ── POST /api/email/welcome ──────────────────────────────────────────────────

@router.post("/api/email/welcome")
async def send_welcome_email(user_email: str = Form(...), credits: int = Form(20)):
    """Send welcome email to a new user."""
    try:
        success = supabase_client.send_welcome_email(user_email, credits)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to send welcome email")
        return {"success": True, "message": "Welcome email sent"}
    except HTTPException:
        raise
    except Exception as exc:
        print(f"❌ Welcome email error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ── POST /api/email/password-reset ───────────────────────────────────────────

@router.post("/api/email/password-reset")
async def send_password_reset_email(
    user_email: str = Form(...),
    reset_link: str = Form(...),
):
    """Send password reset email."""
    try:
        success = supabase_client.send_password_reset_email(user_email, reset_link)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to send password reset email")
        return {"success": True, "message": "Password reset email sent"}
    except HTTPException:
        raise
    except Exception as exc:
        print(f"❌ Password reset email error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
