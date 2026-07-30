# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp>=2.0",
#     "httpx>=0.27",
#     "beautifulsoup4>=4.12",
# ]
# ///
"""MCP server: crawle un site et telecharge les fichiers (pdf/txt/docx/...)."""

import asyncio
import json
import os
import re
import urllib.robotparser
from collections import deque
from html import unescape
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse, urlsplit

import httpx
from bs4 import BeautifulSoup
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("crawler")

UA = "Mozilla/5.0 (compatible; mcp-crawler/1.0)"
HTML_TYPES = ("text/html", "application/xhtml+xml")
DEFAULT_EXTS = ["pdf", "txt", "docx"]
CONCURRENCY = 6
TIMEOUT = 30.0


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": UA},
        follow_redirects=True,
        timeout=TIMEOUT,
    )


def _ext_of(url: str) -> str | None:
    path = unquote(urlsplit(url).path)
    _, dot, ext = path.rpartition(".")
    if not dot or "/" in ext or len(ext) > 5:
        return None
    return ext.lower()


def _norm(url: str) -> str:
    """Enleve le fragment, garde le reste tel quel."""
    s = urlsplit(url)
    return s._replace(fragment="").geturl()


async def _robots(client: httpx.AsyncClient, start_url: str):
    rp = urllib.robotparser.RobotFileParser()
    base = f"{urlsplit(start_url).scheme}://{urlsplit(start_url).netloc}"
    try:
        r = await client.get(f"{base}/robots.txt")
        rp.parse(r.text.splitlines() if r.status_code == 200 else [])
    except Exception:
        rp.parse([])
    return rp


async def _sitemap_urls(client: httpx.AsyncClient, url: str, errors: list[str], depth: int = 0) -> list[str]:
    """Extrait les <loc> d'un sitemap; suit recursivement les index de sitemaps."""
    if depth > 2:
        return []
    try:
        r = await client.get(url)
        r.raise_for_status()
        xml = r.text
    except Exception as e:
        errors.append(f"sitemap {url}: {e}")
        return []
    locs = [unescape(m) for m in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)]
    is_index = "<sitemapindex" in xml[:2000]
    if not is_index:
        return [_norm(u) for u in locs]
    out: list[str] = []
    for sub in locs:
        out += await _sitemap_urls(client, sub, errors, depth + 1)
    return out


def _safe_name(url: str, fallback_index: int) -> str:
    name = unquote(os.path.basename(urlsplit(url).path)) or f"file_{fallback_index}"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:150] or f"file_{fallback_index}"


