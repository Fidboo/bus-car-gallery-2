"""Regelmotor: gaet kategori (bus / car / truck / tram / train / misc) ud fra
albumnavn, titel, tags og beskrivelse.

Principper
- Albumnavnet vejer tungest (en fotograf navngiver typisk et album efter emnet).
- Tydelige ord (b10m, sporvogn, lokomotiv ...) giver mange point.
- Maerker der bruges til flere typer koeretoejer (volvo, scania, man ...) giver
  kun 1 point, saa de aldrig alene afgoer noget.
- For lidt eller uafgjort evidens giver 'misc' (hellere misc end forkert).

Tilpas ordlisterne nedenfor. Efter en aendring koeres:
    python tools/import_flickr.py reclassify
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
        ("ruter", 2), ("skyss", 2), ("busslinje", 5), ("autobus", 5), ("coach", 4),
        ("stadsbuss*", 5), ("linjebuss*", 5),
    ],
    "tram": [
        ("sporvogn*", 7), ("tram", 7), ("trams", 7), ("trikk", 7), ("sparvagn*", 7),
        ("letbane*", 6), ("light rail", 6), ("lrt", 4), ("strassenbahn", 7), ("tramway", 7),
        ("tatra", 4), ("gt6", 5), ("gt8", 5), ("gt6n", 6), ("combino", 7), ("flexity", 7),
        ("sporvej*", 6), ("sporvejsmuseum*", 7), ("aarhus letbane", 7), ("odense letbane", 7),
        ("tw6000", 6), ("m-wagen", 5), ("tram line", 6),
    ],
    "train": [
        ("tog", 6), ("toge", 6), ("dsb", 6), ("s-tog", 7), ("stog", 6), ("lokomotiv*", 7),
        ("lok", 4), ("lokotog", 5), ("jernbane*", 6), ("banegaard*", 5), ("station", 2), ("stationer", 2),
        ("perron*", 4), ("skinnebus*", 6), ("motorvogn*", 4), ("togsaet", 6), ("ic3", 7),
        ("ic4", 7), ("re-tog", 6), ("regionaltog", 7), ("intercity", 6), ("metro", 5),
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
        ("scania t", 3), ("scania g", 3), ("streamline", 4), ("scania 143", 6), ("scania 113", 6),
        ("scania 142", 6), ("scania 112", 6), ("scania 141", 6), ("scania 111", 6),
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
        ("p1800", 6), ("saab 900", 6), ("saab 99", 6), ("saab 9000", 6), ("opel", 3), ("ford", 2), ("mercedes", 1), ("bmw", 4), ("audi", 4), ("toyota", 4),
        ("fiat", 3), ("peugeot", 4), ("renault", 4), ("citroen", 4), ("skoda", 4),
        ("mazda", 4), ("honda", 3), ("nissan", 4), ("hyundai", 4), ("kia", 3), ("porsche", 5),
        ("ferrari", 5), ("lamborghini", 5), ("jaguar", 4), ("mini cooper", 5), ("trabant", 6),
        ("wartburg", 6), ("lada", 5), ("mustang", 5), ("corvette", 6), ("cadillac", 5),
        ("chevrolet", 3), ("dodge", 3), ("buick", 5), ("chrysler", 4), ("tesla", 5),
        ("bilforhandler*", 5), ("bilmoede", 6), ("bilstaevne", 6), ("bilmesse", 6),
        ("veterantraef", 4), ("bilmuseum*", 5), ("rally", 3), ("racerbil*", 5), ("formel", 3),
        ("taxa", 5), ("taxi", 5), ("politibil*", 5), ("politi", 2), ("ladvogn", 1),
    ],
}

# Ord der er for tvetydige til at vaere nyttige alene - bevidst IKKE med paa
# listen: volvo, scania, man, mercedes (kun 1 point), daf, iveco, ...
AMBIGUOUS_BRANDS = [("volvo", 1), ("scania", 1), ("man", 0), ("daf", 1), ("iveco", 1), ("mercedes", 1)]

ALBUM_WEIGHT = 2.0       # albumnavnet taeller dobbelt
TITLE_WEIGHT = 1.5       # titel/tags taeller 1.5
DESC_WEIGHT = 0.6        # beskrivelser er ofte lange og stoejende
MIN_SCORE = 4.0          # under dette -> misc
MIN_LEAD = 2.0           # foerste skal vaere mindst 2 point foran nr. 2
STRONG = 8.0             # et enkelt felt med score >= STRONG kan overstyre albummet


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


def _best(scores):
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (c1, s1), (c2, s2) = ranked[0], ranked[1]
    return c1, s1, s1 - s2


def classify(album_title, title, tags, description):
    """Returnerer (kategori, begrundelse). Begrundelsen gemmes i databasen,
    saa du kan se hvorfor et billede endte hvor det gjorde."""
    a_sc, a_hits = score_text(album_title)
    t_sc, t_hits = score_text(title)
    g_sc, g_hits = score_text(tags)
    d_sc, d_hits = score_text(description)

    total = {c: a_sc[c] * ALBUM_WEIGHT + (t_sc[c] + g_sc[c]) * TITLE_WEIGHT + d_sc[c] * DESC_WEIGHT
             for c in RULES}

    a_cat, a_best, a_lead = _best(a_sc)
    album_decides = a_best >= 3 and a_lead >= 2

    # Albummet er udgangspunktet, medmindre selve billedets titel/tags
    # tydeligt peger paa noget andet.
    if album_decides:
        own = {c: t_sc[c] + g_sc[c] for c in RULES}
        o_cat, o_best, o_lead = _best(own)
        if o_cat != a_cat and o_best >= STRONG and o_lead >= MIN_LEAD:
            hit = (t_hits[o_cat] + g_hits[o_cat])[:3]
            return o_cat, f"billede overstyrer album ({a_cat}): {', '.join(hit)}"
        return a_cat, f"album: {', '.join(a_hits[a_cat][:3])}"

    cat, best, lead = _best(total)
    if best < MIN_SCORE or lead < MIN_LEAD:
        return "misc", f"uklart (bedste {cat} {best:.1f}, forspring {lead:.1f})"
    hit = (t_hits[cat] + g_hits[cat] + d_hits[cat] + a_hits[cat])[:3]
    return cat, f"ord: {', '.join(hit)}"
