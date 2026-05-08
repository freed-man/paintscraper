"""
Merge any number of paint colour databases into a single output.

Each source is defined by:
    name      — short identifier (e.g. 'atu', 'chipex')
    path      — JSON file path
    has_year  — does the source have reliable year info per record?

Year-aware sources are processed first using strict (year, mfr, model) grouping
and code-overlap matching. Year-blind sources are added after, only contributing
records whose codes don't already exist for the same (mfr, model).

Strategy:
    Step 1: Build records from year-aware sources with strong-match merging
            (≥2 shared codes OR full subset — prevents over-merge of distinct
            paints sharing one alternate code)
    Step 2: Master hex table from ALL sources, with format validation
    Step 3: Master name table from ALL sources, deduplicated
    Step 4: Apply hex and names to merged records
    Step 5: Add year-blind source records that don't already exist
    Step 6: Tag year-blind sources on existing records that match by code
    Step 7: Final dedup, classification, sort, drop over-merged records

Usage (legacy 3-source mode):
    python merge_databases.py atu.json chipex.json colorndrive.json output.json

    Equivalent to:
        --source atu=atu.json:year
        --source chipex=chipex.json:year
        --source colorndrive=colorndrive.json:noyear

Usage (any number of sources):
    python merge_databases.py output.json \\
        --source atu=atu.json:year \\
        --source chipex=chipex.json:year \\
        --source colorndrive=colorndrive.json:noyear \\
        --source vernicispray=vernicispray.json:year \\
        --source paintman=paintman.json:noyear
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict


# -- Normalization ----------------------------------------------------------

def normalize_model(text):
    """'Id.3', 'Id 3', 'Id-3' all become 'id3'."""
    return text.strip().lower().replace(".", "").replace("-", "").replace(" ", "")


def normalize_manufacturer(text):
    """'Mercedes-Benz' → 'mercedesbenz', 'Volkswagen' → 'volkswagen'."""
    return text.strip().lower().replace("-", "").replace(" ", "").replace(".", "")


# -- Color classification --------------------------------------------------

COLOR_KEYWORDS = {
    "white":  ["white", "weiss", "weis", "bianco", "branco", "blanco",
               "blanc", "candy"],
    "black":  ["black", "schwarz", "nero", "preto", "negro", "noir"],
    "grey":   ["grey", "gray", "grau", "grigio", "cinza", "gris",
               "silver", "silber", "platinum", "graphite", "graphit"],
    "blue":   ["blue", "blau", "blu", "azul", "bleu", "turquoise",
               "turkis", "türkis", "tuerkis", "tanzanite"],
    "red":    ["red", "rot", "rosso", "vermelho", "rojo", "rouge",
               "infrared"],
    "green":  ["green", "gruen", "grün", "verde", "vert", "olivine",
               "olive"],
    "yellow": ["yellow", "gelb", "giallo", "amarelo", "amarillo", "jaune"],
    "orange": ["orange", "arancio", "laranja", "naranja", "sunset"],
    "brown":  ["brown", "braun", "marrone", "marrom", "sepia",
               "havanna", "mocca"],
    "purple": ["purple", "violet", "lila", "violett", "roxo"],
    "beige":  ["beige", "champagne", "cashmere", "sand", "ivory"],
    "gold":   ["gold"],
}

# Used for over-merge detection — broader than COLOR_KEYWORDS so we don't
# count "silver" and "grey" as different categories
COLOR_CATEGORIES = {
    "white":  ["white", "weiss", "weis", "bianco", "branco", "blanco", "blanc"],
    "black":  ["black", "schwarz", "nero", "preto", "negro", "noir"],
    "grey":   ["grey", "gray", "grau", "grigio", "cinza", "gris",
               "silver", "silber", "platinum", "graphite"],
    "blue":   ["blue", "blau", "blu", "azul", "bleu"],
    "red":    ["red", "rot", "rosso", "vermelho", "rojo", "rouge"],
    "green":  ["green", "gruen", "grün", "verde", "vert"],
    "yellow": ["yellow", "gelb", "giallo", "amarelo", "amarillo", "jaune"],
    "orange": ["orange", "arancio", "laranja", "naranja"],
    "brown":  ["brown", "braun", "marrone", "marrom", "sepia",
               "havanna", "mocca", "mocha"],
    "purple": ["purple", "violet", "lila", "violett", "roxo"],
    "beige":  ["beige", "champagne", "sand", "ivory"],
    "gold":   ["gold"],
}


def classify_color(color_name):
    name_lower = color_name.lower()
    for group, keywords in COLOR_KEYWORDS.items():
        if any(kw in name_lower for kw in keywords):
            return group
    return "other"


def count_distinct_color_categories(color_name):
    """Count distinct basic colour categories in a name. ≥3 = over-merged."""
    name_lower = color_name.lower()
    found = set()
    for category, keywords in COLOR_CATEGORIES.items():
        if any(kw in name_lower for kw in keywords):
            found.add(category)
    return len(found)


# -- Hex validation ---------------------------------------------------------

HEX_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")


def is_valid_hex(hex_value):
    return bool(hex_value and HEX_PATTERN.match(hex_value))


def normalize_hex(hex_value):
    """Return uppercase canonical hex, or empty string if invalid."""
    if not hex_value:
        return ""
    h = hex_value.strip()
    if not h.startswith("#"):
        h = "#" + h
    if not is_valid_hex(h):
        return ""
    return h.upper()


# -- Code helpers -----------------------------------------------------------

def is_reliable_code(code, all_codes_in_record=None):
    """Filter dealer shorthand and substring duplicates."""
    if len(code) <= 2:
        return False
    if len(code) == 4 and code[:2] == code[2:]:
        return False
    if all_codes_in_record:
        hyphenated = [c for c in all_codes_in_record if "-" in c]
        for hcode in hyphenated:
            if code != hcode and code in hcode:
                return False
    return True


def parse_all_codes(code_string):
    return {c.strip().upper() for c in code_string.split(",") if c.strip()}


def parse_reliable_codes(code_string):
    all_codes = [c.strip().upper() for c in code_string.split(",") if c.strip()]
    return {c for c in all_codes if is_reliable_code(c, all_codes)}


def merge_code_strings(codes_a, codes_b):
    seen = set()
    merged = []
    for code_str in [codes_a, codes_b]:
        for code in code_str.split(","):
            code = code.strip()
            if code and code.upper() not in seen:
                seen.add(code.upper())
                merged.append(code)
    return ", ".join(merged)


def is_strong_match(codes_a, codes_b):
    """≥2 codes overlap OR full subset — prevents over-merge."""
    if not codes_a or not codes_b:
        return False
    overlap = codes_a & codes_b
    if len(overlap) >= 2:
        return True
    if codes_a.issubset(codes_b) or codes_b.issubset(codes_a):
        return True
    return False


# -- Name helpers -----------------------------------------------------------

def titlecase_if_upper(name):
    return name.title() if name.isupper() else name


def parse_names(name_string):
    return [titlecase_if_upper(n.strip()) for n in name_string.split(",")
            if n.strip()]


def merge_name_lists(names_a, names_b):
    seen = set()
    merged = []
    for name in names_a + names_b:
        if name.upper() not in seen:
            seen.add(name.upper())
            merged.append(name)
    return merged


def clean_name_string(name_string):
    if "<" in name_string:
        name_string = re.sub(r"<[^>]*>.*", "", name_string)
        name_string = re.sub(r"[\"']?\)\s*$", "", name_string)
    name_string = (name_string
                   .replace("\\\\'", "'")
                   .replace("\\'", "'")
                   .replace("\\\\", "")
                   .replace("''", "'")
                   .replace("  ", " "))
    parts = [p.strip().rstrip(",") for p in name_string.split(",")]
    parts = [p for p in parts if len(p) > 1]
    return ", ".join(parts)


# -- Source spec parsing ----------------------------------------------------

def parse_source_arg(spec):
    """
    Parse a --source flag like 'atu=atu.json:year' into (name, path, has_year).
    """
    if "=" not in spec:
        raise ValueError(
            f"Invalid --source value '{spec}'. "
            f"Expected format: name=path[:year|:noyear]"
        )
    name, rest = spec.split("=", 1)
    name = name.strip().lower()
    if ":" in rest:
        path, year_flag = rest.rsplit(":", 1)
        year_flag = year_flag.strip().lower()
        if year_flag == "year":
            has_year = True
        elif year_flag == "noyear":
            has_year = False
        else:
            raise ValueError(
                f"Invalid year flag '{year_flag}' in '{spec}'. "
                f"Use ':year' or ':noyear'."
            )
    else:
        path = rest
        has_year = True  # Default
    return (name, path.strip(), has_year)


# -- Load -------------------------------------------------------------------

def load_json(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


# -- Main merge logic -------------------------------------------------------

def merge_databases(sources, output_file):
    """
    Merge multiple paint databases.

    sources: list of (name, path, has_year) tuples in priority order
    output_file: path for the merged JSON

    The first year-aware source is the base; subsequent year-aware sources
    merge in via strong-match. Year-blind sources are appended after.
    """

    print(f"Loading {len(sources)} source(s)...")
    loaded = []
    for name, path, has_year in sources:
        data = load_json(path)
        loaded.append((name, has_year, data))
        year_label = "year-aware" if has_year else "year-blind"
        print(f"  {name:18s} ({year_label}) {len(data):>8,} records — {path}")

    year_aware = [(n, d) for (n, hy, d) in loaded if hy]
    year_blind = [(n, d) for (n, hy, d) in loaded if not hy]

    if not year_aware and not year_blind:
        print("\nNo sources provided. Nothing to do.")
        return

    # ── Step 1: Year-aware sources ─────────────────────────────────────

    merged = []
    group_index = defaultdict(list)
    model_names = {}

    if not year_aware:
        print("\n── Step 1: SKIPPED (no year-aware sources) ──")
    else:
        primary_name, primary_data = year_aware[0]
        for record in primary_data:
            idx = len(merged)
            entry = {
                "year": record["year"],
                "manufacturer": record["manufacturer"],
                "model": record["model"],
                "color_name": record["color_name"],
                "color_codes": record["color_codes"],
                "color_hex": normalize_hex(record.get("color_hex", "")),
                "sources": [primary_name],
            }
            merged.append(entry)

            norm_mfr = normalize_manufacturer(record["manufacturer"])
            norm_model = normalize_model(record["model"])
            group = (record["year"], norm_mfr, norm_model)
            model_names.setdefault(group, record["model"])

            reliable = parse_reliable_codes(record["color_codes"])
            group_index[group].append((idx, reliable))

        print(f"\n── Step 1: Year-aware sources ──")
        print(f"  {primary_name}: {len(primary_data):,} records loaded as base")

        for source_name, source_data in year_aware[1:]:
            new_count = 0
            merged_count = 0

            for record in source_data:
                norm_mfr = normalize_manufacturer(record["manufacturer"])
                norm_model = normalize_model(record["model"])
                group = (record["year"], norm_mfr, norm_model)
                reliable = parse_reliable_codes(record["color_codes"])

                match_idx = None
                for existing_idx, existing_codes in group_index[group]:
                    if is_strong_match(reliable, existing_codes):
                        match_idx = existing_idx
                        break

                if match_idx is not None:
                    existing = merged[match_idx]
                    existing["color_codes"] = merge_code_strings(
                        existing["color_codes"], record["color_codes"])
                    existing["color_name"] = ", ".join(merge_name_lists(
                        parse_names(existing["color_name"]),
                        parse_names(record["color_name"])))
                    new_hex = normalize_hex(record.get("color_hex", ""))
                    if new_hex and not existing["color_hex"]:
                        existing["color_hex"] = new_hex
                    if source_name not in existing["sources"]:
                        existing["sources"].append(source_name)
                    new_codes = parse_reliable_codes(existing["color_codes"])
                    for i, (eid, _) in enumerate(group_index[group]):
                        if eid == match_idx:
                            group_index[group][i] = (eid, new_codes)
                            break
                    merged_count += 1
                else:
                    idx = len(merged)
                    entry = {
                        "year": record["year"],
                        "manufacturer": record["manufacturer"],
                        "model": record["model"],
                        "color_name": ", ".join(parse_names(record["color_name"])),
                        "color_codes": record["color_codes"],
                        "color_hex": normalize_hex(record.get("color_hex", "")),
                        "sources": [source_name],
                    }
                    if group in model_names:
                        entry["model"] = model_names[group]
                    merged.append(entry)
                    group_index[group].append((idx, reliable))
                    new_count += 1

            print(f"  {source_name}: matched {merged_count:,}, new {new_count:,}")

        print(f"  Records after step 1: {len(merged):,}")

    # ── Step 2: Master hex table (all sources contribute) ──────────────

    hex_votes = defaultdict(list)
    invalid_hex_count = 0

    for source_name, _, source_data in loaded:
        for r in source_data:
            norm_hex = normalize_hex(r.get("color_hex", ""))
            if not norm_hex:
                if r.get("color_hex"):
                    invalid_hex_count += 1
                continue
            mfr = normalize_manufacturer(r["manufacturer"])
            for code in parse_reliable_codes(r["color_codes"]):
                hex_votes[(mfr, code)].append(norm_hex)

    master_hex = {
        key: Counter(votes).most_common(1)[0][0]
        for key, votes in hex_votes.items()
    }

    print(f"\n── Step 2: Master hex table ──")
    print(f"  Entries: {len(master_hex):,}")
    print(f"  Invalid hex values dropped: {invalid_hex_count:,}")

    # ── Step 3: Master name table (all sources contribute) ─────────────

    master_names = defaultdict(list)
    for _, _, source_data in loaded:
        for r in source_data:
            mfr = normalize_manufacturer(r["manufacturer"])
            names = parse_names(r.get("color_name", ""))
            for code in parse_reliable_codes(r.get("color_codes", "")):
                master_names[(mfr, code)].extend(names)

    for key in master_names:
        seen = set()
        deduped = []
        for name in master_names[key]:
            if name.upper() not in seen:
                seen.add(name.upper())
                deduped.append(name)
        master_names[key] = deduped

    print(f"\n── Step 3: Master name table ──")
    print(f"  Entries: {len(master_names):,}")

    # ── Step 4: Apply hex and names to year-aware records ──────────────

    hex_filled = 0
    names_enriched = 0

    for record in merged:
        mfr = normalize_manufacturer(record["manufacturer"])
        reliable = parse_reliable_codes(record["color_codes"])

        if not record["color_hex"]:
            for code in reliable:
                if (mfr, code) in master_hex:
                    record["color_hex"] = master_hex[(mfr, code)]
                    hex_filled += 1
                    break

        current_names = parse_names(record["color_name"])
        all_names = list(current_names)
        for code in reliable:
            if (mfr, code) in master_names:
                all_names = merge_name_lists(all_names, master_names[(mfr, code)])

        if len(all_names) > len(current_names):
            names_enriched += 1
        record["color_name"] = ", ".join(all_names)

    print(f"\n── Step 4: Apply hex and names ──")
    print(f"  Records given hex:      {hex_filled:,}")
    print(f"  Records enriched names: {names_enriched:,}")

    # ── Step 5: Add year-blind source records ──────────────────────────

    existing_keys = set()
    for record in merged:
        mfr = normalize_manufacturer(record["manufacturer"])
        model = normalize_model(record["model"])
        for code in parse_reliable_codes(record["color_codes"]):
            existing_keys.add((mfr, model, code))

    print(f"\n── Step 5: Year-blind sources ──")
    if not year_blind:
        print(f"  (no year-blind sources)")
    else:
        for source_name, source_data in year_blind:
            new_count = 0
            skipped_empty = 0
            skipped_dupe = 0
            seen_in_source = set()

            for r in source_data:
                codes_raw = r.get("color_codes", "").strip()
                if not codes_raw:
                    skipped_empty += 1
                    continue

                mfr = normalize_manufacturer(r["manufacturer"])
                model = normalize_model(r["model"])
                reliable = parse_reliable_codes(codes_raw)

                if not reliable:
                    skipped_empty += 1
                    continue

                source_key = (mfr, model, frozenset(reliable))
                if source_key in seen_in_source:
                    skipped_dupe += 1
                    continue
                seen_in_source.add(source_key)

                already_exists = any(
                    (mfr, model, code) in existing_keys for code in reliable)
                if already_exists:
                    continue

                hex_val = normalize_hex(r.get("color_hex", ""))
                if not hex_val:
                    for code in reliable:
                        if (mfr, code) in master_hex:
                            hex_val = master_hex[(mfr, code)]
                            break

                names = parse_names(r.get("color_name", ""))
                for code in reliable:
                    if (mfr, code) in master_names:
                        names = merge_name_lists(names, master_names[(mfr, code)])

                entry = {
                    "year": None,
                    "manufacturer": r["manufacturer"],
                    "model": r["model"],
                    "color_name": ", ".join(names),
                    "color_codes": codes_raw,
                    "color_hex": hex_val,
                    "sources": [source_name],
                }
                merged.append(entry)
                for code in reliable:
                    existing_keys.add((mfr, model, code))
                new_count += 1

            print(f"  {source_name}: new {new_count:,}, "
                  f"skipped empty {skipped_empty:,}, "
                  f"dupes {skipped_dupe:,}")

    # ── Step 6: Tag year-blind sources on existing records ─────────────

    if year_blind:
        # Build an index from (mfr, model, code) → record once,
        # so each CND record can find its match in O(1) instead of O(N).
        # A single record can be reachable via multiple codes, but we only
        # need to tag it once; storing the record reference is fine.
        record_by_code_key = {}
        for record in merged:
            mfr = normalize_manufacturer(record["manufacturer"])
            model = normalize_model(record["model"])
            for code in parse_reliable_codes(record["color_codes"]):
                # First record wins for a given key (matches old "break" behaviour)
                key = (mfr, model, code)
                if key not in record_by_code_key:
                    record_by_code_key[key] = record

        for source_name, source_data in year_blind:
            tagged = 0
            for r in source_data:
                codes_raw = r.get("color_codes", "").strip()
                if not codes_raw:
                    continue
                mfr = normalize_manufacturer(r["manufacturer"])
                model = normalize_model(r["model"])
                reliable = parse_reliable_codes(codes_raw)
                if not reliable:
                    continue
                # Look for ANY matching code; tag the first hit
                for code in reliable:
                    record = record_by_code_key.get((mfr, model, code))
                    if record is not None:
                        if source_name not in record["sources"]:
                            record["sources"].append(source_name)
                            tagged += 1
                        break
            print(f"  Step 6: tagged {tagged:,} existing records with '{source_name}'")

    # ── Step 7: Classify, clean, dedupe ────────────────────────────────

    seen_records = set()
    unique = []
    dupes_removed = 0
    over_merged_dropped = 0

    for record in merged:
        record["color_name"] = clean_name_string(record["color_name"])

        if count_distinct_color_categories(record["color_name"]) >= 3:
            over_merged_dropped += 1
            continue

        record["normalized_manufacturer"] = normalize_manufacturer(
            record["manufacturer"])
        record["normalized_model"] = normalize_model(record["model"])
        record["color_group"] = classify_color(record["color_name"])

        dedup = (
            record["year"],
            record["normalized_manufacturer"],
            record["normalized_model"],
            frozenset(parse_all_codes(record["color_codes"])),
        )

        if dedup not in seen_records:
            seen_records.add(dedup)
            unique.append(record)
        else:
            dupes_removed += 1

    result = sorted(
        unique,
        key=lambda r: (
            r["year"] if r["year"] is not None else 9999,
            r["manufacturer"], r["model"], r["color_name"],
        ),
    )

    hex_count = sum(1 for r in result if r["color_hex"])

    print(f"\n── Step 7: Clean & deduplicate ──")
    print(f"  Duplicates removed:  {dupes_removed:,}")
    print(f"  Over-merged dropped: {over_merged_dropped:,}")

    with open(output_file, "w", encoding="utf-8") as f:
        f.write("[\n")
        for i, record in enumerate(result):
            line = json.dumps(record, ensure_ascii=False)
            comma = "," if i < len(result) - 1 else ""
            f.write(f"{line}{comma}\n")
        f.write("]\n")

    source_counts = Counter()
    for r in result:
        source_counts[",".join(sorted(r["sources"]))] += 1

    print(f"\n{'=' * 50}")
    print(f"  Total records:      {len(result):,}")
    print(f"  With hex:           {hex_count:,} ({hex_count/len(result)*100:.1f}%)")
    print(f"  Without hex:        {len(result)-hex_count:,} "
          f"({(len(result)-hex_count)/len(result)*100:.1f}%)")
    print(f"\n  Source distribution:")
    for source_combo, count in source_counts.most_common():
        print(f"    {source_combo:30s} {count:>8,}")
    print(f"\n  Saved to: {output_file}")


# -- CLI --------------------------------------------------------------------

def main():
    # Backwards compat: legacy 4-positional-args invocation
    # python merge_databases.py atu.json chipex.json colorndrive.json out.json
    if (len(sys.argv) == 5
            and not any(a.startswith("--") for a in sys.argv[1:])):
        sources = [
            ('atu',         sys.argv[1], True),
            ('chipex',      sys.argv[2], True),
            ('colorndrive', sys.argv[3], False),
        ]
        merge_databases(sources, sys.argv[4])
        return

    parser = argparse.ArgumentParser(
        description="Merge any number of paint colour databases.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python merge_databases.py atu.json chipex.json "
            "colorndrive.json out.json\n"
            "    (legacy 3-source mode)\n\n"
            "  python merge_databases.py out.json \\\n"
            "      --source atu=atu.json:year \\\n"
            "      --source chipex=chipex.json:year \\\n"
            "      --source colorndrive=colorndrive.json:noyear \\\n"
            "      --source vernicispray=vernicispray.json:year\n"
            "    (any number of sources, year-aware or not)\n\n"
            "Source format: name=path[:year|:noyear]\n"
            "  ':year'   — records have year info (default if omitted)\n"
            "  ':noyear' — records have no year info"
        ),
    )
    parser.add_argument("output", help="Output JSON path")
    parser.add_argument(
        "--source", action="append", required=True,
        help="Source spec: 'name=path[:year|:noyear]'. Repeat for each source.",
    )
    args = parser.parse_args()

    sources = [parse_source_arg(s) for s in args.source]
    merge_databases(sources, args.output)


if __name__ == "__main__":
    main()