from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from datetime import datetime


class UserCreateDTO(BaseModel):
    username: str = Field(..., min_length=3, max_length=150)
    password: str = Field(..., min_length=8)
    email: Optional[str] = Field(None)
    phone: Optional[str] = Field(None)
    first_name: Optional[str] = Field(None)
    last_name: Optional[str] = Field(None)
    created_by: Optional[str] = None


class UserLoginDTO(BaseModel):
    username: str
    password: str


class UserOutDTO(BaseModel):
    id: str
    username: str
    email: Optional[str]
    phone: Optional[str]
    first_name: Optional[str]
    last_name: Optional[str]
    created_by: Optional[str]
    created_date: Optional[datetime]
    last_updated_by: Optional[str]
    last_updated_date: Optional[datetime]


class TokenDTO(BaseModel):
    access_token: str
    token_type: str = "bearer"


class DocumentGenerateRequestDTO(BaseModel):
    document_type: str = Field(..., min_length=1, max_length=100)
    form_data: Dict[str, Any] = Field(...)


class DocumentOutDTO(BaseModel):
    id: str
    owner_id: str
    owner_role: str
    document_type: str
    status: str
    created_date: Optional[datetime]
    signed_url: str
    signed_url_expires_in: int


class KycVerificationOutDTO(BaseModel):
    id: str
    owner_id: str
    owner_role: str
    method: str
    verified: bool
    status: str
    message: str
    masked_aadhaar: str
    extracted_data: Dict[str, Any]
    failure_reason: Optional[str]
    created_date: Optional[datetime]


class PaymentOrderRequestDTO(BaseModel):
    amount: float = Field(..., gt=0, description="Amount in INR (rupees), e.g. 50000.00")


class PaymentOrderOutDTO(BaseModel):
    payment_id: str
    razorpay_order_id: str
    razorpay_key_id: str
    amount: float
    amount_paise: int
    currency: str
    status: str


class PaymentVerifyRequestDTO(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class PaymentCashRequestDTO(BaseModel):
    amount: float = Field(..., gt=0, description="Amount in INR (rupees) the customer handed over in cash")
    note: Optional[str] = Field(None, max_length=500)


class PaymentOutDTO(BaseModel):
    id: str
    owner_id: str
    owner_role: str
    amount: float
    currency: str
    status: str
    method: str
    verified: bool
    razorpay_order_id: Optional[str]
    razorpay_payment_id: Optional[str]
    created_date: Optional[datetime]


class VisitScheduleRequestDTO(BaseModel):
    customer_name: str = Field(..., min_length=1, max_length=200)
    customer_contact: Optional[str] = Field(None, max_length=200)
    date: str = Field(..., description="YYYY-MM-DD")
    time: str = Field(..., description="HH:MM, 24-hour")
    notes: Optional[str] = Field(None, max_length=1000)


class VisitOutDTO(BaseModel):
    id: str
    broker_id: str
    customer_name: str
    customer_contact: Optional[str]
    date: str
    time: str
    notes: Optional[str]
    status: str
    created_date: Optional[datetime]


class MarketTrendOutDTO(BaseModel):
    id: str
    city: str
    locality: Optional[str]
    property_type: str
    period_label: str
    as_of_date: str
    price_per_sqyd: float
    previous_price_per_sqyd: Optional[float]
    price_change_percent: Optional[float]
    trend_direction: str
    rental_yield_percent: Optional[float]
    demand_score: Optional[float]
    supply_score: Optional[float]
    demand_label: str
    sample_size: int


class MarketTrendListDTO(BaseModel):
    count: int
    trends: List[MarketTrendOutDTO]
