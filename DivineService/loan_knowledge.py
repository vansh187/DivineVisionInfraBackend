"""Static home-loan document checklist, keyed by employment type. Curated once,
not fetched from any lender API - exact requirements vary by lender and this is
surfaced to the user every time the checklist is shown.
"""

DOCUMENT_CHECKLISTS = {
    "salaried": [
        "PAN card",
        "Aadhaar card",
        "Recent passport-size photograph",
        "Last 3 months' salary slips",
        "Form 16 / last 2 years' income tax returns",
        "Last 6 months' bank statements (salary account)",
        "Employment proof / offer letter",
        "Property documents (sale agreement, title deed, NOC, approved building plan)",
    ],
    "self_employed": [
        "PAN card",
        "Aadhaar card",
        "Recent passport-size photograph",
        "Last 2-3 years' income tax returns",
        "Business proof / registration certificate",
        "Last 6-12 months' bank statements (business + personal)",
        "Profit & loss statement and balance sheet (CA-certified)",
        "GST registration and returns, where applicable",
        "Property documents (sale agreement, title deed, NOC, approved building plan)",
    ],
}

_EMPLOYMENT_ALIASES = {
    "salaried": "salaried", "employee": "salaried", "job": "salaried", "service": "salaried",
    "self_employed": "self_employed", "self-employed": "self_employed", "business": "self_employed",
    "businessman": "self_employed", "self employed": "self_employed", "professional": "self_employed",
}


def get_document_checklist(employment_type: str) -> dict:
    key = _EMPLOYMENT_ALIASES.get((employment_type or "").strip().lower())
    if not key:
        return {"error": "unknown_employment_type", "valid_options": ["salaried", "self_employed"]}
    return {
        "employment_type": key,
        "documents": DOCUMENT_CHECKLISTS[key],
        "note": "Exact requirements vary by lender.",
    }
