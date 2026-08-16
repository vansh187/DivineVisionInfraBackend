import math
import os
import uuid
import razorpay
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from Divinepersistence import persistenceBrokerCommission


ALLOWED_COMMISSION_STATUSES = ("pending", "paid", "rejected")
ALLOWED_TRANSACTION_MODES = ("cash", "booking")
MAX_MONEY_VALUE = Decimal("999999999999.99")


class serviceBrokerCommission:
    def __init__(self, persistence: persistenceBrokerCommission = None):
        self._persistence = persistence or persistenceBrokerCommission()
        self._key_id = os.getenv("RAZORPAY_KEY_ID")
        self._key_secret = os.getenv("RAZORPAY_KEY_SECRET")

    def _client(self):
        try:
            if not self._key_id or not self._key_secret:
                raise RuntimeError("payment_not_configured")
            return razorpay.Client(auth=(self._key_id, self._key_secret))
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError("payment_client_failed") from e

    def _clean_text(self, value, field_name: str, required: bool, max_length: int):
        try:
            if value is None:
                if required:
                    raise ValueError(f"{field_name}_required")
                return None
            cleaned = str(value).strip()
            if not cleaned:
                if required:
                    raise ValueError(f"{field_name}_required")
                return None
            if len(cleaned) > max_length:
                raise ValueError(f"{field_name}_too_long")
            return cleaned
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"invalid_{field_name}") from e

    def _money(self, value, field_name: str, required: bool):
        try:
            if value is None:
                if required:
                    raise ValueError(f"{field_name}_required")
                return None
            parsed = Decimal(str(value))
            if not math.isfinite(float(parsed)):
                raise ValueError(f"invalid_{field_name}")
            parsed = parsed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if parsed < 0 or parsed > MAX_MONEY_VALUE:
                raise ValueError(f"invalid_{field_name}")
            if required and parsed <= 0:
                raise ValueError(f"invalid_{field_name}")
            return parsed
        except ValueError:
            raise
        except (InvalidOperation, TypeError) as e:
            raise ValueError(f"invalid_{field_name}") from e

    def _timestamp(self):
        try:
            return datetime.now(timezone.utc)
        except Exception:
            return datetime.utcnow().replace(tzinfo=timezone.utc)

    def _format_datetime(self, value):
        try:
            if value is None:
                return None
            if isinstance(value, str):
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            else:
                parsed = value
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
            return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        except Exception:
            return None

    def _format_money(self, value):
        try:
            if value is None:
                return None
            return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        except Exception:
            return None

    def _format_commission(self, record):
        try:
            return {
                "id": getattr(record, "id", ""),
                "brokerId": getattr(record, "broker_id", ""),
                "serialNumber": getattr(record, "serial_number", ""),
                "unitAddress": getattr(record, "unit_address", ""),
                "customerName": getattr(record, "customer_name", None),
                "township": getattr(record, "township", None),
                "saleValue": self._format_money(getattr(record, "sale_value", None)),
                "commissionAmount": self._format_money(getattr(record, "commission_amount", 0)) or 0,
                "status": getattr(record, "status", "paid") if getattr(record, "status", "paid") in ALLOWED_COMMISSION_STATUSES else "paid",
                "transactionMode": getattr(record, "transaction_mode", "cash") if getattr(record, "transaction_mode", "cash") in ALLOWED_TRANSACTION_MODES else "cash",
                "createdAt": self._format_datetime(getattr(record, "created_at", None)),
                "paidAt": self._format_datetime(getattr(record, "paid_at", None)),
                "rejectedAt": self._format_datetime(getattr(record, "rejected_at", None)),
            }
        except Exception as e:
            raise ValueError("invalid_commission_record") from e

    def create_commission(self, brokerId: str, serialNumber: str, unitAddress: str,
                          commissionAmount, transactionMode: str, customerName: str = None,
                          township: str = None, saleValue=None, source: str = "broker",
                          razorpay_order_id: str = None):
        try:
            broker_id = self._clean_text(brokerId, "brokerId", True, 80)
            serial_number = self._clean_text(serialNumber, "serialNumber", True, 100)
            unit_address = self._clean_text(unitAddress, "unitAddress", True, 500)
            customer_name = self._clean_text(customerName, "customerName", False, 200)
            township_value = self._clean_text(township, "township", False, 200)
            sale_value = self._money(saleValue, "saleValue", False)
            commission_amount = self._money(commissionAmount, "commissionAmount", True)
            mode = self._clean_text(transactionMode, "transactionMode", True, 20).lower()
            if mode not in ALLOWED_TRANSACTION_MODES:
                raise ValueError("invalid_transactionMode")
            if source == "broker" and mode != "cash":
                raise ValueError("transactionMode_must_be_cash")
            if source == "admin" and mode != "booking":
                raise ValueError("admin_transactionMode_must_be_booking")

            now = self._timestamp()
            status = "pending" if source == "admin" else "paid"
            paid_at = None if source == "admin" else now
            record = self._persistence.create_commission(
                id="com_" + uuid.uuid4().hex,
                broker_id=broker_id,
                serial_number=serial_number,
                unit_address=unit_address,
                customer_name=customer_name,
                township=township_value,
                sale_value=float(sale_value) if sale_value is not None else None,
                commission_amount=float(commission_amount),
                status=status,
                transaction_mode=mode,
                razorpay_order_id=razorpay_order_id,
                created_at=now,
                paid_at=paid_at,
                rejected_at=None,
                last_updated_at=now,
            )
            return self._format_commission(record)
        except ValueError:
            raise
        except Exception as e:
            raise RuntimeError("commission_create_failed") from e

    def create_paid_commission(self, brokerId: str, serialNumber: str, unitAddress: str,
                               commissionAmount, transactionMode: str, customerName: str = None,
                               township: str = None, saleValue=None):
        try:
            return self.create_commission(
                brokerId=brokerId,
                serialNumber=serialNumber,
                unitAddress=unitAddress,
                customerName=customerName,
                township=township,
                saleValue=saleValue,
                commissionAmount=commissionAmount,
                transactionMode=transactionMode,
                source="broker",
            )
        except ValueError:
            raise
        except Exception as e:
            raise RuntimeError("commission_create_failed") from e

    def initiate_admin_razorpay_commission(self, brokerId: str, serialNumber: str, unitAddress: str,
                                           commissionAmount, transactionMode: str, customerName: str = None,
                                           township: str = None, saleValue=None):
        try:
            amount = self._money(commissionAmount, "commissionAmount", True)
            mode = self._clean_text(transactionMode, "transactionMode", True, 20).lower()
            if mode != "booking":
                raise ValueError("admin_transactionMode_must_be_booking")

            amount_paise = int(round(float(amount) * 100))
            try:
                order = self._client().order.create({
                    "amount": amount_paise,
                    "currency": "INR",
                    "payment_capture": 1,
                    "notes": {
                        "brokerId": self._clean_text(brokerId, "brokerId", True, 80),
                        "serialNumber": self._clean_text(serialNumber, "serialNumber", True, 100),
                        "purpose": "broker_commission",
                    },
                })
            except RuntimeError:
                raise
            except Exception as e:
                raise RuntimeError(f"payment_order_failed:{type(e).__name__}") from e

            commission = self.create_commission(
                brokerId=brokerId,
                serialNumber=serialNumber,
                unitAddress=unitAddress,
                customerName=customerName,
                township=township,
                saleValue=saleValue,
                commissionAmount=commissionAmount,
                transactionMode=transactionMode,
                source="admin",
                razorpay_order_id=order["id"],
            )
            return commission, {
                "razorpayOrderId": order["id"],
                "razorpayKeyId": self._key_id,
                "amount": float(amount),
                "amountPaise": amount_paise,
                "currency": "INR",
                "status": "created",
            }
        except ValueError:
            raise
        except Exception as e:
            if isinstance(e, RuntimeError):
                raise
            raise RuntimeError("commission_create_failed") from e

    def list_for_broker(self, brokerId: str):
        try:
            broker_id = self._clean_text(brokerId, "brokerId", True, 80)
            records = self._persistence.list_by_broker(broker_id)
            summary_rows = self._persistence.summary_by_broker(broker_id)
            summary = {"pending": 0.0, "paid": 0.0, "rejected": 0.0}
            for row in summary_rows:
                status = getattr(row, "status", None)
                if status in summary:
                    summary[status] = self._format_money(getattr(row, "total", 0)) or 0.0
            return {
                "success": True,
                "commissions": [self._format_commission(record) for record in records],
                "summary": summary,
            }
        except ValueError:
            raise
        except Exception as e:
            raise RuntimeError("commission_list_failed") from e
