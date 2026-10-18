"""Regelmotor: gaet kategori (bus / car / truck / tram / train / misc).

Saadan virker det
1. ALLE albums billedet ligger i stemmer. "Trams", "Cars in Denmark 2018", "Volvo B10M" ...
   giver 6 point (maerkealbums som "Toyota" 3). Land- og aarsalbums ("Denmark 2010's") er
   blandede og stemmer ikke. Dine egne rettelser staar i tools/album_categories.csv.
2. Tagget 'bus' staar paa ca. 70 % af billederne, ogsaa paa sporvogne, tog og lastbiler, saa
   det giver kun 3 point og kan overstyres af album og titel.
3. Ord i titel, tags og beskrivelse (b10m, sporvogn, lokomotiv ...) giver point. Maerker der
   bruges til flere typer (renault, hyundai, volvo, scania ...) giver 0-1 point.
4. Er der for lidt eller uafgjort evidens, bliver det 'misc' (hellere misc end forkert).

Efter en aendring: python tools/import_flickr.py reclassify   (viser kun en forhaandsvisning)
"""
import re

# (ord/udtryk, point). Matcher paa ordgraenser, case-insensitivt.
# Et '*' til sidst betyder "starter med" (fx 'rutebil*' matcher 'rutebiler').
RULES = {
    "bus": [
        ("bus*", 5), ("busser", 5), ("rutebil*", 5), ("linjebus*", 5), ("turistbus*", 5),
        ("citybus*", 5), ("midibus*", 5), ("minibus*", 4), ("gelenkbus*", 5),
        ("lavgulvsbus*", 5), ("dobbeltdaekker*", 4), ("togbus*", 5), ("flybus*", 4),
        ("skolebus*", 5), ("rutebus*", 5), ("telebus*", 5), ("flextrafik", 2),
        ("b10m", 6), ("b10b", 6), ("b10r", 6), ("b12", 4), ("b7r", 6), ("b9", 3), ("b6", 3),
        ("b10", 3), ("b58", 5), ("b59", 5), ("b10ble", 6),
        ("o305", 6), ("o405", 6), ("o530", 6), ("citaro*", 6), ("tourismo", 5), ("travego", 5),
        ("irisbus", 6), ("irizar", 6), ("van hool", 6), ("vanhool", 6), ("setra", 6),
        ("neoplan", 6), ("solaris", 6), ("urbino", 6), ("lion's city", 6), ("lions city", 6),
        ("scania omni*", 6), ("omnilink", 6), ("omnicity", 6), ("omniexpress", 6),
        ("volvo 7700", 6), ("volvo 7900", 6), ("volvo 8700", 6), ("volvo 9700", 6),
        ("9700", 3), ("8700", 3), ("7700", 3), ("sunsundegui", 6), ("unvi", 5), ("lahti", 4),
        ("aabenraa karrosseri", 6), ("vest-busvaerk", 6), ("vest busvaerk", 6),
        ("arriva", 3), ("movia", 3), ("keolis", 3), ("din offentlige transport", 3),
        ("midttrafik", 3), ("nordjyllands trafikselskab", 3), ("nt bus", 4), ("fynbus", 5),
        ("sydtrafik", 3), ("fjord1", 1), ("nettbuss", 5), ("vy buss", 5), ("swebus", 5),
        ("ruter", 2), ("skyss", 2), ("busslinje", 5), ("autobus", 5), ("coach", 4), ("dab", 4), ("coaches", 4), ("ansair", 6), ("pr100", 5), ("pr 100", 5), ("pr180", 5), ("caetano", 6), ("utic", 5), ("ikarus", 6), ("karosa", 6), ("bova", 6), ("berkhof", 6), ("jonckheere", 6), ("otokar", 6), ("temsa", 6), ("carrus", 6), ("kiitokori", 6), ("trolleybus*", 6), ("trolley bus*", 6), ("doppeldecker", 5), ("double decker", 5), ("double-decker", 5), ("bodybuilder*", 4), ("busdealer*", 5), ("scrapbus*", 6),
        ("stadsbuss*", 5), ("linjebuss*", 5),
    ],
    "tram": [
        ("sporvogn*", 7), ("tram", 7), ("trams", 7), ("trikk", 7), ("sparvagn*", 7),
        ("letbane*", 6), ("strassenbahn", 7), 
        ("tatra", 4), ("gt6", 5), ("gt8", 5), ("gt6n", 6), ("combino", 7), ("flexity", 7),
        ("aarhus letbane", 7), ("odense letbane", 7),
        ("tw6000", 6), ("m-wagen", 5), ("tram line", 6),
    ],
    "train": [
        ("tog", 6), ("toge", 6), ("dsb", 6), ("s-tog", 7), ("stog", 6), ("lokomotiv*", 7),
        ("lok", 4), ("lokotog", 5), ("jernbane*", 6), ("banegaard*", 5), 
        ("perron*", 4), ("skinnebus*", 6), ("motorvogn*", 4), ("togsaet", 6), ("ic3", 7),
        ("ic4", 7), ("re-tog", 6), ("regionaltog", 7), ("metro", 5),
        ("metrotog", 6), ("railway", 6), ("train", 6), ("trains", 6), ("locomotive", 7),
        ("mz", 5), ("mx", 3), ("litra", 5), ("arriva tog", 7),
        ("lokaltog", 7), ("nesa", 3), ("njk", 4), ("sj", 3), ("sj rc", 5), ("bane danmark", 5),
        ("godstog", 7), ("damplokomotiv*", 7), ("diesellokomotiv*", 7), ("ellokomotiv*", 7),
        ("tågkompaniet", 5), ("zug", 5), ("bahn", 4), ("db bahn", 6), ("sbb", 4), ("cargonet", 6),
        ("jernbanemuseum*", 7), ("veterantog", 7), ("museumstog", 7), ("togfoerer", 3),
    ],
    "truck": [
        ("lastbil*", 7), ("lastvogn*", 7), ("truck*", 6), ("lorry", 7), ("vogntog", 7),
        ("saettevogn*", 6), ("trailer*", 5), ("sloeb", 3), ("tankbil*", 6), ("tankvogn*", 5),
        ("kranbil*", 6), ("flyttebil*", 5), ("renovationsbil*", 5), ("skraldebil*", 5),
        ("tippelad*", 6), ("containerbil*", 6), ("kassebil*", 3), ("varebil*", 2),
        ("f10", 4), ("f12", 4), ("f16", 4), ("fh12", 6), ("fh16", 6), ("fh", 2), ("fm12", 5),
        ("fm10", 5), ("nh12", 6), ("n10", 4), ("n12", 4), ("fl6", 5), ("fl10", 5),
        ("lb140", 6), ("lb110", 6), ("lb111", 6), ("t10", 2), ("tgx", 6), ("tgs", 6), ("tga", 6),
        ("actros", 6), ("atego", 5), ("axor", 6), ("daf xf", 6), ("xf105", 6), ("cf85", 6),
        ("iveco stralis", 6), ("stralis", 6), ("eurotech", 5), ("magirus", 5), ("scania r", 3),
        ("scania t", 3), ("scania g", 3), ("streamline", 4), ("scania 143", 2), ("scania 113", 2),
        ("scania 142", 2), ("scania 112", 2), ("scania 141", 2), ("scania 111", 2),
        ("volvo fh", 6), ("volvo fm", 6), ("volvo f12", 6), ("volvo fl", 5), ("volvo nl", 5),
        ("kaessbohrer", 2), ("hangertrailer", 5), ("semitrailer", 6), ("rigid", 2),
        ("vognmand*", 5), ("vognmandsforretning*", 6), ("spedition*", 5),
        ("fragt*", 3), ("haulage", 6), ("hgv", 6), ("camion", 6), ("lkw", 6), ("lastebil*", 7),
        ("lastbilsmuseum*", 7), ("falck", 2), ("beredskab*", 2), ("brandbil*", 4),
        ("brandvaesen*", 3), ("rednings*", 1), ("ambulance*", 2),
    ],
    "car": [
        ("personbil*", 7), ("bil", 3), ("biler", 4), ("automobil*", 6), ("sedan", 6),
        ("stationcar", 6), ("station wagon", 6), ("hatchback", 6), ("coupe", 6),
        ("cabriolet", 6), ("roadster", 6), ("veteranbil*", 6), ("oldtimer*", 6), ("klassisk bil", 6),
        ("amcar*", 5), ("sportsvogn*", 5), ("sportsbil*", 6), ("pickup", 4), ("suv", 5),
        ("kombi", 3), ("minivan", 5), ("campingbil*", 4), ("autocamper*", 4), ("cars", 6), ("car", 6),
        ("volvo 240", 6), ("volvo 740", 6), ("volvo 940", 6), ("volvo 850", 6), ("volvo v70", 6),
        ("volvo s80", 6), ("volvo 144", 6), ("volvo 164", 6), ("amazon", 3), ("pv544", 6),
        ("p1800", 6), ("saab 900", 6), ("saab 99", 6), ("saab 9000", 6), ("opel", 2), ("ford", 1), ("mercedes", 0), ("bmw", 4), ("audi", 4), ("toyota", 2),
        ("fiat", 1), ("peugeot", 4), ("renault", 1), ("citroen", 4), ("skoda", 1),
        ("mazda", 2), ("honda", 2), ("nissan", 2), ("hyundai", 1), ("kia", 1), ("porsche", 5),
        ("ferrari", 5), ("lamborghini", 5), ("jaguar", 2), ("mini cooper", 5), ("trabant", 6), ("alfa romeo", 5), ("vauxhall", 3), ("lancia", 4), ("land rover", 4), ("jeep", 4), ("daihatsu", 3), ("subaru", 4), ("suzuki", 3), ("lexus", 5),
        ("wartburg", 6), ("lada", 5), ("mustang", 5), ("corvette", 6), ("cadillac", 5),
        ("chevrolet", 1), ("dodge", 1), ("buick", 5), ("chrysler", 1), ("tesla", 5),
        ("bilforhandler*", 5), ("bilmoede", 6), ("bilstaevne", 6), ("bilmesse", 6),
        ("veterantraef", 4), ("bilmuseum*", 5), ("rally", 3), ("racerbil*", 5), ("formel", 3),
        ("taxa", 5), ("taxi", 5), ("politibil*", 5), ("politi", 2), ("ladvogn", 1),
    ],
}

