"""Offline validation of Brazilian documents (CPF, CNPJ and CEP).

CNPJ validation supports the alphanumeric format introduced by Receita Federal
(IN RFB 2.229/2024), in effect since July 2026: the first 12 positions may hold
letters A-Z, and each character is converted with ``ord(c) - 48`` before the
usual modulo-11 check digits are computed. Numeric CNPJs keep working because
``ord("0") - 48 == 0``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_CNPJ_W1 = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
_CNPJ_W2 = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)


class InvalidDocumentError(ValueError):
    """Raised when a document has the wrong shape (not when check digits fail)."""


def only_digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def normalize_cep(cep: str) -> str:
    digits = only_digits(cep)
    if len(digits) != 8:
        raise InvalidDocumentError(f"CEP deve ter 8 dígitos, recebi {len(digits)}: {cep!r}")
    return digits


def normalize_cnpj(cnpj: str) -> str:
    cleaned = re.sub(r"[.\-/\s]", "", cnpj).upper()
    if not re.fullmatch(r"[0-9A-Z]{12}[0-9]{2}", cleaned):
        raise InvalidDocumentError(
            "CNPJ deve ter 14 posições: 12 alfanuméricas e 2 dígitos verificadores numéricos"
        )
    return cleaned


def _cnpj_digit(chars: str, weights: tuple[int, ...]) -> int:
    total = sum((ord(c) - 48) * w for c, w in zip(chars, weights, strict=True))
    rest = total % 11
    return 0 if rest < 2 else 11 - rest


def is_valid_cnpj(cnpj: str) -> bool:
    try:
        value = normalize_cnpj(cnpj)
    except InvalidDocumentError:
        return False
    if len(set(value)) == 1:
        return False
    d1 = _cnpj_digit(value[:12], _CNPJ_W1)
    d2 = _cnpj_digit(value[:12] + str(d1), _CNPJ_W2)
    return value[12:] == f"{d1}{d2}"


def is_valid_cpf(cpf: str) -> bool:
    value = only_digits(cpf)
    if len(value) != 11 or len(set(value)) == 1:
        return False
    for size in (9, 10):
        total = sum(int(value[i]) * (size + 1 - i) for i in range(size))
        digit = (total * 10) % 11 % 10
        if digit != int(value[size]):
            return False
    return True


def format_cnpj(cnpj: str) -> str:
    v = normalize_cnpj(cnpj)
    return f"{v[:2]}.{v[2:5]}.{v[5:8]}/{v[8:12]}-{v[12:]}"


def format_cpf(cpf: str) -> str:
    v = only_digits(cpf)
    return f"{v[:3]}.{v[3:6]}.{v[6:9]}-{v[9:]}"


@dataclass(frozen=True)
class DocumentCheck:
    tipo: Literal["CPF", "CNPJ", "desconhecido"]
    valido: bool
    formatado: str | None
    alfanumerico: bool = False


def check_document(document: str) -> DocumentCheck:
    """Detect whether a string is a CPF or CNPJ and validate its check digits."""
    compact = re.sub(r"[.\-/\s]", "", document).upper()
    if len(compact) == 11 and compact.isdigit():
        ok = is_valid_cpf(compact)
        return DocumentCheck("CPF", ok, format_cpf(compact) if ok else None)
    if len(compact) == 14:
        ok = is_valid_cnpj(compact)
        return DocumentCheck(
            "CNPJ",
            ok,
            format_cnpj(compact) if ok else None,
            alfanumerico=not compact[:12].isdigit(),
        )
    return DocumentCheck("desconhecido", False, None)
