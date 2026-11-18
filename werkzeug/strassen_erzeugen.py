#!/usr/bin/env python3
"""Erzeugt die Straßenverzeichnisse aller aktiven Kommunen aus OpenStreetMap.

Läuft automatisch jede Nacht (GitHub Action „Straßenverzeichnisse“) und kann dort
auch von Hand gestartet werden. Eine neu angelegte Kommune bekommt so ohne weiteres
Zutun ihr Verzeichnis; bis dahin suchen App und Formular wie bisher online.

Aufruf:  python3 werkzeug/strassen_erzeugen.py            (alle aktiven, nur fehlende/veraltete)
         python3 werkzeug/strassen_erzeugen.py --alle     (alle neu)
         python3 werkzeug/strassen_erzeugen.py "Stadt Kempten"

Ergebnis: strassen/<ort>.json, z. B. strassen/mindelheim.json – App, Web-Portal und
Meldeformular suchen darin: sofort, und nur Straßen, die es in der Kommune gibt.

Gebiet: die Verwaltungsgrenze aus OpenStreetMap, die den Mittelpunkt des in der
Datenbank hinterlegten Gemeindegebiets enthält und deren Name zum Kommunennamen passt
(„Kempten“ findet auch „Kempten (Allgäu)“). Nur öffentliche Daten, kein geheimer Schlüssel.
"""
import json, math, re, subprocess, sys, time, unicodedata
from datetime import date, datetime
from pathlib import Path

SUPABASE = "https://jljduusvfmibzpirntbi.supabase.co"
OEFFENTLICHER_SCHLUESSEL = "sb_publishable__sFY04WWa-gVpLFyKD5ceg_NiRjIn0h"
WURZEL = Path(__file__).resolve().parents[1]
ZIEL = WURZEL / "strassen"
GUELTIG_TAGE = 30
SERVER = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
AUSGESCHLOSSEN = ("footway|cycleway|path|steps|track|bridleway|construction|proposed|platform|corridor|elevator|"
                  "raceway|abandoned|razed|motorway|motorway_link|trunk_link|bus_guideway|busway")


def oeffentlicher_schluessel() -> str:
    """Öffentlicher Schlüssel (steht ohnehin in jedem Meldeformular, kein Geheimnis)."""
    return OEFFENTLICHER_SCHLUESSEL


def curl(argumente, zeitlimit=300) -> str:
    lauf = subprocess.run(["curl", "-sS", "--fail", "-m", str(zeitlimit), *argumente], capture_output=True, text=True)
    if lauf.returncode != 0:
        raise RuntimeError(lauf.stderr.strip() or f"curl {lauf.returncode}")
    return lauf.stdout


def supabase(pfad, daten=None):
    schluessel = oeffentlicher_schluessel()
    kopf = ["-H", f"apikey: {schluessel}", "-H", "Content-Type: application/json"]
    if daten is None:
        return json.loads(curl([*kopf, f"{SUPABASE}{pfad}"], 30))
    return json.loads(curl([*kopf, "-X", "POST", "-d", json.dumps(daten), f"{SUPABASE}{pfad}"], 30))


def ort_name(kommune: str) -> str:
    return re.sub(r"^(stadt|markt|gemeinde)\s+", "", kommune.strip(), flags=re.I)


def dateiname(kommune: str) -> str:
    """Muss zu verzeichnisName() in App und Formular passen."""
    t = ort_name(kommune).lower().replace("ß", "ss").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", t))


def overpass(q: str) -> dict:
    letzter = None
    for runde in range(4):
        for url in SERVER:
            try:
                daten = json.loads(curl(["-A", "Laternen-Ausfall.de/1.0 (Strassenverzeichnis)",
                                         "--data-urlencode", f"data={q}", url]))
                hinweis = daten.get("remark", "")
                if re.search("error|timed out|out of memory", hinweis, re.I):
                    raise RuntimeError(hinweis)
                return daten
            except Exception as f:  # noqa: BLE001
                letzter = f
                print(f"  {url}: {f}", file=sys.stderr)
        time.sleep(20 * (runde + 1))
    raise RuntimeError(f"Overpass nicht erreichbar: {letzter}")


def doppelte_entfernen(eintraege):
    """Lange Straßen können in mehrere Gruppen zerfallen: je Name und Ortsteil nur ein Eintrag."""
    gesehen, ergebnis = set(), []
    for e in eintraege:
        if (e["n"], e["o"]) not in gesehen:
            gesehen.add((e["n"], e["o"]))
            ergebnis.append(e)
    return ergebnis


