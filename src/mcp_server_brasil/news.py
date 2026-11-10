"""Notícias e informação pública do Brasil, como um segundo servidor MCP (``--server noticias``).

Fontes, todas sem chave:

- Google News (RSS de busca): só título, veículo, data e link. O link do Google não leva direto à
  matéria, então a leitura do texto fica para as fontes com link direto.
- Feeds dos próprios veículos: Agência Brasil (CC BY), g1, InfoMoney (com busca) e Poder360.
- IBGE (API de notícias da Agência IBGE), Senado e Câmara (dados abertos).

``ler_noticia`` só lê páginas de uma lista fixa de domínios, respeita o robots.txt, não segue
redirecionamento para fora da lista e devolve só o texto, cortado. Ninguém consegue usar o servidor
para buscar uma URL qualquer.
"""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from . import __version__
from .sources import USER_AGENT, NotFoundError, SourceError, _TTLCache

INSTRUCTIONS = """\
Notícias e informação pública do Brasil: busca de notícias (Google News e InfoMoney), últimas
notícias de veículos com feed (Agência Brasil, g1, InfoMoney, Poder360), notícias do IBGE,
matérias do Senado e proposições da Câmara. `ler_noticia` lê o texto de uma matéria quando o link
é direto de um veículo da lista (campo `legivel: true`). Cite sempre veículo, data e link.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True, idempotent_hint=True)

GOOGLE_NEWS_URL = "https://news.google.com/rss/search"
INFOMONEY_SEARCH_URL = "https://www.infomoney.com.br/feed/"
IBGE_NEWS_URL = "https://servicodados.ibge.gov.br/api/v3/noticias/"
SENADO_URL = "https://legis.senado.leg.br/dadosabertos/materia/pesquisa/lista.json"
CAMARA_URL = "https://dadosabertos.camara.leg.br/api/v2/proposicoes"

FEEDS: dict[str, tuple[str, str]] = {
    "agencia_brasil": ("Agência Brasil", "https://agenciabrasil.ebc.com.br/rss/ultimasnoticias/feed.xml"),
    "agencia_brasil_economia": ("Agência Brasil", "https://agenciabrasil.ebc.com.br/rss/economia/feed.xml"),
    "agencia_brasil_politica": ("Agência Brasil", "https://agenciabrasil.ebc.com.br/rss/politica/feed.xml"),
    "g1": ("g1", "https://g1.globo.com/rss/g1/"),
    "g1_economia": ("g1", "https://g1.globo.com/rss/g1/economia/"),
    "infomoney": ("InfoMoney", "https://www.infomoney.com.br/feed/"),
    "poder360": ("Poder360", "https://www.poder360.com.br/feed/"),
}
Fonte = Literal[
    "agencia_brasil", "agencia_brasil_economia", "agencia_brasil_politica",
    "g1", "g1_economia", "infomoney", "poder360",
]  # fmt: skip

# Domínios cujas páginas `ler_noticia` aceita. Só https, sem subdomínio curinga.
READABLE_HOSTS = frozenset(
    {
        "agenciabrasil.ebc.com.br",
        "g1.globo.com",
        "www.infomoney.com.br",
        "www.poder360.com.br",
        "agenciadenoticias.ibge.gov.br",
        "www12.senado.leg.br",
        "www.camara.leg.br",
        "www.gov.br",
    }
)

# Resultados do Google Notícias que não são veículo de imprensa.
SOCIAL = frozenset({"instagram", "facebook", "youtube", "x", "tiktok", "linkedin", "threads", "kwai"})

MAX_QUERY_LEN = 120
MAX_FEED_BYTES = 2_000_000
MAX_PAGE_BYTES = 1_500_000
MAX_TEXT_CHARS = 6000
MAX_REDIRECTS = 3


def readable(url: str) -> bool:
    """A URL é https, sem porta nem usuário, e de um domínio da lista."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname in READABLE_HOSTS
        and parts.port is None
        and not parts.username
        and not parts.password
    )


# ---- parsing --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    titulo: str
    veiculo: str
    publicado_em: str | None
    link: str
    resumo: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "titulo": self.titulo,
            "veiculo": self.veiculo,
            "publicado_em": self.publicado_em,
            "link": self.link,
            "legivel": readable(self.link),
        }
        if self.resumo:
            out["resumo"] = self.resumo
        return out


_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


def clean(text: str | None, limit: int = 300) -> str | None:
    if not text:
        return None
    out = _SPACES.sub(" ", unescape(_TAGS.sub(" ", text))).strip()
    if len(out) > limit:
        out = out[: limit - 1].rsplit(" ", 1)[0] + "…"
    return out or None