@mcp.tool()
async def crawl_files(
    start_url: str,
    extensions: list[str] | None = None,
    max_pages: int = 300,
    max_depth: int = 3,
    url_include: str | None = None,
    probe_unknown: bool = False,
    manifest_path: str | None = None,
    respect_robots: bool = True,
    sitemap_url: str | None = None,
    concurrency: int = CONCURRENCY,
    delay_seconds: float = 0.0,
) -> str:
    """Crawle un site depuis start_url et liste les fichiers telechargeables trouves.

    Args:
        start_url: URL de depart.
        extensions: extensions cherchees, sans point (defaut: pdf, txt, docx).
        max_pages: nombre max de pages HTML visitees.
        max_depth: profondeur max de navigation depuis start_url. 0 = ne visiter que
            les URLs de depart (utile avec sitemap_url).
        url_include: regex; seules les pages HTML dont l'URL matche sont crawlees.
        probe_unknown: teste en HEAD les liens sans extension (liens de download WordPress).
        manifest_path: ou ecrire le manifeste JSON (defaut: ./crawl-manifest.json a cote de ce script).
        respect_robots: respecte robots.txt.
        sitemap_url: sitemap XML (ou index de sitemaps, suivi recursivement) dont les URLs
            servent de points de depart au lieu de decouvrir les liens a l'aveugle.
        concurrency: requetes simultanees.
        delay_seconds: pause entre deux lots de requetes (menagement du serveur).

    Returns:
        Resume JSON + chemin du manifeste.
    """
    exts = {e.lower().lstrip(".") for e in (extensions or DEFAULT_EXTS)}
    include_re = re.compile(url_include) if url_include else None
    host = urlsplit(start_url).netloc

    seen_pages: set[str] = set()
    seen_files: dict[str, dict] = {}
    probed: set[str] = set()
    queue: deque[tuple[str, int]] = deque()
    pages_done = 0
    errors: list[str] = []

    sem = asyncio.Semaphore(max(1, concurrency))

    async with _client() as client:
        rp = await _robots(client, start_url) if respect_robots else None

        seeds = [_norm(start_url)]
        if sitemap_url:
            seeds = await _sitemap_urls(client, sitemap_url, errors)
        for s in seeds:
            if s not in seen_pages:
                seen_pages.add(s)
                queue.append((s, 0))

        async def probe(url: str, source: str):
            async with sem:
                try:
                    r = await client.head(url)
                    if r.status_code >= 400:
                        r = await client.get(url, headers={"Range": "bytes=0-0"})
                    ctype = r.headers.get("content-type", "").split(";")[0].strip()
                    disp = r.headers.get("content-disposition", "")
                    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disp)
                    fname = unquote(m.group(1)) if m else None
                    cand = (fname or str(r.url))
                    ext = _ext_of(cand) or {
                        "application/pdf": "pdf",
                        "text/plain": "txt",
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
                        "application/msword": "doc",
                    }.get(ctype)
                    if ext and ext in exts:
                        seen_files.setdefault(
                            url,
                            {"url": url, "source_page": source, "ext": ext, "filename": fname},
                        )
                except Exception as e:
                    errors.append(f"HEAD {url}: {e}")

        while queue and pages_done < max_pages:
            batch = []
            while queue and len(batch) < max(1, concurrency) and pages_done + len(batch) < max_pages:
                batch.append(queue.popleft())

            async def fetch(item):
                url, depth = item
                if rp and not rp.can_fetch(UA, url):
                    return url, depth, None
                async with sem:
                    try:
                        r = await client.get(url)
                        ctype = r.headers.get("content-type", "").split(";")[0].strip()
                        if r.status_code >= 400 or ctype not in HTML_TYPES:
                            return url, depth, None
                        return url, depth, r.text
                    except Exception as e:
                        errors.append(f"GET {url}: {e}")
                        return url, depth, None

            results = await asyncio.gather(*(fetch(i) for i in batch))
            pages_done += len(batch)
            if delay_seconds:
                await asyncio.sleep(delay_seconds)

            probes = []
            for url, depth, html in results:
                if html is None:
                    continue
                soup = BeautifulSoup(html, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = a["href"].strip()
                    if not href or href.startswith(("mailto:", "javascript:", "tel:", "#")):
                        continue
                    link = _norm(urljoin(url, href))
                    ext = _ext_of(link)
                    if ext and ext in exts:
                        seen_files.setdefault(
                            link,
                            {
                                "url": link,
                                "source_page": url,
                                "ext": ext,
                                "filename": None,
                                "text": a.get_text(strip=True)[:120],
                            },
                        )
                        continue
                    if urlsplit(link).netloc != host:
                        continue
                    if ext is not None:
                        continue  # extension connue mais pas voulue (.jpg, .css...)
                    if include_re and not include_re.search(link):
                        continue
                    if probe_unknown and ("download" in link.lower() or "?" in link) and link not in probed:
                        probed.add(link)
                        probes.append(probe(link, url))
                    if depth + 1 > max_depth:
                        continue
                    if link not in seen_pages:
                        seen_pages.add(link)
                        queue.append((link, depth + 1))

            if probes:
                await asyncio.gather(*probes)

    files = list(seen_files.values())
    dest = Path(manifest_path) if manifest_path else Path(__file__).with_name("crawl-manifest.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps({"start_url": start_url, "files": files}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    by_ext: dict[str, int] = {}
    for f in files:
        by_ext[f["ext"]] = by_ext.get(f["ext"], 0) + 1

    return json.dumps(
        {
            "pages_crawled": pages_done,
            "pages_discovered": len(seen_pages),
            "files_found": len(files),
            "by_extension": by_ext,
            "manifest_path": str(dest),
            "sample": [f["url"] for f in files[:15]],
            "errors": errors[:10],
            "truncated_crawl": bool(queue),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
async def download_files(
    dest_dir: str,
    manifest_path: str | None = None,
    urls: list[str] | None = None,
    overwrite: bool = False,
) -> str:
    """Telecharge les fichiers d'un manifeste (ou une liste d'URLs) dans dest_dir.

    Args:
        dest_dir: dossier de destination (cree si absent).
        manifest_path: manifeste produit par crawl_files.
        urls: liste d'URLs explicites (alternative au manifeste).
        overwrite: re-telecharge les fichiers deja presents.

    Returns:
        Resume JSON: telecharges, ignores, echecs.
    """
    targets: list[str] = list(urls or [])
    if manifest_path:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        targets += [f["url"] for f in data.get("files", [])]
    targets = list(dict.fromkeys(targets))
    if not targets:
        return json.dumps({"error": "aucune URL: fournis manifest_path ou urls"})

    out = Path(dest_dir)
    out.mkdir(parents=True, exist_ok=True)

    downloaded, skipped, failed = [], [], []
    sem = asyncio.Semaphore(CONCURRENCY)

    async with _client() as client:

        async def grab(i: int, url: str):
            async with sem:
                name = _safe_name(url, i)
                path = out / name
                if path.exists() and not overwrite:
                    skipped.append(name)
                    return
                try:
                    async with client.stream("GET", url) as r:
                        r.raise_for_status()
                        # Une page HTML servie a la place du fichier = captcha / anti-bot
                        # (ex. Wix sgcaptcha) ou page d'erreur en 200. Ce n'est pas le fichier.
                        ctype = r.headers.get("content-type", "").split(";")[0].strip()
                        if ctype in HTML_TYPES and _ext_of(url) not in ("html", "htm"):
                            failed.append({"url": url, "error": f"reponse HTML ({ctype}) au lieu du fichier"})
                            return
                        disp = r.headers.get("content-disposition", "")
                        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disp)
                        if m:
                            name = _safe_name("/" + unquote(m.group(1)), i)
                            path = out / name
                            if path.exists() and not overwrite:
                                skipped.append(name)
                                return
                        stem, suffix = path.stem, path.suffix
                        n = 1
                        while path.exists() and overwrite is False:
                            path = out / f"{stem}_{n}{suffix}"
                            n += 1
                        tmp = path.with_suffix(path.suffix + ".part")
                        with tmp.open("wb") as fh:
                            async for chunk in r.aiter_bytes(65536):
                                fh.write(chunk)
                        tmp.replace(path)
                        downloaded.append({"name": path.name, "bytes": path.stat().st_size})
                except Exception as e:
                    failed.append({"url": url, "error": str(e)[:200]})

        await asyncio.gather(*(grab(i, u) for i, u in enumerate(targets)))

    return json.dumps(
        {
            "dest_dir": str(out),
            "downloaded": len(downloaded),
            "skipped_existing": len(skipped),
            "failed": len(failed),
            "total_bytes": sum(d["bytes"] for d in downloaded),
            "failures": failed[:10],
        },
        ensure_ascii=False,
        indent=2,
    )


if __name__ == "__main__":
    mcp.run()
