from pydantic import BaseModel, Field, EmailStr, field_validator
from typing import Optional, Dict, Any, List, Literal
from datetime import datetime

BROKER_PROJECTS = ("suraksha-enclave", "ops-divine-greens")


class ForgotPasswordDTO(BaseModel):
    email: EmailStr


class ResetPasswordDTO(BaseModel):
    email: EmailStr
    otp: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")
    new_password: str = Field(..., min_length=8)


class MessageDTO(BaseModel):
    message: str


class UserCreateDTO(BaseModel):
    username: str = Field(..., min_length=3, max_length=150)
    password: str = Field(..., min_length=8)
    email: Optional[str] = Field(None)
    phone: str = Field(..., min_length=1)
    first_name: Optional[str] = Field(None)
    last_name: Optional[str] = Field(None)
    created_by: Optional[str] = None

    @field_validator("phone")
    @classmethod
    def phone_must_not_be_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("field required")
        return v


class BrokerCreateDTO(UserCreateDTO):
    project: Literal["suraksha-enclave", "ops-divine-greens"] = Field(...)


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
    project: Optional[str] = None
    created_by: Optional[str]
    created_date: Optional[datetime]
    last_updated_by: Optional[str]
    last_updated_date: Optional[datetime]


class TokenDTO(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AdminCreateDTO(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=150)
    employee_id: str = Field(..., min_length=3, max_length=32)
    email: EmailStr
    password: str = Field(..., min_length=8)
    created_by: Optional[str] = None

    @field_validator("employee_id")
    @classmethod
    def employee_id_must_start_with_dv(cls, v: str) -> str:
        v = v.strip().upper()
        if not v.startswith("DV"):
            raise ValueError("employee_id must start with 'DV'")
        return v

    @field_validator("full_name")
    @classmethod
    def full_name_must_not_be_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("field required")
        return v


class AdminLoginDTO(BaseModel):
    email: EmailStr
    password: str


class AdminOutDTO(BaseModel):
    id: str
    full_name: str
    employee_id: str
    email: str
    created_by: Optional[str]
    created_date: Optional[datetime]
    last_updated_by: Optional[str]
    last_updated_date: Optional[datetime]


class AdminTokenDTO(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class AdminAccessTokenDTO(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class AdminRefreshDTO(BaseModel):
    refresh_token: str = Field(..., min_length=1)


class CustomerListItemDTO(BaseModel):
    id: str
    full_name: str
    email: Optional[str]
    phone: Optional[str]
    source: Literal["WEBSITE", "BROKER_CHANNEL"]
    status: Literal["LEAD", "ACTIVE", "BOOKED", "INACTIVE"]
    created_at: Optional[datetime]
    last_activity_at: Optional[datetime]


class PaginationDTO(BaseModel):
    page: int
    page_size: int
    total_items: int
    total_pages: int


class CustomerListResponseDTO(BaseModel):
    items: List[CustomerListItemDTO]
    pagination: PaginationDTO


class CustomerCreateDTO(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=200)
    email: EmailStr
    phone: str = Field(..., min_length=1, max_length=20)

    @field_validator("full_name")
    @classmethod
    def full_name_must_not_be_blank(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("full_name must be at least 2 characters")
        return v

    @field_validator("phone")
    @classmethod
    def phone_must_not_be_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("field required")
        return v


class DocumentGenerateRequestDTO(BaseModel):
    document_type: str = Field(..., min_length=1, max_length=100)
    form_data: Dict[str, Any] = Field(...)


class DocumentOutDTO(BaseModel):
    # Returned by every document endpoint, including GET /documents/{id} and
    # GET /documents/latest/{document_type} which re-sign an existing document of
    # ANY type (identity photos, generated PDFs, booking applications) so an
    # expired signed_url can be refreshed. Shape is stable across all of them;
    # payment_plan / inventory_* are only populated on a booking-application upload.
    id: str
    owner_id: str
    owner_role: str
    document_type: str
    status: str
    created_date: Optional[datetime]
    signed_url: str
    signed_url_expires_in: int
    # Only set on a booking-application upload: the derived payment schedule
    # (total_receivable, total_received, total_outstanding, total_outstanding_words,
    # booking_date, rows[]). Feeds the demand / allotment letters.
    payment_plan: Optional[Dict[str, Any]] = None
    # Booking-application upload only: the linked plot and whether this upload
    # confirmed it as 'booked' ("booked" | "conflict" | null).
    inventory_id: Optional[str] = None
    inventory_status: Optional[str] = None


class InventoryBookingResultDTO(BaseModel):
    id: str
    status: str
    booked_at: Optional[datetime] = None
    booked_payment_id: Optional[str] = None
    booked_by: Optional[str] = None


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
    # "plot_booking" + inventory_id locks that unit to 'booked' when the payment
    # settles. "installment" + installment_no pays a specific payment_schedule
    # milestone; inventory_id is optional there too - it says WHICH plot's
    # milestone #N when the customer holds more than one booking (ignored for a
    # single-booking customer). Omit purpose for any other payment.
    purpose: Literal["plot_booking", "installment", "other"] = "other"
    inventory_id: Optional[str] = Field(None, max_length=36)
    installment_no: Optional[int] = Field(None, ge=1, description="1-based milestone position")
    due_date: Optional[str] = Field(None, max_length=10, description="that milestone's ISO due date")


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
    purpose: Literal["plot_booking", "installment", "other"] = "other"
    # For "plot_booking": the unit to lock. For "installment" (optional): which
    # plot's milestone #N, when the customer holds more than one booking.
    inventory_id: Optional[str] = Field(None, max_length=36)
    installment_no: Optional[int] = Field(None, ge=1)
    due_date: Optional[str] = Field(None, max_length=10)


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
    # Booking linkage. inventory_status is "booked" when this call locked the plot,
    # "conflict" when the payment settled but the unit was already taken (see
    # inventory_conflict_reason), or null on a non-booking payment / plain read.
    inventory_id: Optional[str] = None
    inventory_status: Optional[str] = None
    inventory_conflict_reason: Optional[str] = None
    # Instalment linkage. installment_status is "paid" when this call marked the
    # milestone paid, "rejected" when the payment settled but failed a guard rail
    # (money kept, flagged for review), or null on a non-instalment payment.
    purpose: Optional[str] = None
    installment_no: Optional[int] = None
    installment_status: Optional[str] = None


class VisitScheduleRequestDTO(BaseModel):
    customer_name: str = Field(..., min_length=1, max_length=200)
    customer_contact: Optional[str] = Field(None, max_length=200)
    date: str = Field(..., description="YYYY-MM-DD")
    time: str = Field(..., description="HH:MM, 24-hour")
    notes: Optional[str] = Field(None, max_length=1000)


class VisitCompleteRequestDTO(BaseModel):
    # Only "completed" is supported via PATCH; anything else -> 422.
    status: Literal["completed"]
    notes: str = Field("", max_length=1000, description="Meeting outcome; may be empty")


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


class ReservedUnitOutDTO(InventoryUnitOutDTO):
    reserved_at: Optional[str]
    reserved_until: Optional[str]


class ReserveInventoryResponseDTO(ReservedUnitOutDTO):
    pass


class MyReservationsResponseDTO(BaseModel):
    count: int
    reservations: List[ReservedUnitOutDTO]


class InventoryBookRepairRequestDTO(BaseModel):
    payment_id: Optional[str] = Field(None, max_length=36)
    reason: Optional[str] = Field(None, max_length=200)


class InventoryUnbookRequestDTO(BaseModel):
    reason: Optional[str] = Field(None, max_length=200)


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
    filename: Optional[str] = None
    target: Optional[str] = None  # e.g. "_self" - navigate in the same tab, never "_blank"


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
    # Client-side route to navigate to (same tab). Set e.g. after a successful
    # in-chat login that was started from "Browse & Book Plots".
    redirect_url: Optional[str] = None
    redirect_target: Optional[str] = None  # "_self" - never open a new tab


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


class LoanReportApplicantDTO(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None


class LoanEligibleRangeDTO(BaseModel):
    low: Optional[float] = None
    high: Optional[float] = None


class LoanReportAmortRowDTO(BaseModel):
    month: Optional[int] = None
    principal_component: Optional[float] = None
    interest_component: Optional[float] = None
    balance: Optional[float] = None


class LoanReportDataDTO(BaseModel):
    """Flattened eligibility-report values for client-side (pdf-lib) rendering.
    Every figure is optional - a partial snapshot yields nulls, never an error."""
    report_id: str
    issued_at: str
    applicant: LoanReportApplicantDTO = Field(default_factory=LoanReportApplicantDTO)
    eligible_amount: Optional[float] = None
    eligible_amount_words: Optional[str] = None
    eligible_loan_range: Optional[LoanEligibleRangeDTO] = None
    eligibility_category: Optional[str] = None
    rate_pct: Optional[float] = None
    rate_is_illustrative: bool = False
    tenure_years: Optional[float] = None
    tenure_months: Optional[int] = None
    emi: Optional[float] = None
    total_interest: Optional[float] = None
    total_payment: Optional[float] = None
    monthly_income: Optional[float] = None
    co_applicant_income: Optional[float] = None
    combined_income: Optional[float] = None
    existing_obligations: Optional[float] = None
    foir_pct: Optional[float] = None
    employment_type: Optional[str] = None
    amortization_first_year: Optional[List[LoanReportAmortRowDTO]] = None


# ---- Customer profile -------------------------------------------------------

class CustomerAddressDTO(BaseModel):
    line1: Optional[str] = None
    line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None


class PaymentScheduleRowDTO(BaseModel):
    id: Optional[str] = None
    label: Optional[str] = None
    percent: Optional[float] = None
    due_days: Optional[int] = None
    due_date: Optional[str] = None
    amount: Optional[int] = None
    status: Optional[str] = None          # paid | due | overdue | upcoming
    pay_enabled_from: Optional[str] = None  # due_date - 5 days
    paid_on: Optional[str] = None
    paid_payment_id: Optional[str] = None


class CustomerNextDueDTO(BaseModel):
    milestone_id: Optional[str] = None
    label: Optional[str] = None
    amount: Optional[int] = None
    due_date: Optional[str] = None
    days_until_due: Optional[int] = None   # negative when overdue
    status: Optional[str] = None
    last_reminder_kind: Optional[str] = None
    last_reminder_at: Optional[str] = None


class CustomerBookingDTO(BaseModel):
    has_booking: bool = False
    id: Optional[str] = None
    document_id: Optional[str] = None
    inventory_id: Optional[str] = None
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    township_label: Optional[str] = None
    unit_number: Optional[str] = None
    plot_area_sq_yd: Optional[str] = None
    unit_type: Optional[str] = None
    booking_date: Optional[str] = None
    total_consideration: Optional[int] = None
    amount_received: Optional[int] = None
    payment_id: Optional[str] = None
    booking_payment_amount: Optional[int] = None
    payment_method: Optional[str] = None
    razorpay_order_id: Optional[str] = None
    razorpay_payment_id: Optional[str] = None
    payment_created_date: Optional[str] = None
    payment_schedule: Optional[List[PaymentScheduleRowDTO]] = None
    next_due: Optional[CustomerNextDueDTO] = None


class CustomerProfileDTO(BaseModel):
    customer_id: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    gender: Optional[str] = None
    date_of_birth: Optional[str] = None
    age: Optional[int] = None
    address: Optional[CustomerAddressDTO] = None
    address_text: Optional[str] = None
    # `booking` is the single most-recent / active booking - always present, unchanged
    # shape, kept for existing clients. A customer may now hold more than one plot;
    # `bookings` is the additive full list (newest first), each entry a
    # complete CustomerBookingDTO with its own unit_number / total_consideration /
    # amount_received / booking_date / payment_schedule. `booking` mirrors bookings[0]
    # when the list is non-empty. Old clients that only read `booking` keep working.
    booking: CustomerBookingDTO = Field(default_factory=CustomerBookingDTO)
    bookings: List[CustomerBookingDTO] = Field(default_factory=list)