def iso_date(value: str | None) -> str | None:
    """RFC 822 (RSS) ou 'dd/mm/aaaa hh:mm:ss' (IBGE) para ISO 8601 em UTC."""
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.strptime(value.strip(), "%d/%m/%Y %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="minutes").replace("+00:00", "Z")


def parse_rss(xml: bytes, veiculo: str | None = None) -> list[Item]:
    """Itens de um RSS 2.0. `veiculo` fixo para feeds de um veículo; no Google News vem do <source>.

    O ElementTree não resolve entidade externa, e o expat da stdlib (2.4.1+) já barra bomba de
    entidades; o tamanho é limitado antes de chegar aqui.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise SourceError("Feed inválido") from exc
    items: list[Item] = []
    for node in root.iter("item"):
        title = clean(node.findtext("title"), 300)
        link = (node.findtext("link") or "").strip()
        if not title or not link.startswith("https://"):
            continue
        source = node.find("source")
        name = veiculo or (source.text.strip() if source is not None and source.text else "")
        if source is not None and source.text and title.endswith(f" - {source.text.strip()}"):
            title = title[: -len(source.text.strip()) - 3]
        items.append(
            Item(
                titulo=title,
                veiculo=name,
                publicado_em=iso_date(node.findtext("pubDate")),
                link=link,
                resumo=None if veiculo is None else clean(node.findtext("description")),
            )
        )
    return items


def matches(item: Item, query: str) -> bool:
    words = [w for w in _fold(query).split() if len(w) > 2]
    text = _fold(f"{item.titulo} {item.resumo or ''}")
    return all(w in text for w in words)


def _fold(text: str) -> str:
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")


class _TextExtractor(HTMLParser):
    """Título e parágrafos da página; ignora script, style, nav, header, footer, aside e form."""

    SKIP = frozenset({"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.og_title = ""
        self.paragraphs: list[str] = []
        self._skip = 0
        self._in_title = False
        self._in_p = False
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            a = dict(attrs)
            if a.get("property") == "og:title" and a.get("content"):
                self.og_title = a["content"] or ""
        elif tag == "p" and not self._skip:
            self._in_p = True
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "p" and self._in_p:
            self._in_p = False
            text = _SPACES.sub(" ", "".join(self._buf)).strip()
            if len(text) >= 40:  # legenda, crédito e botão ficam de fora
                self.paragraphs.append(text)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif self._in_p and not self._skip:
            self._buf.append(data)


def extract_text(html: str) -> tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    title = clean(parser.og_title or parser.title, 300) or ""
    seen: set[str] = set()
    paragraphs = [p for p in parser.paragraphs if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]
    return title, "\n\n".join(paragraphs)


def _decoded_headers(headers: httpx.Headers) -> httpx.Headers:
    """`aiter_bytes` já descompacta; sem tirar esses cabeçalhos o corpo seria descompactado de novo."""
    out = httpx.Headers(headers)
    for name in ("content-encoding", "content-length", "transfer-encoding"):
        out.pop(name, None)
    return out


# ---- cliente --------------------------------------------------------------------------------


class NewsClient:
    def __init__(self, http: httpx.AsyncClient | None = None, *, cache_ttl: float = 600) -> None:
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=False,  # redirecionamento é conferido um a um em `_get`
        )
        self._cache = _TTLCache(cache_ttl)
        self._robots: dict[str, RobotFileParser] = {}
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        limit: int = MAX_FEED_BYTES,
        allow: Any = None,
    ) -> httpx.Response:
        """GET com limite de tamanho e redirecionamento manual. `allow(url)` decide se o destino de
        cada redirecionamento é aceito (padrão: mesmo host)."""
        current = httpx.URL(url, params=params)
        for _ in range(MAX_REDIRECTS + 1):
            try:
                async with self._http.stream("GET", current) as resp:
                    if resp.is_redirect:
                        target = httpx.URL(urljoin(str(current), resp.headers.get("location", "")))
                        ok = allow(str(target)) if allow else target.host == current.host
                        if not ok or target.scheme != "https":
                            raise SourceError("Redirecionamento para fora das fontes permitidas")
                        current = target
                        continue
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > limit:
                            raise SourceError("Resposta grande demais")
                    return httpx.Response(
                        resp.status_code,
                        headers=_decoded_headers(resp.headers),
                        content=bytes(body),
                        request=resp.request,
                    )
            except httpx.TransportError as exc:
                raise SourceError(f"Fonte indisponível: {exc.__class__.__name__}") from exc
        raise SourceError("Redirecionamentos demais")

    async def _cached(self, key: str, make: Any) -> Any:
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        value = await make()
        self._cache.set(key, value)
        return value

    @staticmethod
    def _check(resp: httpx.Response, name: str) -> None:
        if resp.status_code == 404:
            raise NotFoundError(f"{name}: não encontrado")
        if resp.status_code >= 400:
            raise SourceError(f"{name} indisponível agora (HTTP {resp.status_code})")

    async def feed(self, fonte: str) -> list[Item]:
        veiculo, url = FEEDS[fonte]

        async def make() -> list[Item]:
            resp = await self._get(url)
            self._check(resp, veiculo)
            return parse_rss(resp.content, veiculo)

        return list(await self._cached(f"feed:{fonte}", make))

    async def google_news(self, query: str, days: int) -> list[Item]:
        params = {"q": f"{query} when:{days}d", "hl": "pt-BR", "gl": "BR", "ceid": "BR:pt-419"}

        async def make() -> list[Item]:
            resp = await self._get(GOOGLE_NEWS_URL, params)
            self._check(resp, "Google Notícias")
            return parse_rss(resp.content)

        return list(await self._cached(f"gn:{query}:{days}", make))

    async def infomoney_search(self, query: str) -> list[Item]:
        async def make() -> list[Item]:
            resp = await self._get(INFOMONEY_SEARCH_URL, {"s": query})
            self._check(resp, "InfoMoney")
            return parse_rss(resp.content, "InfoMoney")

        return list(await self._cached(f"im:{query}", make))

    async def get_json(self, url: str, params: dict[str, Any], name: str) -> Any:
        async def make() -> Any:
            resp = await self._get(url, params)
            self._check(resp, name)
            try:
                return resp.json()
            except ValueError as exc:
                raise SourceError(f"{name}: resposta inesperada") from exc

        return await self._cached(f"json:{url}:{sorted(params.items())}", make)

    async def allowed_by_robots(self, url: str) -> bool:
        host = urlsplit(url).hostname or ""
        async with self._lock:
            parser = self._robots.get(host)
        if parser is None:
            parser = RobotFileParser()
            try:
                resp = await self._get(f"https://{host}/robots.txt", limit=200_000, allow=readable)
            except SourceError:
                return False  # sem robots legível, não arrisca
            if resp.status_code >= 500:
                return False
            # 4xx: sem robots.txt, tudo liberado (convenção do RobotFileParser)
            parser.parse(resp.text.splitlines() if resp.status_code < 400 else [])
            async with self._lock:
                self._robots[host] = parser
        return parser.can_fetch(USER_AGENT, url)

    async def read(self, url: str) -> dict[str, Any]:
        async def make() -> dict[str, Any]:
            if not await self.allowed_by_robots(url):
                raise SourceError("O robots.txt do site não permite ler essa página")
            resp = await self._get(url, limit=MAX_PAGE_BYTES, allow=readable)
            self._check(resp, "Página")
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype:
                raise SourceError("A página não é HTML")
            title, text = extract_text(resp.text)
            if not text:
                raise SourceError("Não achei o texto da matéria nessa página")
            cut = len(text) > MAX_TEXT_CHARS
            return {
                "url": url,
                "veiculo": urlsplit(url).hostname,
                "titulo": title,
                "texto": text[:MAX_TEXT_CHARS] + ("…" if cut else ""),
                "cortado": cut,
            }

        return dict(await self._cached(f"read:{url}", make))


# ---- servidor -------------------------------------------------------------------------------


def _check_query(value: str) -> str:
    value = value.strip()
    if not 2 <= len(value) <= MAX_QUERY_LEN:
        raise ToolError(f"`busca` deve ter entre 2 e {MAX_QUERY_LEN} caracteres")
    return value


def _check_limit(value: int, top: int = 20) -> None:
    if not 1 <= value <= top:
        raise ToolError(f"`limite` deve estar entre 1 e {top}")


def _dedupe(items: list[Item]) -> list[Item]:
    seen: set[str] = set()
    out = []
    for it in items:
        key = _fold(it.titulo)[:80]
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def _newest(items: list[Item]) -> list[Item]:
    return sorted(items, key=lambda i: i.publicado_em or "", reverse=True)


def build_news_server(client: NewsClient | None = None) -> MCPServer:
    data = client or NewsClient()
    mcp = MCPServer(
        name="mcp-server-brasil-noticias",
        title="Notícias e informação pública do Brasil",
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

    @mcp.tool(title="Buscar notícias", annotations=READ_ONLY)
    async def buscar_noticias(busca: str, dias: int = 7, limite: int = 8) -> dict[str, Any]:
        """Busca notícias recentes no Brasil (Google Notícias e InfoMoney). Devolve título,
        veículo, data e link; `legivel: true` indica que `ler_noticia` consegue ler o texto."""
        query = _check_query(busca)
        _check_limit(limite)
        if not 1 <= dias <= 30:
            raise ToolError("`dias` deve estar entre 1 e 30")
        results: list[list[Item] | BaseException] = await asyncio.gather(
            guarded(data.google_news(query, dias)),
            guarded(data.infomoney_search(query)),
            return_exceptions=True,
        )
        if all(isinstance(r, BaseException) for r in results):
            raise results[0]  # type: ignore[misc]
        found = [i for r in results if isinstance(r, list) for i in r if i.veiculo.lower() not in SOCIAL]
        # Os legíveis primeiro, para o agente poder abrir o texto.
        ordered = _dedupe(_newest(found))
        items = [i for i in ordered if readable(i.link)][: limite // 2] + [
            i for i in ordered if not readable(i.link)
        ]
        items = _newest(items[:limite])
        return {"busca": query, "total": len(items), "noticias": [i.as_dict() for i in items]}

    @mcp.tool(title="Últimas notícias de um veículo", annotations=READ_ONLY)
    async def ultimas_noticias(fonte: Fonte, filtro: str | None = None, limite: int = 10) -> dict[str, Any]:
        """Últimas notícias do feed de um veículo (Agência Brasil, g1, InfoMoney, Poder360), com
        link direto. `filtro` mantém só as que citam todas as palavras."""
        _check_limit(limite)
        items = await guarded(data.feed(fonte))
        if filtro:
            items = [i for i in items if matches(i, _check_query(filtro))]
        items = items[:limite]
        return {"fonte": fonte, "total": len(items), "noticias": [i.as_dict() for i in items]}

    @mcp.tool(title="Ler notícia", annotations=READ_ONLY)
    async def ler_noticia(url: str) -> dict[str, Any]:
        """Texto de uma matéria (até ~6 mil caracteres). Só aceita links diretos de Agência Brasil,
        g1, InfoMoney, Poder360, IBGE, Senado, Câmara e gov.br, e respeita o robots.txt."""
        url = url.strip()
        if len(url) > 500 or not readable(url):
            raise ToolError("Só leio links https diretos destes sites: " + ", ".join(sorted(READABLE_HOSTS)))
        return dict(await guarded(data.read(url)))

    @mcp.tool(title="Notícias do IBGE", annotations=READ_ONLY)
    async def noticias_ibge(busca: str, limite: int = 6) -> dict[str, Any]:
        """Notícias e releases da Agência IBGE (PNAD, IPCA, PIB, Censo...)."""
        query = _check_query(busca)
        _check_limit(limite)
        raw = await guarded(data.get_json(IBGE_NEWS_URL, {"busca": query, "qtd": limite}, "IBGE"))
        items = [
            Item(
                titulo=clean(n.get("titulo"), 300) or "",
                veiculo="Agência IBGE",
                publicado_em=iso_date(n.get("data_publicacao")),
                link=str(n.get("link", "")).replace("http://", "https://", 1),
                resumo=clean(n.get("introducao")),
            )
            for n in (raw.get("items") or [])
            if isinstance(n, dict)
        ]
        return {"busca": query, "total": len(items), "noticias": [i.as_dict() for i in items]}

    @mcp.tool(title="Matérias do Senado", annotations=READ_ONLY)
    async def senado_materias(busca: str, limite: int = 8) -> dict[str, Any]:
        """Projetos e requerimentos no Senado que citam o termo, dos mais recentes aos mais antigos."""
        query = _check_query(busca)
        _check_limit(limite)
        raw = await guarded(data.get_json(SENADO_URL, {"palavraChave": query}, "Senado"))
        materias = ((raw.get("PesquisaBasicaMateria") or {}).get("Materias") or {}).get("Materia") or []
        if isinstance(materias, dict):
            materias = [materias]
        materias = sorted(materias, key=lambda m: m.get("Data", ""), reverse=True)[:limite]
        return {
            "busca": query,
            "total": len(materias),
            "materias": [
                {
                    "identificacao": m.get("DescricaoIdentificacao"),
                    "ementa": clean(m.get("Ementa"), 500),
                    "autor": m.get("Autor"),
                    "data": m.get("Data"),
                    "link": f"https://www25.senado.leg.br/web/atividade/materias/-/materia/{m.get('Codigo')}",
                }
                for m in materias
            ],
        }

    @mcp.tool(title="Proposições da Câmara", annotations=READ_ONLY)
    async def camara_proposicoes(busca: str, limite: int = 8) -> dict[str, Any]:
        """Proposições na Câmara dos Deputados que citam o termo, das mais novas para as mais antigas."""
        query = _check_query(busca)
        _check_limit(limite)
        params = {"keywords": query, "itens": limite, "ordem": "DESC", "ordenarPor": "id"}
        raw = await guarded(data.get_json(CAMARA_URL, params, "Câmara"))
        dados = raw.get("dados") or []
        return {
            "busca": query,
            "total": len(dados),
            "proposicoes": [
                {
                    "identificacao": f"{p.get('siglaTipo')} {p.get('numero')}/{p.get('ano')}",
                    "ementa": clean(p.get("ementa"), 500),
                    "link": f"https://www.camara.leg.br/propostas-legislativas/{p.get('id')}",
                }
                for p in dados
            ],
        }

    return mcp
