#!/usr/bin/env python3
"""
scripts/format_print_fallback.py - Token-Minimal Fallback Typesetting & Journal PDF Generator.

When scientific publisher downloads fail or return anti-bot challenge pages and trigger
the print-to-PDF fallback (is_print_fallback=True), this module:
1. Strips repeating publisher UI clutter (LeapSpace, Reading Assistant, high-res download
   links, Scopus, Google Scholar crumbs, duplicate Box blocks).
2. Optionally runs a lightweight fast LLM (gemini-2.5-flash or gpt-4o-mini) on ONLY the
   title/byline/body text (excluding references) to normalize headings and strip residual
   web artifacts, re-attaching references deterministically (0 tokens by default).
3. Renders the cleaned Markdown into an academic journal HTML/CSS layout (serif typography,
   header banner [Reformatted Print Capture - Not Publisher Typeset PDF], structured tables
   and callout boxes, 2-column references) and exports a clean PDF via Playwright Chromium.
"""

import argparse
import html
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

import yaml

BANNER_TEXT = "[Reformatted Print Capture - Not Publisher Typeset PDF]"

CLEANUP_SYSTEM_PROMPT = (
    "You are an academic document typesetter. Clean this extracted scientific paper markdown:\n"
    "1. Strip any residual publisher website navigation, cookie notices, 'back to top', 'cite this', or UI artifacts.\n"
    "2. Normalize Markdown headings (# for main title, ## for major sections like Abstract, Introduction, Results, Discussion, ### for subsections).\n"
    "3. Preserve all scientific content, author names, equations, data, tables, and narrative verbatim. Do not summarize or omit paragraphs.\n"
    "4. Return ONLY the cleaned Markdown without any commentary."
)


def clean_publisher_chrome(markdown_text: str) -> str:
    """Apply deterministic regex stripping to remove repeating publisher web UI clutter."""
    if not markdown_text:
        return ""

    # Inline and line-level publisher chrome patterns
    patterns = [
        # Reading Assistant / LeapSpace
        r"(?i)reading\s+assistant\s*(?:powered\s+by\s+leapspace)?",
        r"(?i)powered\s+by\s+leapspace",
        r"(?i)\bleapspace\b",
        r"(?i)actions\s+you\s+could\s+take:?",
        r"(?i)ask\s+a\s+question\s+about\s+this\s+article",
        r"(?i)summarize\s+article",
        # Image / Figure download artifacts
        r"(?i)download:\s*download\s+(?:high-res|full-size)\s+image",
        r"(?i)download\s+(?:high-res|full-size)\s+image(?:\s*\([^)]*\))?",
        r"(?i)download\s+figure",
        r"(?i)open\s+in\s+figure\s+viewer",
        # Database & search links
        r"(?i)view\s+in\s+scopus",
        r"\[Google\s+Scholar\]\([^)]*\)",
        r"(?i)view\s+in\s+google\s+scholar",
        r"\[Crossref\]\([^)]*\)",
        r"\[PubMed\]\([^)]*\)",
        r"\[Scopus\]\([^)]*\)",
        # Navigation / button chrome
        r"\[Download\s+PDF\]\([^)]*\)",
        r"(?i)download\s+pdf",
        r"(?i)\bshow\s+more\b",
        r"(?i)\bshow\s+less\b",
    ]

    cleaned = markdown_text
    for p in patterns:
        cleaned = re.sub(p, "", cleaned)

    # Paragraph-level deduplication (especially repeated Box / Callout blocks)
    paragraphs = re.split(r"\n{2,}", cleaned)
    deduped_paragraphs = []
    seen_recent = []

    for p in paragraphs:
        p_clean = p.strip()
        if not p_clean:
            continue

        # Skip immediately identical paragraphs
        if seen_recent and p_clean == seen_recent[-1]:
            continue

        # Deduplicate repeated Box blocks within recent history
        if re.match(r"^(?:#{1,4}\s*)?(?:Box|Callout)\s+\d+", p_clean, re.IGNORECASE):
            if any(p_clean == prev for prev in seen_recent[-4:]):
                continue

        deduped_paragraphs.append(p_clean)
        seen_recent.append(p_clean)

    result = "\n\n".join(deduped_paragraphs)
    # Normalize excess blank lines
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip() + "\n"


