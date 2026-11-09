import pytest

from mcp_server_brasil.documents import (
    InvalidDocumentError,
    check_document,
    is_valid_cnpj,
    is_valid_cpf,
    normalize_cep,
)


@pytest.mark.parametrize(
    "cnpj",
    [
        "11.222.333/0001-81",
        "00000000000191",  # Banco do Brasil
        "12.ABC.345/01DE-35",  # exemplo oficial da Receita para o CNPJ alfanumérico
        "12abc34501de35",
    ],
)
def test_valid_cnpjs(cnpj: str) -> None:
    assert is_valid_cnpj(cnpj)


@pytest.mark.parametrize(
    "cnpj",
    ["11.222.333/0001-82", "12.ABC.345/01DE-36", "00000000000000", "1122233300018", "12ABC34501DEAB"],
)
def test_invalid_cnpjs(cnpj: str) -> None:
    assert not is_valid_cnpj(cnpj)


@pytest.mark.parametrize("cpf", ["529.982.247-25", "11144477735"])
def test_valid_cpfs(cpf: str) -> None:
    assert is_valid_cpf(cpf)


@pytest.mark.parametrize("cpf", ["529.982.247-24", "111.111.111-11", "1234567890"])
def test_invalid_cpfs(cpf: str) -> None:
    assert not is_valid_cpf(cpf)


def test_check_document_detects_type_and_format() -> None:
    assert check_document("52998224725").formatado == "529.982.247-25"
    alnum = check_document("12ABC34501DE35")
    assert (alnum.tipo, alnum.valido, alnum.alfanumerico) == ("CNPJ", True, True)
    assert check_document("123").tipo == "desconhecido"


def test_normalize_cep() -> None:
    assert normalize_cep("01310-100") == "01310100"
    with pytest.raises(InvalidDocumentError):
        normalize_cep("0131010")
