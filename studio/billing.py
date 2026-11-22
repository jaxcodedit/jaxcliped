"""Billing, Pricing Plans, and Automated Payment Upgrades for JaxCliped.

Supports Stripe, payment webhooks, and automatic plan activations.
End of Year Sale: 90% OFF applied across Creator and Business tiers!
"""
import logging
import secrets
import time
from typing import Optional

import sqlalchemy as sa

from . import config, db

log = logging.getLogger("jaxcliped.billing")

PLANS = {
    "free": {
        "id": "free",
        "name": "Starter",
        "badge": "Free Forever",
        "price_monthly": 0,
        "price_annual": 0,
        "original_monthly": 0,
        "discount": "0%",
        "description": "Essential AI clipping for casual streamers and hobbyists.",
        "features": [
            "720p / 1080p Clip Generation",
            "Standard Auto-Captions",
            "1 Creator Workspace",
            "Basic Hype Moment Detection",
            "Standard Processing Speed",
            "Community Support"
        ],
        "limits": {
            "max_creators": 1,
            "max_quality": "1080p",
            "priority": 1,
            "phonk_fx": False,
            "auto_post": False,
            "personal_agent": False
        }
    },
    "creator": {
        "id": "creator",
        "name": "Creator",
        "badge": "Most Popular",
        "price_monthly": 149.00,         # 90% OFF ₹1,490
        "price_annual": 1490.00,         # ₹1,490 / year
        "original_monthly": 1490.00,
        "original_annual": 14900.00,
        "discount": "90% OFF",
        "currency": "INR",
        "description": "Built for content creators scaling on TikTok, Shorts, and Reels.",
        "features": [
            "Everything in Starter, plus:",
            "All New Feature Updates 1 Week Earlier",
            "Beat-Synced Phonk Captions & Motion FX",
            "4K / 60FPS High-Bitrate Video Export",
            "Multi-Platform Auto-Posting (Shorts, Reels, TikTok)",
            "Up to 10 Creator Workspaces",
            "Custom Watermarks & Clip Hooks",
            "5x Faster GPU Processing Priority",
            "Priority Email & Chat Support"
        ],
        "limits": {
            "max_creators": 10,
            "max_quality": "4k",
            "priority": 5,
            "phonk_fx": True,
            "auto_post": True,
            "personal_agent": False
        }
    },
    "business": {
        "id": "business",
        "name": "Business VIP",
        "badge": "Ultimate Power",
        "price_monthly": 249.00,         # 90% OFF ₹2,490
        "price_annual": 2490.00,         # ₹2,490 / year
        "original_monthly": 2490.00,
        "original_annual": 24900.00,
        "discount": "90% OFF",
        "currency": "INR",
        "description": "Autonomous 24/7 AI clipping with your own dedicated Personal Agent.",
        "features": [
            "Personal AI Agent (24/7 Autonomous Stream Monitoring)",
            "All features of Creator & Starter included",
            "Unlimited Creators & Workspaces",
            "Maximum GPU Turbo Speed (Zero Queue)",
            "Multi-Account Team Collaboration",
            "Custom Viral AI Style Training",
            "Direct WhatsApp & VIP Dedicated Support",
            "Full Commercial Resale Rights"
        ],
        "limits": {
            "max_creators": 999999,
            "max_quality": "4k",
            "priority": 10,
            "phonk_fx": True,
            "auto_post": True,
            "personal_agent": True
        }
    }
}


def get_plans() -> dict:
    """Return pricing details along with active End-of-Year Sale info."""
    return {
        "sale": {
            "name": "End of Year Sale",
            "discount_pct": 90,
            "banner_text": "🎉 END OF YEAR SALE: 90% OFF ALL PLANS — LIMITED TIME ONLY!",
            "active": True,
        },
        "plans": PLANS,
    }


def get_user_plan(uid: str, is_local: bool = False) -> dict:
    """Get the user's active plan, expiring status, and capability limits."""
    if is_local or config.is_local_host():
        meta = PLANS["business"]
        return {
            "plan": "business",
            "name": "Local Studio",
            "badge": "Free Forever",
            "plan_until": None,
            "is_paid": True,
            "free_forever": True,
            "is_local": True,
            "meta": meta,
        }
    with db.engine.connect() as c:
        row = c.execute(sa.select(db.users).where(db.users.c.id == uid)).first()
        if not row:
            return {"plan": "free", "active": False, "meta": PLANS["free"]}

        plan_id = getattr(row, "plan", "free") or "free"
        plan_until = getattr(row, "plan_until", None)

        # Check if subscription has expired
        if plan_id != "free" and plan_until and plan_until < time.time():
            plan_id = "free"

        meta = PLANS.get(plan_id, PLANS["free"])
        return {
            "plan": plan_id,
            "name": meta["name"],
            "plan_until": plan_until,
            "is_paid": plan_id in ("creator", "business"),
            "meta": meta,
        }