# Ord der er for tvetydige til at vaere nyttige alene - bevidst IKKE med paa
# listen: volvo, scania, man, mercedes (kun 1 point), daf, iveco, ...
AMBIGUOUS_BRANDS = [("volvo", 1), ("scania", 1), ("man", 0), ("daf", 1), ("iveco", 1), ("mercedes", 0)]

def _compile(patterns):
    out = []
    for word, pts in patterns:
        w = re.escape(word.lower())
        if word.endswith("*"):
            w = re.escape(word[:-1].lower()) + r"\w*"
        # ordgraense: ikke bogstav/ciffer foer og efter
        out.append((re.compile(r"(?<![\w])" + w + r"(?![\w])", re.IGNORECASE), word, pts))
    return out


_COMPILED = {cat: _compile(items) for cat, items in RULES.items()}
_AMBIG = _compile(AMBIGUOUS_BRANDS)


def _norm(text):
    # 'VolvoB10M' -> 'Volvo B10M', 'Scania143' -> 'Scania 143'
    text = re.sub(r"(?<=[a-zæøåäöü])(?=[A-Z0-9])", " ", text or "").lower()
    for a, b in (("æ", "ae"), ("ø", "oe"), ("å", "aa"), ("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("é", "e")):
        text = text.replace(a, b)
    # '_' og '-' bliver mellemrum, saa 'volvo_b10m' og 'b10m-bus' kan matches
    return re.sub(r"[_\-/.,;:()\[\]]+", " ", text)


def score_text(text):
    """Point pr. kategori for en tekst. Hvert ord taeller kun een gang."""
    t = _norm(text)
    scores = {c: 0.0 for c in RULES}
    hits = {c: [] for c in RULES}
    if not t.strip():
        return scores, hits
    for cat, items in _COMPILED.items():
        for rx, word, pts in items:
            if rx.search(t):
                scores[cat] += pts
                hits[cat].append(word)
    # tvetydige maerker: lidt point til alle de typer de bruges paa
    for rx, word, pts in _AMBIG:
        if pts and rx.search(t):
            scores["bus"] += pts
            scores["truck"] += pts
    return scores, hits


# ---------------------------------------------------------------------------
# Afgoerelsen
#
# Din fars egne albums er det staerkeste signal ("Trams", "Cars in Denmark 2018",
# "Volvo B10M" ...). Et billede ligger ofte i flere albums, og ALLE tages med.
# Land- og aarsalbums ("Denmark 2010's", "Portugal") er blandede og stemmer ikke.
# Tagget 'bus' staar paa ca. 70 % af billederne - ogsaa paa sporvogne, tog og
# lastbiler - saa det tæller kun lidt og kan overstyres af album og titel.
# ---------------------------------------------------------------------------
import csv
import os

TITLE_WEIGHT = 1.5       # ord i titlen
TAG_WEIGHT = 1.0         # oevrige ord i tags
DESC_WEIGHT = 0.6        # beskrivelser er ofte lange og stoejende
STRONG_ALBUM = 6.0       # album med tydeligt emne (Trams, Cars in ..., Volvo B10M)
MEDIUM_ALBUM = 5.0       # album med overvejende ét emne, men ogsaa andet (Sporvejsmuseet: sporvogne + DAB-busser)
WEAK_ALBUM = 3.0         # maerkealbum (Toyota, Mercedes, Scania ...) - kan indeholde flere typer
MIN_SCORE = 3.0          # under dette -> misc (et enkelt tag som "bus" er nok, hvis intet andet modsiger)
MIN_LEAD = 1.5           # foerste skal vaere mindst 1,5 point foran nr. 2

# Generiske tags. Din far har 'bus' paa trams/tog/lastbiler, saa kun 3 point.
GENERIC_TAGS = {"bus": ("bus", 3), "buses": ("bus", 3), "car": ("car", 3), "cars": ("car", 3),
                "carspotting": ("car", 2), "truck": ("truck", 5), "trucks": ("truck", 5),
                "tram": ("tram", 5), "trams": ("tram", 5), "train": ("train", 5), "trains": ("train", 5)}

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "album_categories.csv")

_BUS_ALBUM = re.compile(
    r"\bbus(es|dealer|bodybuilder)?\b|scrapbus|trolleybus|\bvolvo b\d|\bvolvo [79]\d00\b|citaro|\bo ?[34]0\d\b|"
    r"^(solaris|ikarus|ikarbus|vdl|dab|irisbus)\b|bodybuilder|preserved buses", re.I)
_CAR_MODEL_ALBUM = re.compile(                       # model-albums: altid biler
    r"(^|\b)(new |dutch )?volvo (v\d|s\d|xc|c\d|\d{3}|pv|40|amazon|66)", re.I)
_CAR_BRAND_ALBUM = re.compile(                       # maerkealbums: kan ogsaa have busser/lastbiler
    r"^(toyota|volkswagen|bmw|audi|saab|opel|datsun|citroen|peugeot|renault|chevrolet|mazda|honda|skoda|"
    r"mitsubishi|subaru|porsche|suzuki|seat|proton|perodua|yugo|volga|moskvich|uaz|zaz|lada|isuzu|chrysler|"
    r"ford|us cars)", re.I)


def guess_album_category(title):
    """(kategori eller None, styrke). None = blandet album der ikke stemmer."""
    t = _norm(title)
    has = lambda w: re.search(r"\b" + w + r"\b", t)
    if has("trucks?") and has("bus\\w*"):
        return None, 0                                   # fx 'Staaling Truck/Busdealer = Scrapyard'
    if "tramway museum" in t:
        return "tram", MEDIUM_ALBUM                      # mest sporvogne, men ogsaa DAB-busser (de har selv et busalbum)
    if "museum" in t and "car museum" not in t:
        return None, 0
    if has("trams?"):
        return "tram", STRONG_ALBUM
    if has("trains?"):
        return "train", STRONG_ALBUM
    if has("trucks?"):
        return "truck", STRONG_ALBUM
    if _BUS_ALBUM.search(t):
        return "bus", STRONG_ALBUM
    if re.search(r"\bcars?\b", t):
        return "car", STRONG_ALBUM
    if _CAR_MODEL_ALBUM.search(t):
        return "car", STRONG_ALBUM
    if _CAR_BRAND_ALBUM.search(t):
        return "car", WEAK_ALBUM                         # maerkealbum: svagere stemme
    return None, 0


_overrides = None


def album_overrides():
    """tools/album_categories.csv:  album;category;strength   (category: bus car truck tram train mixed)."""
    global _overrides
    if _overrides is None:
        _overrides = {}
        if os.path.exists(CSV_PATH):
            with open(CSV_PATH, encoding="utf-8", newline="") as fh:
                for row in csv.reader(fh, delimiter=";"):
                    if len(row) >= 2 and row[0] and not row[0].startswith("#") and row[0] != "album":
                        cat = row[1].strip().lower()
                        word = row[2].strip().lower() if len(row) > 2 else "strong"
                        strength = {"weak": WEAK_ALBUM, "medium": MEDIUM_ALBUM}.get(word, STRONG_ALBUM)
                        _overrides[row[0].strip().lower()] = (None if cat in ("mixed", "") else cat, strength)
    return _overrides


def album_category(title):
    ov = album_overrides().get((title or "").strip().lower())
    return ov if ov else guess_album_category(title)


def _best(scores):
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (c1, s1), (c2, s2) = ranked[0], ranked[1]
    return c1, s1, s1 - s2


def classify(albums, title, tags, description):
    """albums: liste med ALLE albumnavne billedet ligger i (en enkelt tekst virker ogsaa).
    Returnerer (kategori, begrundelse); begrundelsen gemmes, saa du kan se hvorfor."""
    if isinstance(albums, str):
        albums = [albums] if albums else []
    total = {c: 0.0 for c in RULES}
    why = []

    # 1) albums: hver kategori faar sin staerkeste stemme (flere ens albums tæller ikke dobbelt)
    votes = {}
    for a in albums:
        cat, strength = album_category(a)
        if cat in total and strength > votes.get(cat, (0, ""))[0]:
            votes[cat] = (strength, a)
    for cat, (strength, a) in votes.items():
        total[cat] += strength
        why.append(f"album {a[:25]!r}={cat}")

    # 2) generiske tags (bus, car, tram ...) faar faste, lave point
    tag_words = [w for w in re.split(r"[,\s]+", (tags or "").lower()) if w]
    seen = set()
    for w in tag_words:
        if w in GENERIC_TAGS and w not in seen:
            seen.add(w)
            cat, pts = GENERIC_TAGS[w]
            total[cat] += pts
    # Din far kopierer ofte titlen ind som tag - et ord skal ikke taelle to gange.
    title_norm = _norm(title)
    pieces = [p.strip().strip(",").strip() for p in re.split(r"(?<=,)\s*", tags or "")]
    rest = " ".join(p for p in pieces
                    if p and p.lower() not in GENERIC_TAGS and _norm(p).strip() not in title_norm)

    # 3) titel, oevrige tags og beskrivelse
    t_sc, t_hits = score_text(title)
    g_sc, g_hits = score_text(rest)
    d_sc, d_hits = score_text(description)
    for c in total:
        total[c] += t_sc[c] * TITLE_WEIGHT + g_sc[c] * TAG_WEIGHT + d_sc[c] * DESC_WEIGHT
    words = []
    for c, _, _ in [(c, 0, 0) for c in total]:
        words += [f"{w}→{c}" for w in (t_hits[c] + g_hits[c])[:2]]

    cat, best, lead = _best(total)
    detail = "; ".join(why + words[:4] + ([f"tags: {','.join(sorted(seen))}"] if seen else []))
    if best < MIN_SCORE or lead < MIN_LEAD:
        return "misc", f"uklart ({cat} {best:.1f}, forspring {lead:.1f}) {detail}"[:240]
    return cat, f"{detail}"[:240]
