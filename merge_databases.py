"""
Build the consolidated paint bridge table from the scraper outputs.

This replaces the old year-aware merge AND prepare_swatches.py. Both
chipex (make-level) and colorndrive (model-level) are now year-blind, so
the entire year-reconciliation machinery is gone. The output is ONE table
keyed by (manufacturer, code) that serves all three downstream jobs:

    code -> name   (fill a missing colour name from a known code)
    name -> code   (fill a missing code from a known colour name)
    swatch         (the hex sits on the row as a column, blank if none)

Output row schema (one per unique (manufacturer, code)):

    {
        "manufacturer": "ford",                  # normalised, lowercase
        "code": "LY3D",                          # uppercase, reliable
        "name": "Magnetic Grey",                 # best (most common) name
        "all_names": ["Magnetic Grey", ...],     # every variant seen
        "normalized_names": ["magnetic grey"],   # for name->code matching
        "hex": "#5B5E60",                        # canonical hex ("" if none)
        "color_group": "grey",                   # for VDG disambiguation
        "models": ["focus", "fiesta", ...],      # from colorndrive (may be [])
        "sources": ["chipex", "colorndrive"]     # provenance / trust
    }

The hex being a plain column (not a gate) is the whole point: code->name
must keep codes that have a name but no swatch, which prepare_swatches
threw away. The name->code direction is derivable by coloureg: invert
on normalized_names, and where one (make, name) maps to several codes,
apply a consensus / drop-if-ambiguous policy (the ambiguity counts are
printed at the end so the split is known up front).

Usage:
    python merge_databases.py chipex=chipex_full.json \\
        colorndrive=colorndrive_full.json -o paint_bridge.json
"""

import json
import re
import sys
from collections import Counter, defaultdict


# -- Manufacturer normalization + cross-source aliases ----------------------

# Aliases are applied AFTER stripping separators, so keys are already
# normalised. These align the two sources where they name a make
# differently. Kept deliberately small and evidence-based.
MANUFACTURER_ALIASES = {
    "fordeurope": "ford",    # chipex 'Ford' == colorndrive 'Ford Europe' (UK)
    "bydauto":    "byd",     # chipex 'Byd'  == colorndrive 'Byd Auto'
    "rovergroup": "rover",   # chipex 'Rover Group' == colorndrive 'Rover'
    # NOT aliased on purpose (different marque or different market/codes):
    #   bedford        -> separate truck marque, not Ford
    #   fordamerica    -> US-market Ford, different codes than UK Ford
    #   fordaustralia  -> AU-market Ford, different codes than UK Ford
}


def normalize_manufacturer(text):
    """'Mercedes-Benz' -> 'mercedesbenz', then apply cross-source aliases."""
    norm = text.strip().lower().replace("-", "").replace(" ", "").replace(".", "")
    return MANUFACTURER_ALIASES.get(norm, norm)


def normalize_model(text):
    """'Id.3', 'Id 3', 'Id-3' all become 'id3'."""
    return text.strip().lower().replace(".", "").replace("-", "").replace(" ", "")


# -- Colour-name normalization (for the name->code direction) ---------------

# Finish / coat descriptors stripped so the same colour matches across
# sources that do or don't spell out the finish ('Brilliant Silver' vs
# 'Brilliant Silver Metallic'). Conservative: over-stripping only ever
# makes a (make, name) MORE ambiguous, which the consensus policy then
# drops -- it can never turn a correct fill into a wrong one.
FINISH_WORDS = {
    "metallic", "metalic", "met", "metallise", "metalise", "metalizado",
    "mica", "pearl", "pearlescent", "perl", "perlato", "nacre", "nacré",
    "effect", "solid", "uni", "gloss", "matt", "matte", "satin",
    "clearcoat", "tricoat", "tri", "coat",
}


def normalize_color_name(name):
    """Lowercase, strip punctuation, drop finish words, collapse spaces."""
    s = name.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    tokens = [t for t in s.split() if t and t not in FINISH_WORDS]
    return " ".join(tokens)


