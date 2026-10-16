"""Tests à partir des réponses réelles enregistrées dans samples/ (16/10/2026)."""
import csv
import json
import re
import sys
from datetime import date
from pathlib import Path

import pytest

ICI = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ICI))
import padel_lyon as P  # noqa: E402

S = ICI / "samples"
TODAY = date(2026, 10, 16)


def lire(nom):
    return (S / nom).read_text("utf-8")


def cfg(*args):
    return P.parse_args(["--debut", "2026-10-16", "--no-open", *args])


def ev(**kw):
    base = dict(source="Playtomic", debut="2026-11-04", fin="2026-11-04", club="Club", ville="Lyon",
                distance_km=5.0, sources=[{"nom": kw.get("source", "Playtomic"), "lien": "https://x"}])
    base.update(kw)
    return P.evenement(**base)


# ── Niveaux ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("texte, attendu", [
    ("P25 Hommes", ["P25"]),
    ("P 25 lyon padel", ["P25"]),
    ("P-50 matinée", ["P50"]),
    ("P50H - Esprit Padel La Boisse", ["P50"]),
    ("Tournoi P1OO", ["P100"]),           # lettre O au lieu de zéro
    ("P250 Hommes TCL", ["P250"]),        # pas P25
    ("P2500", []),                        # niveau inexistant
    ("P100D/P25H Du Vendredi CRAPONNE", ["P25", "P100"]),
    ("ALL IN 100H", ["P100"]),            # libellé réel sans "P"
    ("ALL IN COUNTRY CLUB P25H", ["P25"]),
    ("P50 du 01/12 Padel Côtière", ["P50"]),
    ("P100 Los Maestros (+45ans)", ["P100"]),
    ("PADELSHOT PARTY 100% FEMININE", []),  # pourcentage, pas un niveau
    ("P 100 flash 13h - 17h du Bois d'Ars", ["P100"]),
    ("Americano du jeudi", []),
])
def test_niveaux_de(texte, attendu):
    assert P.niveaux_de(texte) == attendu


def test_niveaux_par_epreuve():
    assert P.niveaux_de("P100D/P25H", "DM") == ["P25"]
    assert P.niveaux_de("P100H/P50M", "DM") == ["P100"]   # M = mixte quand H est présent
    assert P.niveaux_de("P100H/P50M", "DX") == ["P50"]
    assert P.niveaux_de("P50M seul", "DM") == ["P50"]      # sans H, M = messieurs
    assert P.niveaux_de("P25 Hommes", "DD") == ["P25"]     # pas de suffixe : on ne sait pas


# ── Ten'Up ─────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def tenup():
    return P.tenup_parse_cards(json.loads(lire("tenup_tournois.json")))


def test_tenup_liste(tenup):
    assert len(tenup) == 293
    e = tenup[0]
    assert e["libelle"] == "P250 Hommes TCL"
    assert e["club"] == "LYON (TENNIS CLUB DE)" and e["ville"] == "VILLEURBANNE"
    assert e["debut"] == e["fin"] == "2026-10-31"
    assert e["distance_km"] == 2.7                        # mètres -> km
    assert e["epreuves"] == ["DM"] and e["niveaux"] == ["P250"]
    assert e["sources"][0]["lien"] == "https://tenup.fft.fr/tournoi/271510"
    assert all(x["sources"][0]["lien"].startswith("https://tenup.fft.fr/tournoi/") for x in tenup)


def test_tenup_body_pagination():
    b = P.tenup_body(45.764, 4.8357, 30, date(2026, 10, 16), date(2027, 2, 16), 500, 500)
    assert b["from"] == 500 and b["size"] == 500 and b["pratique"] == "PADEL"
    assert b["dateDebut"] == "2026-10-16T00:00:00.000Z"


def fiche(id_):
    return json.loads(lire(f"tenup_fiche_{id_}.json"))


def evt_tenup(tenup, id_):
    return dict(next(e for e in tenup if e["id"] == id_))


