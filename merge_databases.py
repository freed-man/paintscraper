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
    # 'candy' used to sit in this white list, and with white checked first it
    # made EVERY candy name classify white ('Candy Red', 'Blue Candy' -- 172
    # shipped rows, ~150 of them wrongly). It is a finish/marketing word, not
    # a colour. Genuine candy-whites all carry a real white word ('Candy
    # White', 'Candyweiss') and keep classifying white without it -- verified
    # against the shipped data: all 24 true whites stay white, ~150 rows move
    # to their real group, and nothing outside the candy set changes.
    "white":  ["white", "weiss", "weis", "bianco", "branco", "blanco",
               "blanc"],
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

def is_reliable_code(code, all_codes_in_record=None, mfr=None,
                     unambiguous_short=None):
    """Filter dealer shorthand and substring duplicates.

    Short codes (<=2 chars) are dropped by default: a 2-char token is usually
    dealer shorthand and collides across many colours within a make. The one
    exception -- a (make, short-code) pair that maps to exactly ONE colour
    across the whole corpus is a genuine manufacturer code (Ford 'MA', Citroen
    'H6', Dodge 'D3', Lincoln 'XB') and is recovered. That unambiguous set is
    computed up front by compute_unambiguous_short() and threaded in here.
    If it isn't supplied, the old conservative behaviour (drop every <=2) is
    preserved exactly -- so every existing caller is unaffected.
    """
    if len(code) <= 2:
        if not (unambiguous_short and mfr is not None
                and (mfr, code) in unambiguous_short):
            return False
        # recovered: a single-colour short code, still subject to the
        # substring-of-hyphenated check below (it can still be a fragment).
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


def parse_reliable_codes(code_string, mfr=None, unambiguous_short=None):
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
            if is_reliable_code(c, all_codes, mfr, unambiguous_short)
            and not is_composite_crossref(c)}


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


