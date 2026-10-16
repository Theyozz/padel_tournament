#!/usr/bin/env python3
"""
Agrégateur de tournois de padel amateur autour de Lyon (usage personnel).

Sources (vérifiées le 16/10/2026) :
  - Ten'Up (FFT)  : API JSON publique (liste + fiche détaillée de chaque tournoi candidat).
  - Playtomic     : pas d'API publique (api.playtomic.io n'existe plus). Les clubs sont trouvés via
                    la recherche du site, puis la page "Tournaments" de chaque club est lue (HTML rendu
                    côté serveur + flux Next.js pour les liens).
  - 4PADEL        : API JSON api2-front.lefive.fr utilisée par www.4padel.fr/tournois. Elle ne publie
                    aucun tournoi à ce jour ; si elle en renvoie, la page est lue avec Playwright.

Voir README.md pour l'installation et les options.
"""
import argparse
import csv
import html as htmllib
import json
import math
import re
import sys
import threading
import time
import unicodedata
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LYON = (45.7640, 4.8357)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
ICI = Path(__file__).resolve().parent
CACHE = ICI / ".cache"
PRIORITE = {"Ten'Up": 0, "4PADEL": 1, "Playtomic": 2}


# ── Outils ─────────────────────────────────────────────────────────────────────

def log(msg):
    print(msg, file=sys.stderr, flush=True)


def session(accept_language="fr-FR,fr;q=0.9"):
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=None, raise_on_status=False)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent": UA, "Accept-Language": accept_language})
    return s


class Cache:
    """Petit cache disque JSON {clé: {"t": timestamp, "v": valeur}} avec durée de vie."""

    def __init__(self, nom, ttl_h, actif=True):
        self.path, self.ttl, self.actif = CACHE / f"{nom}.json", ttl_h * 3600, actif
        self.lock = threading.Lock()
        try:
            self.data = json.loads(self.path.read_text("utf-8")) if actif else {}
        except (OSError, ValueError):
            self.data = {}

    def get(self, k):
        e = self.data.get(k)
        return e["v"] if e and time.time() - e["t"] < self.ttl else None

    def set(self, k, v):
        with self.lock:
            self.data[k] = {"t": time.time(), "v": v}

    def save(self):
        if not self.actif:
            return
        CACHE.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False), "utf-8")


def haversine(lat1, lng1, lat2, lng2):
    dlat, dlng = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(a))


def ajouter_mois(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    jours = [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30,
             31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, jours))


