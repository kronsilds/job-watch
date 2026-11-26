"""Départements français d'une localisation (texte libre des sites ou du profil).

Sert au filtre « localisation » du scoring : « Clichy (92110) », « 92 -
Clichy », « Hauts-de-Seine » et « Île-de-France » se recoupent par
département, ce qu'une comparaison de texte ne voit pas. Référentiel
statique (départements, régions 2016, grandes villes) : aucun appel réseau.
"""

from __future__ import annotations

import re

from .contracts import fold

DEPARTEMENTS = {
    "01": "Ain", "02": "Aisne", "03": "Allier", "04": "Alpes-de-Haute-Provence",
    "05": "Hautes-Alpes", "06": "Alpes-Maritimes", "07": "Ardèche", "08": "Ardennes",
    "09": "Ariège", "10": "Aube", "11": "Aude", "12": "Aveyron", "13": "Bouches-du-Rhône",
    "14": "Calvados", "15": "Cantal", "16": "Charente", "17": "Charente-Maritime",
    "18": "Cher", "19": "Corrèze", "2A": "Corse-du-Sud", "2B": "Haute-Corse",
    "21": "Côte-d'Or", "22": "Côtes-d'Armor", "23": "Creuse", "24": "Dordogne",
    "25": "Doubs", "26": "Drôme", "27": "Eure", "28": "Eure-et-Loir", "29": "Finistère",
    "30": "Gard", "31": "Haute-Garonne", "32": "Gers", "33": "Gironde", "34": "Hérault",
    "35": "Ille-et-Vilaine", "36": "Indre", "37": "Indre-et-Loire", "38": "Isère",
    "39": "Jura", "40": "Landes", "41": "Loir-et-Cher", "42": "Loire", "43": "Haute-Loire",
    "44": "Loire-Atlantique", "45": "Loiret", "46": "Lot", "47": "Lot-et-Garonne",
    "48": "Lozère", "49": "Maine-et-Loire", "50": "Manche", "51": "Marne",
    "52": "Haute-Marne", "53": "Mayenne", "54": "Meurthe-et-Moselle", "55": "Meuse",
    "56": "Morbihan", "57": "Moselle", "58": "Nièvre", "59": "Nord", "60": "Oise",
    "61": "Orne", "62": "Pas-de-Calais", "63": "Puy-de-Dôme", "64": "Pyrénées-Atlantiques",
    "65": "Hautes-Pyrénées", "66": "Pyrénées-Orientales", "67": "Bas-Rhin", "68": "Haut-Rhin",
    "69": "Rhône", "70": "Haute-Saône", "71": "Saône-et-Loire", "72": "Sarthe",
    "73": "Savoie", "74": "Haute-Savoie", "75": "Paris", "76": "Seine-Maritime",
    "77": "Seine-et-Marne", "78": "Yvelines", "79": "Deux-Sèvres", "80": "Somme",
    "81": "Tarn", "82": "Tarn-et-Garonne", "83": "Var", "84": "Vaucluse", "85": "Vendée",
    "86": "Vienne", "87": "Haute-Vienne", "88": "Vosges", "89": "Yonne",
    "90": "Territoire de Belfort", "91": "Essonne", "92": "Hauts-de-Seine",
    "93": "Seine-Saint-Denis", "94": "Val-de-Marne", "95": "Val-d'Oise",
    "971": "Guadeloupe", "972": "Martinique", "973": "Guyane", "974": "La Réunion",
    "976": "Mayotte",
}

REGIONS = {
    "Auvergne-Rhône-Alpes": "01 03 07 15 26 38 42 43 63 69 73 74",
    "Bourgogne-Franche-Comté": "21 25 39 58 70 71 89 90",
    "Bretagne": "22 29 35 56",
    "Centre-Val de Loire": "18 28 36 37 41 45",
    "Corse": "2A 2B",
    "Grand Est": "08 10 51 52 54 55 57 67 68 88",
    "Hauts-de-France": "02 59 60 62 80",
    "Île-de-France": "75 77 78 91 92 93 94 95",
    "Normandie": "14 27 50 61 76",
    "Nouvelle-Aquitaine": "16 17 19 23 24 33 40 47 64 79 86 87",
    "Occitanie": "09 11 12 30 31 32 34 46 48 65 66 81 82",
    "Pays de la Loire": "44 49 53 72 85",
    "Provence-Alpes-Côte d'Azur": "04 05 06 13 83 84",
}

