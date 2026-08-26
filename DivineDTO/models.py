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


class InventoryUnitOutDTO(BaseModel):
    id: str
    project_name: str
    city: str
    locality: Optional[str]
    block: Optional[str]
    unit_number: str
    unit_type: str
    width_mtr: Optional[float]
    length_mtr: Optional[float]
    area_sqmt: Optional[float]
    area_sqyd: Optional[float]
    status: str
    estimated_price: Optional[float]


class InventorySearchResponseDTO(BaseModel):
    count: int
    units: List[InventoryUnitOutDTO]


class NLSearchRequestDTO(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    session_id: Optional[str] = None


class NLSearchResponseDTO(BaseModel):
    count: int
    units: List[InventoryUnitOutDTO]
    parsed_filters: Dict[str, Any]


class InventoryViewRequestDTO(BaseModel):
    lead_id: Optional[str] = None
    session_id: Optional[str] = None


class InventoryViewResponseDTO(BaseModel):
    recorded: bool


class RecommendationResponseDTO(BaseModel):
    best_fit: List[InventoryUnitOutDTO]
    similar_alternatives: Dict[str, List[InventoryUnitOutDTO]]


class BrokerCommissionCreateDTO(BaseModel):
    brokerId: str = Field(..., min_length=1, max_length=80)
    serialNumber: str = Field(..., min_length=1, max_length=100)
    unitAddress: str = Field(..., min_length=1, max_length=500)
    customerName: Optional[str] = Field(None, max_length=200)
    township: Optional[str] = Field(None, max_length=200)
    saleValue: Optional[float] = Field(None, ge=0)
    commissionAmount: float = Field(..., gt=0)
    transactionMode: str = Field(..., min_length=1, max_length=20)


class BrokerCommissionOutDTO(BaseModel):
    id: str
    brokerId: str
    serialNumber: str
    unitAddress: str
    customerName: Optional[str]
    township: Optional[str]
    saleValue: Optional[float]
    commissionAmount: float
    status: str
    transactionMode: str
    createdAt: str
    paidAt: Optional[str]
    rejectedAt: Optional[str]


class BrokerCommissionPaymentOutDTO(BaseModel):
    razorpayOrderId: str
    razorpayKeyId: str
    amount: float
    amountPaise: int
    currency: str
    status: str


class BrokerCommissionCreateResponseDTO(BaseModel):
    success: bool
    commission: BrokerCommissionOutDTO


class AdminCommissionPaymentResponseDTO(BaseModel):
    success: bool
    commission: BrokerCommissionOutDTO
    payment: BrokerCommissionPaymentOutDTO


class BrokerCommissionSummaryDTO(BaseModel):
    pending: float
    paid: float
    rejected: float


class BrokerCommissionListResponseDTO(BaseModel):
    success: bool
    commissions: List[BrokerCommissionOutDTO]
    summary: BrokerCommissionSummaryDTO


class SessionInitRequestDTO(BaseModel):
    referrer: Optional[str] = Field(None, max_length=2000)
    utm_source: Optional[str] = Field(None, max_length=120)
    utm_medium: Optional[str] = Field(None, max_length=120)
    utm_campaign: Optional[str] = Field(None, max_length=120)
    device_type: Optional[str] = Field(None, max_length=40)


class SessionInitResponseDTO(BaseModel):
    session_id: str
    lead_id: str


class ChatMessageRequestDTO(BaseModel):
    session_id: str
    text: Optional[str] = Field(None, max_length=4000)
    audio_b64: Optional[str] = Field(None, description="Base64-encoded audio for voice input")
    intent: Optional[str] = Field(None, description="e.g. 'request_callback'")
    precise_lat: Optional[float] = None
    precise_long: Optional[float] = None


class CallbackConfirmedDTO(BaseModel):
    name: str
    phone: str
    preferred_time: str


class ChatbotButtonDTO(BaseModel):
    label: str
    value: str
    action: str
    url: Optional[str] = None


class ChatMessageResponseDTO(BaseModel):
    session_id: str
    reply: str
    buttons: Optional[List[ChatbotButtonDTO]] = None
    callback_confirmed: Optional[CallbackConfirmedDTO] = None
    account_created: Optional[Dict[str, Any]] = None
    auth_token: Optional[str] = None
    auth_role: Optional[str] = None
    guardrail_passed: Optional[bool] = None
    llm_provider: Optional[str] = None
    structured_result: Optional[Dict[str, Any]] = None


class CallbackRequestOutDTO(BaseModel):
    id: str
    lead_id: str
    visitor_name: str
    phone: str
    preferred_time: str
    status: str
    requested_at: Optional[datetime]
    actioned_at: Optional[datetime]


# ---- Home Loan Assistant ------------------------------------------------

class EMICalculateRequestDTO(BaseModel):
    principal: float = Field(..., gt=0)
    annual_rate_pct: float = Field(..., gt=0, lt=30)
    tenure_years: float = Field(..., ge=1, le=30)
    include_schedule: bool = False


class AmortizationRowDTO(BaseModel):
    month: int
    principal_component: float
    interest_component: float
    balance: float


class EMICalculateResponseDTO(BaseModel):
    principal: float
    annual_rate_pct: float
    tenure_years: float
    tenure_months: int
    emi: float
    total_interest: float
    total_payment: float
    schedule: Optional[List[AmortizationRowDTO]] = None


class EligibilityRequestDTO(BaseModel):
    monthly_income: float = Field(..., ge=0)
    co_applicant_income: Optional[float] = Field(None, ge=0)
    existing_emi: Optional[float] = Field(None, ge=0)
    requested_loan: Optional[float] = Field(None, gt=0)
    tenure_years: float = Field(20, ge=1, le=30)
    interest_rate: Optional[float] = Field(None, gt=0, lt=30)
    credit_score_band: Optional[str] = None
    age: Optional[float] = Field(None, ge=21, le=70)


class EligibilityResponseDTO(BaseModel):
    combined_income: float
    existing_emi: float
    available_emi_capacity: float
    comfortable_emi_range: Dict[str, float]
    eligible_loan_range: Dict[str, float]
    requested_loan: Optional[float] = None
    category: str
    reasons: List[str]
    foir_cap: float
    interest_rate_used: float
    illustrative_rate_used: bool
    credit_score_band: Optional[str] = None


class AffordabilityRequestDTO(BaseModel):
    property_price: float = Field(..., gt=0)
    down_payment: Optional[float] = Field(None, ge=0)
    monthly_income: float = Field(..., ge=0)
    co_applicant_income: Optional[float] = Field(None, ge=0)
    existing_emi: Optional[float] = Field(None, ge=0)
    tenure_years: float = Field(20, ge=1, le=30)
    interest_rate: Optional[float] = Field(None, gt=0, lt=30)


class AffordabilityResponseDTO(BaseModel):
    property_price: float
    down_payment: float
    required_loan: float
    required_emi: float
    emi_capacity: float
    gap: float
    affordable: bool
    interest_rate_used: float
    illustrative_rate_used: bool
    suggestions: List[str]
    loan_to_property_ratio: Optional[float] = None


class TenureCompareRequestDTO(BaseModel):
    principal: float = Field(..., gt=0)
    annual_rate_pct: float = Field(..., gt=0, lt=30)
    tenure_options_years: List[float] = Field(..., min_length=1)


class TenureCompareResponseDTO(BaseModel):
    rows: List[EMICalculateResponseDTO]


class ReportGenerateRequestDTO(BaseModel):
    profile: Dict[str, Any] = Field(...)
    last_calculation: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None
    lead_id: Optional[str] = None


class ReportGenerateResponseDTO(BaseModel):
    report_id: str
