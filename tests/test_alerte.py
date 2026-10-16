"""Alerte « nouveaux tournois » : détection et message."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import alerte as A  # noqa: E402

TODAY = date(2026, 10, 16)
P2550 = {"P25", "P50"}


def t(lien, niveaux=("P50",), debut="2026-10-23", distance=8.0, source="Ten'Up", **kw):
    base = dict(debut=debut, fin=debut, heure="17:00", niveaux=list(niveaux), libelle="P50 du vendredi",
                club="GONES PADEL", ville="Irigny", distance_km=distance, prix="25 €", inscrits="",
                niveau_playtomic="", sources=[{"nom": source, "lien": lien}])
    base.update(kw)
    return base


def data(*tournois, ko=()):
    sources = [{"nom": n, "ok": n not in ko} for n in ("Ten'Up", "Playtomic", "4PADEL")]
    return {"meta": {"sources": sources}, "tournois": list(tournois)}


def test_seuls_les_tournois_apparus_sont_signales():
    veille = data(t("https://a"))
    jour = data(t("https://a"), t("https://b"))
    assert [x["sources"][0]["lien"] for x in A.nouveaux(veille, jour, P2550, 30, TODAY)] == ["https://b"]


def test_criteres_niveau_distance_et_date():
    veille = data()
    jour = data(t("https://p100", niveaux=["P100"]), t("https://loin", distance=35.0),
                t("https://passe", debut="2026-10-15"), t("https://hors", niveaux=[]), t("https://ok"))
    assert [x["sources"][0]["lien"] for x in A.nouveaux(veille, jour, P2550, 30, TODAY)] == ["https://ok"]
    avec_hors = A.nouveaux(veille, jour, P2550 | {A.HORS}, 30, TODAY)
    assert {x["sources"][0]["lien"] for x in avec_hors} == {"https://hors", "https://ok"}


def test_pas_d_alerte_sans_veille_ni_pour_une_source_revenue():
    jour = data(t("https://b", source="Playtomic"))
    assert A.nouveaux(None, jour, P2550, 30, TODAY) == []
    assert A.nouveaux(data(ko=["Playtomic"]), jour, P2550, 30, TODAY) == []


def test_message():
    titre, corps = A.message([t("https://tenup.fft.fr/tournoi/1")], "https://padel.example")
    assert titre == "🎾 1 nouveau tournoi de padel"
    assert corps == ("• P50 du vendredi\n  ven. 23 oct. 17h00 · GONES PADEL, Irigny (8 km)\n  P50 · 25 €\n"
                     "  https://tenup.fft.fr/tournoi/1\n\nTous les tournois : https://padel.example")
    titre, corps = A.message([t(f"https://{i}") for i in range(12)])
    assert titre.startswith("🎾 12 nouveaux tournois") and corps.endswith("… et 2 autres")