def split_markdown_at_references(markdown_text: str) -> Tuple[str, str]:
    """
    Split markdown at References/Bibliography header into (body, references).
    This enables token minimization: references are never sent to the LLM.
    """
    if not markdown_text:
        return "", ""

    # Match header line like: # References, ## References, ## Literature Cited, etc.
    pattern = re.compile(
        r"^(?P<header>#{1,4}\s*(?:References|Bibliography|Literature\s+Cited|Works\s+Cited)\b.*)$",
        re.MULTILINE | re.IGNORECASE,
    )
    match = pattern.search(markdown_text)
    if match:
        body = markdown_text[: match.start()].rstrip()
        refs = markdown_text[match.start() :].strip()
        return body, refs

    return markdown_text.rstrip(), ""


def extract_frontmatter(markdown_text: str) -> Tuple[Optional[dict], str]:
    """Separate YAML frontmatter from Markdown body."""
    if markdown_text.startswith("---"):
        parts = markdown_text.split("---", 2)
        if len(parts) >= 3:
            raw_yaml = parts[1]
            body = parts[2]
            try:
                data = yaml.safe_load(raw_yaml)
                if isinstance(data, dict):
                    return data, body.strip()
            except Exception:
                pass
    return None, markdown_text


def reformat_with_optional_llm(markdown_text: str, metadata: Optional[dict] = None) -> str:
    """
    Token-Minimal Optional LLM Cleanup.
    - Default: Pure deterministic cleanup (0 tokens consumed).
    - If ENABLE_LLM_FALLBACK_CLEANUP=true and API key exists:
      Splits references away (references are NEVER sent to the LLM), sends
      only title/byline/body to gemini-2.5-flash or gpt-4o-mini, and re-attaches
      references deterministically.
    """
    # Deterministic cleanup first
    cleaned_deterministic = clean_publisher_chrome(markdown_text)

    enable_llm_flag = os.environ.get("ENABLE_LLM_FALLBACK_CLEANUP", "").strip().lower()
    if enable_llm_flag not in ("true", "1", "yes"):
        return cleaned_deterministic

    gemini_key = os.environ.get("GEMINI_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    if not gemini_key and not openai_key:
        print("  ENABLE_LLM_FALLBACK_CLEANUP enabled, but neither GEMINI_API_KEY nor OPENAI_API_KEY is set. Using deterministic cleanup.")
        return cleaned_deterministic

    try:
        import httpx
    except ImportError:
        print("  httpx library not found. Using deterministic cleanup.")
        return cleaned_deterministic

    # Extract frontmatter if present so it is not sent to the LLM
    fm, body_text = extract_frontmatter(cleaned_deterministic)

    # Split body from references: references are NEVER sent to LLM
    body_to_clean, references_part = split_markdown_at_references(body_text)

    llm_cleaned_body = None

    if gemini_key:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": f"{CLEANUP_SYSTEM_PROMPT}\n\nDocument body:\n\n{body_to_clean}"}
                        ],
                    }
                ],
                "generationConfig": {"temperature": 0.1},
            }
            resp = httpx.post(url, json=payload, timeout=45.0)
            if resp.status_code == 200:
                data = resp.json()
                parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                if parts and "text" in parts[0]:
                    llm_cleaned_body = parts[0]["text"].strip()
                    print("✓ LLM cleanup succeeded using gemini-2.5-flash (references omitted to minimize tokens).")
            else:
                print(f"  Gemini API responded with status {resp.status_code}: {resp.text[:150]}")
        except Exception as e:
            print(f"  Note during Gemini LLM cleanup: {e}")

    if not llm_cleaned_body and openai_key:
        try:
            url = "https://api.openai.com/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {openai_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": CLEANUP_SYSTEM_PROMPT},
                    {"role": "user", "content": body_to_clean},
                ],
                "temperature": 0.1,
            }
            resp = httpx.post(url, headers=headers, json=payload, timeout=45.0)
            if resp.status_code == 200:
                data = resp.json()
                llm_cleaned_body = data["choices"][0]["message"]["content"].strip()
                print("✓ LLM cleanup succeeded using gpt-4o-mini (references omitted to minimize tokens).")
            else:
                print(f"  OpenAI API responded with status {resp.status_code}: {resp.text[:150]}")
        except Exception as e:
            print(f"  Note during OpenAI LLM cleanup: {e}")

    final_body = llm_cleaned_body if llm_cleaned_body else body_to_clean

    # Deterministically re-attach the references
    if references_part:
        reconstructed = f"{final_body.strip()}\n\n{references_part.strip()}\n"
    else:
        reconstructed = f"{final_body.strip()}\n"

    # Re-attach frontmatter if previously present
    if fm:
        fm_yaml = yaml.dump(fm, sort_keys=False).strip()
        return f"---\n{fm_yaml}\n---\n\n{reconstructed}"

    return reconstructed


