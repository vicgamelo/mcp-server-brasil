"""Servidor de notícias: um cliente MCP de verdade fala com o servidor, e as fontes são um
``httpx.MockTransport``."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from mcp.client import Client

from mcp_server_brasil.news import (
    MAX_TEXT_CHARS,
    NewsClient,
    build_news_server,
    extract_text,
    parse_rss,
    readable,
)
from mcp_server_brasil.sources import SourceError

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def rss(*items: str) -> bytes:
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{"".join(items)}</channel></rss>'.encode()


def item(title: str, link: str, date: str, source: str | None = None, desc: str = "") -> str:
    src = f'<source url="https://x">{source}</source>' if source else ""
    return (
        f"<item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate>"
        f"<description>{desc}</description>{src}</item>"
    )


GOOGLE = rss(
    item(
        "ATA DO COPOM | post - Instagram",
        "https://news.google.com/rss/articles/ig?oc=5",
        "Thu, 05 Nov 2026 10:00:00 GMT",
        "Instagram",
    ),
    item(
        "Copom mantém a Selic - Valor Econômico",
        "https://news.google.com/rss/articles/abc?oc=5",
        "Wed, 04 Nov 2026 21:31:58 GMT",
        "Valor Econômico",
    ),
    item(
        "Selic: o que muda no CDB - InfoMoney",
        "https://news.google.com/rss/articles/def?oc=5",
        "Wed, 04 Nov 2026 12:00:00 GMT",
        "InfoMoney",
    ),
)
INFOMONEY = rss(
    item(
        "Selic: o que muda no CDB",
        "https://www.infomoney.com.br/onde-investir/selic-cdb/",
        "Wed, 04 Nov 2026 11:00:00 +0000",
        desc="<p>Com a Selic parada, o CDB...</p>",
    ),
    item(
        "Tesouro Selic rende mais que poupança",
        "https://www.infomoney.com.br/onde-investir/tesouro-selic/",
        "Tue, 03 Nov 2026 10:00:00 +0000",
    ),
)
AGENCIA = rss(
    item(
        "Ata do Copom indica que a inflação não cedeu",
        "https://agenciabrasil.ebc.com.br/economia/noticia/2026-11/ata-do-copom",
        "Tue, 10 Nov 2026 10:00:00 -0300",
        desc="Documento do Banco Central...",
    ),
    item(
        "Seleção feminina vence amistoso",
        "https://agenciabrasil.ebc.com.br/esportes/x",
        "Tue, 10 Nov 2026 09:00:00 -0300",
    ),
)

ARTICLE = """<html><head><title>Ata do Copom | Agência Brasil</title>
<meta property="og:title" content="Ata do Copom indica que a inflação não cedeu"></head>
<body><nav><p>Menu com um texto longo o bastante para passar do filtro de quarenta letras</p></nav>
<script>var p = "<p>não é texto da matéria nenhum</p>";</script>
<article><p>Foto: crédito</p>
<p>O Comitê de Política Monetária (Copom) do Banco Central divulgou nesta terça-feira a ata.</p>
<p>Segundo o documento, a desaceleração da economia ainda não foi suficiente para conter a inflação.</p>
</article><footer><p>Todos os direitos reservados a quem publicou esta página de exemplo.</p></footer>
</body></html>"""


def router(request: httpx.Request) -> httpx.Response:
    url = request.url
    host, path = url.host, url.path
    if host == "news.google.com":
        assert "when:7d" in url.params["q"]
        return httpx.Response(200, content=GOOGLE)
    if host == "www.infomoney.com.br" and path == "/feed/":
        return httpx.Response(200, content=INFOMONEY if url.params.get("s") else rss())
    if host == "agenciabrasil.ebc.com.br" and path.startswith("/rss/"):
        return httpx.Response(200, content=AGENCIA)
    if path == "/robots.txt":
        if host == "g1.globo.com":
            return httpx.Response(200, text="User-agent: *\nDisallow: /busca/\n")
        return httpx.Response(200, text="User-agent: *\nDisallow: /includes/\n")
    if host == "agenciabrasil.ebc.com.br" and path == "/economia/noticia/2026-11/ata-do-copom":
        return httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html; charset=utf-8"})
    if host == "agenciabrasil.ebc.com.br" and path == "/redireciona":
        return httpx.Response(302, headers={"location": "https://evil.example/x"})
    if host == "agenciabrasil.ebc.com.br" and path == "/mesmo-site":
        return httpx.Response(301, headers={"location": "/economia/noticia/2026-11/ata-do-copom"})
    if host == "agenciabrasil.ebc.com.br" and path == "/longa":
        body = "<p>" + ("palavra " * 3000) + "</p>"
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})
    if host == "agenciabrasil.ebc.com.br" and path == "/enorme":
        return httpx.Response(200, content=b"<p>" + b"a" * 2_000_000, headers={"content-type": "text/html"})
    if host == "agenciabrasil.ebc.com.br" and path == "/pdf":
        return httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
    if host == "servicodados.ibge.gov.br":
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "titulo": "Desocupação fica em 5,2%",
                        "introducao": "A taxa de desocupação caiu...",
                        "data_publicacao": "30/10/2026 09:00:00",
                        "link": "http://agenciadenoticias.ibge.gov.br/agencia-noticias/2012-agencia-de-noticias/noticias/1",
                    }
                ]
            },
        )
    if host == "legis.senado.leg.br":
        return httpx.Response(
            200,
            json={
                "PesquisaBasicaMateria": {
                    "Materias": {
                        "Materia": [
                            {
                                "Codigo": "1",
                                "DescricaoIdentificacao": "PL 1/2024",
                                "Ementa": "Antiga",
                                "Data": "2024-03-01",
                            },
                            {
                                "Codigo": "2",
                                "DescricaoIdentificacao": "PL 2/2026",
                                "Ementa": "Nova",
                                "Data": "2026-02-12",
                            },
                        ]
                    }
                }
            },
        )
    if host == "dadosabertos.camara.leg.br":
        return httpx.Response(503, text="<html>503</html>")
    return httpx.Response(599)


@pytest.fixture
def calls() -> list[httpx.Request]:
    return []


@pytest.fixture
async def client(calls: list[httpx.Request]) -> AsyncIterator[Client]:
    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return router(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    data = NewsClient(http)
    async with Client(build_news_server(data)) as c:
        yield c
    await data.aclose()


def payload(result) -> dict:  # type: ignore[no-untyped-def,type-arg]
    assert not result.is_error, result.content
    content = result.structured_content
    if content is None:
        return json.loads(result.content[0].text)  # type: ignore[no-any-return]
    return content.get("result", content) if isinstance(content, dict) else content  # type: ignore[no-any-return]


def error_text(result) -> str:  # type: ignore[no-untyped-def]
    assert result.is_error
    return str(result.content[0].text)


async def test_lists_tools(client: Client) -> None:
    tools = {t.name for t in (await client.list_tools()).tools}
    assert tools == {
        "buscar_noticias",
        "ultimas_noticias",
        "ler_noticia",
        "noticias_ibge",
        "senado_materias",
        "camara_proposicoes",
    }


async def test_search_merges_google_and_infomoney(client: Client) -> None:
    out = payload(await client.call_tool("buscar_noticias", {"busca": "selic"}))
    titles = [n["titulo"] for n in out["noticias"]]
    # " - Veículo" sai do título, e a mesma matéria do Google e do InfoMoney aparece uma vez só
    assert "Copom mantém a Selic" in titles
    assert all(n["veiculo"] != "Instagram" for n in out["noticias"])  # rede social não é veículo
    assert titles.count("Selic: o que muda no CDB") == 1
    by_title = {n["titulo"]: n for n in out["noticias"]}
    assert by_title["Copom mantém a Selic"]["veiculo"] == "Valor Econômico"
    assert by_title["Copom mantém a Selic"]["legivel"] is False
    assert any(n["legivel"] for n in out["noticias"])
    dates = [n["publicado_em"] for n in out["noticias"]]
    assert dates == sorted(dates, reverse=True)
    assert dates[0] == "2026-11-04T21:31Z"


@pytest.mark.parametrize(
    "down", [{"www.infomoney.com.br"}, {"news.google.com"}, {"www.infomoney.com.br", "news.google.com"}]
)
async def test_search_survives_one_source_down(down: set[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503) if request.url.host in down else router(request)

    data = NewsClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    async with Client(build_news_server(data)) as c:
        result = await c.call_tool("buscar_noticias", {"busca": "selic"})
    await data.aclose()
    if len(down) == 2:
        assert "indisponível agora (HTTP 503)" in error_text(result)
    else:
        assert payload(result)["total"] == 2


async def test_search_validates_input(client: Client) -> None:
    assert "entre 2 e" in error_text(await client.call_tool("buscar_noticias", {"busca": "x"}))
    assert "`dias`" in error_text(await client.call_tool("buscar_noticias", {"busca": "selic", "dias": 90}))
    assert "`limite`" in error_text(
        await client.call_tool("buscar_noticias", {"busca": "selic", "limite": 99})
    )


async def test_latest_from_feed_with_accent_insensitive_filter(client: Client) -> None:
    out = payload(
        await client.call_tool("ultimas_noticias", {"fonte": "agencia_brasil", "filtro": "inflacao copom"})
    )
    assert [n["titulo"] for n in out["noticias"]] == ["Ata do Copom indica que a inflação não cedeu"]
    assert out["noticias"][0]["legivel"] is True
    assert out["noticias"][0]["publicado_em"] == "2026-11-10T13:00Z"


async def test_feed_is_cached(client: Client, calls: list[httpx.Request]) -> None:
    await client.call_tool("ultimas_noticias", {"fonte": "agencia_brasil"})
    await client.call_tool("ultimas_noticias", {"fonte": "agencia_brasil", "filtro": "copom"})
    assert sum(1 for c in calls if c.url.path.startswith("/rss/")) == 1


async def test_read_article_text_only(client: Client) -> None:
    url = "https://agenciabrasil.ebc.com.br/economia/noticia/2026-11/ata-do-copom"
    out = payload(await client.call_tool("ler_noticia", {"url": url}))
    assert out["titulo"] == "Ata do Copom indica que a inflação não cedeu"
    assert out["texto"].startswith("O Comitê de Política Monetária")
    assert "Menu" not in out["texto"] and "não é texto" not in out["texto"] and "direitos" not in out["texto"]
    assert "crédito" not in out["texto"]
    assert out["cortado"] is False


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/x",
        "http://agenciabrasil.ebc.com.br/economia/x",
        "https://agenciabrasil.ebc.com.br:8443/x",
        "https://user:pw@agenciabrasil.ebc.com.br/x",
        "https://agenciabrasil.ebc.com.br.evil.example/x",
        "https://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "https://news.google.com/rss/articles/abc",
    ],
)
async def test_read_refuses_urls_outside_the_list(
    client: Client, calls: list[httpx.Request], url: str
) -> None:
    assert "Só leio links" in error_text(await client.call_tool("ler_noticia", {"url": url}))
    assert calls == []  # recusa antes de qualquer requisição


async def test_read_respects_robots(client: Client, calls: list[httpx.Request]) -> None:
    text = error_text(await client.call_tool("ler_noticia", {"url": "https://g1.globo.com/busca/?q=selic"}))
    assert "robots.txt" in text
    assert [c.url.path for c in calls] == ["/robots.txt"]


async def test_read_refuses_redirect_out_of_the_list(client: Client, calls: list[httpx.Request]) -> None:
    text = error_text(
        await client.call_tool("ler_noticia", {"url": "https://agenciabrasil.ebc.com.br/redireciona"})
    )
    assert "Redirecionamento" in text
    assert all(c.url.host != "evil.example" for c in calls)


async def test_read_follows_redirect_inside_the_list(client: Client) -> None:
    out = payload(
        await client.call_tool("ler_noticia", {"url": "https://agenciabrasil.ebc.com.br/mesmo-site"})
    )
    assert out["texto"].startswith("O Comitê")


async def test_read_cuts_long_text_and_refuses_huge_or_non_html(client: Client) -> None:
    out = payload(await client.call_tool("ler_noticia", {"url": "https://agenciabrasil.ebc.com.br/longa"}))
    assert out["cortado"] is True and len(out["texto"]) == MAX_TEXT_CHARS + 1
    assert "grande demais" in error_text(
        await client.call_tool("ler_noticia", {"url": "https://agenciabrasil.ebc.com.br/enorme"})
    )
    assert "não é HTML" in error_text(
        await client.call_tool("ler_noticia", {"url": "https://agenciabrasil.ebc.com.br/pdf"})
    )


async def test_ibge_news(client: Client) -> None:
    out = payload(await client.call_tool("noticias_ibge", {"busca": "desemprego"}))
    n = out["noticias"][0]
    assert n["veiculo"] == "Agência IBGE"
    assert n["link"].startswith("https://agenciadenoticias.ibge.gov.br/")
    assert n["legivel"] is True
    assert n["publicado_em"] == "2026-10-30T09:00Z"


async def test_senado_newest_first(client: Client) -> None:
    out = payload(await client.call_tool("senado_materias", {"busca": "inteligencia artificial"}))
    assert [m["identificacao"] for m in out["materias"]] == ["PL 2/2026", "PL 1/2024"]
    assert out["materias"][0]["link"].endswith("/materia/2")


async def test_camara_down_is_a_clear_error(client: Client) -> None:
    text = error_text(await client.call_tool("camara_proposicoes", {"busca": "inteligencia artificial"}))
    assert "Câmara indisponível agora (HTTP 503)" in text


def test_readable() -> None:
    assert readable("https://g1.globo.com/economia/noticia/x.ghtml")
    assert not readable("https://globo.com/x")
    assert not readable("not a url")


def test_rss_rejects_entity_bomb_and_garbage() -> None:
    bomb = b"""<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa">
