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

### News and public information (`--server noticias`)

A second tool set, served on its own (`mcp-server-brasil --server noticias`, or `MCP_SERVER=noticias`):

| Tool | What it does | Source |
| --- | --- | --- |
| `buscar_noticias` | Recent news for a query: title, outlet, date, link | Google News RSS, InfoMoney |
| `ultimas_noticias` | Latest items from an outlet's feed, optionally filtered | Agência Brasil, g1, InfoMoney, Poder360 |
| `ler_noticia` | Article text (up to ~6k characters) | Direct links from an allowlist |
| `noticias_ibge` | IBGE news and releases (PNAD, IPCA, GDP...) | IBGE |
| `senado_materias` | Senate bills mentioning a term, newest first | Senado (dados abertos) |
| `camara_proposicoes` | Chamber of Deputies bills mentioning a term | Câmara (dados abertos) |

`ler_noticia` is not a generic fetcher: it only reads https pages from a fixed list of outlets and
government sites, checks robots.txt, refuses redirects that leave the list, caps the download at
1.5 MB and returns plain text only. Google News links are listed but not read (they don't point to
the article itself); results carry `legivel: true` when `ler_noticia` can open them.

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

### Running it in public

HTTP mode is built to sit on the open internet behind a reverse proxy:

| Protection | Default | Flag / env |
| --- | --- | --- |
| Rate limit per IP (token bucket, `429` + `Retry-After`) | 30/min, burst 10 | `--rate-limit` `MCP_RATE_LIMIT`, `--rate-burst` `MCP_RATE_BURST` |
| Requests in flight (`503` beyond it) | 16 | `--max-concurrency` `MCP_MAX_CONCURRENCY` |
| Request body (`413`) | 64 KB | `--max-body-bytes` `MCP_MAX_BODY_BYTES` |
| Host header allowlist (DNS rebinding) | off | `--allowed-hosts` `MCP_ALLOWED_HOSTS` |
| Proxies whose `X-Forwarded-For` is trusted | none | `--trusted-proxies` `MCP_TRUSTED_PROXIES` (CIDRs) |

The client IP is the socket peer unless that peer is a trusted proxy, so a client can't pick its own
IP by sending `X-Forwarded-For`. `/health` is not rate limited. Tools only call fixed BrasilAPI, BCB
and IBGE URLs and validate their arguments before building them, so there is no way to make the
server fetch an arbitrary URL. The Docker image runs as a non-root user and works with `--read-only`.

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
├── http_guard.py     # rate limit, concurrency and body size for HTTP mode
└── __main__.py       # CLI: stdio or Streamable HTTP
```

## Roadmap

- [ ] State and municipal holidays
- [ ] PIX key format validation
- [ ] FIPE vehicle prices
- [ ] Optional Redis cache for multi-replica deployments

## License

MIT © Victor Gabriel Andrade de Melo