def sans_accents(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def evenement(**kw):
    """Format commun à toutes les sources."""
    e = {"debut": "", "fin": "", "heure": "", "niveaux": [], "libelle": "", "club": "",
         "ville": "", "distance_km": None, "epreuves": [], "categorie": "", "genre": "",
         "prix": "", "inscrits": "", "niveau_playtomic": "", "source": "", "sources": []}
    e.update(kw)
    return e


# ── Niveaux FFT ────────────────────────────────────────────────────────────────

NIVEAUX_FFT = {"25", "50", "100", "250", "500", "1000", "1500", "2000"}
# "P25", "P 25", "P-50", "P50H", "P1OO" (lettre O) ; le lookahead empêche de lire P250 comme P25.
P_RE = re.compile(r"(?<![A-Za-z0-9])[Pp][\s\-]*([0-9][0-9Oo]{1,3})(?![0-9Oo])([HDFMX](?![A-Za-z0-9]))?")
# Libellés sans "P" ("ALL IN 100H") : nombre isolé, hors dates, heures, âges et pourcentages.
NU_RE = re.compile(r"(?<![\w/+:.,°-])(25|50|100|250|500|1000|1500|2000)([HDFMX])?(?![\w/:%])")


def niveaux_suffixes(texte):
    """[(niveau, suffixe)] trouvés dans un libellé ; suffixe = H, D, F, M, X ou ''."""
    t = texte or ""
    out = []
    for m in P_RE.finditer(t):
        n = m.group(1).upper().replace("O", "0")
        if n in NIVEAUX_FFT:
            out.append(("P" + n, (m.group(2) or "").upper()))
    if not out:
        out = [("P" + m.group(1), (m.group(2) or "").upper()) for m in NU_RE.finditer(t)]
    return out


def tri_niveaux(niv):
    return sorted(set(niv), key=lambda x: int(x[1:]))


def niveaux_de(texte, epreuve=None):
    """Niveaux d'un libellé. Avec `epreuve`, ignore ceux suffixés pour une autre épreuve
    ("P100D/P25H" en DM -> P25 ; "P100H/P50M" : M = mixte quand H est aussi présent)."""
    ns = niveaux_suffixes(texte)
    if epreuve is None:
        return tri_niveaux(n for n, _ in ns)
    a_h = any(s == "H" for _, s in ns)
    cible = {"H": "DM", "D": "DD", "F": "DD", "X": "DX", "M": "DX" if a_h else "DM"}
    return tri_niveaux(n for n, s in ns if not s or cible[s] == epreuve)


# ── Filtres texte ──────────────────────────────────────────────────────────────

VETERAN_RE = re.compile(r"\+\s*(35|40|45|50|55|60|65)\b|\b(35|40|45|50|55|60|65)\s*ans\b|"
                        r"v[ée]t[ée]rans?|seniors?\s*\+", re.IGNORECASE)
JEUNES_RE = re.compile(r"\b(enfants?|kids?|juniors?|jeunes|u\s?1\d|ados?|parents?\s*/\s*enfants?|"
                       r"minis?|[ée]cole de padel)\b", re.IGNORECASE)
DAMES_RE = re.compile(r"\b(dames?|femmes?|f[ée]minin\w*|women|ladies|female|girls?|filles?)\b",
                      re.IGNORECASE)
MIXTE_RE = re.compile(r"\b(mixtes?|mixed)\b", re.IGNORECASE)
HOMMES_RE = re.compile(r"\b(hommes?|messieurs|masculin\w*|men|male|boys?)\b", re.IGNORECASE)
GENRE_PLAYTOMIC = {"Female": "DD", "Male": "DM", "Mixed": "DX", "Open": ""}


def titre_incompatible(titre, epreuve):
    """Titre qui indique une autre catégorie que l'épreuve voulue (pour les sources hors fiche FFT)."""
    if JEUNES_RE.search(titre) or VETERAN_RE.search(titre):
        return True
    if epreuve == "DM":
        return bool(DAMES_RE.search(titre) or MIXTE_RE.search(titre))
    if epreuve == "DD":
        return bool(HOMMES_RE.search(titre) or MIXTE_RE.search(titre))
    return bool(re.search(r"100\s*%", titre))  # DX : "100% dames" / "100% hommes"


# ── Source 1 : Ten'Up ──────────────────────────────────────────────────────────

TENUP_API = "https://tenup.fft.fr/back/public/v1/tournois"


def tenup_body(lat, lng, rayon, debut, fin, offset, size):
    return {"pratique": "PADEL", "from": offset, "size": size, "lat": lat, "lng": lng,
            "distance": rayon, "dateDebut": debut.strftime("%Y-%m-%dT00:00:00.000Z"),
            "dateFin": fin.strftime("%Y-%m-%dT00:00:00.000Z"), "type": [], "codeClub": None,
            "ligues": [], "comites": [], "utiliserMesDonnees": False, "naturesEpreuves": [],
            "typesEpreuves": [], "naturesTerrains": [], "categoriesJeu": [], "categoriesAge": [],
            "familles": [], "tournoiInterne": False, "classements": [], "inscriptionEnLigne": None,
            "paiementEnLigne": None, "filtres": True, "sort": "DISTANCE"}


def tenup_lien(id_homologation):
    num = re.search(r"(\d+)$", id_homologation or "")
    return f"https://tenup.fft.fr/tournoi/{num.group(1)}" if num else ""


def tenup_parse_cards(data):
    out = []
    for c in data.get("cards") or []:
        lib = (c.get("libelleTournoi") or "").strip()
        dist = c.get("distance")
        out.append(evenement(
            id=c.get("idHomologation", ""), source="Ten'Up",
            debut=(c.get("dateDebut") or "")[:10], fin=(c.get("dateFin") or "")[:10],
            libelle=lib, niveaux=niveaux_de(lib), heure=heure_de(lib),
            club=((c.get("club") or {}).get("libelle") or "").strip(),
            ville=(c.get("ville") or "").strip(),
            distance_km=round(dist / 1000, 1) if isinstance(dist, (int, float)) else None,
            epreuves=sorted({e for e in c.get("naturesEpreuves") or [] if e}),
            sources=[{"nom": "Ten'Up", "lien": tenup_lien(c.get("idHomologation"))}]))
    return out


AGE_RE = re.compile(r"(\d{2})\s*ans", re.IGNORECASE)
HEURE_RE = re.compile(r"(?<![\d/])([01]?\d|2[0-3])\s?h\s?([0-5]\d)?(?![\w/])", re.IGNORECASE)


def heure_de(texte):
    """Heure de début écrite dans un libellé : "(9h-13h)" -> 09:00, "18h30 -23h30" -> 18:30."""
    m = HEURE_RE.search(texte or "")
    return f"{int(m.group(1)):02d}:{m.group(2) or '00'}" if m else ""


def tenup_appliquer_fiche(e, fiche, epreuve):
    """Complète un événement Ten'Up avec sa fiche : niveaux, catégorie et prix de l'épreuve voulue."""
    eps = [x for x in fiche.get("epreuves") or [] if x.get("nature") == epreuve]
    e["fiche"] = True
    e["epreuves"] = sorted({x.get("nature") for x in fiche.get("epreuves") or [] if x.get("nature")})
    if not eps:
        e["niveaux"] = []
        return e
    seniors = [x for x in eps if not AGE_RE.search(x.get("libelle") or "")]
    if seniors:
        e["categorie"] = "Senior"
    else:
        ages = sorted({AGE_RE.search(x["libelle"]).group(1) for x in eps})
        e["categorie"] = "+" + "/+".join(ages)
    retenues = seniors or eps
    e["niveaux"] = tri_niveaux(x["categorie"] for x in retenues
                               if re.fullmatch(r"P\d+", x.get("categorie") or ""))
    tarifs = sorted({x["tarifAdulte"] for x in retenues if isinstance(x.get("tarifAdulte"), (int, float))})
    if tarifs:
        e["prix"] = " / ".join(f"{t:g} €".replace(".", ",") for t in tarifs)
    t = fiche.get("tournoi") or {}
    e["adresse"] = " ".join(x for x in (t.get("adresse1"), t.get("adresse2"), t.get("codePostal"),
                                        t.get("ville")) if x)
    if t.get("latitude") and t.get("longitude"):
        e["distance_km"] = round(haversine(*LYON, t["latitude"], t["longitude"]), 1)
    if t.get("horaires"):
        h = re.search(r"(\d{1,2})\s*[h:]\s*(\d{2})?", str(t["horaires"]))
        if h:
            e["heure"] = f"{int(h.group(1)):02d}:{h.group(2) or '00'}"
    return e


def tenup_candidat(e, cfg):
    """Pré-filtre avant de demander la fiche (on reste large : la fiche tranche)."""
    if cfg.epreuve not in e["epreuves"] or JEUNES_RE.search(e["libelle"]):
        return False
    niv = set(niveaux_de(e["libelle"], cfg.epreuve))
    return not niv or niveau_ok(niv, cfg)


def source_tenup(cfg):
    s = session()
    s.headers.update({"Accept": "application/json, text/plain, */*", "Origin": "https://tenup.fft.fr",
                      "Referer": "https://tenup.fft.fr/recherche/tournois"})
    evts, offset, size = [], 0, 500
    while True:
        r = s.post(TENUP_API, json=tenup_body(*LYON, cfg.rayon, cfg.debut, cfg.fin, offset, size),
                   timeout=60)
        r.raise_for_status()
        data = r.json()
        page = tenup_parse_cards(data)
        evts += page
        offset += size
        if not page or offset >= data.get("nbResultats", 0):
            break
        time.sleep(1)

    cands = [e for e in evts if tenup_candidat(e, cfg)]
    cache = Cache("tenup_fiches", ttl_h=48, actif=not cfg.rafraichir)
    erreurs = []

    def fiche(e):
        f = cache.get(e["id"])
        if f is None:
            time.sleep(0.4)  # 2 workers x 0,4 s : environ 4 requêtes/s au maximum
            try:
                r = s.get(f"{TENUP_API}/{e['id']}/fiche-tournoi", timeout=30)
                r.raise_for_status()
                f = r.json()
            except Exception as exc:  # fiche indisponible : on garde ce que dit le libellé
                erreurs.append(f"{e['id']}: {exc}")
                return
            f = {k: f.get(k) for k in ("epreuves", "tournoi")}
            cache.set(e["id"], f)
        tenup_appliquer_fiche(e, f, cfg.epreuve)

    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(fiche, cands))
    cache.save()
    log(f"  Ten'Up : {len(evts)} tournois, {len(cands)} fiches consultées"
        + (f", {len(erreurs)} fiches en erreur" if erreurs else ""))
    return evts