def meter(a, b):
    x = (b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2)) * 111320
    y = (b[0] - a[0]) * 111320
    return math.hypot(x, y)


def abfrage(ort: str, mitte) -> str:
    # Sonderzeichen als Zeichenklasse statt mit Backslash (Overpass-Zeichenketten kennen kaum Escapes).
    name = "".join(f"[{c}]" if c in ".^$*+?()[]{}|-\\" else c for c in ort.replace('"', ""))
    return f"""[out:json][timeout:180];
is_in({mitte[0]},{mitte[1]})->.alle;
area.alle["boundary"="administrative"]["admin_level"~"^(6|7|8)$"]["name"~"^{name}( [(]|$)"]->.a;
(way["highway"]["name"]["highway"!~"^({AUSGESCHLOSSEN})$"](area.a););
out tags geom;
node["place"~"^(town|city|village|hamlet|suburb|quarter|isolated_dwelling)$"](area.a);
out;
nwr["addr:housenumber"]["addr:street"](area.a);
out tags center;"""


def erzeugen(kommune: str, gebiet: dict):
    ort = ort_name(kommune)
    mitte = ((gebiet["min_lat"] + gebiet["max_lat"]) / 2, (gebiet["min_lon"] + gebiet["max_lon"]) / 2)
    # Ein überlasteter Server liefert manchmal eine leere Antwort ohne Fehlermeldung – dann neu fragen.
    for versuch in range(3):
        daten = overpass(abfrage(ort, mitte))
        if any(e["type"] == "way" for e in daten["elements"]):
            break
        print(f"  {ort}: leere Antwort, neuer Versuch", file=sys.stderr)
        time.sleep(30)
    else:
        raise RuntimeError(f"Keine Verwaltungsgrenze „{ort}“ am Gemeindemittelpunkt gefunden")
    orte = [(e["tags"]["name"], (e["lat"], e["lon"]), e["tags"]["place"]) for e in daten["elements"]
            if e["type"] == "node" and "place" in e.get("tags", {}) and "name" in e["tags"] and "lat" in e]
    wege = {}
    for e in daten["elements"]:
        if e["type"] != "way" or len(e.get("geometry", [])) < 2:
            continue
        wege.setdefault(e["tags"]["name"].strip(), []).append([(p["lat"], p["lon"]) for p in e["geometry"]])

    def ortsteil(p):
        # Hauptort hat Vorrang: er ist groß, sein Mittelpunkt liegt weit von Randstraßen.
        bester, wert = ort, float("inf")
        for name, q, art in orte:
            d = meter(p, q) / (3.5 if art in ("town", "city") else 1)
            if d < wert:
                bester, wert = name, d
        return bester

    eintraege = []
    for name, teile in wege.items():
        gruppen = []  # Abschnitte, die höchstens 600 m auseinanderliegen
        for t in teile:
            mittelpunkt = t[len(t) // 2]
            for g in gruppen:
                if min(meter(mittelpunkt, q) for s in g for q in s[:: max(1, len(s) // 6)]) < 600:
                    g.append(t)
                    break
            else:
                gruppen.append([t])
        for g in gruppen:
            punkte = [p for s in g for p in s]
            schwer = (sum(p[0] for p in punkte) / len(punkte), sum(p[1] for p in punkte) / len(punkte))
            auf = min(punkte, key=lambda p: meter(p, schwer))  # Punkt auf der Straße nahe der Mitte
            eintraege.append({"n": name, "o": ortsteil(auf), "lat": round(auf[0], 6), "lon": round(auf[1], 6)})

    eintraege = doppelte_entfernen(eintraege)
    hausnummern_zuordnen(eintraege, daten["elements"])
    eintraege.sort(key=lambda e: (e["n"].lower(), e["o"]))
    inhalt = {"kommune": kommune, "ort": ort, "stand": date.today().isoformat(),
              "quelle": "© OpenStreetMap-Mitwirkende (ODbL)", "strassen": eintraege}
    ZIEL.mkdir(exist_ok=True)
    (ZIEL / f"{dateiname(kommune)}.json").write_text(json.dumps(inhalt, ensure_ascii=False, separators=(",", ":")),
                                                     encoding="utf-8")
    print(f"{kommune}: {len(eintraege)} Straßen")


def hausnummer_text(nummer: str) -> str:
    """„38 a“ → „38a“, Leerzeichen am Rand weg."""
    return re.sub(r"(\d)\s+([a-zA-Z])$", r"\1\2", nummer.strip())


def namens_schluessel(name: str) -> str:
    """„Bahnhofstr.“, „Bahnhofstrasse“ und „Bahnhof-Straße“ gelten als dieselbe Straße."""
    t = name.strip().lower().replace("ß", "ss")
    t = re.sub(r"str\.?(?=\s|$)", "strasse", t)
    return re.sub(r"[\s\-]+", " ", t).replace("strasse", " strasse").replace("  ", " ").strip()


def hausnummern_zuordnen(eintraege, elemente):
    """Hängt jedem Straßeneintrag seine Hausnummern mit Position an: "h": [["12", lat, lon], …].
    Gibt es den Straßennamen mehrfach (verschiedene Ortsteile), gilt der nächstgelegene Eintrag."""
    nach_name = {}
    for e in eintraege:
        nach_name.setdefault(namens_schluessel(e["n"]), []).append(e)
    gesehen = set()
    anzahl = 0
    for el in elemente:
        tags = el.get("tags", {})
        if "addr:housenumber" not in tags or "addr:street" not in tags:
            continue
        lat, lon = (el["lat"], el["lon"]) if "lat" in el else (el.get("center", {}).get("lat"), el.get("center", {}).get("lon"))
        if lat is None:
            continue
        kandidaten = nach_name.get(namens_schluessel(tags["addr:street"]))
        if not kandidaten:
            continue
        ziel = min(kandidaten, key=lambda e: meter((lat, lon), (e["lat"], e["lon"])))
        for teil in re.split(r"[;,]", tags["addr:housenumber"]):
            nummer = hausnummer_text(teil)
            if not nummer or len(nummer) > 12:
                continue
            schluessel = (id(ziel), nummer.lower().replace(" ", ""))
            if schluessel in gesehen:
                continue
            gesehen.add(schluessel)
            ziel.setdefault("h", []).append([nummer, round(lat, 5), round(lon, 5)])
            anzahl += 1

    def natuerlich(h):
        m = re.match(r"\d+", h[0])
        return (int(m.group()) if m else 10**6, h[0].lower())

    for e in eintraege:
        if "h" in e:
            e["h"].sort(key=natuerlich)
    print(f"  {anzahl} Hausnummern zugeordnet")


def gebiet_von(kommune: dict) -> dict:
    """Gemeindegebiet aus der Datenbank; ist (noch) keine Grenze hinterlegt, der Ort laut Adresssuche."""
    gebiet = supabase("/rest/v1/rpc/get_municipality_search_area", {"p_id": kommune["id"]})
    if gebiet:
        return gebiet
    from urllib.parse import quote
    treffer = json.loads(curl(["-A", "Laternen-Ausfall.de/1.0 (Strassenverzeichnis)",
                               "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&countrycodes=de"
                               f"&featureType=settlement&q={quote(ort_name(kommune['name']))}"], 30))
    if not treffer:
        raise RuntimeError("Weder Gemeindegrenze noch Ort gefunden")
    lat, lon = float(treffer[0]["lat"]), float(treffer[0]["lon"])
    return {"min_lat": lat, "max_lat": lat, "min_lon": lon, "max_lon": lon}


def veraltet(kommune: str) -> bool:
    datei = ZIEL / f"{dateiname(kommune)}.json"
    if not datei.exists():
        return True
    try:
        inhalt = json.loads(datei.read_text(encoding="utf-8"))
        # Ältere Fassung ohne Hausnummern sofort erneuern.
        if not any("h" in e for e in inhalt.get("strassen", [])):
            return True
        stand = datetime.fromisoformat(inhalt["stand"]).date()
        return (date.today() - stand).days >= GUELTIG_TAGE
    except Exception:  # noqa: BLE001
        return True


def main(argumente):
    alle = "--alle" in argumente
    namen = [a for a in argumente if not a.startswith("--")]
    kommunen = supabase("/rest/v1/municipalities?select=id,name&is_active=eq.true&order=name")
    if namen:
        kommunen = [k for k in kommunen if k["name"] in namen or ort_name(k["name"]) in namen]
    fehler = 0
    for k in kommunen:
        if not alle and not namen and not veraltet(k["name"]):
            print(f"{k['name']}: aktuell")
            continue
        try:
            erzeugen(k["name"], gebiet_von(k))
        except Exception as f:  # noqa: BLE001 – eine Kommune darf die anderen nicht aufhalten
            fehler += 1
            print(f"{k['name']}: FEHLER {f}", file=sys.stderr)
    return 1 if fehler and fehler == len(kommunen) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
