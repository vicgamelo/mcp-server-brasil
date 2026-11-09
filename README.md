# mcp-server-brasil

![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![MCP](https://img.shields.io/badge/MCP-2.x-black)
![License](https://img.shields.io/badge/license-MIT-green)

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives LLM agents
reliable access to Brazilian public data: addresses by CEP, company records by CNPJ, holidays
and business days, Banco Central indicators and IBGE municipalities.

> 🇧🇷 Servidor MCP com dados públicos do Brasil para agentes de IA: CEP, CNPJ (inclusive o
> alfanumérico), feriados, dias úteis, Selic/IPCA/dólar do Banco Central e municípios do IBGE.

## Why

Agents keep getting Brazilian data wrong: they invent CEPs, can't count business days around
Carnaval and don't know the current Selic. This server turns the official public sources into
typed, validated tools, so the model asks instead of guessing.

- **Validates before calling the network.** CPF/CNPJ check digits and CEP shape are checked
  locally, so a hallucinated document never becomes an HTTP request.
- **Ready for the alphanumeric CNPJ** (IN RFB 2.229/2024, in effect since July 2026).
- **Structured output.** Every tool returns a JSON schema'd result, with errors reported as
  tool errors the model can read and recover from.
- **A good citizen of free APIs.** TTL cache, timeouts and retry with backoff for 5xx only.
- **stdio and Streamable HTTP** transports, Docker image with healthcheck and DNS rebinding
  protection for public deployments.

## Tools

| Tool | What it does | Source |
| --- | --- | --- |
| `consultar_cep` | Street, district, city, UF, IBGE code and coordinates | BrasilAPI |
| `consultar_cnpj` | Company name, status, CNAE, address, size | BrasilAPI / Receita Federal |
| `validar_documento` | Offline CPF / CNPJ (numeric or alphanumeric) validation | — |
| `feriados_nacionais` | National holidays for a year | BrasilAPI |
| `dias_uteis` | Business days between two dates, minus weekends and national holidays | BrasilAPI |
| `indicador_economico` | Latest values for `selic_meta`, `selic`, `cdi`, `ipca`, `igpm`, `dolar`, `euro` | Banco Central (SGS) |
| `listar_municipios` | Municipalities of a UF with IBGE codes | IBGE |
| `consultar_ddd` | UF and cities for an area code | BrasilAPI |

It also exposes the resource `brasil://indicadores` (indicator catalog with SGS codes) and the
prompt `resumo_macro` (rates, inflation and FX summary).

## Quickstart

### Claude Code

```bash
claude mcp add brasil -- uvx mcp-server-brasil
```

### Claude Desktop / Cursor / any MCP client

```json
{
  "mcpServers": {
    "brasil": {
      "command": "uvx",
      "args": ["mcp-server-brasil"]
    }
  }
}
```

### Hosted (Streamable HTTP)

A public instance runs at `https://mcp.vgmelo.tech/mcp`:

```bash
claude mcp add --transport http brasil https://mcp.vgmelo.tech/mcp
```

### Docker

```bash
docker run --rm -p 8000:8000 -e MCP_ALLOWED_HOSTS=localhost:8000 ghcr.io/vicgamelo/mcp-server-brasil
curl localhost:8000/health
```

## Example

> **You:** How many business days are there in November 2026, and what's the Selic rate today?
>
> **Agent** → `dias_uteis(inicio="2026-11-01", fim="2026-11-30")` → `19`, excluding Finados (11/02)
> and Consciência Negra (11/20) → `indicador_economico(indicador="selic_meta", ultimos=1)` → `13.75% a.a.`

## Development

```bash
uv sync
uv run pytest          # in-process MCP client + mocked HTTP, no network needed
uv run ruff check . && uv run mypy
uv run mcp-server-brasil --http --port 8000
```

Tests run a real MCP `Client` against the server in-process, with the public APIs replaced by
`httpx.MockTransport`, so they cover the protocol layer, input validation, caching and retry
behavior without hitting the network.

### Project layout

```
src/mcp_server_brasil/
├── server.py         # MCP tools, resource and prompt
├── sources.py        # BrasilAPI / BCB / IBGE clients with cache + retry
├── documents.py      # CPF, CNPJ (alphanumeric) and CEP validation
├── business_days.py  # business-day arithmetic
└── __main__.py       # CLI: stdio or Streamable HTTP
```

## Roadmap

- [ ] State and municipal holidays
- [ ] PIX key format validation
- [ ] FIPE vehicle prices
- [ ] Optional Redis cache for multi-replica deployments

## License

MIT © Victor Gabriel Andrade de Melo