# ── Source 2 : Playtomic ───────────────────────────────────────────────────────

PLAYTOMIC = "https://playtomic.com"
PLAYTOMIC_RECHERCHES = ("Lyon",)
# Départements voisins : évite de charger la page de clubs homonymes ailleurs en France.
DEPARTEMENTS_PROCHES = ("01", "38", "42", "69", "71")
MOIS_EN = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]
JOURS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def next_flight_text(html):
    """Texte décodé du flux Next.js (self.__next_f.push([1, "..."]))."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html)
    out = []
    for c in chunks:
        try:
            out.append(json.loads(f'"{c}"'))
        except ValueError:
            pass
    return "".join(out)


def playtomic_parse_recherche(html):
    """Résultats de playtomic.com/search?q=... -> [{slug, nom, pays, code_postal}]."""
    t = next_flight_text(html)
    vus, out = set(), []
    for m in re.finditer(r'\{"id":"[0-9a-f-]{36}","name":"((?:[^"\\]|\\.)*)","slug":"([a-z0-9-]+)",'
                         r'"country_code":"([A-Z]*)","address":\{"street":"(?:[^"\\]|\\.)*",'
                         r'"postal_code":"([^"]*)"', t):
        if m.group(2) not in vus:
            vus.add(m.group(2))
            out.append({"slug": m.group(2), "nom": json.loads(f'"{m.group(1)}"'),
                        "pays": m.group(3), "code_postal": m.group(4)})
    return out


def playtomic_parse_club(html):
    """Page playtomic.com/clubs/{slug} -> {slug, nom, ville, lat, lng} (objet "tenant" du flux)."""
    t = next_flight_text(html)
    i = t.find('"tenant":{"tenant_id"')
    if i < 0:
        return None
    try:
        tenant, _ = json.JSONDecoder().raw_decode(t, i + len('"tenant":'))
    except ValueError:
        return None
    addr = tenant.get("address") or {}
    co = addr.get("coordinate") or {}
    return {"slug": tenant.get("slug"), "nom": (tenant.get("tenant_name") or tenant.get("slug")).strip(),
            "ville": addr.get("city") or "", "pays": addr.get("country_code") or "",
            "lat": co.get("lat"), "lng": co.get("lon")}


def date_sans_annee(mois, jour, jour_semaine, today):
    """'Wednesday, October 28' n'a pas d'année : on prend celle où le jour de semaine concorde."""
    m = MOIS_EN.index(mois) + 1
    for y in (today.year, today.year + 1, today.year - 1):
        try:
            d = date(y, m, jour)
        except ValueError:
            continue
        if JOURS_EN[d.weekday()] == jour_semaine and d >= today - timedelta(days=60):
            return d
    d = date(today.year, m, jour)
    return d if d >= today - timedelta(days=60) else date(today.year + 1, m, jour)


DATE_PT_RE = re.compile(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
                        r"(" + "|".join(MOIS_EN) + r") (\d{1,2}) \| (\d{1,2}:\d{2})h$")
NIVEAU_PT_RE = re.compile(r"^\d+(?:\.\d+)? – \d+(?:\.\d+)?$")
PRIX_RE = re.compile(r"^(?:([€£])\s?(\d+(?:[.,]\d+)?)|(\d+(?:[.,]\d+)?)\s?([€£]))$")
ACTIVITE_RE = re.compile(r'href="(/activities/[a-z-]+/[0-9a-f-]{36})"')
LIEN_FLUX_RE = re.compile(r'"href":"(/activities/[a-z-]+/[0-9a-f-]{36})"[^{}]*?"aria-label":"((?:[^"\\]|\\.)*)"')


def _tokens(fragment):
    f = re.sub(r"<svg[\s\S]*?</svg>|<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", fragment)
    f = f.replace("<!-- -->", "")
    return [t for t in (htmllib.unescape(x).strip() for x in re.sub(r"<[^>]+>", "\n", f).split("\n"))
            if t]


def playtomic_parse_tournois(html, club, today):
    """Page /clubs/{slug}/competitions/tournaments -> événements.

    Les cartes sont dans le HTML rendu côté serveur ; pour une partie d'entre elles le lien
    n'y figure pas mais est dans le flux Next.js (href + aria-label = titre), dans le même ordre."""
    liens_flux = [(json.loads(f'"{lab}"').strip(), href) for href, lab in LIEN_FLUX_RE.findall(
        next_flight_text(html))]
    out, utilises = [], set()
    for bloc in html.split('data-slot="card"')[1:]:
        bloc = bloc.split("<script", 1)[0]
        toks = _tokens('<x ' + bloc)
        i = next((k for k, t in enumerate(toks) if DATE_PT_RE.match(t)), None)
        if i is None or i + 1 >= len(toks):
            continue
        jsem, mois, jour, heure = DATE_PT_RE.match(toks[i]).groups()
        titre = toks[i + 1]
        reste = toks[i + 2:]
        e = evenement(source="Playtomic", libelle=titre, heure=f"{int(heure[:-3]):02d}{heure[-3:]}",
                      club=club["nom"], ville=club.get("ville", ""),
                      distance_km=club.get("distance_km"))
        d = date_sans_annee(mois, int(jour), jsem, today).isoformat()
        e["debut"] = e["fin"] = d
        e["niveaux"] = niveaux_de(titre)
        for t in reste:
            if NIVEAU_PT_RE.match(t) and not e["niveau_playtomic"]:
                e["niveau_playtomic"] = t.replace(" – ", "–")
            elif t in GENRE_PLAYTOMIC and not e["genre"]:
                e["genre"] = t
            elif re.fullmatch(r"\d+/\d+", t) and not e["inscrits"]:
                e["inscrits"] = t
            elif PRIX_RE.match(t) and not e["prix"]:
                m = PRIX_RE.match(t)
                montant, devise = (m.group(2), m.group(1)) if m.group(1) else (m.group(3), m.group(4))
                e["prix"] = f"{montant.replace('.', ',')} {devise}"
            elif t == "Free" and not e["prix"]:
                e["prix"] = "gratuit"
        if e["genre"] and GENRE_PLAYTOMIC[e["genre"]]:
            e["epreuves"] = [GENRE_PLAYTOMIC[e["genre"]]]
        href = ACTIVITE_RE.search(bloc)
        href = href.group(1) if href else None
        if not href:
            href = next((h for lab, h in liens_flux if lab == titre.strip() and h not in utilises), None)
        if href:
            utilises.add(href)
        lien = PLAYTOMIC + href if href else f"{PLAYTOMIC}/clubs/{club['slug']}/competitions/tournaments"
        e["sources"] = [{"nom": "Playtomic", "lien": lien}]
        e["id"] = href or f"{club['slug']}:{d}:{e['heure']}:{titre}"
        out.append(e)
    return out