<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;"><!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
<!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;"><!ENTITY e "&d;&d;&d;&d;&d;&d;&d;&d;&d;&d;">
<!ENTITY f "&e;&e;&e;&e;&e;&e;&e;&e;&e;&e;"><!ENTITY g "&f;&f;&f;&f;&f;&f;&f;&f;&f;&f;">
<!ENTITY h "&g;&g;&g;&g;&g;&g;&g;&g;&g;&g;"><!ENTITY i "&h;&h;&h;&h;&h;&h;&h;&h;&h;&h;">]>
<rss><channel><item><title>&i;</title><link>https://x</link></item></channel></rss>"""
    with pytest.raises(SourceError):
        parse_rss(bomb)
    with pytest.raises(SourceError):
        parse_rss(b"<html>not xml")


def test_rss_skips_items_without_https_link() -> None:
    assert parse_rss(rss(item("a", "javascript:alert(1)", "x"), item("b", "http://x", "x")), "X") == []


def test_extract_text_ignores_short_paragraphs() -> None:
    title, text = extract_text("<title>T</title><p>curto</p><p>" + "longo " * 20 + "</p>")
    assert title == "T"
    assert text.startswith("longo")


async def test_gzip_responses_are_decoded_once() -> None:
    import gzip

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=gzip.compress(AGENCIA), headers={"content-encoding": "gzip"})

    data = NewsClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    items = await data.feed("agencia_brasil")
    await data.aclose()
    assert items[0].titulo == "Ata do Copom indica que a inflação não cedeu"