def test_tenup_fiche_multi_epreuves(tenup):
    e = P.tenup_appliquer_fiche(evt_tenup(tenup, "MOJA_267191"), fiche("MOJA_267191"), "DM")
    assert e["niveaux"] == ["P25"] and e["categorie"] == "Senior"   # le P100 est en dames
    assert e["prix"] == "25 €" and e["epreuves"] == ["DD", "DM"]
    assert "CRAPONNE" in e["adresse"] and e["distance_km"] == 7.1
    assert P.garder(e, cfg())


def test_tenup_fiche_veteran_exclu(tenup):
    e = P.tenup_appliquer_fiche(evt_tenup(tenup, "MOJA_271452"), fiche("MOJA_271452"), "DM")
    assert e["categorie"] == "+45"
    assert not P.garder(e, cfg("--niveaux", "P100"))


def test_tenup_fiche_mixte_non_compte(tenup):
    e = P.tenup_appliquer_fiche(evt_tenup(tenup, "MOJA_267225"), fiche("MOJA_267225"), "DM")
    assert e["niveaux"] == ["P100"]                # le P50 est en mixte
    assert not P.garder(e, cfg())
    assert P.garder(e, cfg("--niveaux", "P100"))


def test_tenup_libelle_sans_p_corrige_par_fiche(tenup):
    e = P.tenup_appliquer_fiche(evt_tenup(tenup, "MOJA_270366"), fiche("MOJA_270366"), "DM")
    assert e["niveaux"] == ["P100"]


def test_tenup_parent_enfant_exclu(tenup):
    e = P.tenup_appliquer_fiche(evt_tenup(tenup, "MOJA_271435"), fiche("MOJA_271435"), "DM")
    assert e["categorie"] == "Senior" and not P.garder(e, cfg())


def test_tenup_sans_fiche_utilise_le_libelle(tenup):
    c = cfg()
    e = evt_tenup(tenup, "MOJA_267191")                    # P100D/P25H, fiche absente
    assert P.garder(e, c)
    dames = next(x for x in tenup if x["epreuves"] == ["DD"])
    assert not P.garder(dames, c)


def test_tenup_candidats(tenup):
    c = cfg()
    cands = [e for e in tenup if P.tenup_candidat(e, c)]
    assert 0 < len(cands) < len(tenup)
    assert all("DM" in e["epreuves"] for e in cands)
    assert not any("P250" in e["libelle"] and "P50" not in e["libelle"] for e in cands)


# ── Playtomic ──────────────────────────────────────────────────────────────────

CLUB = {"slug": "padelshot-lyon-craponne", "nom": "PadelShot Lyon - Craponne", "ville": "Craponne",
        "distance_km": 6.8}


def test_playtomic_tournois_craponne():
    evts = P.playtomic_parse_tournois(lire("playtomic_padelshot-lyon-craponne_tournaments.html"),
                                      CLUB, TODAY)
    assert len(evts) == 7
    e = evts[0]
    assert (e["debut"], e["heure"], e["libelle"]) == ("2026-10-28", "19:00", "PADELSHOT PARTY FEMININE")
    assert (e["inscrits"], e["prix"], e["niveau_playtomic"], e["genre"]) == ("3/12", "20 €", "0–3", "Open")
    assert e["sources"][0]["lien"] == \
        "https://playtomic.com/activities/tournaments/fe189bfa-d8be-47b8-b5b3-4c6eaca2b3b3"
    assert e["club"] == CLUB["nom"] and e["distance_km"] == 6.8
    # le dernier lien n'est que dans le flux Next.js
    assert evts[-1]["sources"][0]["lien"].endswith("d402a993-bd84-4eb4-8d88-c52989fb5cc0")
    assert len({x["id"] for x in evts}) == 7


def test_playtomic_liens_depuis_flux_next():
    evts = P.playtomic_parse_tournois(lire("playtomic_sportfield_tournaments.html"),
                                      {"slug": "sportfield", "nom": "Sportfield", "ville": "Lyon"}, TODAY)
    assert len(evts) == 7
    assert all("/activities/tournaments/" in e["sources"][0]["lien"] for e in evts)
    assert len({e["sources"][0]["lien"] for e in evts}) == 7
    girls = evts[0]
    assert girls["libelle"] == "GIRLS SESSION" and girls["genre"] == "Female" and girls["epreuves"] == ["DD"]
    assert girls["prix"] == "15 €"


