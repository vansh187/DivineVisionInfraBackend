import pytest
from pydantic import ValidationError

from DivineDTO.models import UserCreateDTO, BrokerCreateDTO


def test_user_create_dto_requires_phone():
    with pytest.raises(ValidationError):
        UserCreateDTO(username="someone", password="strongpassword")


def test_user_create_dto_rejects_blank_phone():
    with pytest.raises(ValidationError):
        UserCreateDTO(username="someone", password="strongpassword", phone="   ")


def test_user_create_dto_accepts_valid_phone():
    dto = UserCreateDTO(username="someone", password="strongpassword", phone="9876500000")
    assert dto.phone == "9876500000"


def test_broker_create_dto_requires_project():
    with pytest.raises(ValidationError):
        BrokerCreateDTO(username="broker1", password="strongpassword", phone="9876500001")


def test_broker_create_dto_rejects_unknown_project():
    with pytest.raises(ValidationError):
        BrokerCreateDTO(
            username="broker1", password="strongpassword", phone="9876500001",
            project="not-a-real-project",
        )


def test_broker_create_dto_accepts_known_project_slugs():
    for slug in ("suraksha-enclave", "ops-divine-greens"):
        dto = BrokerCreateDTO(
            username="broker1", password="strongpassword", phone="9876500001", project=slug,
        )
        assert dto.project == slug


def test_broker_create_dto_also_requires_phone():
    with pytest.raises(ValidationError):
        BrokerCreateDTO(username="broker1", password="strongpassword", project="suraksha-enclave")
