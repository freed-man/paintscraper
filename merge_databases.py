"""
Build the consolidated paint lookup table from the scraper outputs.

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
        colorndrive=colorndrive_full.json -o paint_lookup.json
"""

import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict


# -- Manufacturer normalization + cross-source aliases ----------------------

# Aliases are applied AFTER stripping separators, so keys are already
# normalised. These align the sources where they name a make differently.
# Kept deliberately small and evidence-based.
MANUFACTURER_ALIASES = {
    # chipex / colorndrive
    "fordeurope": "ford",    # chipex 'Ford' == colorndrive 'Ford Europe' (UK)
    "bydauto":    "byd",     # chipex 'Byd'  == colorndrive 'Byd Auto'
    "rovergroup": "rover",   # chipex 'Rover Group' == colorndrive 'Rover'
    "rangerover": "landrover",  # Range Rover is a Land Rover model line -- provably
                                # identical codes + hex (820/PAB/1AG/2103, #111112)

    # peinturevoiture verbose / historical names -> existing make (same marque)
    "tatamotors":        "tata",
    "cheryautomobile":   "chery",
    "changanautomobile": "changan",
    "samsungmotors":     "samsung",
    "mahindra&mahindra": "mahindra",
    "polestarracing":    "polestar",
    "ineosautomotive":   "ineos",
    "jacmotors":         "jac",
    "scaniavabis":       "scania",     # historical Scania (commercial)
    "hanomaghenschel":   "hanomag",    # commercial

    # NOT aliased on purpose (different marque or different market/codes):
    #   bedford        -> separate truck marque, not Ford
    #   fordamerica    -> US-market Ford, different codes than UK Ford
    #   fordaustralia  -> AU-market Ford, different codes than UK Ford
    #   ford(usa)      -> peinturevoiture US Ford; would be fordamerica, NOT ford
    # peinturevoiture combined buckets spanning two real marques are left as
    # their own single-source rows rather than mislabelled onto one side:
    #   chrysler/jeep, ferrari-maserati, amc-jeep, zastava-yugo, omoda-jaecoo,
    #   blmc-rover (British-Leyland shared-code bucket; no clean single make)
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
    """Lowercase, fold accents, strip punctuation, drop finish words, collapse.

    Accents are folded to ASCII first (NFKD, then drop combining marks) so
    'Crème'/'Creme' -- and 'Métallisé'/'Metallise' -- normalise to the same
    thing instead of fragmenting into junk tokens ('cr me', 'm tallis') at
    the punctuation strip below. Without this an accented name and its plain
    twin never cross-match in the name->code direction, and accented finish
    words slip past FINISH_WORDS.
    """
    s = unicodedata.normalize("NFKD", name.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
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


def is_composite_crossref(code):
    """colorndrive JLR composite cross-ref codes -- several equivalent codes
    mashed into one string, e.g. 'JLRC820 PAB 2103', 'JLRCPAB', 'LRC820 - 1AG'.
    These never match a real door-sticker code and the clean underlying codes
    (PAB, 820, 1AG...) are already present from other rows, so they are pure
    clutter and could otherwise surface as an ugly answer on a tie.

    Scoped tightly to the JLRC/LRC prefix so it can't touch legitimate spaced
    codes elsewhere (e.g. Citroen 'AC 126'). Clean LRC singles like
    'LRC797-PBF' (no space) are KEPT -- they can be a distinct colour.
    """
    cu = code.upper().strip()
    if cu.startswith("JLRC"):
        return True
    if cu.startswith("LRC") and " " in code:
        return True
    return False


def parse_reliable_codes(code_string):
    """Reliable paint codes from a record's code string.

    Codes are matched EXACTLY -- uppercased and whitespace-trimmed only,
    and deliberately NOT stripped of internal punctuation. That punctuation
    is frequently semantic: Renault encodes a matt finish in a dot
    ('220.116' matt vs '220116' gloss), and dashes mark shade variants
    elsewhere. Stripping it fuses genuinely different paints into one row,
    so exact matching is the safe, correct behaviour -- verified against the
    real data, where every stripping rule introduced false merges. Do NOT
    "optimise" this by stripping dots / dashes / spaces from codes.

    Split on both ',' and ';': chipex joins multi-codes with ',', but a
    stray ';' separator occasionally leaks through and must not survive as
    a junk code like '6;V'.
    """
    raw = re.split(r"[,;]", code_string)
    all_codes = [c.strip().upper() for c in raw if c.strip()]
    return {c for c in all_codes
            if is_reliable_code(c, all_codes) and not is_composite_crossref(c)}


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

def top(counter, default=""):
    """Most-common value, ties broken alphabetically.

    Counter.most_common() breaks ties by insertion order, which depends on
    the order sources are passed and rows appear in each file -- so the same
    inputs could yield a different 'name'/'hex'/'color_group' on re-run or
    arg reorder. Sorting by (-count, key) makes the choice deterministic, to
    match the already-sorted models/sources/normalized_names fields.
    """
    if not counter:
        return default
    return min(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def build_lookup(sources):
    """sources: list of (name, records). Returns (output_rows, stats)."""
    # (mfr, code) -> accumulator
    lookup = defaultdict(lambda: {
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
        "alias_hits": Counter(),
    }

    for name, records in sources:
        used = 0
        for r in records:
            raw_mfr = str(r.get("manufacturer") or "")
            mfr = normalize_manufacturer(raw_mfr)
            if not mfr:
                stats["skipped_no_mfr"] += 1
                continue
            if re.sub(r"[-. ]", "", raw_mfr.strip().lower()) in MANUFACTURER_ALIASES:
                stats["alias_hits"][mfr] += 1

            color_name = str(r.get("color_name") or "")
            codes = parse_reliable_codes(str(r.get("color_codes") or ""))
            if not codes:
                stats["skipped_no_code"] += 1
                continue

            names = parse_names(color_name)
            hexv = normalize_hex(str(r.get("color_hex") or ""))
            model = normalize_model(str(r.get("model") or "")) if r.get("model") else None
            group = classify_color(color_name) if names else "other"
            norms = {normalize_color_name(n) for n in names}
            norms.discard("")

            for code in codes:
                entry = lookup[(mfr, code)]
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
    for (mfr, code), e in lookup.items():
        best_name = top(e["names"])
        all_names = [n for n, _ in sorted(e["names"].items(),
                                          key=lambda kv: (-kv[1], kv[0]))]
        canonical_hex = top(e["hexes"])
        if len(e["hexes"]) > 1:
            stats["multi_hex_codes"] += 1
        group = top(e["groups"], "other")
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
    if stats.get("alias_hits"):
        hits = ", ".join(f"{m} ({n:,})" for m, n in stats["alias_hits"].most_common())
        print(f"  make aliases applied:      {hits}")

    print("\n── Lookup table ──")
    if not rows:
        print("  no output rows produced -- check the input files have data")
        return
    makes = len(set(r["manufacturer"] for r in rows))
    print(f"  unique (make, code) rows:  {len(rows):,}  across {makes} makes")
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
    denom = total_names or 1   # rows>0 here, but defend the no-names case
    print("\n  name -> code direction:")
    print(f"    distinct (make, name) keys: {total_names:,}")
    print(f"    -> exactly 1 code (clean):  {single:,} ({single/denom*100:.1f}%)")
    print(f"    -> 2+ codes (needs policy): {multi:,} ({multi/denom*100:.1f}%)")

    print("\n  swatch quality:")
    mh = stats.get("multi_hex_codes", 0)
    print(f"    codes with >1 distinct hex: {mh:,} "
          f"({mh/len(rows)*100:.1f}%) -- canonical = most common")


DEFAULT_OUTPUT = "paint_lookup.json"


def main():
    args = sys.argv[1:]
    if "-o" in args:
        oi = args.index("-o")
        output_file = args[oi + 1]
        specs = args[:oi] + args[oi + 2:]
    else:
        output_file = DEFAULT_OUTPUT
        specs = args
    if not specs:
        print("Usage: python merge_databases.py name=path [name=path ...] [-o output.json]")
        print(f"       output defaults to {DEFAULT_OUTPUT}")
        sys.exit(1)

    sources = []
    for spec in specs:
        name, path = parse_source_arg(spec)
        data = load_json(path)
        print(f"loaded {name:14s} {len(data):>8,} records  ({path})")
        sources.append((name, data))

    rows, stats = build_lookup(sources)
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