def upgrade_user(
    uid_or_email: str,
    plan_id: str,
    duration_days: int = 365,
    provider: str = "system",
    amount: float = 0.0,
    txn_id: str = "",
) -> bool:
    """Automatically upgrade a user's plan and grant access immediately."""
    plan_id = plan_id.lower().strip()
    if plan_id not in PLANS:
        raise ValueError(f"Unknown plan: {plan_id}")

    with db.engine.begin() as c:
        if "@" in uid_or_email:
            u = c.execute(sa.select(db.users).where(db.users.c.email == uid_or_email.lower().strip())).first()
        else:
            u = c.execute(sa.select(db.users).where(db.users.c.id == uid_or_email)).first()

        if not u:
            log.warning(f"Failed upgrade: user {uid_or_email} not found")
            return False

        now = time.time()
        plan_until = now + (duration_days * 86400) if duration_days > 0 else None

        # Update user row
        c.execute(
            db.users.update()
            .where(db.users.c.id == u.id)
            .values(plan=plan_id, plan_until=plan_until)
        )

        # Record or update payment transaction
        t_id = txn_id or f"pay_{secrets.token_hex(12)}"
        try:
            existing_pay = c.execute(sa.select(db.payments).where(db.payments.c.id == t_id)).first()
            if existing_pay:
                c.execute(
                    db.payments.update()
                    .where(db.payments.c.id == t_id)
                    .values(status="completed", plan=plan_id, amount=float(amount or existing_pay.amount))
                )
            else:
                c.execute(
                    db.payments.insert().values(
                        id=t_id,
                        user_id=u.id,
                        plan=plan_id,
                        amount=float(amount),
                        currency="inr" if provider == "upi" else "usd",
                        provider=provider,
                        status="completed",
                        created_at=now,
                    )
                )
        except Exception as e:
            log.warning(f"Error recording payment table: {e}")

        # Send in-app notification to the user
        plan_name = PLANS[plan_id]["name"]
        try:
            c.execute(
                db.notices.insert().values(
                    user_id=u.id,
                    t=now,
                    kind="account",
                    title=f"🎉 Upgraded to {plan_name} Plan!",
                    body=f"Your account is now on the {plan_name} plan. All pro features and AI capabilities are unlocked.",
                    link="/app",
                    ref=f"upgrade_{t_id}",
                )
            )
        except Exception:
            pass

        # Log action
        try:
            c.execute(
                db.admin_log.insert().values(
                    t=now,
                    admin_id="system",
                    action=f"upgrade:{plan_id}",
                    target_id=u.id,
                    detail=f"Auto-upgrade via {provider} for ${amount:.2f}",
                )
            )
        except Exception:
            pass

        log.info(f"User {u.email} ({u.id}) successfully upgraded to {plan_id}!")
        return True


def create_checkout(user, plan_id: str, cycle: str = "monthly") -> dict:
    """Create a checkout flow or session for the user."""
    plan = PLANS.get(plan_id)
    if not plan or plan_id == "free":
        raise ValueError("Invalid plan selected")

    amount = plan["price_annual"] if cycle == "annual" else plan["price_monthly"]

    stripe_key = config.env("STRIPE_SECRET_KEY", "")
    if stripe_key:
        try:
            import stripe
            stripe.api_key = stripe_key
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": f"JaxCliped {plan['name']} Plan ({cycle.capitalize()})",
                            "description": plan["description"],
                        },
                        "unit_amount": int(round(amount * 100)),
                    },
                    "quantity": 1,
                }],
                mode="payment",
                customer_email=user["email"],
                metadata={
                    "user_id": user["id"],
                    "email": user["email"],
                    "plan_id": plan_id,
                    "cycle": cycle,
                },
                success_url=f"{config.PUBLIC_URL}/app?upgraded=1&plan={plan_id}",
                cancel_url=f"{config.PUBLIC_URL}/app?canceled=1",
            )
            return {"url": session.url, "mode": "stripe"}
        except Exception as e:
            log.error(f"Stripe error: {e}")

    # Seamless instant upgrade mode (when gateway credentials are pending or for instant automated demo)
    upgrade_user(user["id"], plan_id, duration_days=365 if cycle == "annual" else 30, provider="instant_checkout", amount=amount)
    return {
        "url": f"/app?upgraded=1&plan={plan_id}",
        "mode": "instant",
        "message": f"Successfully activated {plan['name']} Plan!",
    }


