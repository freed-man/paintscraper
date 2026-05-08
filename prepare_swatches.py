"""
Prepare paint_swatches.json for the coloureg Django app.

Reads merged_database.json (output of merge_databases.py) and produces a 
lean lookup table optimised for the production swatch lookup feature.

Output schema (one row per unique manufacturer/code/hex combination):

    {
        "manufacturer": "skoda",                  # normalised, lowercase
        "code": "8E8E",                            # uppercase, normalised
        "hex": "#9B9D9B",                          # validated, uppercase
        "name": "Brilliant Silver",                # most common name across sources
        "applicable_models": ["roomster", ...],    # all models that use this hex
        "color_group": "grey",                     # for VDG colour disambiguation
        "year_min": 2008,                          # earliest seen
        "year_max": 2025,                          # latest seen
        "sources_count": 133,                      # how many records contributed
        "sources": ["atu", "chipex"]               # which scrapers
    }

Note: model_families (first-word fragments for fuzzy matching) are NOT stored
in the JSON. They're computed on the fly during the load_paint_swatches step
to keep the canonical file lean. Recoverable from applicable_models.

This produces ~109K rows / ~22 MB JSON from ~800K input records.

Usage:
    python prepare_swatches.py merged_database.json paint_swatches.json
"""

import json
import re
import sys
from collections import Counter, defaultdict


HEX_PATTERN = re.compile(r"^#[0-9A-F]{6}$")


def is_valid_hex(h):
    """Match exactly the canonical uppercase hex format."""
    return bool(h and HEX_PATTERN.match(h))


def first_word(model):
    """Extract first word from a normalised model string.
    
    Normalised model has no spaces/dots/hyphens, so 'first word' here means
    'first alphabetic chunk before any digit'. Used for fuzzy model matching:
    'mondeotitanium20tdci' → 'mondeo', 'roomstergreenlinetdicr' → 'roomster'.
    """
    if not model:
        return ""
    # Take everything up to the first digit
    match = re.match(r"^[a-z]+", model)
    if match:
        return match.group(0)
    # Fallback: first 6 chars
    return model[:6]


def best_name(name_strings):
    """Pick the most representative name from a list of comma-separated strings.
    
    Each string may be 'Brilliant Silver, Reflexsilver Metallic, Silber Brillant'.
    We split everything, count individual name occurrences, and return the most
    common one. Ties broken by length (longer = more descriptive).
    """
    candidates = []
    for s in name_strings:
        if not s:
            continue
        for piece in s.split(","):
            p = piece.strip()
            if p and len(p) > 1:
                candidates.append(p)
    
    if not candidates:
        return ""
    
    counter = Counter(candidates)
    # Most common, with longest as tiebreak
    sorted_names = sorted(
        counter.items(),
        key=lambda x: (-x[1], -len(x[0])),
    )
    return sorted_names[0][0]


