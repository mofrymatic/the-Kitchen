"""
FTS Arcade Cloud Functions - Python Version
Firebase Cloud Functions for monetization and invite system
"""

import functions_framework
import firebase_admin
from firebase_admin import credentials, firestore, auth
from google.cloud.firestore import transactional
import secrets
import string
from datetime import datetime, timedelta
from typing import Any, Dict
import logging

# Initialize Firebase (automatically done by Functions framework)
if not firebase_admin.get_app():
    firebase_admin.initialize_app()

db = firestore.client()
logger = logging.getLogger(__name__)


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def generate_invite_code(length: int = 6) -> str:
    """Generate a unique, URL-safe invite code (no confusing characters)."""
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # No 0/O/I/1
    return "".join(secrets.choice(chars) for _ in range(length))


def verify_auth(request) -> str:
    """Verify request has valid Firebase auth token, return user ID."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise ValueError("Missing or invalid Authorization header")
    
    token = auth_header.split(" ")[1]
    try:
        decoded = auth.verify_id_token(token)
        return decoded["uid"]
    except Exception as e:
        logger.error(f"Auth verification failed: {e}")
        raise ValueError(f"Invalid token: {str(e)}")


# ============================================================================
# INVITE SYSTEM
# ============================================================================

@functions_framework.http
def generate_invite(request):
    """
    Generate a unique invite code for the authenticated user.
    Returns: {code: str, url: str}
    """
    try:
        user_id = verify_auth(request)
    except ValueError as e:
        return {"error": str(e)}, 401

    try:
        # Check invite limit (10 per user)
        invites_query = db.collection("invites").where("inviterId", "==", user_id)
        invite_count = len(invites_query.stream())

        if invite_count >= 10:
            return {"error": "Maximum 10 invite codes per user"}, 429

        # Generate unique code
        code = generate_invite_code()
        attempts = 0
        
        while attempts < 10:
            if not db.collection("invites").document(code).get().exists:
                break
            code = generate_invite_code()
            attempts += 1

        if attempts >= 10:
            logger.error("Failed to generate unique invite code after 10 attempts")
            return {"error": "Failed to generate unique code"}, 500

        # Create invite document
        db.collection("invites").document(code).set({
            "code": code,
            "inviterId": user_id,
            "inviteeId": None,
            "status": "pending",
            "bonusAmount": 500,
            "createdAt": firestore.SERVER_TIMESTAMP
        })

        invite_url = f"https://your-project.web.app/?invite={code}"
        
        logger.info(f"Generated invite code {code} for user {user_id}")
        return {
            "code": code,
            "url": invite_url
        }, 200

    except Exception as e:
        logger.error(f"Error in generate_invite: {e}")
        return {"error": str(e)}, 500


@functions_framework.http
def claim_invite(request):
    """
    Claim an invite code and credit both inviter and invitee with tokens.
    Requires: inviteCode (str), deviceId (str)
    Returns: {success: bool, message: str, ...}
    """
    try:
        user_id = verify_auth(request)
        data = request.get_json() or {}
        invite_code = data.get("inviteCode", "").upper()
        device_id = data.get("deviceId", "")

        if not invite_code:
            return {"error": "Missing inviteCode"}, 400
        if not device_id:
            return {"error": "Missing deviceId"}, 400

    except ValueError as e:
        return {"error": str(e)}, 401

    @transactional
    def claim_transaction(transaction):
        """Atomic transaction for claiming invite."""
        # Get invite
        invite_ref = db.collection("invites").document(invite_code)
        invite_doc = transaction.get(invite_ref)

        if not invite_doc.exists:
            raise ValueError("Invalid invite code")

        invite = invite_doc.to_dict()

        if invite["status"] != "pending":
            raise ValueError("Invite already claimed or expired")

        if invite["inviterId"] == user_id:
            raise ValueError("Cannot use your own invite")

        # Check device limit (prevent farming)
        recent_invitees = db.collection("users").where("deviceId", "==", device_id).stream()
        last_30_days = datetime.now() - timedelta(days=30)
        
        recent_count = sum(
            1 for doc in recent_invitees
            if doc.get("createdAt").timestamp() > last_30_days.timestamp()
        )

        if recent_count >= 3:
            raise ValueError("Maximum 3 accounts per device within 30 days")

        # Check KYC status
        invitee_ref = db.collection("users").document(user_id)
        invitee_doc = transaction.get(invitee_ref)
        
        if not invitee_doc.exists:
            raise ValueError("User document not found")

        invitee = invitee_doc.to_dict()

        if invitee.get("kycStatus") != "approved":
            # Mark as pending KYC
            transaction.update(invite_ref, {
                "inviteeId": user_id,
                "status": "pending_kyc"
            })

            return {
                "success": False,
                "requiresKyc": True,
                "message": "Complete KYC verification to claim your $5 bonus"
            }

        # Credit both users
        inviter_balance_ref = db.collection("tokenBalances").document(invite["inviterId"])
        invitee_balance_ref = db.collection("tokenBalances").document(user_id)

        bonus = invite["bonusAmount"]

        # Update inviter balance
        inviter_balance = transaction.get(inviter_balance_ref).to_dict() or {}
        transaction.update(inviter_balance_ref, {
            "balance": (inviter_balance.get("balance", 0) or 0) + bonus,
            "lifetimeEarned": (inviter_balance.get("lifetimeEarned", 0) or 0) + bonus
        })

        # Update invitee balance
        invitee_balance = transaction.get(invitee_balance_ref).to_dict() or {}
        transaction.update(invitee_balance_ref, {
            "balance": (invitee_balance.get("balance", 0) or 0) + bonus,
            "lifetimeEarned": (invitee_balance.get("lifetimeEarned", 0) or 0) + bonus
        })

        # Update invite status
        transaction.update(invite_ref, {
            "inviteeId": user_id,
            "status": "claimed",
            "claimedAt": firestore.SERVER_TIMESTAMP
        })

        # Log inviter transaction
        inviter_tx_ref = db.collection("transactions").document()
        transaction.set(inviter_tx_ref, {
            "userId": invite["inviterId"],
            "type": "invite_bonus",
            "amount": bonus,
            "metadata": {"inviteCode": invite_code, "role": "inviter"},
            "timestamp": firestore.SERVER_TIMESTAMP
        })

        # Log invitee transaction
        invitee_tx_ref = db.collection("transactions").document()
        transaction.set(invitee_tx_ref, {
            "userId": user_id,
            "type": "invite_bonus",
            "amount": bonus,
            "metadata": {"inviteCode": invite_code, "role": "invitee"},
            "timestamp": firestore.SERVER_TIMESTAMP
        })

        return {
            "success": True,
            "bonusAmount": bonus,
            "message": "You both earned $5 in tokens!"
        }

    try:
        transaction = db.transaction()
        result = claim_transaction(transaction)
        return result, 200

    except ValueError as e:
        logger.warning(f"Claim validation failed: {e}")
        return {"error": str(e)}, 400
    except Exception as e:
        logger.error(f"Error in claim_invite: {e}")
        return {"error": "Transaction failed"}, 500


# ============================================================================
# KYC TRIGGER - Auto-credit bonuses when KYC approved
# ============================================================================

@functions_framework.cloud_event
def on_kyc_approved(cloud_event):
    """
    Firestore trigger: fires when user document is updated.
    Credits pending invite bonuses when KYC status becomes 'approved'.
    """
    firestore_payload = cloud_event.data["value"]
    
    if "fields" not in firestore_payload:
        logger.info("No fields in payload, skipping")
        return

    fields = firestore_payload["fields"]
    user_id = cloud_event.data["name"].split("/")[-1]
    
    # Check if KYC status changed to approved
    old_kyc = cloud_event.data.get("oldValue", {}).get("fields", {}).get("kycStatus")
    new_kyc = fields.get("kycStatus", {}).get("stringValue")

    if new_kyc != "approved" or old_kyc == new_kyc:
        logger.info(f"KYC not approved or unchanged for user {user_id}")
        return

    logger.info(f"KYC approved for user {user_id}, crediting pending bonuses...")

    # Find pending invites for this user
    pending_invites = db.collection("invites").where(
        "inviteeId", "==", user_id
    ).where(
        "status", "==", "pending_kyc"
    ).stream()

    @transactional
    def credit_bonuses_transaction(transaction):
        total_bonus = 0
        
        for invite_doc in pending_invites:
            invite = invite_doc.to_dict()
            total_bonus += invite["bonusAmount"]

            # Credit inviter
            inviter_balance_ref = db.collection("tokenBalances").document(
                invite["inviterId"]
            )
            inviter_balance = transaction.get(inviter_balance_ref).to_dict() or {}

            transaction.update(inviter_balance_ref, {
                "balance": (inviter_balance.get("balance", 0) or 0) + invite["bonusAmount"],
                "lifetimeEarned": (inviter_balance.get("lifetimeEarned", 0) or 0) + invite["bonusAmount"]
            })

            # Update invite status
            transaction.update(invite_doc.reference, {
                "status": "claimed",
                "claimedAt": firestore.SERVER_TIMESTAMP
            })

            # Log inviter transaction
            inviter_tx_ref = db.collection("transactions").document()
            transaction.set(inviter_tx_ref, {
                "userId": invite["inviterId"],
                "type": "invite_bonus",
                "amount": invite["bonusAmount"],
                "metadata": {
                    "inviteCode": invite["code"],
                    "role": "inviter",
                    "trigger": "kyc_approved"
                },
                "timestamp": firestore.SERVER_TIMESTAMP
            })

        # Credit invitee
        invitee_balance_ref = db.collection("tokenBalances").document(user_id)
        invitee_balance = transaction.get(invitee_balance_ref).to_dict() or {}

        transaction.update(invitee_balance_ref, {
            "balance": (invitee_balance.get("balance", 0) or 0) + total_bonus,
            "lifetimeEarned": (invitee_balance.get("lifetimeEarned", 0) or 0) + total_bonus
        })

        # Log invitee transaction
        invitee_tx_ref = db.collection("transactions").document()
        transaction.set(invitee_tx_ref, {
            "userId": user_id,
            "type": "invite_bonus",
            "amount": total_bonus,
            "metadata": {"reason": "kyc_approved"},
            "timestamp": firestore.SERVER_TIMESTAMP
        })

    try:
        transaction = db.transaction()
        credit_bonuses_transaction(transaction)
        logger.info(f"Successfully credited {total_bonus} tokens to user {user_id}")
    except Exception as e:
        logger.error(f"Error crediting bonuses: {e}")


# ============================================================================
# TOKEN SPENDING
# ============================================================================

@functions_framework.http
def spend_tokens(request):
    """
    Deduct tokens from user's balance.
    Requires: amount (int), reason (str)
    Returns: {success: bool, newBalance: int}
    """
    try:
        user_id = verify_auth(request)
        data = request.get_json() or {}
        amount = data.get("amount", 0)
        reason = data.get("reason", "unknown")

        if not isinstance(amount, int) or amount <= 0:
            return {"error": "Amount must be a positive integer"}, 400

    except ValueError as e:
        return {"error": str(e)}, 401

    @transactional
    def spend_transaction(transaction):
        balance_ref = db.collection("tokenBalances").document(user_id)
        balance_doc = transaction.get(balance_ref)

        if not balance_doc.exists:
            raise ValueError("Balance not found")

        current_balance = balance_doc.to_dict().get("balance", 0)

        if current_balance < amount:
            raise ValueError("Insufficient balance")

        # Deduct tokens
        transaction.update(balance_ref, {
            "balance": current_balance - amount,
            "lifetimeSpent": balance_doc.to_dict().get("lifetimeSpent", 0) + amount
        })

        # Log transaction
        tx_ref = db.collection("transactions").document()
        transaction.set(tx_ref, {
            "userId": user_id,
            "type": "spend",
            "amount": -amount,
            "balanceBefore": current_balance,
            "balanceAfter": current_balance - amount,
            "metadata": {"reason": reason},
            "timestamp": firestore.SERVER_TIMESTAMP
        })

        return current_balance - amount

    try:
        transaction = db.transaction()
        new_balance = spend_transaction(transaction)
        return {"success": True, "newBalance": new_balance}, 200

    except ValueError as e:
        logger.warning(f"Spend validation failed: {e}")
        return {"error": str(e)}, 400
    except Exception as e:
        logger.error(f"Error in spend_tokens: {e}")
        return {"error": "Transaction failed"}, 500


# ============================================================================
# STRIPE INTEGRATION
# ============================================================================

@functions_framework.http
def create_stripe_checkout(request):
    """
    Create a Stripe checkout session document for the Stripe extension to process.
    Requires: priceId (str)
    Returns: {url: str} (via Firestore listener on client)
    """
    try:
        user_id = verify_auth(request)
        data = request.get_json() or {}
        price_id = data.get("priceId", "")

        if not price_id:
            return {"error": "Missing priceId"}, 400

    except ValueError as e:
        return {"error": str(e)}, 401

    try:
        # Create checkout session document
        # The Stripe extension listens for this and populates the session URL
        checkout_ref = db.collection("customers").document(user_id).collection(
            "checkout_sessions"
        ).document()

        checkout_ref.set({
            "price": price_id,
            "success_url": "https://your-project.web.app/?payment=success",
            "cancel_url": "https://your-project.web.app/?payment=canceled",
            "mode": "payment",
            "allow_promotion_codes": True,
            "metadata": {
                "userId": user_id,
                "priceId": price_id
            }
        })

        logger.info(f"Created checkout session for user {user_id}")
        
        # Return session doc ID (client will poll for URL)
        return {
            "sessionId": checkout_ref.id,
            "message": "Check checkout_sessions collection for URL"
        }, 200

    except Exception as e:
        logger.error(f"Error creating checkout: {e}")
        return {"error": str(e)}, 500


# ============================================================================
# PAYMENT SUCCESS HANDLER (Cloud Firestore trigger)
# ============================================================================

@functions_framework.cloud_event
def on_payment_success(cloud_event):
    """
    Firestore trigger: fires when payment document is created.
    Credits tokens when Stripe extension confirms successful payment.
    """
    payment_payload = cloud_event.data["value"]
    
    if "fields" not in payment_payload:
        logger.info("No fields in payment payload")
        return

    fields = payment_payload["fields"]
    user_id = cloud_event.data["name"].split("/documents/customers/")[1].split("/")[0]
    
    # Check payment status
    status = fields.get("status", {}).get("stringValue")
    if status != "succeeded":
        logger.info(f"Payment status is {status}, skipping credit")
        return

    try:
        # Extract token amount from price metadata
        items = fields.get("items", {}).get("arrayValue", {}).get("values", [])
        if not items:
            logger.warning("No items in payment")
            return

        first_item = items[0].get("mapValue", {}).get("fields", {})
        price = first_item.get("price", {}).get("mapValue", {}).get("fields", {})
        metadata = price.get("metadata", {}).get("mapValue", {}).get("fields", {})
        
        token_amount_str = metadata.get("tokens", {}).get("stringValue", "0")
        token_amount = int(token_amount_str)

        if token_amount <= 0:
            logger.warning(f"Invalid token amount: {token_amount}")
            return

        # Get payment ID
        payment_id = fields.get("id", {}).get("stringValue", "unknown")
        amount_usd = fields.get("amount", {}).get("integerValue", 0) / 100

        @transactional
        def credit_tokens_transaction(transaction):
            balance_ref = db.collection("tokenBalances").document(user_id)
            balance_doc = transaction.get(balance_ref)
            
            if not balance_doc.exists:
                logger.warning(f"Balance document not found for user {user_id}")
                return

            current_balance = balance_doc.to_dict().get("balance", 0)

            # Credit tokens
            transaction.update(balance_ref, {
                "balance": current_balance + token_amount,
                "lifetimeEarned": balance_doc.to_dict().get("lifetimeEarned", 0) + token_amount
            })

            # Log transaction
            tx_ref = db.collection("transactions").document()
            transaction.set(tx_ref, {
                "userId": user_id,
                "type": "purchase",
                "amount": token_amount,
                "balanceBefore": current_balance,
                "balanceAfter": current_balance + token_amount,
                "metadata": {
                    "stripePaymentId": payment_id,
                    "amountUsd": amount_usd
                },
                "timestamp": firestore.SERVER_TIMESTAMP
            })

            # Log purchase record
            purchase_ref = db.collection("purchases").document()
            transaction.set(purchase_ref, {
                "userId": user_id,
                "stripePaymentId": payment_id,
                "amount": amount_usd,
                "tokensAwarded": token_amount,
                "status": "completed",
                "timestamp": firestore.SERVER_TIMESTAMP
            })

        transaction = db.transaction()
        credit_tokens_transaction(transaction)
        
        logger.info(f"Credited {token_amount} tokens to user {user_id} for payment {payment_id}")

    except Exception as e:
        logger.error(f"Error processing payment success: {e}")