def handle_stripe_webhook(payload: bytes, sig_header: str) -> bool:
    """Handle incoming Stripe webhook to automatically upgrade paid users."""
    stripe_key = config.env("STRIPE_SECRET_KEY", "")
    webhook_secret = config.env("STRIPE_WEBHOOK_SECRET", "")
    if not stripe_key or not webhook_secret:
        return False

    try:
        import stripe
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            meta = session.get("metadata", {})
            user_id = meta.get("user_id")
            plan_id = meta.get("plan_id")
            cycle = meta.get("cycle", "monthly")
            amount = float(session.get("amount_total", 0)) / 100.0

            if user_id and plan_id:
                days = 365 if cycle == "annual" else 30
                upgrade_user(user_id, plan_id, duration_days=days, provider="stripe", amount=amount, txn_id=session.get("id"))
                return True
    except Exception as e:
        log.error(f"Webhook processing error: {e}")

    return False


# ================================================================ UPI Instant QR Payments
UPI_ID = "8360101258@upi"
UPI_NAME = "JaxCliped"
UPI_PRICES = {
    "creator": {
        "inr": 149.0,
        "usd": 1.79,
        "name": "Creator Pro",
        "original_inr": 1490.0,
        "save": "90% OFF (Save ₹1,341)",
        "desc": "All new feature updates 1 week earlier, Beat-synced Phonk captions, 4K 60fps export, multi-platform auto-posting.",
    },
    "business": {
        "inr": 249.0,
        "usd": 2.99,
        "name": "Business VIP",
        "original_inr": 2490.0,
        "save": "90% OFF (Save ₹2,241)",
        "desc": "Dedicated 24/7 Personal AI Agent, unlimited workspaces, turbo GPU.",
    },
}


def get_upi_config() -> dict:
    """Return UPI configuration and active plans in INR."""
    return {
        "upi_id": UPI_ID,
        "upi_name": UPI_NAME,
        "qr_image": "/brand/upi-qr.png",
        "plans": UPI_PRICES,
    }


def submit_upi_payment(user_id: str, plan_id: str, utr: str, upi_sender: str = "") -> dict:
    """Submit UPI transaction reference ID for admin verification and approval."""
    plan_id = plan_id.lower().strip()
    if plan_id not in ("creator", "business"):
        raise ValueError("Invalid plan selected")

    clean_utr = "".join(c for c in utr if c.isalnum()).strip()
    if len(clean_utr) < 6:
        raise ValueError("Please provide a valid 12-digit UPI UTR / Transaction Reference Number.")

    price_info = UPI_PRICES[plan_id]
    amount = price_info["inr"]
    txn_id = f"upi_{clean_utr}"
    now = time.time()

    with db.engine.begin() as c:
        existing = c.execute(sa.select(db.payments).where(db.payments.c.id == txn_id)).first()
        if existing:
            if existing.status == "completed":
                raise ValueError("This UPI Reference / UTR has already been verified and redeemed.")
            if existing.status == "rejected":
                raise ValueError("This UPI Reference / UTR was previously rejected. Please contact support.")
            return {
                "ok": True,
                "status": "pending",
                "plan": plan_id,
                "message": f"⏳ UTR {clean_utr} is already submitted and awaiting admin approval.",
                "utr": clean_utr,
            }

        # Insert payment with 'pending' status
        c.execute(
            db.payments.insert().values(
                id=txn_id,
                user_id=user_id,
                plan=plan_id,
                amount=amount,
                currency="inr",
                provider="upi",
                status="pending",
                created_at=now,
            )
        )

        # Notify user
        try:
            c.execute(
                db.notices.insert().values(
                    user_id=user_id,
                    t=now,
                    kind="account",
                    title="⏳ UPI Payment Submitted for Verification",
                    body=f"Your UPI payment (Ref: {clean_utr}) for {price_info['name']} (₹{amount:g}) has been submitted. Our team will verify and activate your plan shortly!",
                    link="/app",
                    ref=f"pending_{txn_id}",
                )
            )
        except Exception:
            pass

        # Log for admin
        try:
            c.execute(
                db.admin_log.insert().values(
                    t=now,
                    admin_id="system",
                    action=f"upi_pending:{plan_id}",
                    target_id=user_id,
                    detail=f"UPI UTR: {clean_utr} | Plan: {plan_id} | Amount: INR {amount} | Status: Pending Verification",
                )
            )
        except Exception:
            pass

    return {
        "ok": True,
        "status": "pending",
        "plan": plan_id,
        "message": f"⏳ Payment reference submitted (UTR: {clean_utr})! It is under verification and will be activated shortly.",
        "utr": clean_utr,
    }


