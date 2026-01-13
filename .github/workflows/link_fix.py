from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

repo = os.getenv("GITHUB_REPOSITORY")  # e.g., "fs-ise/handbook"
sha = os.getenv("GITHUB_SHA")  # commit being checked
server = os.getenv("GITHUB_SERVER_URL", "https://github.com")

# Match inline markdown http(s) links (but NOT images):
#   [text](http...)
# optionally followed by an attribute block:
#   { ... }
#
# The (?<!\!) prevents matching image syntax: ![alt](...)
MARKDOWN_HTTP_LINK_PATTERN = re.compile(
    r"(?<!\!)\[(?P<text>[^\]]+)\]\((?P<url>http[^\)]+)\)(?P<attrs>\{[^}]*\})?"
)

# Match ANY inline markdown link (but NOT images):
#   [text](dest)
MARKDOWN_LINK_PATTERN = re.compile(r"(?<!\!)\[(?P<text>[^\]]+)\]\((?P<dest>[^)]+)\)")

SKIP_URL_SUBSTRINGS = ("img.shields.io",)

# Treat these as "assets", not pages (skip in internal-link checking;
# also don't add target=_blank to external asset URLs).
ASSET_SUFFIXES = (
    ".png",
    ".svg",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
)


def iter_content_files(root: Path) -> Iterable[Path]:
    """Yield .md and .qmd files (skip root-level files)."""
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix not in {".md", ".qmd"}:
            continue
        if len(p.relative_to(root).parts) == 1:
            continue
        yield p


def strip_fragment_and_query(url: str) -> str:
    """Remove #fragment and ?query for suffix checks, keeping the path-ish portion."""
    return url.split("#", 1)[0].split("?", 1)[0].strip()


def is_asset_link(url: str) -> bool:
    """
    True if the link target looks like a static asset.
    Handles query strings like foo.png?raw=1 by stripping ?...
    """
    clean = strip_fragment_and_query(url).lower()
    return clean.endswith(ASSET_SUFFIXES)


def should_skip_external(url: str) -> bool:
    # External "skip": shields + assets
    return any(s in url for s in SKIP_URL_SUBSTRINGS) or is_asset_link(url)