def test_playtomic_club_sans_tournoi():
    assert P.playtomic_parse_tournois(lire("playtomic_lyon-vv-padel_tournaments.html"),
                                      {"slug": "lyon-vv-padel", "nom": "V&V"}, TODAY) == []


def test_playtomic_page_club():
    c = P.playtomic_parse_club(lire("playtomic_club_lyon-7-padel.html"))
    assert c == {"slug": "lyon-7-padel", "nom": "Lyon 7 Padel", "ville": "Lyon", "pays": "FR",
                 "lat": 45.74110181, "lng": 4.84246669}


def test_playtomic_recherche():
    r = P.playtomic_parse_recherche(lire("playtomic_search_Lyon.html"))
    fr = {c["slug"]: c["code_postal"] for c in r if c["pays"] == "FR"}
    assert fr["padelshot-lyon-craponne"] == "69290"
    assert "padel-basel-dreispitz" not in fr          # "Lyon-Strasse", en Suisse
    assert len(r) == len({c["slug"] for c in r})


def test_date_sans_annee():
    assert P.date_sans_annee("October", 28, "Wednesday", TODAY) == date(2026, 10, 28)
    # en décembre, un tournoi de janvier est l'année suivante (vérifié par le jour de semaine)
    assert P.date_sans_annee("January", 9, "Saturday", date(2026, 12, 20)) == date(2027, 1, 9)


# ── 4PADEL ─────────────────────────────────────────────────────────────────────

def test_4padel_api_vide():
    assert json.loads(lire("4padel_competitions.json")) == []


def test_4padel_cartes_dom():
    cartes = [
        {"niveau": "P100", "club": "4PADEL Saint-Priest", "lignes": ["Tournoi P100", "Le 09/01/2026 à 17h00"],
         "lien": "https://www.4padel.fr/tournois/1"},
        {"niveau": "P25", "club": "4PADEL Bordeaux", "lignes": ["P25", "Le 10/01/2026 à 09h00"], "lien": ""},
    ]
    evts = P.quatrepadel_parse_cartes(cartes, 30)
    assert len(evts) == 1
    e = evts[0]
    assert (e["debut"], e["heure"], e["niveaux"], e["ville"]) == ("2026-01-09", "17:00", ["P100"], "Saint-Priest")


# ── Filtres ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("titre, garde", [
    ("AMERICANO", True),
    ("TOURNOI AMICAL", True),
    ("americano femme", False),
    ("PS PARTY 100% DAMES", False),
    ("PADELSHOT PARTY FEMININE", False),
    ("Tournoi mixte du dimanche", False),
    ("Stage enfants", False),
    ("Open juniors U14", False),
    ("Tournoi +45", False),
    ("P50 Hommes", True),
    ("P100 Hommes", False),
])
def test_filtre_titres_hors_tenup(titre, garde):
    assert P.garder(ev(libelle=titre, niveaux=P.niveaux_de(titre)), cfg()) is garde


def test_filtre_hors_fft_optionnel():
    e = ev(libelle="AMERICANO")
    assert P.garder(e, cfg()) and not P.garder(e, cfg("--sans-hors-fft"))


def test_filtre_genre_playtomic():
    assert not P.garder(ev(libelle="Session", genre="Female", epreuves=["DD"]), cfg())


def test_filtre_periode_et_rayon():
    assert not P.garder(ev(libelle="AMERICANO", debut="2027-03-01"), cfg())       # > 4 mois
    assert P.garder(ev(libelle="AMERICANO", debut="2027-02-15"), cfg())
    assert not P.garder(ev(libelle="AMERICANO", debut="2026-10-15"), cfg())       # passé
    assert not P.garder(ev(libelle="AMERICANO", distance_km=45.0), cfg())
    assert P.parse_args(["--debut", "2026-10-31", "--mois", "4"]).fin == date(2027, 2, 28)


def test_option_niveaux():
    c = cfg("--niveaux", "p100, 250")
    assert c.niveaux == {"P100", "P250"}


# ── Dédoublonnage ──────────────────────────────────────────────────────────────