def list_payments(limit: int = 100) -> list:
    """List all payment transactions with user details for admin review."""
    with db.engine.connect() as c:
        j = db.payments.join(db.users, db.payments.c.user_id == db.users.c.id)
        rows = c.execute(
            sa.select(
                db.payments.c.id,
                db.payments.c.user_id,
                db.payments.c.plan,
                db.payments.c.amount,
                db.payments.c.currency,
                db.payments.c.provider,
                db.payments.c.status,
                db.payments.c.created_at,
                db.users.c.email,
                db.users.c.name,
            )
            .select_from(j)
            .order_by(db.payments.c.created_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "id": r.id,
                "user_id": r.user_id,
                "email": r.email,
                "name": r.name,
                "plan": r.plan,
                "amount": r.amount,
                "currency": r.currency,
                "provider": r.provider,
                "status": r.status,
                "created_at": r.created_at,
                "utr": r.id.replace("upi_", "") if r.id.startswith("upi_") else r.id,
            }
            for r in rows
        ]


def approve_payment(payment_id: str, admin_email: str = "admin") -> dict:
    """Approve a pending payment, upgrade user plan, and notify them."""
    with db.engine.begin() as c:
        p = c.execute(sa.select(db.payments).where(db.payments.c.id == payment_id)).first()
        if not p:
            raise ValueError("Payment record not found")
        if p.status == "completed":
            return {"ok": True, "message": "Payment already approved."}

        now = time.time()
        plan_until = now + (30 * 86400)

        # Upgrade user
        c.execute(
            db.users.update()
            .where(db.users.c.id == p.user_id)
            .values(plan=p.plan, plan_until=plan_until)
        )

        # Mark payment completed
        c.execute(
            db.payments.update()
            .where(db.payments.c.id == payment_id)
            .values(status="completed")
        )

        # Send user celebration notice
        plan_name = PLANS.get(p.plan, {}).get("name", p.plan.upper())
        try:
            c.execute(
                db.notices.insert().values(
                    user_id=p.user_id,
                    t=now,
                    kind="account",
                    title=f"🎉 Payment Approved: Welcome to {plan_name}!",
                    body=f"Your UPI payment (Ref: {p.id.replace('upi_', '')}) has been verified. All {plan_name} features are now unlocked!",
                    link="/app",
                    ref=f"approved_{payment_id}",
                )
            )
        except Exception:
            pass

        # Log admin action
        try:
            c.execute(
                db.admin_log.insert().values(
                    t=now,
                    admin_id=admin_email,
                    action=f"approve_payment:{p.plan}",
                    target_id=p.user_id,
                    detail=f"Approved UPI Payment {p.id} for plan {p.plan} (Amount: {p.amount})",
                )
            )
        except Exception:
            pass

    return {"ok": True, "message": f"Payment {payment_id} approved! User upgraded to {p.plan}."}


def reject_payment(payment_id: str, admin_email: str = "admin", reason: str = "") -> dict:
    """Reject an invalid / fraudulent payment submission."""
    with db.engine.begin() as c:
        p = c.execute(sa.select(db.payments).where(db.payments.c.id == payment_id)).first()
        if not p:
            raise ValueError("Payment record not found")

        now = time.time()
        c.execute(
            db.payments.update()
            .where(db.payments.c.id == payment_id)
            .values(status="rejected")
        )

        try:
            c.execute(
                db.notices.insert().values(
                    user_id=p.user_id,
                    t=now,
                    kind="account",
                    title="⚠️ Payment Verification Failed",
                    body=f"We could not verify your UPI payment reference (Ref: {p.id.replace('upi_', '')}). Please verify the 12-digit UTR from your bank app or contact support.",
                    link="/app",
                    ref=f"rejected_{payment_id}",
                )
            )
        except Exception:
            pass

        try:
            c.execute(
                db.admin_log.insert().values(
                    t=now,
                    admin_id=admin_email,
                    action=f"reject_payment:{p.plan}",
                    target_id=p.user_id,
                    detail=f"Rejected UPI Payment {p.id}: {reason or 'Invalid UTR'}",
                )
            )
        except Exception:
            pass

    return {"ok": True, "message": f"Payment {payment_id} marked rejected."}

