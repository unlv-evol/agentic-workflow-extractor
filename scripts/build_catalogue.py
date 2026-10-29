#!/usr/bin/env python3
"""Regenerate the agent catalogue from the GitHub Actions Marketplace.

The first catalogue was written by hand from vendor documentation. That does
not scale and does not stay true: three of its nine entries turned out to name
repositories that no longer exist, so those patterns could never match, and it
missed the long tail of third-party agent actions entirely.

The Marketplace publishes an ``ai-assisted`` category. Enumerating it and
resolving each listing to its source repository yields a catalogue that is
reproducible, dated, and an order of magnitude broader. Actions whose source
repository belongs to a known agent vendor are grouped under that agent;
everything else lands in ``generic_ai``, mirroring the way an AGENTS.md-style
"generic" group is handled in context-file datasets.

    python3 scripts/build_catalogue.py -o config/marketplace.toml

Network-bound and deliberately polite: expect several minutes for ~1,200
listings. Re-run to refresh; commit the output so a dataset build is
reproducible without network access.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date

CATEGORY_URL = (
    "https://github.com/marketplace?type=actions&category=ai-assisted&page={page}"
)
LISTING_URL = "https://github.com/marketplace/actions/{slug}"
USER_AGENT = "awe-catalogue-builder"

_SLUG_RE = re.compile(r'href="/marketplace/actions/([^"?#]+)"')
_REPO_RE = re.compile(r'href="/([A-Za-z0-9][\w.-]*)/([A-Za-z0-9][\w.-]*)"')

# Repositories that are not agent actions even though GitHub files them under
# `ai-assisted`, or that appear on every listing page as chrome.
_REPO_DENYLIST = {
    "features", "marketplace", "pricing", "enterprise", "security",
    "customer-stories", "readme", "sponsors", "collections", "topics",
    "trending", "events", "about", "site", "git-guides", "solutions",
}

# Owner or repository-name fragments that identify an agent. Order matters:
# the first match wins, so put specific vendors before generic substrings.
_AGENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("claude", ("anthropics/", "claude-code", "anthropic")),
    ("codex", ("openai/codex", "codex-action", "codex-cli")),
    ("copilot", ("github/copilot", "copilot-cli", "actions/ai-inference")),
    ("gemini", ("google-github-actions/run-gemini", "gemini-cli", "google-gemini/")),
    ("cursor", ("cursor/", "cursor-agent")),
    ("aider", ("aider-ai/", "aider-chat", "paul-gauthier/aider")),
    ("devin", ("cognition-ai/", "cognitionai/", "devin-")),
    ("opencode", ("sst/opencode", "opencode-")),
    ("goose", ("block/goose", "goose-ai")),
    ("openhands", ("all-hands-ai/", "openhands")),
    ("qodo", ("qodo-ai/", "codium-ai/", "pr-agent")),
    ("coderabbit", ("coderabbitai/",)),
    ("sweep", ("sweepai/",)),
)


def fetch(url: str, retries: int = 3) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == retries - 1:
                return ""
            time.sleep(2 * (attempt + 1))
    return ""


def enumerate_slugs(max_pages: int = 200, verbose: bool = True) -> list[str]:
    """Walk the category until it is genuinely exhausted.

    A transient fetch failure yields an empty page, which looks exactly like
    the end of the category. Stopping on the first such page silently
    truncates the catalogue -- it cost a third of the listings on the first
    run. So an empty *response* is retried, and only pages that parse but add
    nothing count towards termination, two in a row before stopping.
    """
    slugs: dict[str, None] = {}
    barren = 0
    for page in range(1, max_pages + 1):
        html = fetch(CATEGORY_URL.format(page=page))
        if not html:
            time.sleep(3)
            html = fetch(CATEGORY_URL.format(page=page))
        if not html:
            if verbose:
                print(f"  page {page:3}: fetch failed, skipping", file=sys.stderr)
            barren += 1
            if barren >= 2:
                break
            continue

        fresh = [s for s in set(_SLUG_RE.findall(html)) if s not in slugs]
        if not fresh:
            barren += 1
            if barren >= 2:
                break
            continue
        barren = 0
        for slug in fresh:
            slugs[slug] = None
        if verbose:
            print(f"  page {page:3}: +{len(fresh):3} (total {len(slugs)})", file=sys.stderr)
    return list(slugs)


def resolve_repository(slug: str) -> str | None:
    """Map a Marketplace listing to the owner/name that publishes it."""
    html = fetch(LISTING_URL.format(slug=slug))
    if not html:
        return None
    for owner, name in _REPO_RE.findall(html):
        if owner.lower() in _REPO_DENYLIST or name.lower() in _REPO_DENYLIST:
            continue
        if owner.startswith(("marketplace", "orgs", "apps", "sponsors")):
            continue
        return f"{owner}/{name}"
    return None


def classify(repository: str) -> str:
    lowered = repository.lower()
    for agent, fragments in _AGENT_RULES:
        if any(fragment in lowered for fragment in fragments):
            return agent
    return "generic_ai"


def render(groups: dict[str, list[str]], n_listings: int) -> str:
    lines = [
        "# awe -- agent catalogue derived from the GitHub Actions Marketplace",
        "#",
        f"# Generated by scripts/build_catalogue.py on {date.today().isoformat()}",
        f"# from the `ai-assisted` category: {n_listings} listings resolved to",
        f"# {sum(len(v) for v in groups.values())} distinct action repositories.",
        "#",
        "# Do not hand-edit. Vendor-specific grouping lives in _AGENT_RULES in",
        "# the generator; everything unmatched is grouped as `generic_ai`.",
        "",
    ]
    for agent in sorted(groups, key=lambda a: (a == "generic_ai", a)):
        repositories = sorted(set(groups[agent]))
        if not repositories:
            continue
        lines.append(f"[agents.{agent}]")
        lines.append(f'display_name = "{agent}"')
        lines.append("uses = [")
        lines += [f'    "{repository}",' for repository in repositories]
        lines.append("]")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--out", required=True, help="TOML file to write")
    parser.add_argument("-j", "--jobs", type=int, default=8, help="parallel fetches")
    parser.add_argument("--limit", type=int, help="stop after N listings (for testing)")
    args = parser.parse_args()

    print("enumerating ai-assisted category ...", file=sys.stderr)
    slugs = enumerate_slugs()
    if args.limit:
        slugs = slugs[: args.limit]
    print(f"{len(slugs)} listings; resolving source repositories ...", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        resolved = list(pool.map(resolve_repository, slugs))

    groups: dict[str, list[str]] = {}
    unresolved = 0
    for repository in resolved:
        if not repository:
            unresolved += 1
            continue
        groups.setdefault(classify(repository), []).append(repository)

    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(render(groups, len(slugs)))

    total = sum(len(set(v)) for v in groups.values())
    print(f"\nwrote {args.out}: {total} actions, {unresolved} unresolved", file=sys.stderr)
    for agent in sorted(groups, key=lambda a: -len(set(groups[a]))):
        print(f"  {len(set(groups[agent])):5}  {agent}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