def tu(**kw):
    return ev(source="Ten'Up", sources=[{"nom": "Ten'Up", "lien": "https://tenup.fft.fr/tournoi/1"}], **kw)


def pt(**kw):
    return ev(source="Playtomic", sources=[{"nom": "Playtomic", "lien": "https://playtomic.com/a"}], **kw)


def test_noms_de_club():
    assert P.noms_proches("PADEL SHOT LYON", "PadelShot Lyon - Craponne")
    assert P.noms_proches("ESPRIT PADEL CLUB", "Esprit Padel Lyon")
    assert P.noms_proches("GONES PADEL", "Gones Padel Irigny")
    assert not P.noms_proches("GONES PADEL", "URBAN PADEL DARDILLY")
    assert P.tokens_club("Lyon (Tennis Club de)") == set()


def test_doublon_tenup_playtomic_fusionne():
    a = tu(libelle="P25 Hommes", niveaux=["P25"], club="PADEL SHOT LYON", ville="CRAPONNE", distance_km=7.1)
    b = pt(libelle="P25 PadelShot", niveaux=["P25"], club="PadelShot Lyon - Craponne", ville="Craponne",
           distance_km=6.8, heure="19:00", prix="20 €", inscrits="3/12")
    garde, nb = P.fusionner([b, a])
    assert nb == 1 and len(garde) == 1
    g = garde[0]
    assert g["source"] == "Ten'Up" and g["libelle"] == "P25 Hommes"          # priorité Ten'Up
    assert [s["nom"] for s in g["sources"]] == ["Ten'Up", "Playtomic"]
    assert (g["heure"], g["prix"], g["inscrits"]) == ("19:00", "20 €", "3/12")  # complétés


def test_pas_doublon_villes_differentes():
    a = tu(niveaux=["P25"], club="PADEL SHOT LYON", ville="SAINT GENIS LAVAL", distance_km=9.0)
    b = pt(niveaux=["P25"], club="PadelShot Lyon - Craponne", ville="Craponne", distance_km=6.8)
    assert not P.doublons(a, b)


def test_pas_doublon_niveaux_disjoints():
    a = tu(niveaux=["P25"], club="Gones Padel", ville="Irigny")
    b = pt(niveaux=["P100"], club="Gones Padel", ville="Irigny")
    assert not P.doublons(a, b)


def test_doublon_un_seul_niveau_et_heure():
    a = tu(niveaux=["P50"], club="Gones Padel", ville="Irigny", heure="")
    assert P.doublons(a, pt(club="Gones Padel Irigny", ville="Irigny", heure="09:00"))
    a["heure"] = "18:00"
    assert not P.doublons(a, pt(club="Gones Padel Irigny", ville="Irigny", heure="09:00"))


def test_doublon_ville_ecrite_differemment():
    a = tu(niveaux=["P25"], club="SAINT PRIEST (TENNIS CLUB DE)", ville="ST PRIEST", distance_km=10.2)
    b = ev(source="4PADEL", sources=[{"nom": "4PADEL", "lien": "https://www.4padel.fr"}],
           niveaux=["P25"], club="4PADEL Saint-Priest", ville="Saint-Priest", distance_km=10.0)
    assert P.doublons(a, b)
    garde, nb = P.fusionner([b, a])
    assert nb == 1 and [s["nom"] for s in garde[0]["sources"]] == ["Ten'Up", "4PADEL"]


def test_pas_doublon_meme_source_ou_autre_jour():
    a = tu(niveaux=["P25"], club="Gones Padel")
    assert not P.doublons(a, tu(niveaux=["P25"], club="Gones Padel"))
    assert not P.doublons(a, pt(niveaux=["P25"], club="Gones Padel", debut="2026-11-05"))


# ── Sorties ────────────────────────────────────────────────────────────────────