def strip_chatgpt_utm(url: str) -> str:
    """
    Remove utm_source=chatgpt.com from URLs while preserving all other query params.
    Works for absolute URLs and relative links alike.
    """
    if "utm_source=chatgpt.com" not in url:
        return url

    parts = urlsplit(url)
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)

    filtered = [
        (k, v)
        for (k, v) in query_pairs
        if not (k == "utm_source" and v == "chatgpt.com")
    ]

    new_query = urlencode(filtered, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def attrs_already_have_target_blank(attrs: str) -> bool:
    """
    True if the attribute block already sets target blank in either form:
      - target="_blank"
      - target=_blank
    """
    return bool(re.search(r'target\s*=\s*("_blank"|_blank)', attrs))


def normalize_to_brace_attrs(existing_attrs: str) -> str:
    """
    Convert an attribute block to the plain brace form:
      "{ ... }"
    If it's "{: ... }", drop the leading ":" while keeping the content.
    """
    inner = existing_attrs.strip()[1:-1].strip()  # remove outer braces
    if inner.startswith(":"):
        inner = inner[1:].strip()
    return inner


def append_target_blank_to_http_links(file_path: Path) -> bool:
    """Add {target=_blank} to http(s) links unless already present (any form)."""
    content = file_path.read_text(encoding="utf-8")

    def add_target_blank(match: re.Match) -> str:
        text = match.group("text")
        url = match.group("url")
        existing_attrs = match.group("attrs")  # {..} or {: ..} or None

        url = strip_chatgpt_utm(url)

        # Rebuild the [text](url) part with the cleaned URL
        link = f"[{text}]({url})"

        if should_skip_external(url):
            # Keep attrs if any, but do not add target=_blank
            return link + (existing_attrs or "")

        if existing_attrs:
            if attrs_already_have_target_blank(existing_attrs):
                # Keep exactly as-is if it already has target blank
                return link + existing_attrs

            inner = normalize_to_brace_attrs(existing_attrs)
            if inner:
                return link + "{ " + inner + " target=_blank }"
            return link + "{ target=_blank }"

        return link + "{target=_blank}"

    updated = MARKDOWN_HTTP_LINK_PATTERN.sub(add_target_blank, content)

    if updated != content:
        file_path.write_text(updated, encoding="utf-8")
        print(f"Updated external links in {file_path}")
        return True

    print(f"No changes needed in {file_path}")
    return False


def candidates_for_quarto_source(file_path: Path, link: str, repo_root: Path) -> list[Path]:
    """
    Given an internal link target, return plausible Quarto source candidates.

    Supports:
    - pretty URLs (no extension): foo/bar/baz
    - directory pretty URLs:      foo/bar/baz/
    - explicit HTML output:       foo/bar/baz.html
    - relative links and site-root links (/foo/bar)

    For non-directory links, we check:
      - target.qmd / target.md
      - target/index.qmd / target/index.md
    For directory links (ending with '/'), we check:
      - target/index.qmd / target/index.md
    """
    clean = link.split("#", 1)[0].split("?", 1)[0].strip()

    # Strip .html if present (old behavior)
    if clean.endswith(".html"):
        clean = clean[:-5]

    # Detect directory link (pretty URL with trailing slash)
    is_dir = clean.endswith("/")
    if is_dir:
        clean = clean.rstrip("/")

    # Resolve Quarto site-root paths
    if clean.startswith("/"):
        clean = clean.lstrip("/")
        base = repo_root / clean
    else:
        base = file_path.parent / clean

    if is_dir:
        return [
            base / "index.qmd",
            base / "index.md",
        ]

    return [
        Path(str(base) + ".qmd"),
        Path(str(base) + ".md"),
        base / "index.qmd",
        base / "index.md",
    ]


def is_templated_link(dest: str) -> bool:
    d = dest.strip()
    return d.startswith(("{{<", "{{%"))


def check_internal_links(file_path: Path, repo_root: Path) -> list[tuple[str, list[Path]]]:
    """
    For each internal markdown link target, verify at least one plausible source exists.
    Returns list of (link, candidates) for broken ones.
    """
    content = file_path.read_text(encoding="utf-8")
    broken: list[tuple[str, list[Path]]] = []

    for m in MARKDOWN_LINK_PATTERN.finditer(content):
        link = strip_chatgpt_utm(m.group("dest").strip())

        if is_templated_link(link):
            continue

        # Skip external and non-file links
        if link.startswith(("http://", "https://", "mailto:", "#", "tel:")):
            continue
        if link in ["{nc[k]}"]:
            continue

        # Skip assets (png/jpg/etc.), shields, etc.
        if any(s in link for s in SKIP_URL_SUBSTRINGS):
            continue
        if is_asset_link(link):
            continue

        # Keep your existing exception
        if "_news" in link:
            continue

        cands = candidates_for_quarto_source(file_path, link, repo_root=repo_root)
        if not any(p.exists() for p in cands):
            broken.append((link, cands))

    return broken


def write_broken_links_report(
    broken: dict[Path, list[tuple[str, list[Path]]]],
    repo_root: Path,
) -> None:
    report_path = Path("broken_links.md")
    if not broken:
        if report_path.exists():
            report_path.unlink()
        print("No broken internal links found.")
        return

    with report_path.open("w", encoding="utf-8") as f:
        for src_file, items in sorted(broken.items(), key=lambda x: str(x[0])):
            rel = src_file.relative_to(repo_root).as_posix()

            if repo:
                # Direct link to GitHub's editor
                edit_url = f"{server}/{repo}/edit/main/{rel}"
                f.write(f"## In [{rel}]({edit_url})\n\n")
            else:
                f.write(f"## In `{rel}`\n\n")

            f.write("The following links are broken:")
            for link, _cands in items:
                f.write(f"\n```sh\n{link}\n```\n")
            f.write("\n")


def sort_lycheeignore_file(path: Path) -> bool:
    """
    Sort a lychee ignore file alphabetically (removing duplicates).

    - Keeps comment lines (starting with '#') in their original order, at the top.
    - Keeps a single blank line between comments and the sorted block (if comments exist).
    - Dedupes and sorts non-empty, non-comment lines case-insensitively.
    """
    if not path.exists():
        return False

    original = path.read_text(encoding="utf-8").splitlines()

    comments: list[str] = []
    entries: list[str] = []

    for line in original:
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            comments.append(line.rstrip())
        else:
            entries.append(s)

    # Dedupe while preserving first-seen casing
    seen_lower: set[str] = set()
    deduped: list[str] = []
    for e in entries:
        key = e.lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        deduped.append(e)

    deduped_sorted = sorted(deduped, key=lambda x: x.lower())

    out_lines: list[str] = []
    if comments:
        out_lines.extend(comments)
        out_lines.append("")  # separator

    out_lines.extend(deduped_sorted)
    out_text = "\n".join(out_lines).rstrip() + "\n"

    before = path.read_text(encoding="utf-8")
    if before != out_text:
        path.write_text(out_text, encoding="utf-8")
        print(f"Sorted {path}")
        return True

    print(f"No changes needed in {path}")
    return False


def main() -> None:
    root = Path.cwd()

    # 1) Add {target=_blank} to external links in .md/.qmd (excluding root-level files)
    #    and remove ?utm_source=chatgpt.com from those URLs
    for fp in iter_content_files(root):
        append_target_blank_to_http_links(fp)

    # 2) Check internal links (pretty URLs, .html, etc.) against plausible Quarto sources
    #    (and ignore/remove utm_source=chatgpt.com when evaluating)
    broken: dict[Path, list[tuple[str, list[Path]]]] = {}
    for fp in iter_content_files(root):
        b = check_internal_links(fp, repo_root=root)
        if b:
            broken[fp] = b

    write_broken_links_report(broken, repo_root=root)

    # 3) Sort lychee ignore file alphabetically (remove duplicates)
    #    Lychee typically uses ".lycheeignore", but we handle "lycheeignore" too.
    sort_lycheeignore_file(root / ".lycheeignore")
    sort_lycheeignore_file(root / "lycheeignore")


if __name__ == "__main__":
    main()
