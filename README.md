# mcp-crawler

Serveur MCP qui crawle un site et télécharge les fichiers (pdf / docx / txt / autres).

Un seul fichier, `server.py`. Pas de build, pas de `pip install` : les dépendances
(`mcp`, `httpx`, `beautifulsoup4`) sont déclarées en inline script metadata (PEP 723) et `uv`
les installe au premier lancement.

## Installation

Prérequis : [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/gityoni/mcp-crawler.git
claude mcp add --scope user crawler -- uv run --script <chemin>/mcp-crawler/server.py
```

`--scope user` le rend disponible dans toutes les sessions Claude Code, quel que soit le projet.
Vérifier : `claude mcp list` doit afficher `crawler … ✔ Connected`. Le premier lancement peut
dépasser le timeout de 30 s le temps d'installer les dépendances — relancer la commande suffit.

## Périmètre

Fait bien : HTML statique, sitemaps (index suivi récursivement), n'importe quelles extensions,
cadrage par regex, débit réglable, téléchargement reprenable.

Ne fait pas : sites rendus en JavaScript (le HTML brut est lu tel quel), protections anti-bot
(Cloudflare challenge, captcha Wix), authentification. Pour ces cas-là, il faut un vrai navigateur.

## Outils

### `crawl_files`

Crawle depuis `start_url` et écrit un manifeste JSON des fichiers trouvés.

| paramètre | défaut | rôle |
|---|---|---|
| `start_url` | — | URL de départ |
| `extensions` | `["pdf","txt","docx"]` | extensions cherchées, sans point |
| `max_pages` | `300` | plafond de pages HTML visitées |
| `max_depth` | `3` | profondeur depuis `start_url` |
| `url_include` | `None` | regex — seules les pages HTML dont l'URL matche sont crawlées (essentiel : sans ça, on part dans les menus du site entier) |
| `probe_unknown` | `False` | teste en HEAD les liens sans extension (liens de download WordPress) |
| `manifest_path` | `./crawl-manifest.json` | où écrire le manifeste |
| `respect_robots` | `True` | respecte robots.txt |

Retourne un résumé : pages crawlées, fichiers trouvés par extension, chemin du manifeste,
`truncated_crawl` (true = `max_pages` atteint, il reste des pages en file).

Les liens vers des fichiers sont retenus **quel que soit le domaine** ; seule la navigation
HTML est limitée au domaine de départ.

### `download_files`

Télécharge les URLs d'un manifeste (ou une liste explicite) dans `dest_dir`.
Gère `Content-Disposition`, écrit en `.part` puis renomme (pas de fichier tronqué en cas de coupure),
saute les fichiers déjà présents sauf `overwrite=True`.

**Rejette les réponses `text/html`** quand l'URL n'est pas une page HTML : sinon les protections
anti-bot (Wix `sgcaptcha` par exemple) renvoient un HTTP 200 avec une page de 226 octets, et on
se retrouve avec des faux `.pdf` sur le disque. Vérifié en test : le captcha part en `failed`,
le vrai PDF passe.

## Leçon d'usage : toujours cadrer avec `url_include`

Sur `or-breslev.co.il`, sans `url_include`, le crawler part dans les menus (radio, VOD, boutique) :
519 pages découvertes, 0 fichier en 25 pages. Avec le scope, il trouve tout.

Le scope se donne en regex sur l'URL — ici, les pages livres plus la pagination de la catégorie
visée :

```
/books/|%d7%a1%d7%a4%d7%a8%d7%99-%d7%91%d7%a8%d7%a1%d7%9c%d7%91/
```

Attention : un scope large plus `max_depth ≥ 2` déborde vite du périmètre voulu, les pages se
liant entre elles. Vérifier `source_page` dans le manifeste avant de tout télécharger.

## Scripts d'appoint

`examples/check-integrity.py` — générique : vérifie la signature de chaque fichier téléchargé
(`%PDF`, `PK`), `--delete` supprime les faux. À lancer après tout gros téléchargement.

Patterns qui ont servi sur un gros run et qui valent d'être refaits au besoin :

- crawler par sitemaps successifs plutôt qu'en suivant les liens
- indexer une collection déjà possédée, puis ne télécharger que le delta
- normaliser les noms avant comparaison (tirets ↔ espaces, casse, ponctuation) — indispensable
  quand la source et la copie locale n'écrivent pas les titres pareil
- diagnostiquer les échecs par code HTTP avant de conclure qu'un fichier est introuvable
- vérifier la signature des fichiers après coup : un HTTP 200 ne garantit pas un vrai PDF

## Passer par le sitemap, pas par les liens

Le BFS à l'aveugle sur ce site plafonnait à **609 pages livres sur 2240** (27 %) : les menus
noient la file d'attente. `sitemap_url` règle le problème — `sitemap_index.xml` liste 2240 livres,
468 articles, 48 pages, 36 produits. Avec `max_depth=0`, on ne visite que ces URLs : exact,
aucune requête perdue, aucune troncature.

Bilan comparé sur le même site : 2659 documents trouvés en BFS, **5211 via les sitemaps**.

## Débit et robots.txt

`or-breslev.co.il` déclare `Crawl-delay: 60` pour `User-agent: *` — inapplicable à 2792 pages
(46 h). Le crawl a tourné à `concurrency=3, delay_seconds=0.4`. Le site déclare aussi
`ai-train=no, use=reference` et bloque nommément ClaudeBot / GPTBot / CCBot : usage de
référence personnelle uniquement.

## Résultat du run sur or-breslev.co.il

Crawl sitemap : 2792 pages, **5211 documents uniques**.

Collection de référence déjà possédée hors ligne : 3190 fichiers répartis en deux dossiers, dont
l'un s'est révélé **intégralement inclus** dans l'autre — soit **2910 documents** réellement
distincts. Vérifier ce genre de recouvrement avant de calculer un delta, sinon on surestime ce
qu'on possède.

Répartition des 5211 :

| n | |
|---|---|
| 2438 | déjà dans la collection de référence — non téléchargés |
| 2636 | téléchargés (1336 pdf + 1300 docx, 3,3 Go) |
| 93 | échecs (voir ci-dessous) |
| 44 | liens morts écartés d'office : 30 double extension, 14 dossiers Google Drive |

Le dossier local a été purgé de ses 227 doublons de la collection de référence, contient
0 fichier corrompu (`check-integrity.py`) et 0 doublon résiduel.

93 échecs, tous des liens morts ou protégés côté serveur :

| n | cause |
|---|---|
| 60 | `www.matokmidvash.com` — protection anti-bot Wix (captcha). UA navigateur + `Referer` ne suffisent pas : il faut une vraie session de navigateur |
| 33 | HTTP 404 côté or-breslev — liens morts dans les pages du site |

Un 94ᵉ échec pendant le lot était un 403 Cloudflare passager (débit) : récupéré en le relançant seul.