def playtomic_clubs(s, cfg):
    """Clubs Playtomic dans le rayon : recherche par nom + slugs ajoutés à la main, puis
    coordonnées lues sur la page de chaque club (gardées 30 jours en cache)."""
    cache = Cache("playtomic_clubs", ttl_h=24 * 30, actif=not cfg.rafraichir)
    slugs = []
    for q in PLAYTOMIC_RECHERCHES:
        r = s.get(f"{PLAYTOMIC}/search", params={"q": q}, timeout=30)
        r.raise_for_status()
        slugs += [c["slug"] for c in playtomic_parse_recherche(r.text)
                  if c["pays"] == "FR" and c["code_postal"][:2] in DEPARTEMENTS_PROCHES]
        time.sleep(1)
    slugs += cfg.playtomic_clubs
    clubs = []
    for slug in dict.fromkeys(slugs):
        info = cache.get(slug)
        if info is None:
            try:
                r = s.get(f"{PLAYTOMIC}/clubs/{slug}", timeout=30)
                r.raise_for_status()
                info = playtomic_parse_club(r.text) or {"slug": slug, "nom": slug, "lat": None}
            except Exception as exc:
                log(f"    (club Playtomic {slug} illisible : {exc})")
                continue
            cache.set(slug, info)
            time.sleep(0.5)
        if info.get("lat") is None:
            continue
        info = dict(info, nom=info["nom"].strip(),
                    distance_km=round(haversine(*LYON, info["lat"], info["lng"]), 1))
        if info["distance_km"] <= cfg.rayon:
            clubs.append(info)
    cache.save()
    return clubs