def format_inlines(text: str) -> str:
    """Format markdown inline styles: bold, italic, code, links, images."""
    # Escape existing HTML special chars first
    text = html.escape(text)

    # Images: ![alt](url) -> inline figure or small img
    text = re.sub(
        r"!\[([^\]]*)\]\(([^)]+)\)",
        r'<figure class="article-image"><img src="\2" alt="\1" /><figcaption>\1</figcaption></figure>',
        text,
    )
    # Bold + Italic
    text = re.sub(r"\*\*\*(.*?)\*\*\*", r"<strong><em>\1</em></strong>", text)
    # Bold
    text = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__(.*?)__", r"<strong>\1</strong>", text)
    # Italic
    text = re.sub(r"\*(.*?)\*", r"<em>\1</em>", text)
    text = re.sub(r"_(.*?)_", r"<em>\1</em>", text)
    # Inline code
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # Links: [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

    return text


def parse_markdown_blocks_to_html(md_text: str) -> str:
    """Pure-Python markdown to HTML block parser (zero external LaTeX/pandoc/markdown dependencies)."""
    lines = md_text.splitlines()
    html_lines = []
    in_code_block = False
    in_list = False
    in_table = False

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Code block fence
        if stripped.startswith("```"):
            if in_code_block:
                html_lines.append("</code></pre>")
                in_code_block = False
            else:
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                lang = stripped[3:].strip()
                cls = f' class="language-{lang}"' if lang else ""
                html_lines.append(f"<pre><code{cls}>")
                in_code_block = True
            i += 1
            continue

        if in_code_block:
            html_lines.append(html.escape(line))
            i += 1
            continue

        # Blank line
        if not stripped:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            if in_table:
                html_lines.append("</tbody></table>")
                in_table = False
            i += 1
            continue

        # Headings (# through ######)
        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading_match:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            if in_table:
                html_lines.append("</tbody></table>")
                in_table = False
            level = len(heading_match.group(1))
            h_text = format_inlines(heading_match.group(2).strip())
            html_lines.append(f"<h{level}>{h_text}</h{level}>")
            i += 1
            continue

        # Markdown tables (| col1 | col2 |)
        if stripped.startswith("|") and stripped.endswith("|"):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            cells = [c.strip() for c in stripped[1:-1].split("|")]
            # Check if alignment/separator row
            if all(re.match(r"^:?-+:?$", c) for c in cells):
                i += 1
                continue
            if not in_table:
                in_table = True
                html_lines.append('<table class="styled-table"><thead><tr>')
                for c in cells:
                    html_lines.append(f"<th>{format_inlines(c)}</th>")
                html_lines.append("</tr></thead><tbody>")
            else:
                html_lines.append("<tr>")
                for c in cells:
                    html_lines.append(f"<td>{format_inlines(c)}</td>")
                html_lines.append("</tr>")
            i += 1
            continue
        elif in_table:
            html_lines.append("</tbody></table>")
            in_table = False

        # Blockquote or Box
        if stripped.startswith(">"):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            bq_text = format_inlines(stripped[1:].strip())
            html_lines.append(f'<blockquote class="callout-box">{bq_text}</blockquote>')
            i += 1
            continue

        # Unordered list item
        if stripped.startswith(("- ", "* ")):
            if not in_list:
                html_lines.append('<ul class="article-list">')
                in_list = True
            item_text = format_inlines(stripped[2:].strip())
            html_lines.append(f"<li>{item_text}</li>")
            i += 1
            continue
        elif in_list:
            html_lines.append("</ul>")
            in_list = False

        # Box callouts (e.g. Box 1: Key Concepts)
        if re.match(r"^(?:Box|Callout)\s+\d+", stripped, re.IGNORECASE):
            html_lines.append(f'<div class="callout-box"><strong>{format_inlines(stripped)}</strong></div>')
            i += 1
            continue

        # Paragraph
        p_text = format_inlines(stripped)
        html_lines.append(f"<p>{p_text}</p>")
        i += 1

    if in_list:
        html_lines.append("</ul>")
    if in_table:
        html_lines.append("</tbody></table>")
    if in_code_block:
        html_lines.append("</code></pre>")

    return "\n".join(html_lines)