def prepare_swatches(input_file, output_file):
    print(f"Loading {input_file}...")
    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"  {len(data):,} records loaded")

    # Step 1: Explode each record into per-(mfr, code) entries
    # An entry contributes its hex/model/year/group/name to the (mfr, code) key
    print("\nExploding records by individual code...")
    
    exploded = defaultdict(list)
    skipped_no_hex = 0
    skipped_bad_hex = 0
    skipped_no_mfr = 0
    skipped_short_code = 0
    
    for r in data:
        mfr = r.get("normalized_manufacturer", "").strip()
        if not mfr:
            skipped_no_mfr += 1
            continue
        
        h = r.get("color_hex", "").upper().strip()
        if not h:
            skipped_no_hex += 1
            continue
        if not is_valid_hex(h):
            skipped_bad_hex += 1
            continue
        
        for code in r.get("color_codes", "").split(","):
            code = code.strip().upper()
            if not code:
                continue
            if len(code) < 2:
                skipped_short_code += 1
                continue
            
            exploded[(mfr, code)].append({
                "hex": h,
                "name": r.get("color_name", ""),
                "model": r.get("normalized_model", ""),
                "year": r.get("year"),
                "color_group": r.get("color_group", ""),
                "sources": r.get("sources", []),
            })

    total_exploded = sum(len(v) for v in exploded.values())
    print(f"  Records with valid hex:    {len(data) - skipped_no_hex - skipped_bad_hex - skipped_no_mfr:,}")
    print(f"  Skipped (no hex):          {skipped_no_hex:,}")
    print(f"  Skipped (bad hex):         {skipped_bad_hex:,}")
    print(f"  Skipped (no manufacturer): {skipped_no_mfr:,}")
    print(f"  Short codes ignored:       {skipped_short_code:,}")
    print(f"  Total (mfr, code) keys:    {len(exploded):,}")
    print(f"  Total contributions:       {total_exploded:,}")

    # Step 2: For each (mfr, code), produce one row per unique hex
    # Aggregate disambiguators (models, years, groups) per hex
    print("\nAggregating per (mfr, code, hex)...")
    
    swatches = []
    
    for (mfr, code), records in exploded.items():
        # Group by hex
        by_hex = defaultdict(list)
        for r in records:
            by_hex[r["hex"]].append(r)
        
        for hex_val, hex_records in by_hex.items():
            models = sorted({r["model"] for r in hex_records if r["model"]})

            years = [r["year"] for r in hex_records if r["year"]]
            
            # Most common color_group
            groups = [r["color_group"] for r in hex_records if r["color_group"]]
            best_group = Counter(groups).most_common(1)[0][0] if groups else ""
            
            # Best name across all contributing records
            name_strings = [r["name"] for r in hex_records]
            name = best_name(name_strings)
            
            # Aggregate sources from contributing records
            all_sources = set()
            for r in hex_records:
                all_sources.update(r.get("sources", []))
            sources = sorted(all_sources)
            
            swatches.append({
                "manufacturer": mfr,
                "code": code,
                "hex": hex_val,
                "name": name,
                "applicable_models": models,
                "color_group": best_group,
                "year_min": min(years) if years else None,
                "year_max": max(years) if years else None,
                "sources_count": len(hex_records),
                "sources": sources,
            })
    
    # Sort for determinism
    swatches.sort(key=lambda s: (s["manufacturer"], s["code"], s["hex"]))
    
    print(f"  Total swatch rows: {len(swatches):,}")

    # Step 3: Statistics
    print("\nQuality statistics:")
    
    # How many (mfr, code) have just one hex (no conflict)?
    keys_with_one_hex = 0
    keys_with_conflict = 0
    keys_with_many = 0
    
    hex_counts = Counter()
    for s in swatches:
        hex_counts[(s["manufacturer"], s["code"])] += 1
    
    for n in hex_counts.values():
        if n == 1:
            keys_with_one_hex += 1
        elif n <= 3:
            keys_with_conflict += 1
        else:
            keys_with_many += 1
    
    total_keys = len(hex_counts)
    print(f"  Unique (mfr, code) keys:    {total_keys:,}")
    print(f"    1 hex (no conflict):      {keys_with_one_hex:,} ({keys_with_one_hex/total_keys*100:.1f}%)")
    print(f"    2-3 hexes (conflict):     {keys_with_conflict:,} ({keys_with_conflict/total_keys*100:.1f}%)")
    print(f"    4+ hexes (heavy reuse):   {keys_with_many:,} ({keys_with_many/total_keys*100:.1f}%)")
    
    # Source distribution
    source_dist = Counter()
    for s in swatches:
        source_dist[",".join(s["sources"])] += 1
    print(f"\n  Source distribution (top 5):")
    for sources, count in source_dist.most_common(5):
        print(f"    {sources:30s} {count:>8,}")

    # Step 4: Save
    print(f"\nSaving to {output_file}...")
    with open(output_file, "w", encoding="utf-8") as f:
        # Pretty single-line per record for readable diffs in git
        f.write("[\n")
        for i, s in enumerate(swatches):
            line = json.dumps(s, ensure_ascii=False, sort_keys=True)
            comma = "," if i < len(swatches) - 1 else ""
            f.write(f"{line}{comma}\n")
        f.write("]\n")
    
    # File size
    import os
    size_mb = os.path.getsize(output_file) / 1024 / 1024
    print(f"  File size: {size_mb:.1f} MB")
    print(f"\nDone.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python prepare_swatches.py <merged_database.json> <output.json>")
        sys.exit(1)
    prepare_swatches(sys.argv[1], sys.argv[2])
