"""
Stripe Integration for LLEO Billing System
Handles webhooks, checkout sessions, and customer portal
"""
import os
import stripe
from typing import Dict, Any, Optional
from datetime import datetime
from dotenv import load_dotenv
from .supabase_client import supabase_client

load_dotenv()

# Initialize Stripe  (strip whitespace / trailing newlines that can creep in
# from Secret Manager or .env files — a trailing \r\n breaks signature checks)
stripe.api_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip() or None
STRIPE_WEBHOOK_SECRET = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip() or None
STRIPE_PRO_PRICE_ID = (os.getenv("STRIPE_PRO_PRICE_ID") or "").strip() or None

# ── Startup health-check: log which Stripe env vars are present ──────────
_missing = []
if not stripe.api_key:
    _missing.append("STRIPE_SECRET_KEY")
if not STRIPE_WEBHOOK_SECRET:
    _missing.append("STRIPE_WEBHOOK_SECRET")
if not STRIPE_PRO_PRICE_ID:
    _missing.append("STRIPE_PRO_PRICE_ID")

if _missing:
    print(f"⚠️  Stripe init — MISSING env vars: {', '.join(_missing)}")
else:
    print("✅ Stripe initialized (all keys present)")


class StripeHandler:
    """Handles all Stripe operations"""

    @staticmethod
    def verify_webhook(payload: bytes, sig_header: str) -> Optional[Dict[str, Any]]:
        """Verify webhook signature and return event"""
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_WEBHOOK_SECRET
            )
            return event
        except ValueError as e:
            print(f"❌ Invalid payload: {e}")
            return None
        except stripe.error.SignatureVerificationError as e:
            print(f"❌ Invalid signature: {e}")
            return None

    @staticmethod
    def handle_subscription_created(event_data: Dict[str, Any]) -> bool:
        """Handle customer.subscription.created event"""
        try:
            subscription_id = event_data['id']
            customer_id = event_data['customer']
            status = event_data['status']
            current_period_start = event_data.get('current_period_start')
            current_period_end = event_data.get('current_period_end')
            price_id = event_data['items']['data'][0]['price']['id']

            # Get customer email from Stripe
            customer = stripe.Customer.retrieve(customer_id)
            customer_email = customer['email']

            print(f"📧 New subscription for {customer_email}")

            # Get user from Supabase
            user = supabase_client.get_user_by_email(customer_email)
            if not user:
                print(f"❌ User not found: {customer_email}")
                return False

            user_id = user['id']

            # Convert Unix timestamps to ISO format strings (if available)
            period_start_iso = datetime.fromtimestamp(current_period_start).isoformat() if current_period_start else None
            period_end_iso = datetime.fromtimestamp(current_period_end).isoformat() if current_period_end else None

            # Update subscription in Supabase
            supabase_client.update_subscription(
                user_id=user_id,
                stripe_customer_id=customer_id,
                stripe_subscription_id=subscription_id,
                stripe_price_id=price_id,
                status='active',
                plan_type='pro',
                current_period_start=period_start_iso,
                current_period_end=period_end_iso
            )

            print(f"✅ Subscription created for user {user_id}")
            return True

        except Exception as e:
            print(f"❌ Error handling subscription.created: {e}")
            return False

    @staticmethod
    def handle_subscription_updated(event_data: Dict[str, Any]) -> bool:
        """Handle customer.subscription.updated event"""
        try:
            subscription_id = event_data['id']
            status = event_data['status']
            current_period_start = event_data.get('current_period_start')
            current_period_end = event_data.get('current_period_end')

            # Get subscription from database
            subscription = supabase_client.get_subscription_by_stripe_id(subscription_id)
            if not subscription:
                print(f"❌ Subscription not found: {subscription_id}")
                return False

            user_id = subscription['user_id']

            # Convert Unix timestamps to ISO format strings (if available)
            period_start_iso = datetime.fromtimestamp(current_period_start).isoformat() if current_period_start else None
            period_end_iso = datetime.fromtimestamp(current_period_end).isoformat() if current_period_end else None

            # Update subscription status
            update_kwargs = {
                'user_id': user_id,
                'status': status,
            }
            if period_start_iso:
                update_kwargs['current_period_start'] = period_start_iso
            if period_end_iso:
                update_kwargs['current_period_end'] = period_end_iso

            supabase_client.update_subscription(**update_kwargs)

            print(f"✅ Subscription {subscription_id} updated to status: {status}")
            return True

        except Exception as e:
            print(f"❌ Error handling subscription.updated: {e}")
            return False

    @staticmethod
    def handle_subscription_deleted(event_data: Dict[str, Any]) -> bool:
        """Handle customer.subscription.deleted event"""
        try:
            subscription_id = event_data['id']

            # Get subscription from database
            subscription = supabase_client.get_subscription_by_stripe_id(subscription_id)
            if not subscription:
                print(f"❌ Subscription not found: {subscription_id}")
                return False

            user_id = subscription['user_id']

            # Update subscription to canceled
            supabase_client.update_subscription(
                user_id=user_id,
                status='canceled',
                plan_type='free'
            )

            print(f"✅ Subscription {subscription_id} canceled")
            return True

        except Exception as e:
            print(f"❌ Error handling subscription.deleted: {e}")
            return False

    @staticmethod
    def handle_payment_succeeded(event_data: Dict[str, Any]) -> bool:
        """Handle invoice.payment_succeeded event - Add monthly credits"""
        try:
            import time

            customer_id = event_data['customer']
            subscription_id = event_data['subscription']
            invoice_id = event_data['id']
            amount_paid = event_data['amount_paid']  # In cents

            # Get subscription from database with retry logic (race condition fix)
            subscription = None
            max_retries = 5
            retry_delay = 2  # Start with 2 seconds

            for attempt in range(max_retries):
                subscription = supabase_client.get_subscription_by_stripe_id(subscription_id)
                if subscription:
                    print(f"✅ Found subscription on attempt {attempt + 1}")
                    break

                if attempt < max_retries - 1:
                    print(f"⏳ Subscription not found yet (attempt {attempt + 1}/{max_retries}), retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                    retry_delay *= 1.5  # Gentler backoff: 2s, 3s, 4.5s, 6.75s (total ~16s)

            if not subscription:
                print(f"❌ Subscription not found after {max_retries} retries: {subscription_id}")
                return False

            user_id = subscription['user_id']
            plan_type = subscription['plan_type']

            # Add credits based on plan
            if plan_type == 'pro':
                credits_to_add = 100

                # Add credits
                success = supabase_client.add_credits(
                    user_id=user_id,
                    amount=credits_to_add,
                    transaction_type='subscription_renewal',
                    description=f'Monthly Pro plan renewal (${amount_paid / 100:.2f})'
                )

                if success:
                    # Record payment
                    supabase_client.create_payment_record(
                        user_id=user_id,
                        stripe_invoice_id=invoice_id,
                        amount=amount_paid,
                        status='succeeded',
                        description='Pro subscription payment'
                    )

                    # Get user email
                    user = supabase_client.get_user_by_email(subscription.get('user_email', ''))
                    if not user:
                        # Fallback: get customer email from Stripe
                        customer = stripe.Customer.retrieve(customer_id)
                        user_email = customer['email']
                    else:
                        user_email = user['email']

                    # Send thank you email
                    supabase_client.send_upgrade_email(user_email, credits_to_add, amount_paid / 100)

                    print(f"✅ Added {credits_to_add} credits to user {user_id}")
                    return True

            return False

        except Exception as e:
            print(f"❌ Error handling payment.succeeded: {e}")
            return False

    @staticmethod
    def handle_payment_failed(event_data: Dict[str, Any]) -> bool:
        """Handle invoice.payment_failed event"""
        try:
            subscription_id = event_data['subscription']
            invoice_id = event_data['id']

            # Get subscription from database
            subscription = supabase_client.get_subscription_by_stripe_id(subscription_id)
            if not subscription:
                print(f"❌ Subscription not found: {subscription_id}")
                return False

            user_id = subscription['user_id']

            # Update subscription status to past_due
            supabase_client.update_subscription(
                user_id=user_id,
                status='past_due'
            )

            # Record failed payment
            supabase_client.create_payment_record(
                user_id=user_id,
                stripe_invoice_id=invoice_id,
                amount=event_data['amount_due'],
                status='failed',
                description='Failed payment attempt'
            )

            print(f"⚠️ Payment failed for subscription {subscription_id}")
            return True

        except Exception as e:
            print(f"❌ Error handling payment.failed: {e}")
            return False

    @staticmethod
    def create_checkout_session(user_email: str, user_id: str) -> str:
        """Create Stripe Checkout session for Pro subscription.

        Returns the checkout URL on success.
        Raises RuntimeError / ValueError with a human-readable message on failure
        so the billing router can forward it to the frontend.
        """
        # ── Pre-flight: make sure required env vars are present ──────────
        if not stripe.api_key:
            print("❌ create_checkout_session: STRIPE_SECRET_KEY is not set")
            raise RuntimeError("Stripe is not configured on the server (missing API key). Please contact support.")
        if not STRIPE_PRO_PRICE_ID:
            print("❌ create_checkout_session: STRIPE_PRO_PRICE_ID is not set")
            raise RuntimeError("Stripe Pro price is not configured on the server. Please contact support.")

        try:
            # Create or retrieve customer
            customers = stripe.Customer.list(email=user_email, limit=1)

            if customers.data:
                customer = customers.data[0]

                # Check for existing active subscription to prevent duplicates
                existing_subs = stripe.Subscription.list(
                    customer=customer.id,
                    status='active',
                    limit=1
                )
                if existing_subs.data:
                    print(f"⚠️ User {user_email} already has active subscription {existing_subs.data[0].id}")
                    raise ValueError("You already have an active Pro subscription. Visit your billing page to manage it.")
            else:
                customer = stripe.Customer.create(
                    email=user_email,
                    metadata={'user_id': user_id}
                )

            print(f"📦 Creating checkout session: customer={customer.id}, price={STRIPE_PRO_PRICE_ID}")

            # Create checkout session
            checkout_session = stripe.checkout.Session.create(
                customer=customer.id,
                payment_method_types=['card'],
                line_items=[{
                    'price': STRIPE_PRO_PRICE_ID,
                    'quantity': 1,
                }],
                mode='subscription',
                success_url='https://lleo.earth/billing?success=true',
                cancel_url='https://lleo.earth/billing?canceled=true',
                metadata={'user_id': user_id},
                allow_promotion_codes=True,  # Allow discount codes
            )

            print(f"✅ Checkout session created for {user_email}: {checkout_session.id}")
            return checkout_session.url

        except ValueError:
            raise                       # re-raise user-facing messages as-is
        except stripe.error.StripeError as e:
            # Stripe SDK errors carry a user_message we can safely surface
            msg = getattr(e, 'user_message', None) or str(e)
            print(f"❌ Stripe API error creating checkout session: {e}")
            raise RuntimeError(f"Payment provider error: {msg}") from e
        except Exception as e:
            print(f"❌ Unexpected error creating checkout session: {e}")
            raise RuntimeError(f"Could not start checkout: {e}") from e

    @staticmethod
    def create_customer_portal_session(user_email: str) -> Optional[str]:
        """Create Stripe Customer Portal session for subscription management"""
        try:
            # Get customer by email
            customers = stripe.Customer.list(email=user_email, limit=1)

            if not customers.data:
                print(f"❌ Customer not found: {user_email}")
                return None

            customer = customers.data[0]

            # Create portal session
            portal_session = stripe.billing_portal.Session.create(
                customer=customer.id,
                return_url='https://lleo.earth/billing',
            )

            print(f"✅ Customer portal session created for {user_email}")
            return portal_session.url

        except Exception as e:
            print(f"❌ Error creating portal session: {e}")
            return None

    @staticmethod
    def cancel_subscription(user_email: str) -> Dict[str, Any]:
        """Cancel a user's subscription — gracefully at period end if active, immediately if past_due"""
        try:
            # Get customer by email
            customers = stripe.Customer.list(email=user_email, limit=1)

            if not customers.data:
                print(f"❌ Cancel: No Stripe customer found for email: {user_email}")
                return {"success": False, "error": "Customer not found"}

            customer = customers.data[0]
            print(f"📧 Cancel: Found Stripe customer {customer.id} for {user_email}")

            # Search for cancelable subscriptions (any non-canceled status)
            subscription = None
            found_status = None
            for status in ['active', 'trialing', 'past_due', 'unpaid', 'incomplete']:
                subs = stripe.Subscription.list(
                    customer=customer.id,
                    status=status,
                    limit=1
                )
                if subs.data:
                    subscription = subs.data[0]
                    found_status = status
                    print(f"📋 Cancel: Found subscription {subscription.id} with status={status}")
                    break

            if not subscription:
                # Log all subs for debugging
                all_subs = stripe.Subscription.list(customer=customer.id, limit=5)
                statuses = [f"{s.id}({s.status})" for s in all_subs.data]
                print(f"❌ Cancel: No cancelable subscription. All subs: {statuses or 'none'}")
                return {"success": False, "error": "No active subscription found"}

            # Helper: get period end date
            period_end_ts = getattr(subscription, 'current_period_end', None)
            period_end_date = None
            period_expired = False
            if period_end_ts:
                period_end_dt = datetime.fromtimestamp(period_end_ts)
                period_end_date = period_end_dt.strftime('%B %d, %Y')
                period_expired = period_end_dt < datetime.now()

            # Already set to cancel?
            if getattr(subscription, 'cancel_at_period_end', False):
                if period_expired:
                    # Period already ended — cancel immediately in Stripe and update Supabase
                    try:
                        stripe.Subscription.cancel(subscription.id)
                        user = supabase_client.get_user_by_email(user_email)
                        if user:
                            supabase_client.update_subscription(
                                user_id=user['id'],
                                status='canceled',
                                plan_type='free'
                            )
                        print(f"✅ Expired subscription {subscription.id} fully canceled for {user_email}")
                    except Exception as cleanup_err:
                        print(f"⚠️ Cleanup error: {cleanup_err}")

                    # Send cancel confirmation email
                    try:
                        u = supabase_client.get_user_by_email(user_email)
                        cr = supabase_client.get_user_credits(u['id']) if u else None
                        remaining = cr.get('credits', 0) if cr else 0
                        supabase_client.send_cancel_email(user_email, period_end_date, remaining)
                    except Exception:
                        pass

                    return {
                        "success": True,
                        "message": "Subscription has been canceled. Your remaining credits are still available for use.",
                        "cancel_date": period_end_date,
                        "period_expired": True,
                        "subscription_id": subscription.id
                    }
                else:
                    return {
                        "success": True,
                        "message": f"Subscription is set to cancel on {period_end_date}. You'll keep Pro access and your remaining credits until then. No further charges will be made.",
                        "cancel_date": period_end_date,
                        "period_expired": False,
                        "subscription_id": subscription.id
                    }

            # For past_due/unpaid/incomplete: cancel immediately (no active period to preserve)
            if found_status in ('past_due', 'unpaid', 'incomplete'):
                stripe.Subscription.cancel(subscription.id)
                print(f"✅ Subscription {subscription.id} canceled immediately (was {found_status}) for {user_email}")

                # Also update Supabase to reflect cancellation
                try:
                    user = supabase_client.get_user_by_email(user_email)
                    if user:
                        supabase_client.update_subscription(
                            user_id=user['id'],
                            status='canceled',
                            plan_type='free'
                        )
                except Exception as db_err:
                    print(f"⚠️ Could not update Supabase after cancel: {db_err}")

                # Send cancel confirmation email
                try:
                    u = supabase_client.get_user_by_email(user_email)
                    cr = supabase_client.get_user_credits(u['id']) if u else None
                    remaining = cr.get('credits', 0) if cr else 0
                    supabase_client.send_cancel_email(user_email, period_end_date, remaining)
                except Exception:
                    pass

                return {
                    "success": True,
                    "message": "Subscription has been canceled. Your remaining credits are still available for use.",
                    "cancel_date": period_end_date,
                    "period_expired": True,
                    "subscription_id": subscription.id
                }

            # For active/trialing: cancel at end of current billing period
            updated_sub = stripe.Subscription.modify(
                subscription.id,
                cancel_at_period_end=True
            )

            period_end_ts = getattr(updated_sub, 'current_period_end', None)
            if period_end_ts:
                cancel_date = datetime.fromtimestamp(period_end_ts).strftime('%B %d, %Y')
                msg = f"Subscription will be canceled on {cancel_date}. You'll keep Pro access and your remaining credits until then. No further charges will be made."
            else:
                cancel_date = None
                msg = "Subscription has been set to cancel at the end of the billing period. Your credits remain available."

            print(f"✅ Subscription {subscription.id} set to cancel at period end for {user_email}")

            # Send cancel confirmation email
            try:
                u = supabase_client.get_user_by_email(user_email)
                cr = supabase_client.get_user_credits(u['id']) if u else None
                remaining = cr.get('credits', 0) if cr else 0
                supabase_client.send_cancel_email(user_email, cancel_date, remaining)
            except Exception:
                pass

            return {
                "success": True,
                "message": msg,
                "cancel_date": cancel_date,
                "period_expired": False,
                "subscription_id": subscription.id
            }

        except Exception as e:
            print(f"❌ Error canceling subscription: {e}")
            return {"success": False, "error": str(e)}

    @staticmethod
    def reactivate_subscription(user_email: str) -> Dict[str, Any]:
        """Reactivate a subscription that was set to cancel at period end"""
        try:
            customers = stripe.Customer.list(email=user_email, limit=1)

            if not customers.data:
                return {"success": False, "error": "Customer not found"}

            customer = customers.data[0]

            # Find subscription set to cancel
            subscriptions = stripe.Subscription.list(
                customer=customer.id,
                limit=1
            )

            if not subscriptions.data:
                return {"success": False, "error": "No subscription found"}

            subscription = subscriptions.data[0]

            if not subscription.cancel_at_period_end:
                return {"success": False, "error": "Subscription is not set to cancel"}

            # Reactivate by removing cancel_at_period_end
            stripe.Subscription.modify(
                subscription.id,
                cancel_at_period_end=False
            )

            print(f"✅ Subscription {subscription.id} reactivated for {user_email}")
            return {
                "success": True,
                "message": "Subscription reactivated! You'll continue to be billed normally."
            }

        except Exception as e:
            print(f"❌ Error reactivating subscription: {e}")
            return {"success": False, "error": str(e)}


# Global instance
stripe_handler = StripeHandler()
