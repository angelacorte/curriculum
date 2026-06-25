#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class BibEntry:
    key: str
    entry_type: str
    fields: OrderedDict[str, str]
    source: str

    @property
    def doi(self) -> str:
        return normalize_doi(self.fields.get("doi", ""))

    @property
    def title_key(self) -> str:
        return normalize_title(self.fields.get("title", ""))


SOURCE_PRIORITY = {
    "dblp": 0,
    "orcid-crossref": 1,
    "crossref": 2,
    "orcid": 3,
    "existing": -1,
}


def http_get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "curriculum-bib-updater/1.0 "
                          "(https://github.com/angelacorte/curriculum)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read().decode("utf-8")
        return json.loads(body)

def http_get_text(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "curriculum-bib-updater/1.0 "
                          "(https://github.com/angelacorte/curriculum)",
            "Accept": "text/plain, application/x-bibtex, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8")

def normalize_doi(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value)
    value = re.sub(r"^doi:\s*", "", value)
    return value.strip().rstrip(".")


def normalize_title(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[{}\\]", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def latex_escape(value: str) -> str:
    value = value.replace("&", r"\&")
    value = value.replace("%", r"\%")
    return value


def parse_bibtex_entries(content: str, source: str = "existing") -> list[BibEntry]:
    entries: list[BibEntry] = []
    pattern = re.compile(r"@(?P<type>\w+)\s*\{\s*(?P<key>[^,]+),(?P<body>.*?)\n\}", re.DOTALL)

    for match in pattern.finditer(content):
        entry_type = match.group("type").strip()
        key = match.group("key").strip()
        body = match.group("body")
        fields: OrderedDict[str, str] = OrderedDict()

        field_pattern = re.compile(
            r"(?P<name>[A-Za-z][A-Za-z0-9_-]*)\s*=\s*"
            r"(?P<value>\{(?:[^{}]|\{[^{}]*\})*\}|\"[^\"]*\")\s*,?",
            re.DOTALL,
        )

        for field_match in field_pattern.finditer(body):
            name = field_match.group("name").strip().lower()
            raw_value = field_match.group("value").strip()
            if raw_value.startswith("{") and raw_value.endswith("}"):
                value = raw_value[1:-1].strip()
            elif raw_value.startswith('"') and raw_value.endswith('"'):
                value = raw_value[1:-1].strip()
            else:
                value = raw_value.strip()
            fields[name] = value

        entries.append(BibEntry(key=key, entry_type=entry_type, fields=fields, source=source))

    return entries


def serialize_entry(entry: BibEntry) -> str:
    lines = [f"@{entry.entry_type}{{{entry.key},"]
    for name, value in entry.fields.items():
        lines.append(f"  {name} = {{{value}}},")
    lines.append("}")
    return "\n".join(lines)


def serialize_bibliography(entries: Iterable[BibEntry]) -> str:
    return "\n\n".join(serialize_entry(entry) for entry in entries) + "\n"


def make_key(entry: BibEntry) -> str:
    year = entry.fields.get("year", "unknown")
    first_author = "unknown"

    author = entry.fields.get("author", "")
    if author:
        first = author.split(" and ")[0].strip()
        parts = [p.strip() for p in re.split(r"\s+", first) if p.strip()]
        if parts:
            first_author = re.sub(r"[^A-Za-z0-9]", "", parts[-1]).lower() or "unknown"

    title_words = normalize_title(entry.fields.get("title", "")).split()
    title_part = "".join(title_words[:3]) or "publication"

    return f"{first_author}{year}{title_part}"


def unique_key(base_key: str, used: set[str]) -> str:
    key = base_key
    counter = 2
    while key in used:
        key = f"{base_key}-{counter}"
        counter += 1
    used.add(key)
    return key


def merge_entries(existing: BibEntry, incoming: BibEntry) -> BibEntry:
    fields = OrderedDict(existing.fields)

    for name, value in incoming.fields.items():
        if not value:
            continue
        if name not in fields or not fields[name].strip():
            fields[name] = value

    fields["annote"] = "pub"

    preferred_source = (
        incoming
        if SOURCE_PRIORITY.get(incoming.source, 99) < SOURCE_PRIORITY.get(existing.source, 99)
        else existing
    )

    return BibEntry(
        key=existing.key,
        entry_type=preferred_source.entry_type or existing.entry_type,
        fields=fields,
        source=existing.source,
    )


def dedupe_and_merge(existing_entries: list[BibEntry], incoming_entries: list[BibEntry]) -> list[BibEntry]:
    result: list[BibEntry] = []
    by_doi: dict[str, int] = {}
    by_title: dict[str, int] = {}
    used_keys = {entry.key for entry in existing_entries}

    for entry in existing_entries:
        index = len(result)
        result.append(entry)
        if entry.doi:
            by_doi[entry.doi] = index
        if entry.title_key:
            by_title[entry.title_key] = index

    for incoming in sorted(
            incoming_entries,
            key=lambda entry: SOURCE_PRIORITY.get(entry.source, 99),
    ):
        match_index = None

        if incoming.doi and incoming.doi in by_doi:
            match_index = by_doi[incoming.doi]
        elif incoming.title_key and incoming.title_key in by_title:
            match_index = by_title[incoming.title_key]

        if match_index is not None:
            merged = merge_entries(result[match_index], incoming)
            result[match_index] = merged
            if merged.doi:
                by_doi[merged.doi] = match_index
            if merged.title_key:
                by_title[merged.title_key] = match_index
            continue

        fields = OrderedDict(incoming.fields)
        fields["annote"] = "pub"

        base_key = incoming.key or make_key(incoming)
        key = unique_key(base_key, used_keys)

        new_entry = BibEntry(
            key=key,
            entry_type=incoming.entry_type,
            fields=fields,
            source=incoming.source,
        )

        index = len(result)
        result.append(new_entry)

        if new_entry.doi:
            by_doi[new_entry.doi] = index
        if new_entry.title_key:
            by_title[new_entry.title_key] = index

    return result


def fetch_dblp_entries(dblp_pid: str) -> list[BibEntry]:
    url = f"https://dblp.org/pid/{dblp_pid}.bib"
    try:
        content = http_get_text(url)
    except Exception as error:
        print(f"Warning: could not fetch DBLP entries: {error}", file=sys.stderr)
        return []

    entries = parse_bibtex_entries(content, source="dblp")
    normalized: list[BibEntry] = []

    for entry in entries:
        fields = OrderedDict(entry.fields)
        fields["annote"] = "pub"
        normalized.append(
            BibEntry(
                key=entry.key,
                entry_type=entry.entry_type,
                fields=fields,
                source="dblp",
            )
        )

    return normalized


def crossref_item_to_entry(item: dict, source: str) -> BibEntry | None:
    titles = item.get("title") or []
    if not titles:
        return None

    title = titles[0].strip()
    if not title:
        return None

    year = None
    issued = item.get("issued", {}).get("date-parts", [])
    if issued and issued[0]:
        year = str(issued[0][0])

    authors = []
    for author in item.get("author", []):
        given = author.get("given", "").strip()
        family = author.get("family", "").strip()
        if given and family:
            authors.append(f"{given} {family}")
        elif family:
            authors.append(family)

    container_titles = item.get("container-title") or []
    doi = normalize_doi(item.get("DOI", ""))
    url = item.get("URL", "")

    fields: OrderedDict[str, str] = OrderedDict()
    if authors:
        fields["author"] = " and ".join(latex_escape(author) for author in authors)
    fields["title"] = latex_escape(title)

    if container_titles:
        fields["booktitle"] = latex_escape(container_titles[0])

    if year:
        fields["year"] = year

    if doi:
        fields["doi"] = doi

    if url:
        fields["url"] = url

    fields["annote"] = "pub"

    entry_type = "inproceedings"
    crossref_type = item.get("type", "")
    if crossref_type in {"journal-article", "article"}:
        entry_type = "article"
    elif crossref_type in {"proceedings-article", "paper-conference"}:
        entry_type = "inproceedings"

    entry = BibEntry(
        key="",
        entry_type=entry_type,
        fields=fields,
        source=source,
    )

    return BibEntry(
        key=make_key(entry),
        entry_type=entry.entry_type,
        fields=entry.fields,
        source=source,
    )


def fetch_crossref_by_doi(doi: str, source: str = "orcid-crossref") -> BibEntry | None:
    if not doi:
        return None

    encoded = urllib.parse.quote(doi, safe="")
    url = f"https://api.crossref.org/works/{encoded}"

    try:
        data = http_get_json(url)
    except Exception as error:
        print(f"Warning: could not fetch Crossref DOI {doi}: {error}", file=sys.stderr)
        return None

    return crossref_item_to_entry(data.get("message", {}), source=source)


def author_matches(author: dict, expected_author: str) -> bool:
    expected = normalize_title(expected_author)
    given = author.get("given", "")
    family = author.get("family", "")

    forms = {
        normalize_title(f"{given} {family}"),
        normalize_title(f"{family} {given}"),
    }

    return expected in forms


def fetch_crossref_author_entries(author_name: str) -> list[BibEntry]:
    query = urllib.parse.urlencode(
        {
            "query.author": author_name,
            "rows": "50",
            "sort": "published",
            "order": "desc",
        }
    )
    url = f"https://api.crossref.org/works?{query}"

    try:
        data = http_get_json(url)
    except Exception as error:
        print(f"Warning: could not fetch Crossref author entries: {error}", file=sys.stderr)
        return []

    entries: list[BibEntry] = []

    for item in data.get("message", {}).get("items", []):
        authors = item.get("author", [])
        if not any(author_matches(author, author_name) for author in authors):
            continue

        entry = crossref_item_to_entry(item, source="crossref")
        if entry is not None:
            entries.append(entry)

    return entries


def fetch_orcid_entries(orcid: str) -> list[BibEntry]:
    url = f"https://pub.orcid.org/v3.0/{orcid}/works"

    try:
        data = http_get_json(url)
    except Exception as error:
        print(f"Warning: could not fetch ORCID entries: {error}", file=sys.stderr)
        return []

    entries: list[BibEntry] = []

    for group in data.get("group", []):
        summary = None
        for work_summary in group.get("work-summary", []):
            summary = work_summary
            break

        if not summary:
            continue

        doi = ""
        for external_id in summary.get("external-ids", {}).get("external-id", []):
            if external_id.get("external-id-type", "").lower() == "doi":
                doi = normalize_doi(external_id.get("external-id-value", ""))
                break

        if doi:
            crossref_entry = fetch_crossref_by_doi(doi, source="orcid-crossref")
            if crossref_entry is not None:
                entries.append(crossref_entry)
                continue

        title = (
            summary.get("title", {})
            .get("title", {})
            .get("value", "")
            .strip()
        )

        if not title:
            continue

        year = (
            summary.get("publication-date", {})
            .get("year", {})
            .get("value", "")
        )

        fields: OrderedDict[str, str] = OrderedDict()
        fields["title"] = latex_escape(title)
        if year:
            fields["year"] = year
        if doi:
            fields["doi"] = doi
        fields["annote"] = "pub"

        entry = BibEntry(
            key="",
            entry_type="misc",
            fields=fields,
            source="orcid",
        )

        entries.append(
            BibEntry(
                key=make_key(entry),
                entry_type=entry.entry_type,
                fields=entry.fields,
                source=entry.source,
            )
        )

    return entries


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update biblio.bib from DBLP, ORCID, and Crossref without duplicates."
    )
    parser.add_argument("--bib", required=True, help="Path to biblio.bib")
    parser.add_argument("--dblp-pid", required=True, help="DBLP author PID, for example 392/6452")
    parser.add_argument("--orcid", required=True, help="ORCID identifier")
    parser.add_argument("--author", required=True, help="Author name for Crossref matching")
    args = parser.parse_args()

    bib_path = Path(args.bib)

    if not bib_path.exists():
        print(f"Error: {bib_path} does not exist", file=sys.stderr)
        return 1

    original_content = bib_path.read_text(encoding="utf-8")
    existing_entries = parse_bibtex_entries(original_content, source="existing")

    incoming_entries: list[BibEntry] = []
    incoming_entries.extend(fetch_dblp_entries(args.dblp_pid))
    incoming_entries.extend(fetch_orcid_entries(args.orcid))
    incoming_entries.extend(fetch_crossref_author_entries(args.author))

    updated_entries = dedupe_and_merge(existing_entries, incoming_entries)
    updated_content = serialize_bibliography(updated_entries)

    if updated_content != original_content:
        bib_path.write_text(updated_content, encoding="utf-8")
        print(f"Updated {bib_path}")
    else:
        print(f"No changes needed for {bib_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())