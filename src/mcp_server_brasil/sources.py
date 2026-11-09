"""Async HTTP clients for Brazilian public data sources.

- BrasilAPI (https://brasilapi.com.br): CEP, CNPJ, feriados, DDD
- Banco Central SGS (https://api.bcb.gov.br): séries temporais (Selic, IPCA, dólar...)
- IBGE Localidades (https://servicodados.ibge.gov.br): municípios por UF

Every call goes through :class:`PublicDataClient`, which adds a timeout, a small
retry with backoff for 5xx/network errors and an in-memory TTL cache. These
sources are free and rate limited, so caching is part of being a good citizen.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, cast

import httpx

BRASILAPI_URL = "https://brasilapi.com.br/api"
BCB_SGS_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados"
IBGE_URL = "https://servicodados.ibge.gov.br/api/v1/localidades"

USER_AGENT = "mcp-server-brasil (+https://github.com/vicgamelo/mcp-server-brasil)"


class SourceError(RuntimeError):
    """A public source answered with an error the user should see."""


class NotFoundError(SourceError):
    pass


@dataclass(frozen=True)
class Indicator:
    code: int
    name: str
    unit: str
    frequency: str
    # Daily series in SGS only accept windows of up to 10 years.
    lookback_days: int


INDICATORS: dict[str, Indicator] = {
    "selic_meta": Indicator(432, "Meta Selic definida pelo Copom", "% a.a.", "diária", 400),
    "selic": Indicator(11, "Taxa Selic diária", "% a.d.", "diária", 60),
    "cdi": Indicator(12, "Taxa CDI diária", "% a.d.", "diária", 60),
    "ipca": Indicator(433, "IPCA - variação mensal", "% a.m.", "mensal", 3 * 365),
    "igpm": Indicator(189, "IGP-M - variação mensal", "% a.m.", "mensal", 3 * 365),
    "dolar": Indicator(1, "Dólar comercial (venda)", "R$", "diária", 60),
    "euro": Indicator(21619, "Euro (venda)", "R$", "diária", 60),
}


class _TTLCache:
    def __init__(self, ttl_seconds: float, max_items: int = 512) -> None:
        self.ttl = ttl_seconds
        self.max_items = max_items
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        if len(self._data) >= self.max_items:
            self._data.pop(next(iter(self._data)))
        self._data[key] = (time.monotonic() + self.ttl, value)


class PublicDataClient:
    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        *,
        cache_ttl: float = 3600,
        retries: int = 2,
        backoff: float = 0.4,
    ) -> None:
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )
        self._cache = _TTLCache(cache_ttl)
        self._retries = retries
        self._backoff = backoff

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        key = f"{url}?{sorted((params or {}).items())}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                response = await self._http.get(url, params=params)
            except httpx.TransportError as exc:
                last_error = exc
            else:
                if response.status_code == 404:
                    raise NotFoundError(_error_message(response) or "não encontrado")
                if response.status_code < 500:
                    if response.status_code >= 400:
                        raise SourceError(_error_message(response) or f"HTTP {response.status_code}")
                    data = response.json()
                    self._cache.set(key, data)
                    return data
                last_error = SourceError(f"HTTP {response.status_code} em {url}")
            if attempt < self._retries:
                await asyncio.sleep(self._backoff * 2**attempt)
        raise SourceError(f"Fonte indisponível: {last_error}")

    # BrasilAPI -------------------------------------------------------------
    async def cep(self, cep: str) -> dict[str, Any]:
        return cast(dict[str, Any], await self.get_json(f"{BRASILAPI_URL}/cep/v2/{cep}"))

    async def cnpj(self, cnpj: str) -> dict[str, Any]:
        return cast(dict[str, Any], await self.get_json(f"{BRASILAPI_URL}/cnpj/v1/{cnpj}"))

    async def holidays(self, year: int) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], await self.get_json(f"{BRASILAPI_URL}/feriados/v1/{year}"))

    async def ddd(self, ddd: str) -> dict[str, Any]:
        return cast(dict[str, Any], await self.get_json(f"{BRASILAPI_URL}/ddd/v1/{ddd}"))

    # Banco Central ---------------------------------------------------------
    async def series(
        self, indicator: Indicator, *, last: int, today: date | None = None
    ) -> list[dict[str, str]]:
        # The SGS ``/ultimos/N`` endpoint is flaky (empty bodies / 304), so we
        # always ask for an explicit date window and slice locally.
        end = today or date.today()
        start = end - timedelta(days=indicator.lookback_days)
        data = await self.get_json(
            BCB_SGS_URL.format(code=indicator.code),
            params={
                "formato": "json",
                "dataInicial": start.strftime("%d/%m/%Y"),
                "dataFinal": end.strftime("%d/%m/%Y"),
            },
        )
        if not isinstance(data, list):
            raise SourceError("Resposta inesperada do Banco Central")
        return data[-last:]

    # IBGE ------------------------------------------------------------------
    async def municipalities(self, uf: str) -> list[dict[str, Any]]:
        data = await self.get_json(f"{IBGE_URL}/estados/{uf}/municipios", params={"orderBy": "nome"})
        if not data:
            raise NotFoundError(f"UF {uf!r} não encontrada no IBGE")
        return cast(list[dict[str, Any]], data)


def _error_message(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        return body.get("message") or body.get("detail")
    return None
