# Literature Research Skills

Portable skills for finding scientific papers, obtaining readable sources, and producing evidence-backed answers. Includes optional local MCP connector source, pinned Python dependencies, setup tools, and tests. **Mac-first; Python 3.12 for the optional tool stack.**

[Download a release](https://github.com/jstin777/literature-research-skills/releases) · [Installation](docs/install.md) · [Dependencies](docs/dependencies.md) · [Compatibility and validation](docs/compatibility.md)

## Choose what to install

| Skill | What it does | Activation |
| --- | --- | --- |
| [literature-search-review](skills/literature-search-review/SKILL.md) | Documented discovery, screening, citation tracing, source verification, and a compact handoff to a fresh writer | Subject-based paper searches |
| [paper-fulltext-access](skills/paper-fulltext-access/SKILL.md) | Direct LibKey recovery for a known paper; verifies actual readable content | Known-paper access and paywalls |
| [multi-agent-research-review](skills/multi-agent-research-review/SKILL.md) | Evidence mapping, independent advocate/adversarial evaluation, one cross-examination, synthesis and separate audits | **Explicit request only**; requires real agent delegation |

Install search and full-text access together for the integrated workflow. The access skill also works alone. Multi-agent review is optional and uses more model calls; no measured accuracy improvement is claimed.

**One repository, individual downloads.** All three skills have their own release ZIP. The skills-only plugin ZIP includes all three. The complete source bundle adds optional connectors and setup tools. This keeps one maintained version of each skill while letting recipients choose.

## Quick start in Codex on macOS

Download/extract the complete source ZIP, or clone this repository. Then, from its root:

```sh
python3 scripts/manage.py install-skills --dest "$HOME/.agents/skills"
```

This installs instructions and helper scripts, refuses overwrites, and leaves host settings alone. Do not also install the plugin ZIP with the same skills. Start a fresh chat if the skills do not appear.

Try:

> Use $literature-search-review to find five useful papers on urban tree cover and summer heat. Give me links, relevance and source-access labels; do not download files.

The host must already offer suitable search and source-reading tools. Skill instructions do not supply those capabilities. A missing fresh-context writer, browser control, account, or database must be disclosed. See [installation](docs/install.md) for the optional tool stack and other hosts.

## Optional connectors and external services

Included source: **OpenAlex**, **Semantic Scholar**, **PMC/Europe PMC full-text recovery**, and a **guarded Asta proxy** with timeouts, caching and explicit partial-recovery labels. Asta downloads its current tool schema during opt-in setup; the upstream schema and corpus are not redistributed.

Documented external integrations: **PubMed MCP**, **Scite MCP**, direct **LibKey**, browser/source-reading tools, and optional **Zotero–Obsidian**. See the [dependency matrix](docs/dependencies.md) and [connector setup](docs/connectors.md). Personal API keys, paid accounts and institutional access are not included. Public discovery remains usable without institutional subscriptions.

## Dynamic University Library Routing & Institutional Authentication

This repository supports dynamic institutional proxy routing across top universities worldwide (with preconfigured EZproxy prefixes and library portals in `config/universities.json`, including Vanderbilt University).

### Architecture & Routing Workflow

```mermaid
flowchart TD
    User["User / Agent"] -->|"1. Run select_university.py"| Selector["Interactive University Selector (scripts/select_university.py)"]
    Selector -->|"Loads Database"| UniDB["Top 100 Universities (config/universities.json)"]
    Selector -->|"Saves Selection"| Config["Active Institution Config (config/active_institution.json & .env)"]
    
    Config -->|"Reads Proxy Prefix & Portal"| Auth["Playwright Authentication (auth_setup.py)"]
    Auth -->|"2. Interactive Login & MFA"| IdP["Institutional SSO / EZproxy (e.g. Vanderbilt proxy)"]
    IdP -->|"3. Persists Cookies"| SessionState["Authenticated Session (state/storage_state.json)"]
    
    PaperReq["Paper Retrieval Request (DOI / URL)"] --> Router["Proxy URL Rewriter (get_proxied_url)"]
    Config --> Router
    Router -->|"Rewritten Proxied Link"| Resolver["LibKey / Publisher Gateway"]
    SessionState --> Resolver
    Resolver -->|"4. Download Full-Text PDF"| VerifiedFile["Verified Local Article (downloads/)"]
```

### Quick Setup

1. **Select Your University**:
   Launch the interactive selection CLI:
   ```sh
   python3 scripts/select_university.py
   ```
   Or specify an institution directly by ID:
   ```sh
   python3 scripts/select_university.py --id vanderbilt
   ```
   This automatically updates `config/active_institution.json` and your local `.env`.

2. **Authenticate with Your Library Portal**:
   Open an interactive Playwright browser session to sign in through your institution's SSO / Duo MFA:
   ```sh
   python3 auth_setup.py
   ```
   Your authenticated session cookies are saved securely to `state/storage_state.json` (gitignored).

3. **Retrieve and Verify Full-Text Articles**:
   Generate proxied links or automate full-text paper downloads using your active session:
   ```sh
   # Generate a proxied resolver link
   python3 auth_setup.py --proxy-url "10.1002/cphy.c170043"

   # Download full text PDF using authenticated session
   python3 auth_setup.py --download "10.1002/cphy.c170043"
   ```

## What the workflow preserves

- Stable paper/evidence IDs, exact search/access events, deduplication, pending work and resumable checkpoints.
- Separate eligibility, identity verification, abstract access, full-text coverage, evidence verification, and download states.
- Original-source locations and access limits in the writer handoff; snippets and provider summaries do not become verified claims automatically.
- Explicit limits on database coverage, failed routes, partial sources and unprocessed pagination.
- Optional Zotero organization that keeps review candidates separate from selected reading intake; AI processing never assigns human reading status.

The release contains reusable source and synthetic test fixtures. It excludes personal notes, papers, library exports, credentials, cached responses and research history. This is a GitHub distribution, not a listing in a plugin marketplace.

## Development

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements/dev.txt
.venv/bin/python scripts/test.py
.venv/bin/python scripts/smoke.py
.venv/bin/python scripts/check_release.py
.venv/bin/python scripts/build_release.py
```

Offline tests use synthetic records and mocked network responses. `smoke.py --live` makes a few public-provider calls; it is not an end-to-end literature review. See [maintenance](docs/maintenance.md) for release, backup and uninstall instructions.

MIT for original code and documentation. Dependencies and external services retain their own licenses and terms; see [third-party notices](THIRD_PARTY_NOTICES.md).

## Future Directions

### 1. Dedicated Local PDF-to-Markdown Extraction Pipeline
- **Deterministic Structural Parsing:** Replace raw `pypdf` text dumping with a local layout-aware parser (e.g., PyMuPDF layout analysis, Microsoft `markitdown`, or `marker`) to preserve document hierarchy (`#` title, `##` sections), multi-column reading flow, and reference lists.
- **Tabular & Figure Extraction:** Automatically extract data tables into standard Markdown grid tables and convert caption blocks into structured Markdown quotes to prevent hallucinated numbers during extraction audits.
- **Cost & Token Optimization:** Index clean Markdown artifacts locally instead of feeding multi-megabyte PDFs or unformatted text blocks to agent context windows.

### 2. Multi-Model and Multi-Host Portability
- **Beyond OpenAI Spec:** Decouple skill declarations from OpenAI-specific agent configurations (`openai.yaml`) by providing native definitions for Anthropic Claude (Model Context Protocol / MCP tool specs) and Google Gemini (Gemini CLI / Workspace agent manifests).
- **Standardized Execution Boundary:** Ensure system prompt contracts, tool calls, and handoff protocols execute consistently regardless of the underlying foundation model.

### 3. Local Retrieval & Hybrid Search Integration
- **Hybrid Retrieval (BM25 + Dense Embeddings):** Enable fast local vector and keyword indexing over converted markdown files in `search-records/` and `working/` for large corpora.
- **Chunk-Level Sourcing:** Allow the writer and audit agents to retrieve specific sub-sections or page locators without reading whole-paper bodies into active memory.

### 4. Automated Reference Manager Synchronization
- **Direct Zotero / Obsidian Bridge:** Automate the movement of included papers from the staging directory into Zotero collections via the Zotero Web API/local translation server, writing linked markdown literature notes directly into designated Obsidian vaults.
- **Bi-directional Annotations:** Extract highlighted PDF annotations and reader notes back into the `working/evidence.jsonl` record schema.
### 5. Reference Manager Expansion
- **Expanded Compatibility:** Add compatibility with other reference/citation managaers such as Paperpile, Mendeley, and EndNote.
  - **Zotero**: Free, open-source platform with robust community plugins and an excellent web clipper, but offers limited free cloud storage (300 MB) requiring paid upgrades for heavy PDF libraries.
  - **Paperpile**: Strong integration across Google workspace (e.g. Google Docs, Google Drive, etc) and may be better suited for Gemini-based workflows, but requires paid subscription
  - **Mendeley**: Built-in PDF annotation tools and literature recommendation features, but comes with corporate-owned data privacy concerns and a historically heavier desktop app.
  - **EndNote**: Industry standard for massive multi-thousand-reference libraries with complex journal styles, but is expensive proprietary software with a steep learning curve and dated interface.
### 6. README.md Visualization
- Add visualizations of workflow to the readme.md