def format_references_html(references_text: str) -> str:
    """Format references into a clean academic two-column section."""
    if not references_text:
        return ""

    lines = references_text.strip().splitlines()
    ref_heading = "References"
    ref_items = []

    # First line might be heading (## References)
    start_idx = 0
    if lines and re.match(r"^#{1,4}\s*(?:References|Bibliography|Literature\s+Cited|Works\s+Cited)\b", lines[0], re.IGNORECASE):
        ref_heading = re.sub(r"^#{1,4}\s*", "", lines[0]).strip()
        start_idx = 1

    current_item = []
    for line in lines[start_idx:]:
        stripped = line.strip()
        if not stripped:
            if current_item:
                ref_items.append(" ".join(current_item))
                current_item = []
            continue

        # Check if line starts a new reference (e.g., [1], 1., or author year)
        if re.match(r"^(?:\[\d+\]|\d+\.|\b[A-Z][a-zA-Z\-]+,\s+[A-Z]\.)", stripped):
            if current_item:
                ref_items.append(" ".join(current_item))
                current_item = []

        current_item.append(stripped)

    if current_item:
        ref_items.append(" ".join(current_item))

    # Format items
    formatted_items = []
    for item in ref_items:
        formatted_items.append(f'<p class="reference-item">{format_inlines(item)}</p>')

    items_html = "\n".join(formatted_items) if formatted_items else parse_markdown_blocks_to_html("\n".join(lines[start_idx:]))

    return f"""
    <section class="references-section">
      <h2 class="references-heading">{html.escape(ref_heading)}</h2>
      <div class="references-list">
        {items_html}
      </div>
    </section>
    """


