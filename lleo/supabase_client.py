"""
Supabase Client — AUTH ONLY.

Supabase is used exclusively for authentication (sign-up / sign-in / password
reset / looking up a user id by e-mail).  All application data — analyses,
credits, subscriptions, payments, likes, conversation messages — lives in
Cloud SQL and is accessed through ``lleo.services.credits_service`` and the
``/api/*`` routers.

The credit / subscription / payment helper methods below are kept as thin
wrappers around ``credits_service`` so existing callers (Stripe webhooks)
keep working without changes.
"""
import os
from typing import Optional, Dict, Any
from dotenv import load_dotenv

from .services import credits_service

load_dotenv()


class SupabaseClient:
    """Auth lookups (Supabase) + transactional e-mails (Resend)."""

    def __init__(self):
        self.url = os.getenv("SUPABASE_URL")
        # Use SERVICE_ROLE_KEY for backend operations (admin auth API)
        self.key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        self.client = None

        if not self.url or not self.key:
            print("⚠️  SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set — auth lookups disabled")
            return

        try:
            from supabase import create_client
            self.client = create_client(self.url, self.key)
            print(f"✅ Supabase auth client initialized: {self.url}")
        except Exception as e:
            print(f"⚠️  Supabase client init failed: {e}")

    # ------------------------------------------------------------------ auth
    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Resolve a Supabase auth user by e-mail (admin API)."""
        if self.client is None or not email:
            return None
        try:
            email_l = email.strip().lower()
            page = 1
            while True:
                users = self.client.auth.admin.list_users(page=page, per_page=1000)
                if not users:
                    return None
                for user in users:
                    if (user.email or "").lower() == email_l:
                        return {
                            'id': str(user.id),
                            'email': user.email,
                            'created_at': user.created_at
                        }
                if len(users) < 1000:
                    return None
                page += 1
        except Exception as e:
            print(f"Error getting user by email: {e}")
            return None

    # --------------------------------------------- data (delegated to Cloud SQL)
    def get_user_credits(self, user_id: str) -> Optional[Dict[str, Any]]:
        try:
            return credits_service.ensure_user_credits(user_id)
        except Exception as e:
            print(f"Error getting user credits: {e}")
            return None

    def add_credits(
        self,
        user_id: str,
        amount: int,
        transaction_type: str = 'subscription_renewal',
        description: str = 'Monthly credit allocation'
    ) -> bool:
        return credits_service.add_credits(user_id, amount, transaction_type, description)

    def deduct_credits(
        self,
        user_id: str,
        amount: int,
        analysis_id: Optional[str] = None,
        description: str = 'Analysis usage'
    ) -> bool:
        result = credits_service.deduct_credits(user_id, amount, analysis_id, description)
        return bool(result.get("success"))

    def update_subscription(self, user_id: str, **kwargs) -> bool:
        return credits_service.update_subscription(user_id, **kwargs)

    def get_subscription_by_stripe_id(self, stripe_subscription_id: str) -> Optional[Dict[str, Any]]:
        try:
            return credits_service.get_subscription_by_stripe_id(stripe_subscription_id)
        except Exception as e:
            print(f"Error getting subscription: {e}")
            return None

    def create_payment_record(
        self,
        user_id: str,
        stripe_payment_intent_id: Optional[str] = None,
        stripe_invoice_id: Optional[str] = None,
        amount: int = 0,
        currency: str = 'usd',
        status: str = 'succeeded',
        description: Optional[str] = None
    ) -> bool:
        return credits_service.create_payment_record(
            user_id, stripe_payment_intent_id, stripe_invoice_id,
            amount, currency, status, description,
        )

    def send_upgrade_email(self, user_email: str, credits_added: int, amount_paid: float) -> bool:
        """Send thank you email using Resend"""
        try:
            import resend
            from datetime import datetime, timedelta

            resend.api_key = os.getenv("RESEND_API_KEY")

            next_billing_date = (datetime.now() + timedelta(days=30)).strftime('%B %d, %Y')

            html_content = f"""
            <html>
                <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                        <h2 style="color: #4CAF50;">Welcome to LLEO Pro!</h2>
                        <p>Dear valued customer,</p>
                        <p>Thank you for upgrading to LLEO Pro! Your payment of <strong>${amount_paid:.2f}</strong> has been processed successfully.</p>
                        <div style="background-color: #f5f5f5; padding: 15px; border-radius: 5px; margin: 20px 0;">
                            <h3 style="margin-top: 0; color: #2196F3;">Your Benefits:</h3>
                            <ul>
                                <li><strong>{credits_added} credits</strong> have been added to your account</li>
                                <li>100 credits renewed monthly</li>
                                <li>Advanced satellite data access</li>
                                <li>Priority processing</li>
                                <li>API access</li>
                            </ul>
                        </div>
                        <p><strong>Next billing date:</strong> {next_billing_date}</p>
                        <p>You can start using your credits immediately to run geospatial analyses. Visit <a href="https://lleo.earth" style="color: #2196F3;">lleo.earth</a> to get started!</p>
                        <hr style="border: none; border-top: 1px solid #ddd; margin: 30px 0;">
                        <p style="font-size: 14px; color: #666;">
                            If you have any questions or need assistance, please don't hesitate to contact us at <a href="mailto:support@lleo.earth" style="color: #2196F3;">support@lleo.earth</a>
                        </p>
                        <p style="font-size: 14px; color: #666;">
                            Best regards,<br>
                            <strong>Your LLEO Team</strong>
                        </p>
                    </div>
                </body>
            </html>
            """

            params = {
                "from": "LLEO <noreply@lleo.earth>",
                "to": [user_email],
                "subject": "Thank you for upgrading to LLEO Pro!",
                "html": html_content,
            }

            email = resend.Emails.send(params)
            print(f"✅ Email sent to {user_email} - ID: {email['id']}")
            return True

        except Exception as e:
            print(f"❌ Error sending email: {e}")
            return False

    def send_welcome_email(self, user_email: str, credits_added: int = 20) -> bool:
        """Send welcome email on registration using Resend"""
        try:
            import resend

            resend.api_key = os.getenv("RESEND_API_KEY")

            html_content = f"""
            <html>
                <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                        <h2 style="color: #4CAF50;">Welcome to LLEO!</h2>
                        <p>Hi there,</p>
                        <p>Thank you for signing up for LLEO! We're excited to have you on board.</p>
                        <div style="background-color: #f5f5f5; padding: 15px; border-radius: 5px; margin: 20px 0;">
                            <h3 style="margin-top: 0; color: #2196F3;">You're ready to get started!</h3>
                            <ul>
                                <li><strong>{credits_added} free credits</strong> have been added to your account</li>
                                <li>Access to satellite data and geospatial analysis</li>
                                <li>Easy-to-use interface</li>
                                <li>Run NDVI analyses on any location</li>
                            </ul>
                        </div>
                        <p>Start exploring satellite imagery and running analyses at <a href="https://lleo.earth" style="color: #2196F3;">lleo.earth</a></p>
                        <p style="margin-top: 30px;">Want more credits? <a href="https://lleo.earth/billing" style="color: #2196F3;">Upgrade to Pro</a> for 100 credits per month at just $1/month.</p>
                        <hr style="border: none; border-top: 1px solid #ddd; margin: 30px 0;">
                        <p style="font-size: 14px; color: #666;">
                            Need help? Contact us at <a href="mailto:support@lleo.earth" style="color: #2196F3;">support@lleo.earth</a>
                        </p>
                        <p style="font-size: 14px; color: #666;">
                            Best regards,<br>
                            <strong>The LLEO Team</strong>
                        </p>
                    </div>
                </body>
            </html>
            """

            params = {
                "from": "LLEO <noreply@lleo.earth>",
                "to": [user_email],
                "subject": "Welcome to LLEO - Your free credits are ready!",
                "html": html_content,
            }

            email = resend.Emails.send(params)
            print(f"✅ Welcome email sent to {user_email} - ID: {email['id']}")
            return True

        except Exception as e:
            print(f"❌ Error sending welcome email: {e}")
            return False

    def send_password_reset_email(self, user_email: str, reset_link: str) -> bool:
        """Send password reset email using Resend"""
        try:
            import resend

            resend.api_key = os.getenv("RESEND_API_KEY")

            html_content = f"""
            <html>
                <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                        <h2 style="color: #2196F3;">Reset Your Password</h2>
                        <p>Hi there,</p>
                        <p>We received a request to reset your password for your LLEO account.</p>
                        <div style="background-color: #f5f5f5; padding: 20px; border-radius: 5px; margin: 20px 0; text-align: center;">
                            <p style="margin-bottom: 15px;">Click the button below to reset your password:</p>
                            <a href="{reset_link}" style="display: inline-block; padding: 12px 30px; background-color: #2196F3; color: white; text-decoration: none; border-radius: 5px; font-weight: bold;">Reset Password</a>
                        </div>
                        <p style="font-size: 14px; color: #666;">Or copy and paste this link into your browser:</p>
                        <p style="font-size: 14px; color: #2196F3; word-break: break-all;">{reset_link}</p>
                        <p style="margin-top: 30px; font-size: 14px; color: #666;">This link will expire in 1 hour for security reasons.</p>
                        <p style="font-size: 14px; color: #666;">If you didn't request a password reset, you can safely ignore this email.</p>
                        <hr style="border: none; border-top: 1px solid #ddd; margin: 30px 0;">
                        <p style="font-size: 14px; color: #666;">
                            Need help? Contact us at <a href="mailto:support@lleo.earth" style="color: #2196F3;">support@lleo.earth</a>
                        </p>
                        <p style="font-size: 14px; color: #666;">
                            Best regards,<br>
                            <strong>The LLEO Team</strong>
                        </p>
                    </div>
                </body>
            </html>
            """

            params = {
                "from": "LLEO <noreply@lleo.earth>",
                "to": [user_email],
                "subject": "Reset your LLEO password",
                "html": html_content,
            }

            email = resend.Emails.send(params)
            print(f"✅ Password reset email sent to {user_email} - ID: {email['id']}")
            return True

        except Exception as e:
            print(f"❌ Error sending password reset email: {e}")
            return False

    def send_cancel_email(self, user_email: str, cancel_date: str = None, credits_remaining: int = 0) -> bool:
        """Send cancellation confirmation email using Resend"""
        try:
            import resend

            resend.api_key = os.getenv("RESEND_API_KEY")

            if cancel_date:
                date_line = f"<li>Your Pro access continues until <strong>{cancel_date}</strong></li>"
                subject = f"Your LLEO Pro subscription will end on {cancel_date}"
            else:
                date_line = "<li>Your Pro access has ended</li>"
                subject = "Your LLEO Pro subscription has been canceled"

            html_content = f"""
            <html>
                <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                        <h2 style="color: #FF9800;">Subscription Cancellation Confirmed</h2>
                        <p>Hi there,</p>
                        <p>We're sorry to see you go. Your LLEO Pro subscription has been canceled.</p>
                        <div style="background-color: #f5f5f5; padding: 15px; border-radius: 5px; margin: 20px 0;">
                            <h3 style="margin-top: 0; color: #2196F3;">What happens next:</h3>
                            <ul>
                                {date_line}
                                <li>Your remaining <strong>{credits_remaining} credits</strong> are still available for use</li>
                                <li>No further charges will be made</li>
                                <li>You can re-subscribe anytime at <a href="https://lleo.earth/billing" style="color: #2196F3;">lleo.earth/billing</a></li>
                            </ul>
                        </div>
                        <p>We'd love to have you back! If you changed your mind, you can reactivate your subscription from your <a href="https://lleo.earth/billing" style="color: #2196F3;">billing page</a> before the period ends.</p>
                        <hr style="border: none; border-top: 1px solid #ddd; margin: 30px 0;">
                        <p style="font-size: 14px; color: #666;">
                            Questions? Contact us at <a href="mailto:support@lleo.earth" style="color: #2196F3;">support@lleo.earth</a>
                        </p>
                        <p style="font-size: 14px; color: #666;">
                            Best regards,<br>
                            <strong>The LLEO Team</strong>
                        </p>
                    </div>
                </body>
            </html>
            """

            params = {
                "from": "LLEO <noreply@lleo.earth>",
                "to": [user_email],
                "subject": subject,
                "html": html_content,
            }

            email = resend.Emails.send(params)
            print(f"✅ Cancel email sent to {user_email} - ID: {email['id']}")
            return True

        except Exception as e:
            print(f"❌ Error sending cancel email: {e}")
            return False

# Global instance
supabase_client = SupabaseClient()