def source_playtomic(cfg):
    s = session("en-GB,en;q=0.9")  # le parsing repose sur les dates en anglais
    s.headers["Accept"] = "text/html,application/xhtml+xml"
    clubs = playtomic_clubs(s, cfg)
    log(f"  Playtomic : {len(clubs)} clubs dans le rayon ({', '.join(c['nom'] for c in clubs)})")
    today, erreurs = date.today(), []

    def un_club(club):
        try:
            r = s.get(f"{PLAYTOMIC}/clubs/{club['slug']}/competitions/tournaments", timeout=40)
            r.raise_for_status()
            return playtomic_parse_tournois(r.text, club, today)
        except Exception as exc:  # un club en échec ne bloque pas les autres
            erreurs.append(f"{club['nom']} : {exc}")
            return []
        finally:
            time.sleep(0.5)

    out = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for evts in ex.map(un_club, clubs):
            out += evts
    for err in erreurs:
        log(f"    (club ignoré : {err})")
    if clubs and len(erreurs) == len(clubs):
        raise RuntimeError(f"aucune page club lisible ({erreurs[0]})")
    vus, uniques = set(), []
    for e in out:
        if e["id"] not in vus:
            vus.add(e["id"])
            uniques.append(e)
    return uniques


# ── Source 3 : 4PADEL ──────────────────────────────────────────────────────────

QUATREPADEL_URL = "https://www.4padel.fr/tournois"
QUATREPADEL_API = "https://api2-front.lefive.fr/competitions"
QUATREPADEL_CLUBS = {"priest": ("Saint-Priest", (45.6960, 4.9440))}
DATE_4P_RE = re.compile(r"Le\s+(\d{2})/(\d{2})/(\d{4})\s+à\s+(\d{1,2})h(\d{2})")


def quatrepadel_params(debut, fin):
    # Paramètres relevés sur les requêtes de www.4padel.fr/tournois (sport=3 : padel).
    return {"appId": 2, "isChannelWeb": "true", "startingDateFrom": f"{debut}T00:00:00.000Z",
            "startingDateTo": f"{fin}T00:00:00.000Z", "endingDateFrom": "", "endingDateTo": "",
            "sex": "All", "fieldTypeId": "", "competitionLevels": "", "center": "", "active": "true",
            "type": "2,3", "isLazy": "false", "sport": 3, "userLatitude": LYON[0],
            "userLongitude": LYON[1], "distance": 0, "sortField": "startingDate", "sortOrder": "asc"}


def quatrepadel_parse_cartes(cartes, rayon):
    """Cartes lues dans le DOM (.lf-tournament-preview-container) -> événements Saint-Priest."""
    out = []
    for c in cartes:
        cle = next((k for k in QUATREPADEL_CLUBS if k in sans_accents(c.get("club"))), None)
        if not cle:
            continue
        ville, (clat, clng) = QUATREPADEL_CLUBS[cle]
        dist = haversine(*LYON, clat, clng)
        m = DATE_4P_RE.search(" ".join(c.get("lignes") or []))
        if dist > rayon or not m:
            continue
        jour = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        nom = next((l for l in c.get("lignes") or [] if not DATE_4P_RE.search(l)), "") or "Tournoi"
        out.append(evenement(
            source="4PADEL", debut=jour, fin=jour, heure=f"{int(m.group(4)):02d}:{m.group(5)}",
            niveaux=niveaux_de(f"{c.get('niveau', '')} {nom}"), libelle=nom, club=c["club"],
            ville=ville, distance_km=round(dist, 1),
            sources=[{"nom": "4PADEL", "lien": c.get("lien") or QUATREPADEL_URL}]))
    return out