def markdown_to_journal_html(markdown_text: str, metadata: Optional[dict] = None) -> str:
    """
    Render cleaned Markdown into academic journal HTML/CSS layout.
    Features:
    - Serif body typography (Charter / Georgia)
    - Subtle top header banner noting [Reformatted Print Capture - Not Publisher Typeset PDF]
    - Clean title/byline/metadata header
    - Structured tables and callout boxes
    - Two-column references section
    """
    # Extract YAML frontmatter
    fm, body = extract_frontmatter(markdown_text)
    meta = {}
    if fm:
        meta.update(fm)
    if metadata:
        meta.update(metadata)

    title = meta.get("title") or "Untitled Paper"
    doi = meta.get("doi") or ""
    year = meta.get("year") or ""
    venue = meta.get("venue") or ""
    authors = meta.get("authors") or []

    # Format authors string
    author_names = []
    if isinstance(authors, list):
        for a in authors:
            if isinstance(a, dict):
                name = a.get("literal") or f"{a.get('family', '')}, {a.get('given', '')}".strip(", ")
            else:
                name = str(a)
            if name:
                author_names.append(name)
    elif authors:
        author_names.append(str(authors))
    authors_display = " &bull; ".join([html.escape(a) for a in author_names]) if author_names else "Unknown Author"

    # Meta display line
    meta_parts = []
    if venue:
        meta_parts.append(f"<em>{html.escape(venue)}</em>")
    if year:
        meta_parts.append(str(year))
    if doi:
        meta_parts.append(f'DOI: <a href="https://doi.org/{html.escape(doi)}">{html.escape(doi)}</a>')
    meta_display = " &bull; ".join(meta_parts)

    # Split body and references
    body_part, references_part = split_markdown_at_references(body)

    # If first heading in body_part is identical to title, remove it to avoid repetition
    body_lines = body_part.strip().splitlines()
    if body_lines and re.match(r"^#\s+(.*)$", body_lines[0]):
        h1_text = re.sub(r"^#\s+", "", body_lines[0]).strip()
        if h1_text.lower() == title.lower():
            body_part = "\n".join(body_lines[1:]).strip()

    body_html = parse_markdown_blocks_to_html(body_part)
    references_html = format_references_html(references_part) if references_part else ""

    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{html.escape(title)}</title>
  <style>
    @page {{
      size: A4;
      margin: 20mm 18mm 20mm 18mm;
      @bottom-center {{
        content: counter(page);
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        font-size: 8.5pt;
        color: #6b7280;
      }}
    }}

    body {{
      font-family: 'Charter', 'Cambria', 'Georgia', 'Times New Roman', serif;
      color: #1a1a1a;
      background-color: #ffffff;
      line-height: 1.55;
      font-size: 10.5pt;
      margin: 0;
      padding: 0;
      text-rendering: optimizeLegibility;
      -webkit-font-smoothing: antialiased;
    }}

    /* Top Subtle Header Banner */
    .print-fallback-banner {{
      background-color: #f3f4f6;
      color: #4b5563;
      border: 1px solid #d1d5db;
      border-radius: 4px;
      padding: 6px 14px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      font-size: 8pt;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      text-align: center;
      margin-bottom: 24px;
    }}

    /* Title & Byline Header */
    .article-header {{
      margin-bottom: 26px;
      padding-bottom: 16px;
      border-bottom: 2px solid #111827;
    }}
    .article-title {{
      font-family: 'Charter', 'Georgia', serif;
      font-size: 22pt;
      font-weight: 700;
      line-height: 1.25;
      color: #111827;
      margin: 0 0 10px 0;
    }}
    .article-authors {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 11pt;
      font-weight: 500;
      color: #374151;
      margin-bottom: 8px;
    }}
    .article-meta {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 9pt;
      color: #6b7280;
    }}
    .article-meta a {{
      color: #2563eb;
      text-decoration: none;
    }}

    /* Headings */
    h1, h2, h3, h4, h5, h6 {{
      font-family: 'Charter', 'Georgia', serif;
      color: #111827;
      page-break-after: avoid;
      break-after: avoid;
    }}
    h1 {{
      font-size: 17pt;
      margin-top: 24px;
      margin-bottom: 10px;
    }}
    h2 {{
      font-size: 13.5pt;
      font-weight: 700;
      margin-top: 20px;
      margin-bottom: 8px;
      border-bottom: 1px solid #e5e7eb;
      padding-bottom: 4px;
    }}
    h3 {{
      font-size: 11.5pt;
      font-weight: 600;
      margin-top: 16px;
      margin-bottom: 6px;
    }}

    p {{
      margin: 0 0 10px 0;
      text-align: justify;
    }}

    /* Callouts and Boxes */
    .callout-box, blockquote {{
      border-left: 4px solid #2563eb;
      background-color: #f8fafc;
      padding: 10px 16px;
      margin: 14px 0;
      border-radius: 0 4px 4px 0;
      font-size: 9.8pt;
      page-break-inside: avoid;
      break-inside: avoid;
    }}

    /* Tables */
    table.styled-table {{
      width: 100%;
      border-collapse: collapse;
      margin: 16px 0;
      font-size: 9pt;
      page-break-inside: avoid;
      break-inside: avoid;
    }}
    table.styled-table th {{
      border-top: 1.5px solid #111827;
      border-bottom: 1.5px solid #111827;
      padding: 6px 8px;
      font-weight: 700;
      text-align: left;
      background-color: #f9fafb;
    }}
    table.styled-table td {{
      border-bottom: 1px solid #e5e7eb;
      padding: 6px 8px;
      text-align: left;
    }}

    /* Lists */
    ul.article-list {{
      margin: 8px 0 12px 20px;
      padding: 0;
    }}
    ul.article-list li {{
      margin-bottom: 4px;
    }}

    /* Code blocks */
    pre {{
      background-color: #f3f4f6;
      border: 1px solid #e5e7eb;
      border-radius: 4px;
      padding: 10px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 8.5pt;
      overflow-x: auto;
      page-break-inside: avoid;
      break-inside: avoid;
    }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 9pt;
      background-color: #f3f4f6;
      padding: 1px 4px;
      border-radius: 3px;
    }}

    /* Two-Column References Section */
    .references-section {{
      margin-top: 26px;
      padding-top: 14px;
      border-top: 1.5px solid #111827;
      page-break-before: auto;
    }}
    .references-heading {{
      font-size: 13pt;
      font-weight: 700;
      margin-bottom: 10px;
      border-bottom: none;
    }}
    .references-list {{
      column-count: 2;
      column-gap: 22px;
      font-size: 8.5pt;
      line-height: 1.42;
      text-align: justify;
    }}
    .references-list p.reference-item, .references-list p {{
      margin: 0 0 6px 0;
      page-break-inside: avoid;
      break-inside: avoid;
    }}

    /* Print media rules */
    @media print {{
      body {{
        font-size: 10pt;
      }}
      h1, h2, h3, h4 {{
        page-break-after: avoid;
        break-after: avoid;
      }}
      table, .callout-box, blockquote, pre {{
        page-break-inside: avoid;
        break-inside: avoid;
      }}
    }}
  </style>