def test_html_echappe_et_json_integre(tmp_path):
    e = pt(libelle="</script><img src=x onerror=alert(1)>", club="A & B", heure="10:00")
    meta = {"rayon": 30, "epreuve": "DM", "niveaux": ["P25"], "hors_fft": True, "debut": "2026-10-16",
            "fin": "2027-02-16", "genere": "2026-10-16T10:00", "doublons": 0,
            "sources": [{"nom": "4PADEL", "ok": False, "nb": 0, "erreur": "<b>boom</b>"}]}
    out = P.ecrire_html([e], meta, tmp_path / "p.html")
    page = out.read_text("utf-8")
    brut = re.search(r"window\.PADEL_DATA = (.*?);</script>", page).group(1)
    assert "</" not in brut and "<" not in brut
    data = json.loads(brut)
    assert data["tournois"][0]["libelle"] == e["libelle"]
    assert "prefers-color-scheme: dark" in page and "esc(s.erreur)" in page


def test_csv_excel(tmp_path):
    e = pt(libelle="Americano", heure="19:00", prix="20 €", distance_km=6.8)
    p = tmp_path / "t.csv"
    P.ecrire_csv([e], p)
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(p.open(encoding="utf-8-sig"), delimiter=";"))
    assert rows[0][0] == "date" and rows[1][3] == "Hors FFT" and rows[1][7] == "6,8"


@pytest.mark.parametrize("texte, attendu", [
    ("P50 du Vendredi (17h-23h)", "17:00"),
    ("P50 en journée de 09h00 à 16h00", "09:00"),
    ("P100 Dames 2B SPORTS - 18h30 -23h30", "18:30"),
    ("P25H - Esprit Padel La Boisse", ""),      # le H du niveau n'est pas une heure
    ("P100H/P50M", ""),
    ("TC TREVOUX - P25 H - 30/10", ""),
    ("P 50 mardi matin 24/11", ""),
])
def test_heure_dans_libelle(texte, attendu):
    assert P.heure_de(texte) == attendu


def test_tenup_titre_multi_epreuves_avec_veteran_reste_candidat():
    e = P.evenement(source="Ten'Up", libelle="P250 +45ansH / P50 DAMES / P50 MESSIEURS",
                    epreuves=["DD", "DM"])
    assert P.tenup_candidat(e, cfg())


def test_source_en_echec_isolee(tmp_path, monkeypatch):
    def plante(cfg):
        raise ConnectionError("hôte injoignable")

    def marche(cfg):
        return [pt(libelle="AMERICANO", debut="2026-11-04", heure="19:00")]

    monkeypatch.setattr(P, "SOURCES", {"tenup": ("Ten'Up", plante), "playtomic": ("Playtomic", marche)})
    html = tmp_path / "p.html"
    retenus, etat, nb = P.main(["--debut", "2026-10-16", "--no-open", "--sources", "tenup,playtomic",
                                "--html", str(html)])
    assert [e["libelle"] for e in retenus] == ["AMERICANO"]
    assert etat[0] == {"nom": "Ten'Up", "ok": False, "nb": 0,
                       "erreur": "ConnectionError: hôte injoignable"}
    assert "hôte injoignable" in html.read_text("utf-8")


def test_mode_tous_niveaux_laisse_la_page_filtrer():
    c = cfg("--niveaux", "tous")
    assert c.niveaux is None
    assert P.garder(ev(libelle="P1000 Hommes", niveaux=["P1000"]), c)
    assert not P.garder(ev(libelle="P1000 Dames"), c)          # les filtres de catégorie restent
    assert P.tenup_candidat(P.evenement(source="Ten'Up", libelle="P500 Messieurs", epreuves=["DM"]), c)


def test_sortie_json_pour_le_site(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "SOURCES", {"playtomic": ("Playtomic", lambda c: [
        pt(libelle="AMERICANO", debut="2026-11-04", heure="19:00")])})
    monkeypatch.chdir(tmp_path)
    P.main(["--debut", "2026-10-16", "--no-open", "--sources", "playtomic", "--json", "site/data.json"])
    data = json.loads((tmp_path / "site" / "data.json").read_text("utf-8"))
    assert data["tournois"][0]["libelle"] == "AMERICANO"
    assert data["meta"]["sources"] == [{"nom": "Playtomic", "ok": True, "nb": 1}]
    assert not (tmp_path / "tournois_padel_lyon.html").exists()     # --json seul : pas de page autonome


def test_front_charge_data_json():
    page = (ICI / "public" / "index.html").read_text("utf-8")
    assert P.MARQUEUR_DONNEES in page and 'fetch("data.json"' in page
