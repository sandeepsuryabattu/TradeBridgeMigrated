"""
Kotak Neo Trader — API wrapper for authentication and order management.
Uses neo_api_client with the new access_token auth flow (no consumer_secret).

[FIX #33] Token expiry tracking: Kotak JWT tokens expire at midnight IST.
          Added _token_expiry field + is_token_valid() so the bot detects
          stale tokens instead of silently failing API calls.
"""
import base64
import json as _json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

try:
    from neo_api_client import NeoAPI
except ImportError:
    NeoAPI = None

try:
    import pyotp
except ImportError:
    pyotp = None

from .config import Config

log = logging.getLogger(__name__)


class KotakTrader:
    """Wraps Kotak Neo API for auth, order placement, and data retrieval."""

    def __init__(self):
        self.client: Optional[NeoAPI] = None
        self.is_authenticated = False
        self.session_active = False
        self._last_login: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._token_expiry: Optional[datetime] = None  # [FIX #33] JWT exp timestamp


    @staticmethod
    def _iter_dicts(obj):
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from KotakTrader._iter_dicts(v)
        elif isinstance(obj, list):
            for item in obj:
                yield from KotakTrader._iter_dicts(item)

    @classmethod
    def _extract_error_message(cls, payload) -> Optional[str]:
        if not isinstance(payload, (dict, list)):
            return None

        for d in cls._iter_dicts(payload):
            if d.get("status") == "error":
                return str(d.get("message") or "Unknown error")
            if d.get("error") or d.get("Error") or d.get("Error Message"):
                return str(d.get("error") or d.get("Error") or d.get("Error Message"))

            stat = str(d.get("stat", "")).strip().lower()
            if stat in ("not_ok", "not ok", "notok"):
                return str(d.get("errMsg") or d.get("message") or "Kotak returned Not_Ok")

        return None

    # ── Authentication ──

    def initialize(self):
        """Create NeoAPI client with consumer_key."""
        if NeoAPI is None:
            log.warning("neo_api_client not installed — running in offline mode")
            self._last_error = "neo_api_client not installed"
            return False

        try:
            self.client = NeoAPI(
                environment="prod",
                consumer_key=Config.KOTAK_CONSUMER_KEY,
            )
            log.info("NeoAPI client initialized")
            return True
        except Exception as e:
            msg = f"Failed to initialize NeoAPI: {e}"
            log.error(msg)
            self._last_error = str(e)
            return False

    def login(self, retries: int = 1) -> dict:
        """Authenticate with Kotak Neo using TOTP + MPIN."""
        # Short-circuit if we already have an active session
        if self.session_active and self.is_authenticated:
            log.info("Kotak login called but session is already active; reusing existing session.")
            return {
                "status": "ok",
                "message": "Already authenticated",
                "data": {"last_login": self._last_login.isoformat() if self._last_login else None},
            }

        if not self.client:
            if not self.initialize():
                return {"status": "error", "message": "Client not initialized"}

        # Auto-generate TOTP
        if not Config.KOTAK_TOTP_SECRET:
            self._last_error = "KOTAK_TOTP_SECRET not configured"
            log.error("Cannot login — KOTAK_TOTP_SECRET not configured")
            return {"status": "error", "message": "KOTAK_TOTP_SECRET not configured"}

        if not pyotp:
            self._last_error = "pyotp not installed"
            log.error("Cannot login — pyotp not installed")
            return {"status": "error", "message": "pyotp not installed"}

        attempt = 0
        while True:
            attempt += 1
            try:
                totp_code = pyotp.TOTP(Config.KOTAK_TOTP_SECRET).now()
                log.info("Auto-generated TOTP code (len=%s) [attempt %s]", len(str(totp_code)), attempt)

                result = self.client.totp_login(
                    mobile_number=Config.KOTAK_MOBILE_NUMBER,
                    ucc=Config.KOTAK_CLIENT_ID,
                    totp=totp_code,
                )
                log.info("totp_login result: %s", result)

                if isinstance(result, dict) and (result.get("error") or result.get("Error")):
                    raise Exception(str(result.get("error") or result.get("Error")))

                time.sleep(1)

                result2 = self.client.totp_validate(mpin=Config.KOTAK_MPIN)
                log.info("totp_validate result: %s", result2)

                if isinstance(result2, dict) and (result2.get("error") or result2.get("Error")):
                    raise Exception(str(result2.get("error") or result2.get("Error")))

                self.is_authenticated = True
                self.session_active = True
                self._last_login = datetime.now()
                self._last_error = None

                self._token_expiry = self._extract_token_expiry(result2)
                if self._token_expiry:
                    log.info("✅ Kotak Neo login successful! Token expires at %s", self._token_expiry.isoformat())
                else:
                    log.info("✅ Kotak Neo login successful! (could not determine token expiry)")

                return {"status": "ok", "message": "Authenticated successfully", "data": result2}

            except Exception as e:
                self.is_authenticated = False
                self.session_active = False
                self._last_error = str(e)
                log.error("Login failed on attempt %s: %s", attempt, e)

                if attempt > retries:
                    return {"status": "error", "message": str(e)}

                time.sleep(2)

    def complete_2fa(self, otp: str = None) -> dict:
        """Legacy 2FA — now handled automatically in login()."""
        return self.login()

    # ── Token Expiry Helpers [FIX #33] ─────────────────────────────────────────

    def _extract_token_expiry(self, login_result: dict) -> Optional[datetime]:
        try:
            token_str = None
            if isinstance(login_result, dict):
                data = login_result.get("data", login_result)
                if isinstance(data, dict):
                    token_str = data.get("token")
            if not token_str:
                return None

            parts = token_str.split(".")
            if len(parts) < 2:
                return None
            payload_b64 = parts[1]
            payload_b64 += "=" * (4 - len(payload_b64) % 4)
            payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
            exp_ts = payload.get("exp")
            if exp_ts:
                return datetime.fromtimestamp(int(exp_ts), tz=timezone.utc)
        except Exception:
            log.exception("Could not extract token expiry from JWT")
        return None

    def is_token_valid(self) -> bool:
        if not self.is_authenticated or not self.session_active:
            return False
        if self._token_expiry:
            from datetime import timedelta
            return datetime.now(timezone.utc) < (self._token_expiry - timedelta(minutes=5))
        if self._last_login:
            from datetime import timedelta
            elapsed = (datetime.now() - self._last_login).total_seconds()
            return elapsed < 8 * 3600
        return False

    # ── Order Management ──

    def place_order(
        self,
        exchange_segment: str = "bse_fo",
        trading_symbol: str = "",
        transaction_type: str = "B",
        order_type: str = "L",
        quantity: int = 15,
        price: float = 0,
        product: str = "NRML",
        validity: str = "DAY",
        trigger_price: float = 0,
        tag: str = None,
    ) -> dict:
        if not self.client or not self.is_authenticated:
            return {"status": "error", "message": "Not authenticated"}
        if not self.is_token_valid():
            log.error("Token expired — cannot place order for %s. Re-login required.", trading_symbol)
            return {"status": "error", "message": "Session token expired — re-login required"}

        try:
            result = self.client.place_order(
                exchange_segment=exchange_segment,
                product=product,
                price=str(price),
                order_type=order_type,
                quantity=str(quantity),
                validity=validity,
                trading_symbol=trading_symbol,
                transaction_type=transaction_type,
                amo="NO",
                disclosed_quantity="0",
                market_protection="0",
                pf="N",
                trigger_price=str(trigger_price),
                tag=tag,
            )
            log.info(f"Order placed: {result}")
            err = self._extract_error_message(result)
            if err:
                return {"status": "error", "message": err, "data": result}
            return {"status": "ok", "data": result}
        except Exception as e:
            log.error(f"Order placement failed: {e}")
            return {"status": "error", "message": str(e)}

    def modify_order(
        self,
        order_id: str,
        price: float = 0,
        order_type: str = "SL",
        quantity: int = 0,
        validity: str = "DAY",
        trigger_price: float = 0,
        trading_symbol: str = "",
        exchange_segment: str = "bse_fo",
        product: str = "NRML",
        transaction_type: str = "S",
    ) -> dict:
        if not self.client or not self.is_authenticated:
            return {"status": "error", "message": "Not authenticated"}
        if not self.is_token_valid():
            log.error("Token expired — cannot modify order %s. Re-login required.", order_id)
            return {"status": "error", "message": "Session token expired — re-login required"}
        try:
            result = self.client.modify_order(
                order_id=order_id,
                price=str(price),
                order_type=order_type,
                quantity=str(quantity),
                validity=validity,
                trigger_price=str(trigger_price),
                trading_symbol=trading_symbol,
                exchange_segment=exchange_segment,
                product=product,
                transaction_type=transaction_type,
            )
            log.info(f"Order modified: {result}")
            err = self._extract_error_message(result)
            if err:
                return {"status": "error", "message": err, "data": result}
            return {"status": "ok", "data": result}
        except Exception as e:
            log.error(f"Order modification failed: {e}")
            return {"status": "error", "message": str(e)}

    def cancel_order(self, order_id: str) -> dict:
        if not self.client or not self.is_authenticated:
            return {"status": "error", "message": "Not authenticated"}
        if not self.is_token_valid():
            log.error("Token expired — cannot cancel order %s. Re-login required.", order_id)
            return {"status": "error", "message": "Session token expired — re-login required"}
        try:
            result = self.client.cancel_order(order_id=order_id)
            err = self._extract_error_message(result)
            if err:
                return {"status": "error", "message": err, "data": result}
            return {"status": "ok", "data": result}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def order_history(self, order_id: str) -> dict:
        if not self.client or not self.is_authenticated:
            return {"status": "error", "message": "Not authenticated"}
        try:
            result = self.client.order_history(order_id=order_id)
            err = self._extract_error_message(result)
            if err:
                return {"status": "error", "message": err, "data": result}
            return {"status": "ok", "data": result}
        except Exception as e:
            log.error(f"Order history failed: {e}")
            return {"status": "error", "message": str(e)}

    def get_limits(self) -> dict:
        if not self.client or not self.is_authenticated:
            return {"status": "error", "message": "Not authenticated"}
        try:
            result = self.client.limits(segment="ALL", exchange="ALL", product="ALL")
            err = self._extract_error_message(result)
            if err:
                return {"status": "error", "message": err, "data": result}
            return {"status": "ok", "data": result}
        except Exception as e:
            log.error(f"Get limits failed: {e}")
            return {"status": "error", "message": str(e)}

    # ── Data Retrieval ──

    def search_scrip(self, symbol: str, expiry: str = "", option_type: str = "", strike_price: str = "") -> dict:
        if not self.client:
            return {}
        try:
            result = self.client.search_scrip(
                exchange_segment="bse_fo",
                symbol=symbol,
                expiry=expiry,
                option_type=option_type,
                strike_price=strike_price,
            )
            return result or {}
        except Exception as e:
            log.error(f"Scrip search failed: {e}")
            return {}

    def get_positions(self) -> dict:
        if not self.client:
            return {}
        try:
            return self.client.positions() or {}
        except Exception as e:
            log.error(f"Positions fetch failed: {e}")
            return {}

    def get_order_book(self) -> dict:
        if not self.client:
            return {}
        try:
            return self.client.order_report() or {}
        except Exception as e:
            log.error(f"Order book fetch failed: {e}")
            return {}

    def get_trade_book(self) -> dict:
        if not self.client:
            return {}
        try:
            return self.client.trade_report() or {}
        except Exception as e:
            log.error(f"Trade book fetch failed: {e}")
            return {}

    def get_holdings(self) -> dict:
        if not self.client:
            return {}
        try:
            return self.client.holdings() or {}
        except Exception as e:
            log.error(f"Holdings fetch failed: {e}")
            return {}

    def get_status(self) -> dict:
        if not any(Config.kotak_env().values()):
            login_state = "not_configured"
        elif NeoAPI is None or pyotp is None:
            login_state = "dependency_missing"
        elif self.session_active and self.is_authenticated:
            login_state = "logged_in"
        elif self._last_error:
            login_state = "login_failed"
        else:
            login_state = "unknown"

        return {
            "initialized": self.client is not None,
            "authenticated": self.is_authenticated,
            "session_active": self.session_active,
            "last_login": self._last_login.isoformat() if self._last_login else None,
            "last_error": self._last_error,
            "login_state": login_state,
        }