</head>
<body>
  <div class="print-fallback-banner">{BANNER_TEXT}</div>

  <header class="article-header">
    <h1 class="article-title">{html.escape(title)}</h1>
    <div class="article-authors">{authors_display}</div>
    {f'<div class="article-meta">{meta_display}</div>' if meta_display else ''}
  </header>

  <main class="article-body">
    {body_html}
  </main>

  {references_html}
</body>
</html>
"""
    return html_template


def render_html_to_pdf(html_content: str, output_pdf_path: Path, page=None) -> Path:
    """Render HTML string to PDF using Playwright Chromium."""
    output_pdf_path = Path(output_pdf_path).resolve()
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    if page is not None:
        page.set_content(html_content, wait_until="load")
        pdf_bytes = page.pdf(
            format="A4",
            print_background=True,
            margin={"top": "20mm", "bottom": "20mm", "left": "18mm", "right": "18mm"},
        )
        output_pdf_path.write_bytes(pdf_bytes)
        return output_pdf_path

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        new_page = browser.new_page()
        new_page.set_content(html_content, wait_until="load")
        pdf_bytes = new_page.pdf(
            format="A4",
            print_background=True,
            margin={"top": "20mm", "bottom": "20mm", "left": "18mm", "right": "18mm"},
        )
        browser.close()
        output_pdf_path.write_bytes(pdf_bytes)
        return output_pdf_path


def format_print_capture(
    markdown_path: Path,
    output_pdf_path: Optional[Path] = None,
    metadata: Optional[dict] = None,
    page=None,
) -> Path:
    """
    High-level entry point to format a print-to-PDF Markdown capture:
    1. Reads Markdown file.
    2. Runs deterministic cleanup and optional LLM cleanup (0 tokens by default).
    3. Converts to journal-style HTML.
    4. Renders and saves journal-formatted PDF.
    """
    markdown_path = Path(markdown_path).resolve()
    if not markdown_path.is_file():
        raise FileNotFoundError(f"Markdown file not found: {markdown_path}")

    raw_text = markdown_path.read_text(encoding="utf-8")

    # Clean text (deterministic + optional LLM)
    cleaned_md = reformat_with_optional_llm(raw_text, metadata=metadata)

    # Convert to journal HTML
    journal_html = markdown_to_journal_html(cleaned_md, metadata=metadata)

    # Determine output path
    if output_pdf_path is None:
        stem = markdown_path.stem
        output_pdf_path = markdown_path.parent.parent / "zotero_intake" / stem / "formatted_paper.pdf"

    output_pdf_path = Path(output_pdf_path).resolve()
    render_html_to_pdf(journal_html, output_pdf_path, page=page)
    return output_pdf_path


def main():
    parser = argparse.ArgumentParser(
        description="Format print-to-PDF markdown capture into a clean academic journal PDF."
    )
    parser.add_argument("markdown_file", help="Path to input Markdown file")
    parser.add_argument(
        "--output",
        "-o",
        help="Path for output formatted PDF (default: working/zotero_intake/<stem>/formatted_paper.pdf)",
    )
    parser.add_argument(
        "--enable-llm",
        action="store_true",
        help="Enable optional LLM cleanup (overrides ENABLE_LLM_FALLBACK_CLEANUP)",
    )
    args = parser.parse_args()

    if args.enable_llm:
        os.environ["ENABLE_LLM_FALLBACK_CLEANUP"] = "true"

    md_path = Path(args.markdown_file).resolve()
    if not md_path.is_file():
        sys.exit(f"Error: Input Markdown file does not exist: {md_path}")

    out_path = Path(args.output).resolve() if args.output else None
    try:
        final_pdf = format_print_capture(md_path, output_pdf_path=out_path)
        print(f"✓ Journal-formatted PDF created at: {final_pdf}")
    except Exception as e:
        sys.exit(f"Error formatting print capture: {e}")


if __name__ == "__main__":
    main()
