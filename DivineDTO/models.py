from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
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