# -- Colour classification (for VDG colour-group disambiguation) ------------

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


def classify_color(color_name):
    name_lower = color_name.lower()
    for group, keywords in COLOR_KEYWORDS.items():
        if any(kw in name_lower for kw in keywords):
            return group
    return "other"


# -- Hex validation ---------------------------------------------------------

HEX_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")


def is_valid_hex(hex_value):
    return bool(hex_value and HEX_PATTERN.match(hex_value))


def normalize_hex(hex_value):
    """Uppercase canonical hex, or '' if missing/invalid."""
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
    if len(code) == 4 and code[:2] == code[2:]:   # 'F1F1' style doubling
        return False
    if all_codes_in_record:
        hyphenated = [c for c in all_codes_in_record if "-" in c]
        for hcode in hyphenated:
            if code != hcode and code in hcode:
                return False
    return True


def parse_reliable_codes(code_string):
    all_codes = [c.strip().upper() for c in code_string.split(",") if c.strip()]
    return {c for c in all_codes if is_reliable_code(c, all_codes)}


# -- Name helpers -----------------------------------------------------------

def titlecase_if_upper(name):
    return name.title() if name.isupper() else name


def clean_name_string(name_string):
    """Strip HTML/escaping junk that leaked into a few colorndrive names."""
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


def parse_names(name_string):
    cleaned = clean_name_string(name_string)
    return [titlecase_if_upper(n.strip()) for n in cleaned.split(",") if n.strip()]


# -- Source spec parsing ----------------------------------------------------

def parse_source_arg(spec):
    """'chipex=chipex_full.json' -> ('chipex', 'chipex_full.json')."""
    if "=" not in spec:
        raise ValueError(f"Expected 'name=path', got '{spec}'")
    name, path = spec.split("=", 1)
    return name.strip(), path.strip()


