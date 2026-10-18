"""Category rules (tools/flickr_rules.py), tested with real photos from the gallery.

Each case is something that went wrong with the first version of the rules, or
something that must keep working. album names, titles and tags are the real ones.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import flickr_rules as R  # noqa: E402

CASES = [
    # buses that used to end up as cars because of brand names (Renault, Hyundai, Skoda ...)
    (["Misc. Bus brands", "Australia"], "1990' Renault PR 100.2 Ansair", "bus,australia,renault,pr100", "bus"),
    (["Misc. Bus brands", "Lithuania"], "1996' Hyundai Aero 600", "hyundai,aero,600,bus,lithuania", "bus"),
    (["Trolleybuses", "Lithuania"], "Skoda 14", "skoda,trolleybus,vilnius,14,bus", "bus"),
    # DAB buses at the tramway museum used to become trams ("Tramway" in tags and album)
    (["DAB", "Danish Tramway Museum", "Denmark 2010's"], "1968' Leyland LIDRT 12/4 DAB Series 2",
     "leyland,dab,danish,tramway,museum,copenhagen", "bus"),
    (["Preserved buses", "Danish Tramway Museum"], "1961' Volvo B655-06 Nordisk", "sporvejsmuseet,tramway,museum", "bus"),
    # the tag 'bus' is on trams, trains, trucks and cars too: the album decides
    (["Trams", "Latvia"], "RVZ-6", "rvz-6,latvia,bus", "tram"),
    (["Trains"], "Adelaide train", "bus,australia", "train"),
    (["Trains", "Singapore"], "Singapore Light Rail Transit", "singapore,light,rail,transit,bus", "train"),
    (["Trucks"], "AEC", "aec,truck,bus,new zealand,trucks", "truck"),
    (["Cars in New Zealand"], "Morris Minor", "morris,minor,bus,new zealand,car,cars", "car"),
    # the tramway museum album never votes; the buses there have their own albums
    (["DAB", "Danish Tramway Museum"], "1983' Leyland 7-1-TL11/8 DAB series 7", "dab,tramway,museum", "bus"),
    # a tie between your own album and ambiguous words ('bahn', 'railway') is decided by the album
    (["Germany", "Trams"], "H-Bahn 21 Dortmund", "bus,railway", "tram"),
    # ... unless the photo itself clearly says otherwise
    (["Trucks", "Australia"], "Volvo B12BLE Custom Coaches CB60", "bus,australia", "bus"),
    # Volvo model albums are always cars
    (["Volvo 240/260"], "1989' Volvo 240GL", "ns39312", "car"),
    (["New Volvo V90/S90"], "Volvo V90", "", "car"),
    (["Volvo B10M", "Denmark 2010's"], "IMG_1234", "bus,volvo,b10m", "bus"),
    # no album: words decide
    ([], "Scania R500 med saettevogn", "scania", "truck"),
    (["Denmark 2010's"], "DSB MZ ved Odense", "bus", "train"),
    (["Denmark 2010's"], "Intercity coach to Aarhus", "bus", "bus"),
    (["Denmark 2010's"], "Sporvogn linje 10", "", "tram"),
    # a title word copied into the tags must not count twice
    (["Cars in Russia", "Korean"], "Hyundai Solaris", "hyundai solaris,car,cars,russia,carspotting", "car"),
    # a single 'bus' tag and nothing contradicting it is still a bus
    (["Australia"], "2000' Hino RK250 Express", "bus,australia", "bus"),
    # a model code that is ambiguous between trucks and buses is not forced to truck
    (["Lithuania"], "1985' Scania CN112CL", "scania 112,bus,lithuania", None),
]


@pytest.mark.parametrize("albums,title,tags,expected", CASES)
def test_real_examples(albums, title, tags, expected):
    category, reason = R.classify(albums, title, tags, "")
    if expected is None:
        assert category != "truck", reason
    else:
        assert category == expected, reason


def test_ignored_album_never_votes():
    assert R.classify(["Danish Tramway Museum"], "x", "", "")[0] == "misc"


def test_nothing_to_go_on_is_misc():
    assert R.classify([], "Solnedgang", "", "")[0] == "misc"


def test_a_single_album_name_still_works():
    assert R.classify("Trams", "x", "", "")[0] == "tram"


def test_album_guesses():
    assert R.guess_album_category("Cars in Denmark 2018") == ("car", R.STRONG_ALBUM)
    assert R.guess_album_category("Volvo B10M") == ("bus", R.STRONG_ALBUM)
    assert R.guess_album_category("Toyota/Lexus/Scion") == ("car", R.WEAK_ALBUM)
    assert R.guess_album_category("Denmark 2010's")[0] is None            # country/year: mixed
    assert R.guess_album_category("Danish Tramway Museum")[0] is None     # trams AND buses
    assert R.guess_album_category("Staaling Truck/Busdealer = Scrapyard")[0] is None


def test_album_csv_is_valid_and_matches_the_guesses():
    import csv
    seen = set()
    with open(R.CSV_PATH, encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh, delimiter=";"):
            if not row or row[0].startswith("#") or row[0] == "album":
                continue
            album, category = row[0], row[1]
            assert category in ("bus", "car", "truck", "tram", "train", "mixed"), row
            assert album not in seen, f"duplicate album {album!r}"
            seen.add(album)
            if album.lower() in R.IGNORED_ALBUMS:
                continue                                  # never votes, whatever the csv says
            expected = R.guess_album_category(album)[0] or "mixed"
            assert category == expected, f"{album!r}: csv={category} guess={expected}"
    assert len(seen) > 200
