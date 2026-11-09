"""End-to-end tests: a real MCP client talks to the server in-process while the
public APIs are replaced by an ``httpx.MockTransport``."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import date

import httpx
import pytest
from mcp.client import Client

from mcp_server_brasil.business_days import business_days_between
from mcp_server_brasil.server import build_server
from mcp_server_brasil.sources import PublicDataClient

HOLIDAYS_2026 = [
    {"date": "2026-01-01", "name": "Confraternização mundial", "type": "national"},
    {"date": "2026-02-16", "name": "Carnaval", "type": "national"},
    {"date": "2026-02-17", "name": "Carnaval", "type": "national"},
    {"date": "2026-04-03", "name": "Sexta-feira Santa", "type": "national"},
    {"date": "2026-04-21", "name": "Tiradentes", "type": "national"},
]

Handler = Callable[[httpx.Request], httpx.Response]

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def router(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/cep/v2/01310100":
        return httpx.Response(
            200,
            json={
                "cep": "01310100",
                "state": "SP",
                "city": "São Paulo",
                "neighborhood": "Bela Vista",
                "street": "Avenida Paulista",
                "ibge": {"city": "3550308"},
                "location": {"coordinates": {"longitude": "-46.63611", "latitude": "-23.5475"}},
            },
        )
    if path.startswith("/api/cep/v2/"):
        return httpx.Response(404, json={"message": "CEP 99999999 não encontrado."})
    if path == "/api/cnpj/v1/00000000000191":
        return httpx.Response(
            200,
            json={
                "razao_social": "BANCO DO BRASIL SA",
                "nome_fantasia": "DIRECAO GERAL",
                "descricao_situacao_cadastral": "ATIVA",
                "cnae_fiscal": 6422100,
                "cnae_fiscal_descricao": "Bancos múltiplos, com carteira comercial",
                "descricao_tipo_de_logradouro": "QUADRA",
                "logradouro": "SAUN QUADRA 5 LOTE B",
                "uf": "DF",
                "qsa": [{"nome_socio": "X"}, {"nome_socio": "Y"}],
            },
        )
    if path == "/api/feriados/v1/2026":
        return httpx.Response(200, json=HOLIDAYS_2026)
    if path == "/api/ddd/v1/12":
        return httpx.Response(200, json={"state": "SP", "cities": ["TAUBATÉ", "SÃO JOSÉ DOS CAMPOS"]})
    if path == "/dados/serie/bcdata.sgs.432/dados":
        assert request.url.params["formato"] == "json"
        assert "dataInicial" in request.url.params
        return httpx.Response(
            200,
            json=[
                {"data": "05/11/2026", "valor": "14.25"},
                {"data": "06/11/2026", "valor": "14.25"},
                {"data": "09/11/2026", "valor": "14.00"},
            ],
        )
    if path == "/api/v1/localidades/estados/AC/municipios":
        return httpx.Response(200, json=[{"id": 1200013, "nome": "Acrelândia"}])
    return httpx.Response(503)


@pytest.fixture
def calls() -> list[httpx.Request]:
    return []


@pytest.fixture
async def client(calls: list[httpx.Request]) -> AsyncIterator[Client]:
    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return router(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    data = PublicDataClient(http, retries=1, backoff=0)
    async with Client(build_server(data)) as c:
        yield c
    await data.aclose()


def payload(result) -> object:  # type: ignore[no-untyped-def]
    assert not result.is_error, result.content
    if result.structured_content is not None:
        content = result.structured_content
        return content.get("result", content) if isinstance(content, dict) else content
    return json.loads(result.content[0].text)


async def test_lists_all_tools(client: Client) -> None:
    tools = {t.name for t in (await client.list_tools()).tools}
    assert tools == {
        "consultar_cep",
        "consultar_cnpj",
        "validar_documento",
        "feriados_nacionais",
        "dias_uteis",
        "indicador_economico",
        "listar_municipios",
        "consultar_ddd",
    }


async def test_consultar_cep_normalizes_input(client: Client) -> None:
    data = payload(await client.call_tool("consultar_cep", {"cep": "01310-100"}))
    assert data["logradouro"] == "Avenida Paulista"
    assert data["latitude"] == pytest.approx(-23.5475)


async def test_cep_not_found_is_a_tool_error(client: Client) -> None:
    result = await client.call_tool("consultar_cep", {"cep": "99999-999"})
    assert result.is_error
    assert "não encontrado" in result.content[0].text


async def test_invalid_cep_never_hits_network(client: Client, calls: list[httpx.Request]) -> None:
    result = await client.call_tool("consultar_cep", {"cep": "123"})
    assert result.is_error
    assert calls == []


async def test_consultar_cnpj(client: Client) -> None:
    data = payload(await client.call_tool("consultar_cnpj", {"cnpj": "00.000.000/0001-91"}))
    assert data["cnpj"] == "00.000.000/0001-91"
    assert data["situacao"] == "ATIVA"
    assert data["endereco"]["logradouro"] == "QUADRA SAUN QUADRA 5 LOTE B"
    assert data["quantidade_socios"] == 2


async def test_invalid_cnpj_rejected_before_request(client: Client, calls: list[httpx.Request]) -> None:
    result = await client.call_tool("consultar_cnpj", {"cnpj": "00.000.000/0001-92"})
    assert result.is_error
    assert calls == []


async def test_validar_documento_alphanumeric(client: Client) -> None:
    data = payload(await client.call_tool("validar_documento", {"documento": "12.ABC.345/01DE-35"}))
    assert data == {"tipo": "CNPJ", "valido": True, "formatado": "12.ABC.345/01DE-35", "alfanumerico": True}


async def test_dias_uteis_discount_holidays(client: Client) -> None:
    # Fevereiro/2026: 20 dias de semana, menos Carnaval (16 e 17) = 18
    data = payload(await client.call_tool("dias_uteis", {"inicio": "2026-02-01", "fim": "2026-02-28"}))
    assert data["dias_uteis"] == 18
    assert data["feriados_no_periodo"] == ["2026-02-16", "2026-02-17"]


async def test_holidays_are_cached(client: Client, calls: list[httpx.Request]) -> None:
    await client.call_tool("feriados_nacionais", {"ano": 2026})
    await client.call_tool("feriados_nacionais", {"ano": 2026})
    assert len(calls) == 1


async def test_indicador_economico_converts_dates_and_values(client: Client) -> None:
    data = payload(await client.call_tool("indicador_economico", {"indicador": "selic_meta", "ultimos": 2}))
    assert data["codigo_sgs"] == 432
    assert data["valores"] == [
        {"data": "2026-11-06", "valor": 14.25},
        {"data": "2026-11-09", "valor": 14.0},
    ]


async def test_unknown_indicator_is_rejected(client: Client) -> None:
    result = await client.call_tool("indicador_economico", {"indicador": "bitcoin"})
    assert result.is_error


async def test_municipios_and_ddd(client: Client) -> None:
    mun = payload(await client.call_tool("listar_municipios", {"uf": "AC"}))
    assert mun["municipios"][0] == {"codigo_ibge": 1200013, "nome": "Acrelândia"}
    ddd = payload(await client.call_tool("consultar_ddd", {"ddd": "12"}))
    assert ddd["cidades"] == ["SÃO JOSÉ DOS CAMPOS", "TAUBATÉ"]


async def test_upstream_5xx_is_retried_then_reported(calls: list[httpx.Request]) -> None:
    def always_down(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(502)

    http = httpx.AsyncClient(transport=httpx.MockTransport(always_down))
    data = PublicDataClient(http, retries=2, backoff=0)
    async with Client(build_server(data)) as c:
        result = await c.call_tool("consultar_ddd", {"ddd": "11"})
    assert result.is_error
    assert "indisponível" in result.content[0].text
    assert len(calls) == 3


def test_business_days_pure() -> None:
    days = business_days_between(date(2026, 4, 3), date(2026, 3, 30), [date(2026, 4, 3)])
    assert [d.isoformat() for d in days] == ["2026-03-30", "2026-03-31", "2026-04-01", "2026-04-02"]
