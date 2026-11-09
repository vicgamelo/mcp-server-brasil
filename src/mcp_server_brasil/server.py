"""MCP server exposing Brazilian public data as tools for LLM agents."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from . import __version__
from .business_days import business_days_between, years_in_range
from .documents import InvalidDocumentError, check_document, normalize_cep, normalize_cnpj
from .sources import INDICATORS, NotFoundError, PublicDataClient, SourceError

INSTRUCTIONS = """\
Ferramentas para dados públicos do Brasil: endereço por CEP, dados cadastrais de
CNPJ (inclusive alfanumérico), validação offline de CPF/CNPJ, feriados nacionais,
contagem de dias úteis, indicadores do Banco Central (Selic, CDI, IPCA, IGP-M,
dólar, euro), municípios do IBGE e cidades por DDD. Datas no formato ISO (AAAA-MM-DD).
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True, idempotent_hint=True)
OFFLINE = ToolAnnotations(read_only_hint=True, open_world_hint=False, idempotent_hint=True)

IndicatorName = Literal["selic_meta", "selic", "cdi", "ipca", "igpm", "dolar", "euro"]
UF = Literal[
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT", "PA",
    "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO",
]  # fmt: skip


# CPF, CNPJ e CEP formatados cabem folgados em 32; mais que isso é lixo ou abuso.
MAX_ID_LEN = 32


def _check_len(value: str, field: str) -> None:
    if len(value) > MAX_ID_LEN:
        raise ToolError(f"`{field}` muito longo (máximo {MAX_ID_LEN} caracteres)")