def _strict_color_id(name):
    """Conservative colour identity for the short-code ambiguity test.

    Lowercase, keep alphanumerics + spaces, collapse runs of whitespace. This
    deliberately does NOT strip finish words the way normalize_color_name does:
    a short code is treated as unambiguous only when its colour is genuinely
    consistent, never by folding 'X Metallic' and 'X Pearl' into one identity.
    Erring strict means a borderline short code is dropped, not mis-recovered.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", name.lower())).strip()


def compute_unambiguous_short(sources):
    """Pre-pass: the set of (mfr, code) where code is <=2 chars and maps to
    exactly one colour across the entire corpus.

    These are the short manufacturer codes safe to recover past the default
    <=2 drop -- e.g. Ford 'MA' is always Light Crystal Blue Metallic, so it
    identifies a colour as reliably as any longer code. A short code naming
    two or more different colours within a make stays dropped: that collision
    is the whole reason the blunt filter exists.
    """
    short_colours = defaultdict(set)
    for _name, records in sources:
        for r in records:
            mfr = normalize_manufacturer(str(r.get("manufacturer") or ""))
            if not mfr:
                continue
            raw = re.split(r"[,;]", str(r.get("color_codes") or ""))
            codes = [c.strip().upper() for c in raw if c.strip()]
            shorts = [c for c in codes
                      if len(c) <= 2 and not is_composite_crossref(c)]
            if not shorts:
                continue
            ids = {_strict_color_id(n)
                   for n in parse_names(str(r.get("color_name") or ""))}
            ids.discard("")
            if not ids:
                continue
            for c in shorts:
                short_colours[(mfr, c)].update(ids)
    return {key for key, ids in short_colours.items() if len(ids) == 1}


def backfill_swatches(rows):
    """Fill a hexless row's swatch from a same-base, same-name sibling that has
    one -- e.g. the pv base 'DDSE' (Blue Candy, no hex) inherits #006D8A from its
    colorndrive sibling 'DDSEWTA'. Fill-only: it never overwrites an existing hex
    and never touches code or names, so it cannot change any code<->name
    resolution -- it only raises swatch coverage. A sibling is a longer code that
    shares the base (startswith) AND a normalized name. If several qualifying
    siblings that disagree, the most common wins; ties are broken by the lowest
    hex value so the result is fully order-independent (byte-reproducible).
    """
    by_mfr = defaultdict(list)
    for r in rows:
        by_mfr[r["manufacturer"]].append(r)
    filled = 0
    for group in by_mfr.values():
        hex_by_name = defaultdict(list)          # normalized name -> rows that carry a hex
        for r in group:
            if r["hex"]:
                for n in r["normalized_names"]:
                    hex_by_name[n].append(r)
        for a in group:
            if a["hex"]:
                continue
            sib_hexes, seen = [], set()
            for n in a["normalized_names"]:
                for b in hex_by_name.get(n, []):
                    if b["code"] in seen:
                        continue
                    if b["code"].startswith(a["code"]) and len(b["code"]) > len(a["code"]):
                        seen.add(b["code"])
                        sib_hexes.append(b["hex"])
            if sib_hexes:
                counts = Counter(sib_hexes)
                top_n = max(counts.values())
                a["hex"] = min(h for h, c in counts.items() if c == top_n)
                filled += 1
    return filled


# Curated colour-name aliases: official manufacturer names that no scraper
# carries, keyed to the code they belong on. Applied after the merge so the
# name lands on the real, orderable code already in the data. Kept tiny and
# hand-verified against the manufacturer's own listing -- not a general rule.
CURATED_ALIASES = {
    # Ford's shop lists the colour "Blue Candy FOE" with Colour Code DDSEWTA
    # (verified: FOE is part of the NAME, not a code -- it is on no Ford code
    # list). partslink24 returns "Blue Candy (FOE)"; this alias resolves that
    # straight to DDSEWTA, the real #006D8A retail code already in the data.
    ("ford", "DDSEWTA"): ["Blue Candy FOE"],
    # partslink24 returns "Red Candy Tint Cc" (Tinted Clearcoat -- part of the
    # paint's official name, cf. US "Red Candy Tint Metallic" U6) for the
    # European S-Max in this colour. The paint is 9SSEWTA: the smax-tagged
    # Candy Red Pearl (#8F001D) -- an EU S-Max-specific retail listing groups
    # 9SSEWTA/PN3K7/9SSE as this one paint. NOT the same as "Red Candy 2"
    # (RZ/BRREWTA/M7293A #741A24), a later, different paint.
    ("ford", "9SSEWTA"): ["Red Candy Tint CC"],
    # partslink24 returns the ENGLISH "Obsession Blue"; the dataset stores the
    # French "Bleu Obsession (Metallic)". Same paint, same code: EDP is
    # Peugeot's universal code for Bleu Obsession (the 408's 2022 launch
    # colour, extended range-wide -- current 3008/5008 hybrids in the live
    # log). Verified against UK + FR retail listings ("Bleu Obsession Blue,
    # Colour Code EDP"); no second-generation variant exists.
    ("peugeot", "EDP"): ["Obsession Blue"],
    # partslink24 returns the ENGLISH "Okenite White"; the dataset stores the
    # French "Blanc Okenite (Nacre Metallic)" on peugeot. Same paint, same
    # code: ESU is the Stellantis-wide Blanc Okenite / Okenite White, Peugeot's
    # default white since ~2023 (replaced Blanc Banquise / Blanc Nacre).
    # Triangulated: FR specialist + multiple retailers ("BLANC OKENITE, Colour
    # Code ESU"); in-dataset, jeep/lancia/vauxhall already carry "Okenite
    # White" on ESU verbatim, and citroen/ESU's hex #9EA2A5 matches peugeot's
    # #9DA2A4. The row's stray "Aquamarine Mica" name is 90s-era ESU code
    # reuse (306 Cabriolet tag) -- pre-existing, harmless, same quirk as
    # EDP's "Brun Palombe".
    ("peugeot", "ESU"): ["Okenite White"],
}


def apply_curated_aliases(rows):
    """Append hand-verified official names (that no scraper carries) onto the
    code they belong to. Name-only: it adds to all_names / normalized_names and
    never touches a code, hex, or model tag, so it cannot change any existing
    resolution -- it only lets the official name resolve to its real code.
    """
    added = 0
    for r in rows:
        extra = CURATED_ALIASES.get((r["manufacturer"], r["code"]))
        if not extra:
            continue
        for name in extra:
            if name not in r["all_names"]:
                r["all_names"].append(name)
            norm = normalize_color_name(name)
            if norm and norm not in r["normalized_names"]:
                r["normalized_names"].append(norm)
                added += 1
        r["normalized_names"].sort()
    return added


def build_lookup(sources):
    """sources: list of (name, records). Returns (output_rows, stats)."""
    # Short manufacturer codes (<=2 chars) that unambiguously name one colour
    # within their make are recovered past the default drop; computed once.
    unambiguous_short = compute_unambiguous_short(sources)
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
            codes = parse_reliable_codes(str(r.get("color_codes") or ""),
                                         mfr, unambiguous_short)
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

    stats["curated_aliases_added"] = apply_curated_aliases(rows)
    stats["backfilled_swatches"] = backfill_swatches(rows)
    rows.sort(key=lambda x: (x["manufacturer"], x["code"]))
    stats["short_code_rows"] = sum(1 for r in rows if len(r["code"]) <= 2)
    return rows, stats


def print_stats(rows, stats):
    print("\n── Inputs ──")
    for name, n in stats["per_source"].items():
        print(f"  {name:14s} {n:>8,} records used")
    print(f"  skipped (no manufacturer): {stats['skipped_no_mfr']:,}")
    print(f"  skipped (no reliable code): {stats['skipped_no_code']:,}")
    if stats.get("backfilled_swatches"):
        print(f"  swatches backfilled:       {stats['backfilled_swatches']:,} "
              f"(hexless codes filled from a same-name sibling)")
    if stats.get("curated_aliases_added"):
        print(f"  curated aliases added:     {stats['curated_aliases_added']:,} "
              f"(official names attached to their real code)")
    if stats.get("short_code_rows"):
        print(f"  recovered short codes:     {stats['short_code_rows']:,} "
              f"(<=2 chars, unambiguous within make)")
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
        if oi + 1 >= len(args):
            sys.exit("-o needs a filename, e.g. -o paint_lookup.json")
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