def quatrepadel_dom():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("l'API annonce des tournois mais Playwright n'est pas installé "
                           "(pip install playwright && python -m playwright install chromium)")
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_context(user_agent=UA, locale="fr-FR").new_page()
        page.goto(QUATREPADEL_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector(".lf-tournament-preview-container", timeout=25000)
        for _ in range(10):
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(600)
        cartes = page.evaluate("""() => [...document.querySelectorAll('.lf-tournament-preview-container')]
          .map(c => ({
            niveau: c.querySelector('.fft p')?.innerText.trim() || '',
            club: c.querySelector('p.lf-tournament-type')?.innerText.trim() || '',
            lignes: [...c.querySelectorAll('p')].map(p => p.innerText.trim()).filter(Boolean),
            lien: (c.closest('a') || c.querySelector('a'))?.href || ''
          }))""")
        b.close()
    return cartes


def source_4padel(cfg):
    s = session()
    s.headers.update({"Accept": "application/json", "Origin": "https://www.4padel.fr",
                      "Referer": "https://www.4padel.fr/"})
    r = s.get(QUATREPADEL_API, params=quatrepadel_params(cfg.debut, cfg.fin), timeout=30)
    r.raise_for_status()
    data = r.json()
    items = data if isinstance(data, list) else data.get("data") or []
    if not items:
        log("  4PADEL : l'API ne publie aucun tournoi sur la période")
        return []
    # Structure JSON jamais observée (API vide à l'écriture du script) : on lit la page rendue.
    return quatrepadel_parse_cartes(quatrepadel_dom(), cfg.rayon)


SOURCES = {"tenup": ("Ten'Up", source_tenup),
           "playtomic": ("Playtomic", source_playtomic),
           "4padel": ("4PADEL", source_4padel)}


# ── Filtres ────────────────────────────────────────────────────────────────────

def niveau_ok(niv, cfg):
    """cfg.niveaux = None (--niveaux tous) : tous les niveaux, le tri se fait dans la page."""
    return cfg.niveaux is None or bool(set(niv) & cfg.niveaux)


def garder(e, cfg):
    if not e["debut"] or not (cfg.debut.isoformat() <= e["debut"] <= cfg.fin.isoformat()):
        return False
    if e["distance_km"] is not None and e["distance_km"] > cfg.rayon + 0.5:
        return False
    if e["source"] == "Ten'Up":
        if cfg.epreuve not in e["epreuves"] or JEUNES_RE.search(e["libelle"]):
            return False
        if e.get("fiche"):
            if e["categorie"] != "Senior":
                return False
            niv = set(e["niveaux"])
        else:  # fiche indisponible : on se fie au libellé
            if VETERAN_RE.search(e["libelle"]):
                return False
            niv = set(niveaux_de(e["libelle"], cfg.epreuve))
        return niveau_ok(niv, cfg) if niv else True
    # Hors Ten'Up : on lit le titre et le genre affiché
    if titre_incompatible(e["libelle"], cfg.epreuve):
        return False
    if e["epreuves"] and cfg.epreuve not in e["epreuves"]:
        return False
    niv = set(niveaux_de(e["libelle"], cfg.epreuve))
    if niv:
        return niveau_ok(niv, cfg)
    return cfg.hors_fft


# ── Dédoublonnage ──────────────────────────────────────────────────────────────

MOTS_VIDES = {"padel", "club", "tennis", "tc", "lyon", "de", "du", "des", "la", "le", "les", "l",
              "d", "et", "sport", "sports", "the", "asso", "association", "as", "us", "cs", "center",
              "centre", "country"}


def tokens_club(nom):
    return {t for t in re.split(r"[^a-z0-9]+", sans_accents(nom)) if t and t not in MOTS_VIDES}


def norm_ville(v):
    t = re.split(r"[^a-z0-9]+", sans_accents(v))
    return {"saint" if x == "st" else "sainte" if x == "ste" else x for x in t if x}


def noms_proches(a, b):
    ta, tb = tokens_club(a), tokens_club(b)
    if not ta or not tb:
        return False
    if ta <= tb or tb <= ta or len(ta & tb) / len(ta | tb) >= 0.5:
        return True
    # "PADEL SHOT" / "PadelShot" : on compare aussi les noms collés
    ca, cb = "".join(sorted(ta)), "".join(sorted(tb))
    return any(len(t) >= 4 and t in cb for t in ta) or any(len(t) >= 4 and t in ca for t in tb)


def meme_lieu(a, b):
    va, vb = norm_ville(a["ville"]), norm_ville(b["ville"])
    villes = None if not (va and vb) else (va <= vb or vb <= va)
    dist = None
    if a["distance_km"] is not None and b["distance_km"] is not None:
        dist = abs(a["distance_km"] - b["distance_km"])
    return villes, dist


def meme_club(a, b):
    villes, dist = meme_lieu(a, b)
    if villes is False and (dist is None or dist > 1.5):
        return False  # deux villes différentes (ex. PadelShot Craponne / Saint-Genis-Laval)
    if noms_proches(a["club"], b["club"]):
        return True
    # noms trop différents : même ville et même distance de Lyon
    return bool(villes) and dist is not None and dist < 0.8


def minutes(h):
    return int(h[:2]) * 60 + int(h[3:5]) if re.fullmatch(r"\d{2}:\d{2}", h or "") else None


def doublons(a, b):
    if a["source"] == b["source"] or a["debut"] != b["debut"] or not meme_club(a, b):
        return False
    na, nb = set(a["niveaux"]), set(b["niveaux"])
    if na and nb:
        return bool(na & nb)
    ma, mb = minutes(a["heure"]), minutes(b["heure"])
    heures_ok = ma is None or mb is None or abs(ma - mb) <= 45
    if na or nb:
        return heures_ok
    return heures_ok and (ma is not None and mb is not None or noms_proches(a["libelle"], b["libelle"]))


def fusionner(evts):
    evts = sorted(evts, key=lambda e: PRIORITE.get(e["source"], 9))
    garde, nb = [], 0
    for e in evts:
        cible = next((g for g in garde if any(doublons(x, e) for x in g["_membres"])), None)
        if not cible:
            e["_membres"] = [dict(e)]
            garde.append(e)
            continue
        nb += 1
        cible["_membres"].append(e)
        noms = {s["nom"] for s in cible["sources"]}
        cible["sources"] += [s for s in e["sources"] if s["nom"] not in noms]
        for champ in ("heure", "prix", "inscrits", "niveau_playtomic", "ville", "fin"):
            if not cible[champ] and e[champ]:
                cible[champ] = e[champ]
        if cible["distance_km"] is None:
            cible["distance_km"] = e["distance_km"]
    for g in garde:
        del g["_membres"]
    return garde, nb


# ── Sorties ────────────────────────────────────────────────────────────────────

def infos(e):
    return ", ".join(x for x in (
        e["prix"], f"{e['inscrits']} inscrits" if e["inscrits"] else "",
        f"niveau Playtomic {e['niveau_playtomic']}" if e["niveau_playtomic"] else "") if x)


def ecrire_csv(evts, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["date", "fin", "heure", "niveaux", "tournoi", "club", "ville", "distance_km",
                    "prix", "inscrits", "niveau_playtomic", "sources", "liens"])
        for e in evts:
            w.writerow([e["debut"], e["fin"], e["heure"], "/".join(e["niveaux"]) or "Hors FFT",
                        e["libelle"], e["club"], e["ville"],
                        str(e["distance_km"]).replace(".", ",") if e["distance_km"] is not None else "",
                        e["prix"], e["inscrits"], e["niveau_playtomic"],
                        " / ".join(s["nom"] for s in e["sources"]),
                        " ".join(s["lien"] for s in e["sources"])])


CHAMPS_PAGE = ("debut", "fin", "heure", "niveaux", "libelle", "club", "ville", "distance_km",
               "prix", "inscrits", "niveau_playtomic", "sources")


def donnees_json(evts, meta):
    """JSON à intégrer dans <script> : tout '<' devient \\u003c, donc aucun '</script>' possible."""
    payload = {"meta": meta, "tournois": [{k: e[k] for k in CHAMPS_PAGE} for e in evts]}
    return (json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


FRONT = ICI / "public" / "index.html"
MARQUEUR_DONNEES = "window.PADEL_DATA = null;"


def ecrire_json(evts, meta, path):
    """data.json lu par le site (public/index.html)."""
    out = Path(path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "tournois": [{k: e[k] for k in CHAMPS_PAGE} for e in evts]}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def ecrire_html(evts, meta, path):
    """Version autonome du site : le même front, avec les données intégrées (ouvrable en file://)."""
    page = FRONT.read_text("utf-8")
    assert MARQUEUR_DONNEES in page, "marqueur de données absent de public/index.html"
    out = Path(path).resolve()
    out.write_text(page.replace(MARQUEUR_DONNEES, f"window.PADEL_DATA = {donnees_json(evts, meta)};"),
                   encoding="utf-8")
    return out


# ── Programme principal ────────────────────────────────────────────────────────

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Tournois de padel amateur autour de Lyon (Ten'Up, "
                                             "Playtomic, 4PADEL), dédoublonnés, en page HTML.")
    ap.add_argument("--debut", type=date.fromisoformat, default=date.today(),
                    help="date de début AAAA-MM-JJ (défaut : aujourd'hui)")
    ap.add_argument("--mois", type=int, default=4, help="nombre de mois couverts (défaut : 4)")
    ap.add_argument("--rayon", type=int, default=30, help="rayon autour de Lyon en km (défaut : 30)")
    ap.add_argument("--niveaux", default="P25,P50", help="niveaux FFT séparés par des virgules "
                    "(défaut : P25,P50), ou 'tous' pour laisser la page filtrer")
    ap.add_argument("--epreuve", default="DM", choices=["DM", "DD", "DX"],
                    help="DM double messieurs (défaut), DD dames, DX mixte")
    ap.add_argument("--sources", default="tenup,playtomic,4padel",
                    help="sources parmi tenup, playtomic, 4padel (défaut : toutes)")
    ap.add_argument("--sans-hors-fft", action="store_true",
                    help="exclure les tournois sans niveau FFT (americanos, tournois maison)")
    ap.add_argument("--playtomic-clubs", default="",
                    help="slugs Playtomic à ajouter (ex. mon-club,autre-club)")
    ap.add_argument("--csv", help="écrire aussi un CSV (séparateur ;, pour Excel)")
    ap.add_argument("--json", help="écrire les données pour le site (ex. public/data.json)")
    ap.add_argument("--html", help="page autonome à générer (défaut : tournois_padel_lyon.html, "
                                   "sauf si --json est donné)")
    ap.add_argument("--no-open", action="store_true", help="ne pas ouvrir la page dans le navigateur")
    ap.add_argument("--rafraichir", action="store_true", help="ignorer le cache (.cache/)")
    a = ap.parse_args(argv)
    a.fin = ajouter_mois(a.debut, a.mois)
    if a.html is None and not a.json:
        a.html = "tournois_padel_lyon.html"
    a.niveaux = (None if a.niveaux.strip().lower() == "tous" else
                 {"P" + n.strip().upper().lstrip("P") for n in a.niveaux.split(",") if n.strip()})
    a.hors_fft = not a.sans_hors_fft
    a.playtomic_clubs = [x.strip() for x in a.playtomic_clubs.split(",") if x.strip()]
    return a


def main(argv=None):
    cfg = parse_args(argv)
    log(f"Recherche : padel {cfg.epreuve}, {cfg.rayon} km autour de Lyon, "
        f"du {cfg.debut:%d/%m/%Y} au {cfg.fin:%d/%m/%Y}, niveaux {'/'.join(tri_niveaux(cfg.niveaux)) if cfg.niveaux else 'tous'}")

    tous, etat = [], []
    for cle in [s.strip().lower() for s in cfg.sources.split(",") if s.strip()]:
        if cle not in SOURCES:
            log(f"Source inconnue ignorée : {cle}")
            continue
        nom, fn = SOURCES[cle]
        log(f"→ {nom}…")
        t0 = time.time()
        try:
            evts = fn(cfg)
            tous += evts
            etat.append({"nom": nom, "ok": True, "nb": len(evts)})
            log(f"  {nom} : {len(evts)} événements ({time.time() - t0:.0f} s)")
        except Exception as exc:  # chaque source est isolée
            msg = f"{type(exc).__name__}: {exc}"[:300]
            etat.append({"nom": nom, "ok": False, "nb": 0, "erreur": msg})
            log(f"  {nom} indisponible : {msg}")

    filtres = [e for e in tous if garder(e, cfg)]
    retenus, nb_doublons = fusionner(filtres)
    retenus.sort(key=lambda e: (e["debut"], e["heure"] or "99", e["distance_km"] or 0))

    print("\n" + "─" * 78)
    for s in etat:
        print(f"  {s['nom']:<10} " + (f"{s['nb']} événements" if s["ok"] else f"ÉCHEC — {s['erreur']}"))
    print(f"  Après filtres : {len(filtres)}  |  doublons fusionnés : {nb_doublons}  |  "
          f"tournois retenus : {len(retenus)}")
    print("─" * 78)
    for e in retenus:
        jour = datetime.strptime(e["debut"], "%Y-%m-%d").strftime("%d/%m")
        niv = "/".join(e["niveaux"]) or "Hors FFT"
        srcs = "+".join(s["nom"] for s in e["sources"])
        print(f"{jour} {e['heure'] or '':>5}  {niv:<9} {e['libelle'][:34]:<34} "
              f"{e['club'][:22]:<22} [{srcs}]")

    if cfg.csv:
        ecrire_csv(retenus, cfg.csv)
        log(f"CSV écrit : {cfg.csv}")
    meta = {"rayon": cfg.rayon, "epreuve": cfg.epreuve, "niveaux": tri_niveaux(cfg.niveaux or []),
            "hors_fft": cfg.hors_fft, "debut": cfg.debut.isoformat(), "fin": cfg.fin.isoformat(),
            "genere": datetime.now().strftime("%Y-%m-%dT%H:%M"), "sources": etat,
            "doublons": nb_doublons}
    if cfg.json:
        log(f"Données écrites : {ecrire_json(retenus, meta, cfg.json)}")
    if cfg.html:
        out = ecrire_html(retenus, meta, cfg.html)
        log(f"Page générée : {out}")
        if not cfg.no_open:
            webbrowser.open(out.as_uri())
    return retenus, etat, nb_doublons


if __name__ == "__main__":
    _, etat, _ = main()
    # code 2 si aucune source n'a répondu : l'Action GitHub ne publie pas une liste vide
    sys.exit(0 if any(s["ok"] for s in etat) else 2)