def build_server(client: PublicDataClient | None = None) -> MCPServer:
    data = client or PublicDataClient()
    mcp = MCPServer(
        name="mcp-server-brasil",
        title="Dados públicos do Brasil",
        version=__version__,
        instructions=INSTRUCTIONS,
        website_url="https://github.com/vicgamelo/mcp-server-brasil",
    )

    async def guarded(coro: Any) -> Any:
        try:
            return await coro
        except NotFoundError as exc:
            raise ToolError(f"Não encontrado: {exc}") from exc
        except SourceError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(title="Consultar CEP", annotations=READ_ONLY)
    async def consultar_cep(cep: str) -> dict[str, Any]:
        """Endereço (logradouro, bairro, cidade, UF e coordenadas quando houver) de um CEP."""
        _check_len(cep, "cep")
        try:
            normalized = normalize_cep(cep)
        except InvalidDocumentError as exc:
            raise ToolError(str(exc)) from exc
        raw = await guarded(data.cep(normalized))
        coords = (raw.get("location") or {}).get("coordinates") or {}
        return {
            "cep": raw.get("cep"),
            "logradouro": raw.get("street") or None,
            "bairro": raw.get("neighborhood") or None,
            "cidade": raw.get("city"),
            "uf": raw.get("state"),
            "codigo_ibge": (raw.get("ibge") or {}).get("city"),
            "latitude": _to_float(coords.get("latitude")),
            "longitude": _to_float(coords.get("longitude")),
        }

    @mcp.tool(title="Consultar CNPJ", annotations=READ_ONLY)
    async def consultar_cnpj(cnpj: str) -> dict[str, Any]:
        """Dados cadastrais públicos de uma empresa na Receita Federal a partir do CNPJ."""
        _check_len(cnpj, "cnpj")
        check = check_document(cnpj)
        if check.tipo != "CNPJ" or not check.valido:
            raise ToolError(f"CNPJ inválido: {cnpj!r}")
        raw = await guarded(data.cnpj(normalize_cnpj(cnpj)))
        return {
            "cnpj": check.formatado,
            "razao_social": raw.get("razao_social"),
            "nome_fantasia": raw.get("nome_fantasia") or None,
            "situacao": raw.get("descricao_situacao_cadastral"),
            "data_abertura": raw.get("data_inicio_atividade"),
            "natureza_juridica": raw.get("natureza_juridica"),
            "porte": raw.get("porte") or raw.get("descricao_porte"),
            "capital_social": raw.get("capital_social"),
            "cnae_principal": {
                "codigo": raw.get("cnae_fiscal"),
                "descricao": raw.get("cnae_fiscal_descricao"),
            },
            "endereco": {
                "logradouro": " ".join(
                    p for p in (raw.get("descricao_tipo_de_logradouro"), raw.get("logradouro")) if p
                ),
                "numero": raw.get("numero"),
                "bairro": raw.get("bairro"),
                "municipio": raw.get("municipio"),
                "uf": raw.get("uf"),
                "cep": raw.get("cep"),
            },
            "quantidade_socios": len(raw.get("qsa") or []),
        }

    @mcp.tool(title="Validar CPF/CNPJ", annotations=OFFLINE)
    def validar_documento(documento: str) -> dict[str, Any]:
        """Valida os dígitos verificadores de um CPF ou CNPJ (numérico ou alfanumérico), sem rede."""
        _check_len(documento, "documento")
        check = check_document(documento)
        return {
            "tipo": check.tipo,
            "valido": check.valido,
            "formatado": check.formatado,
            "alfanumerico": check.alfanumerico,
        }

    @mcp.tool(title="Feriados nacionais", annotations=READ_ONLY)
    async def feriados_nacionais(ano: int) -> list[dict[str, str]]:
        """Feriados nacionais de um ano (inclui Carnaval, Sexta-feira Santa e Corpus Christi)."""
        if not 1900 <= ano <= 2199:
            raise ToolError("Ano fora do intervalo suportado (1900-2199)")
        raw = await guarded(data.holidays(ano))
        return [{"data": h["date"], "nome": h["name"], "tipo": h.get("type", "national")} for h in raw]

    @mcp.tool(title="Contar dias úteis", annotations=READ_ONLY)
    async def dias_uteis(inicio: date, fim: date) -> dict[str, Any]:
        """Quantidade de dias úteis entre duas datas (inclusive), descontando fins de semana e
        feriados nacionais. Não considera feriados estaduais/municipais."""
        if abs((fim - inicio).days) > 366 * 5:
            raise ToolError("Intervalo máximo de 5 anos")
        holidays: list[date] = []
        for year in years_in_range(inicio, fim):
            holidays += [date.fromisoformat(h["date"]) for h in await guarded(data.holidays(year))]
        days = business_days_between(inicio, fim, holidays)
        in_range = sorted({h for h in holidays if min(inicio, fim) <= h <= max(inicio, fim)})
        return {
            "inicio": min(inicio, fim).isoformat(),
            "fim": max(inicio, fim).isoformat(),
            "dias_uteis": len(days),
            "feriados_no_periodo": [h.isoformat() for h in in_range],
        }

    @mcp.tool(title="Indicador econômico (BCB)", annotations=READ_ONLY)
    async def indicador_economico(indicador: IndicatorName, ultimos: int = 12) -> dict[str, Any]:
        """Últimos valores de um indicador do Banco Central (SGS): selic_meta, selic, cdi,
        ipca, igpm, dolar ou euro."""
        if not 1 <= ultimos <= 120:
            raise ToolError("`ultimos` deve estar entre 1 e 120")
        info = INDICATORS[indicador]
        raw = await guarded(data.series(info, last=ultimos))
        return {
            "indicador": indicador,
            "nome": info.name,
            "codigo_sgs": info.code,
            "unidade": info.unit,
            "periodicidade": info.frequency,
            "valores": [{"data": _br_to_iso(p["data"]), "valor": _to_float(p["valor"])} for p in raw],
        }

    @mcp.tool(title="Municípios por UF (IBGE)", annotations=READ_ONLY)
    async def listar_municipios(uf: UF) -> dict[str, Any]:
        """Lista os municípios de uma UF com o código IBGE de cada um."""
        raw = await guarded(data.municipalities(uf))
        return {
            "uf": uf,
            "total": len(raw),
            "municipios": [{"codigo_ibge": m["id"], "nome": m["nome"]} for m in raw],
        }

    @mcp.tool(title="Cidades por DDD", annotations=READ_ONLY)
    async def consultar_ddd(ddd: str) -> dict[str, Any]:
        """UF e cidades atendidas por um DDD."""
        if not (ddd.isdigit() and len(ddd) == 2):
            raise ToolError("DDD deve ter 2 dígitos")
        raw = await guarded(data.ddd(ddd))
        cities = sorted(raw.get("cities", []))
        return {"ddd": ddd, "uf": raw.get("state"), "total": len(cities), "cidades": cities}

    @mcp.resource("brasil://indicadores", mime_type="application/json")
    def catalogo_indicadores() -> dict[str, Any]:
        """Catálogo dos indicadores disponíveis e seus códigos no SGS do Banco Central."""
        return {
            key: {"codigo_sgs": i.code, "nome": i.name, "unidade": i.unit, "periodicidade": i.frequency}
            for key, i in INDICATORS.items()
        }

    @mcp.prompt(title="Resumo macroeconômico")
    def resumo_macro() -> str:
        """Pede ao modelo um resumo do cenário de juros, inflação e câmbio."""
        return (
            "Use `indicador_economico` para buscar selic_meta (últimos 3), ipca (últimos 12) e "
            "dolar (últimos 30). Resuma em até 5 bullets o cenário de juros, inflação acumulada "
            "em 12 meses e câmbio, citando as datas dos dados."
        )

    return mcp


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _br_to_iso(value: str) -> str:
    day, month, year = value.split("/")
    return f"{year}-{month}-{day}"
