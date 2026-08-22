import uuid
import io

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from DivineDTO.models import (
    EMICalculateRequestDTO, EMICalculateResponseDTO, AmortizationRowDTO,
    EligibilityRequestDTO, EligibilityResponseDTO,
    AffordabilityRequestDTO, AffordabilityResponseDTO,
    TenureCompareRequestDTO, TenureCompareResponseDTO,
    ReportGenerateRequestDTO, ReportGenerateResponseDTO,
)
from DivineService.service_loan_calculator import (
    calculate_emi, build_amortization_schedule, compare_tenures,
)
from DivineService.service_loan_eligibility import calculate_loan_eligibility, calculate_affordability
from DivineService.service_loan_report import generate_eligibility_report_pdf
from Divinepersistence.persistence_loan import persistenceLoan

router = APIRouter(prefix="/loan", tags=["loan"])
_persistence = persistenceLoan()


@router.post("/emi", response_model=EMICalculateResponseDTO)
def emi(dto: EMICalculateRequestDTO):
    try:
        result = calculate_emi(dto.principal, dto.annual_rate_pct, dto.tenure_years)
        if dto.include_schedule:
            schedule = build_amortization_schedule(dto.principal, dto.annual_rate_pct, dto.tenure_years)
            result["schedule"] = [AmortizationRowDTO(**row) for row in schedule]
        return EMICalculateResponseDTO(**result)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/eligibility", response_model=EligibilityResponseDTO)
def eligibility(dto: EligibilityRequestDTO):
    try:
        result = calculate_loan_eligibility(
            monthly_income=dto.monthly_income, co_applicant_income=dto.co_applicant_income,
            existing_emi=dto.existing_emi, requested_loan=dto.requested_loan,
            tenure_years=dto.tenure_years, interest_rate=dto.interest_rate,
            credit_score_band=dto.credit_score_band, age=dto.age,
        )
        return EligibilityResponseDTO(**result)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/affordability", response_model=AffordabilityResponseDTO)
def affordability(dto: AffordabilityRequestDTO):
    try:
        result = calculate_affordability(
            property_price=dto.property_price, down_payment=dto.down_payment,
            monthly_income=dto.monthly_income, co_applicant_income=dto.co_applicant_income,
            existing_emi=dto.existing_emi, tenure_years=dto.tenure_years,
            interest_rate=dto.interest_rate,
        )
        return AffordabilityResponseDTO(**result)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/compare", response_model=TenureCompareResponseDTO)
def compare(dto: TenureCompareRequestDTO):
    try:
        rows = compare_tenures(dto.principal, dto.annual_rate_pct, dto.tenure_options_years)
        return TenureCompareResponseDTO(rows=[EMICalculateResponseDTO(**row) for row in rows])
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/report", response_model=ReportGenerateResponseDTO)
def create_report(dto: ReportGenerateRequestDTO):
    snapshot = {"profile": dto.profile, "last_calculation": dto.last_calculation}
    try:
        row = _persistence.create_loan_report(
            id=str(uuid.uuid4()), snapshot=snapshot, session_id=dto.session_id, lead_id=dto.lead_id,
        )
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")
    return ReportGenerateResponseDTO(report_id=row.id)


@router.get("/report/{report_id}/download")
def download_report(report_id: str):
    row = _persistence.get_loan_report_by_id(report_id)
    if not row:
        raise HTTPException(status_code=404, detail="report_not_found")

    import json
    snapshot = json.loads(row.snapshot_json)
    try:
        pdf_bytes = generate_eligibility_report_pdf(snapshot)
    except Exception:
        raise HTTPException(status_code=500, detail="report_generation_failed")

    return StreamingResponse(
        io.BytesIO(pdf_bytes), media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="home-loan-eligibility-{report_id}.pdf"'},
    )
