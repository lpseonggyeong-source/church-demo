"""Internal interface for hosted payments."""
import hashlib
import hmac
import os
import secrets
from abc import ABC, abstractmethod


class PaymentProvider(ABC):
    @abstractmethod
    def create_session(self, order):
        """Return an external payment reference and optional checkout URL."""

    @abstractmethod
    def verify_event(self, order, event, signature):
        """Return a verified final payment state."""


class MockPaymentProvider(PaymentProvider):
    def __init__(self):
        self.key = secrets.token_bytes(32)

    def create_session(self, order):
        return {"reference": "mock_" + secrets.token_hex(16), "checkout_url": None}

    def sign_event(self, event):
        payload = "|".join(str(event[k]) for k in ("reference", "amount", "status"))
        return hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()

    def verify_event(self, order, event, signature):
        if not hmac.compare_digest(signature, self.sign_event(event)):
            raise ValueError("결제 결과 서명을 확인할 수 없습니다.")
        if event["reference"] != order["reference"] or event["amount"] != order["amount"]:
            raise ValueError("결제 결과가 요청 정보와 일치하지 않습니다.")
        if event["status"] not in {"PAID", "FAILED", "CANCELLED"}:
            raise ValueError("지원하지 않는 결제 상태입니다.")
        return event["status"]


class LinkPaymentTestProvider(PaymentProvider):
    """3DS browser SDK from the LinkPayment card sample."""

    def __init__(self):
        self.public_key = os.environ.get("LINKPAYMENT_TEST_PUBLIC_KEY", "").strip()
        if not self.public_key.startswith("pk") or os.environ.get("LINKPAYMENT_TEST_TERMINAL_CONFIRMED") != "yes":
            raise SystemExit("PG 담당자가 확인한 테스트 단말기의 공개 키를 설정해 주세요.")

    def create_session(self, order):
        return {"reference": "lp_test_" + secrets.token_hex(16), "checkout_url": None}

    def checkout_payload(self, order):
        # The working HTML sample passes the same digit string for both fields.
        amount_digits = str(order["amount"])
        if not amount_digits.isascii() or not amount_digits.isdecimal() or int(amount_digits) <= 0:
            raise ValueError("PG에 전달할 결제금액은 양의 정수여야 합니다.")
        return {
            "publicKey": self.public_key,
            "payRoute": "3ds",
            "amount": amount_digits,
            "udf1": order["id"],
            "udf2": "ongil-test",
            "products": [{
                "name": order["category"], "qty": "1", "price": amount_digits,
            }],
            "mode": "layer",
            "debugMode": "live",
        }

    def verify_event(self, order, event, signature):
        raise ValueError("서버에서 확인한 PG 거래 결과가 필요합니다.")


def configured_provider():
    mode = os.environ.get("PAYMENT_MODE", "mock")
    if mode == "mock":
        return MockPaymentProvider()
    if mode == "linkpayment-test":
        return LinkPaymentTestProvider()
    raise SystemExit("지원하는 결제 모드는 mock, linkpayment-test입니다.")