# Grandes villes et pôles d'emploi fréquents dans les annonces.
CITIES = {
    "lyon": "69", "villeurbanne": "69", "marseille": "13", "aix en provence": "13",
    "toulouse": "31", "nice": "06", "sophia antipolis": "06", "nantes": "44",
    "montpellier": "34", "strasbourg": "67", "bordeaux": "33", "merignac": "33",
    "lille": "59", "roubaix": "59", "tourcoing": "59", "villeneuve d ascq": "59",
    "rennes": "35", "saint malo": "35", "reims": "51", "toulon": "83",
    "saint etienne": "42", "le havre": "76", "rouen": "76", "grenoble": "38",
    "dijon": "21", "angers": "49", "nimes": "30", "clermont ferrand": "63",
    "le mans": "72", "brest": "29", "quimper": "29", "tours": "37", "amiens": "80",
    "limoges": "87", "annecy": "74", "perpignan": "66", "metz": "57", "besancon": "25",
    "orleans": "45", "mulhouse": "68", "colmar": "68", "caen": "14", "nancy": "54",
    "avignon": "84", "poitiers": "86", "pau": "64", "bayonne": "64", "biarritz": "64",
    "la rochelle": "17", "niort": "79", "valence": "26", "chambery": "73",
    "lorient": "56", "vannes": "56", "laval": "53", "troyes": "10", "chartres": "28",
    "ajaccio": "2A", "bastia": "2B",
    "boulogne billancourt": "92", "nanterre": "92", "courbevoie": "92", "puteaux": "92",
    "la defense": "92", "issy les moulineaux": "92", "levallois perret": "92",
    "neuilly sur seine": "92", "clichy": "92", "rueil malmaison": "92", "montrouge": "92",
    "suresnes": "92", "colombes": "92", "saint denis": "93", "saint ouen": "93",
    "montreuil": "93", "pantin": "93", "noisy le grand": "93", "creteil": "94",
    "ivry sur seine": "94", "vincennes": "94", "versailles": "78",
    "saint quentin en yvelines": "78", "massy": "91", "evry": "91", "saclay": "91",
    "palaiseau": "91", "cergy": "95", "argenteuil": "95", "marne la vallee": "77",
    "melun": "77",
}

_NAMES = {fold(name).replace("-", " ").replace("'", " "): code
          for code, name in DEPARTEMENTS.items()}
_REGION_CODES = {fold(name).replace("-", " ").replace("'", " "): codes.split()
                 for name, codes in REGIONS.items()}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-'’/]", " ", fold(text))).strip()


def _from_postal(code: str) -> str:
    if code.startswith("97"):
        return code[:3]
    if code.startswith("20"):
        return "2A" if int(code) < 20200 else "2B"
    return code[:2]


def departements(location) -> set[str]:
    """Départements désignés par une localisation (vide si inconnue/étrangère).

    Codes postaux (« 92110 »), numéros isolés (« (92) », « 92 - Clichy »,
    « Paris 02 - 75 »), noms de départements, de régions (→ tous leurs
    départements) et de grandes villes.
    """
    text = str(location or "")
    found: set[str] = set()
    for m in re.finditer(r"(?<!\d)(\d{5})(?!\d)", text):
        found.add(_from_postal(m.group(1)))
    for m in re.finditer(r"\((\d{2,3}|2[AB])\)", text, re.I):
        found.add(m.group(1).upper())
    for part in re.split(r"\s+-\s+|,|/", text):
        part = part.strip()
        if re.fullmatch(r"\d{2}|2[AB]|97\d", part, re.I):
            found.add(part.upper())
    if found:
        return {c for c in found if c in DEPARTEMENTS}
    for part in [text] + re.split(r"\s+-\s+|,|\(|\)", text):
        key = _clean(part)
        if not key:
            continue
        if key in _NAMES:
            found.add(_NAMES[key])
        elif key in _REGION_CODES:
            found.update(_REGION_CODES[key])
        elif key in CITIES:
            found.add(CITIES[key])
    return found


_ABROAD = re.compile(
    r"\b(?:remote us|united states|usa|u\.s\.a?|etats unis|united kingdom|royaume uni|england|"
    r"uk|germany|deutschland|allemagne|spain|espana|espagne|italy|italia|italie|"
    r"netherlands|nederland|pays bas|portugal|ireland|irlande|poland|pologne|canada|"
    r"india|inde|singapore|singapour|japan|japon|australia|australie|brazil|bresil|"
    r"mexico|mexique|switzerland|suisse|schweiz|belgium|belgique|luxembourg|"
    r"sweden|suede|sverige|denmark|danemark|norway|norvege|finland|austria|autriche|"
    r"czech|romania|roumanie|greece|grece|israel|dubai|emirates|morocco|maroc|"
    r"tunisia|tunisie|algeria|algerie|china|chine|hong kong|korea|coree|"
    r"london|londres|berlin|munich|hamburg|madrid|barcelona|barcelone|lisbon|lisbonne|"
    r"amsterdam|dublin|milan|rome|brussels|bruxelles|geneva|geneve|zurich|"
    r"stockholm|copenhagen|oslo|warsaw|varsovie|prague|vienna|vienne autriche|"
    r"new york|san francisco|boston|seattle|austin|chicago|los angeles|dallas|"
    r"toronto|montreal|vancouver|sydney|bangalore|tokyo)\b")
# « Dallas, TX » : code d'État américain après une virgule.
_US_STATE = re.compile(
    r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|"
    r"MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b")


def is_abroad(location) -> bool:
    """Vrai si la localisation désigne explicitement un lieu hors de France."""
    text = str(location or "")
    if not text.strip():
        return False
    return bool(_US_STATE.search(text) or _ABROAD.search(_clean(text)))


_REMOTE = re.compile(r"\b(?:remote|teletravail|full remote|anywhere|partout)\b")


def in_france(location) -> bool:
    """Lieu français reconnu, télétravail sans pays étranger, ou lieu non précisé."""
    text = str(location or "")
    if not text.strip():
        return True
    if departements(text) or re.search(r"\bfrance\b", _clean(text)):
        return True
    return bool(_REMOTE.search(_clean(text))) and not is_abroad(text)


def compatible(profile_locations, offer_location) -> bool | None:
    """True si un département est commun, False si aucun, None si indéterminable."""
    wanted: set[str] = set()
    for loc in profile_locations or []:
        wanted |= departements(loc)
    have = departements(offer_location)
    if not wanted or not have:
        return None
    return bool(wanted & have)