def load_json(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


# -- The merge --------------------------------------------------------------

def build_bridge(sources):
    """sources: list of (name, records). Returns (output_rows, stats)."""
    # (mfr, code) -> accumulator
    bridge = defaultdict(lambda: {
        "names": Counter(),       # variant -> count (best name + all names)
        "norm_names": set(),      # normalised variants (name->code)
        "hexes": Counter(),       # hex -> count (canonical hex)
        "models": set(),          # models seen (colorndrive)
        "groups": Counter(),      # colour group -> count
        "sources": set(),         # provenance
    })

    stats = {
        "per_source": {},
        "skipped_no_mfr": 0,
        "skipped_no_code": 0,
        "records_used": 0,
    }

    for name, records in sources:
        used = 0
        for r in records:
            mfr = normalize_manufacturer(r.get("manufacturer", ""))
            if not mfr:
                stats["skipped_no_mfr"] += 1
                continue
            codes = parse_reliable_codes(r.get("color_codes", ""))
            if not codes:
                stats["skipped_no_code"] += 1
                continue

            names = parse_names(r.get("color_name", ""))
            hexv = normalize_hex(r.get("color_hex", ""))
            model = normalize_model(r.get("model", "")) if r.get("model") else None
            group = classify_color(r.get("color_name", "")) if names else "other"
            norms = {normalize_color_name(n) for n in names}
            norms.discard("")

            for code in codes:
                entry = bridge[(mfr, code)]
                for n in names:
                    entry["names"][n] += 1
                entry["norm_names"].update(norms)
                if hexv:
                    entry["hexes"][hexv] += 1
                if model:
                    entry["models"].add(model)
                entry["groups"][group] += 1
                entry["sources"].add(name)
            used += 1

        stats["per_source"][name] = used
        stats["records_used"] += used

    # Build output rows
    rows = []
    stats["multi_hex_codes"] = 0
    for (mfr, code), e in bridge.items():
        best_name = e["names"].most_common(1)[0][0] if e["names"] else ""
        all_names = [n for n, _ in e["names"].most_common()]
        canonical_hex = e["hexes"].most_common(1)[0][0] if e["hexes"] else ""
        if len(e["hexes"]) > 1:
            stats["multi_hex_codes"] += 1
        group = e["groups"].most_common(1)[0][0] if e["groups"] else "other"
        rows.append({
            "manufacturer": mfr,
            "code": code,
            "name": best_name,
            "all_names": all_names,
            "normalized_names": sorted(e["norm_names"]),
            "hex": canonical_hex,
            "color_group": group,
            "models": sorted(e["models"]),
            "sources": sorted(e["sources"]),
        })

    rows.sort(key=lambda x: (x["manufacturer"], x["code"]))
    return rows, stats


def print_stats(rows, stats):
    print("\n── Inputs ──")
    for name, n in stats["per_source"].items():
        print(f"  {name:14s} {n:>8,} records used")
    print(f"  skipped (no manufacturer): {stats['skipped_no_mfr']:,}")
    print(f"  skipped (no reliable code): {stats['skipped_no_code']:,}")

    print("\n── Bridge table ──")
    print(f"  unique (make, code) rows:  {len(rows):,}")
    with_hex = sum(1 for r in rows if r["hex"])
    print(f"  with hex (swatch):         {with_hex:,} ({with_hex/len(rows)*100:.1f}%)")
    print(f"  without hex (name-only):   {len(rows)-with_hex:,}  "
          f"<- these are what prepare_swatches dropped")

    # provenance
    prov = Counter(" + ".join(r["sources"]) for r in rows)
    print("\n  provenance:")
    for combo, n in prov.most_common():
        print(f"    {combo:24s} {n:>8,} ({n/len(rows)*100:.1f}%)")

    # code -> name is clean by construction (one row per code). Report the
    # name -> code direction, which is where ambiguity lives.
    name_to_codes = defaultdict(set)
    for r in rows:
        for nn in r["normalized_names"]:
            name_to_codes[(r["manufacturer"], nn)].add(r["code"])
    counts = Counter(len(v) for v in name_to_codes.values())
    total_names = len(name_to_codes)
    single = counts.get(1, 0)
    multi = total_names - single
    print("\n  name -> code direction:")
    print(f"    distinct (make, name) keys: {total_names:,}")
    print(f"    -> exactly 1 code (clean):  {single:,} ({single/total_names*100:.1f}%)")
    print(f"    -> 2+ codes (needs policy): {multi:,} ({multi/total_names*100:.1f}%)")

    print("\n  swatch quality:")
    mh = stats.get("multi_hex_codes", 0)
    print(f"    codes with >1 distinct hex: {mh:,} "
          f"({mh/len(rows)*100:.1f}%) -- canonical = most common")


def main():
    args = sys.argv[1:]
    if "-o" not in args:
        print("Usage: python merge_databases.py name=path [name=path ...] -o output.json")
        sys.exit(1)
    oi = args.index("-o")
    output_file = args[oi + 1]
    specs = args[:oi] + args[oi + 2:]
    if not specs:
        print("No sources given.")
        sys.exit(1)

    sources = []
    for spec in specs:
        name, path = parse_source_arg(spec)
        data = load_json(path)
        print(f"loaded {name:14s} {len(data):>8,} records  ({path})")
        sources.append((name, data))

    rows, stats = build_bridge(sources)
    print_stats(rows, stats)

    with open(output_file, "w", encoding="utf-8") as f:
        f.write("[\n")
        for i, r in enumerate(rows):
            line = json.dumps(r, ensure_ascii=False, sort_keys=True)
            comma = "," if i < len(rows) - 1 else ""
            f.write(f"{line}{comma}\n")
        f.write("]\n")
    import os
    print(f"\nWrote {len(rows):,} rows to {output_file} "
          f"({os.path.getsize(output_file)/1024/1024:.1f} MB)")


if __name__ == "__main__":
    main()
