#!/usr/bin/env python3
"""Alerte « nouveaux tournois » : compare data.json de la veille et du jour, et envoie
les tournois apparus qui collent aux critères (niveaux, distance).

    python alerte.py veille.json public/data.json

Réglages par variables d'environnement (dans GitHub : Settings > Secrets and variables > Actions) :
    ALERTE_URLS       secret, une ou plusieurs adresses Apprise séparées par des espaces
                      (tgram://…, discord://…, ntfy://…, mailto://…). Absent : le message est
                      seulement affiché.
    ALERTE_NIVEAUX    niveaux suivis, défaut "P25,P50,Hors FFT"
    ALERTE_RAYON      distance max en km depuis Lyon (défaut 30)
    ALERTE_SITE       adresse du site, ajoutée en fin de message
"""
import json
import os
import sys
from datetime import date
from pathlib import Path

HORS = "Hors FFT"
MAX_LIGNES = 10
JOURS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def lire(path):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return None


def liens(t):
    return {s["lien"] for s in t["sources"] if s.get("lien")}


def correspond(t, niveaux, rayon):
    if t["distance_km"] is not None and t["distance_km"] > rayon:
        return False
    return bool(set(t["niveaux"]) & niveaux) if t["niveaux"] else HORS in niveaux


def nouveaux(veille, jour, niveaux, rayon, today):
    """Tournois du jour absents de la veille. Un tournoi n'est « nouveau » que si toutes ses
    sources avaient répondu la veille : sinon une source revenue après une panne enverrait
    tous ses tournois d'un coup."""
    if not veille:
        return []
    deja = set().union(*(liens(t) for t in veille["tournois"]))
    ok_veille = {s["nom"] for s in veille["meta"]["sources"] if s["ok"]}
    return [t for t in jour["tournois"]
            if t["debut"] >= today.isoformat()
            and liens(t) and not liens(t) & deja
            and all(s["nom"] in ok_veille for s in t["sources"])
            and correspond(t, niveaux, rayon)]


def ligne(t):
    d = date.fromisoformat(t["debut"])
    quand = f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]}" + (f" {t['heure'].replace(':', 'h')}" if t["heure"] else "")
    ou = ", ".join(filter(None, [t["club"], t["ville"]]))
    if t["distance_km"] is not None:
        ou += f" ({t['distance_km']:g} km)".replace(".", ",")
    details = " · ".join(filter(None, ["/".join(t["niveaux"]) or HORS, t["prix"]]))
    lien = sorted(liens(t))[0]
    return f"• {t['libelle']}\n  {quand} · {ou}\n  {details}\n  {lien}"


def message(tournois, site=""):
    n = len(tournois)
    titre = f"🎾 {n} nouveau{'x' if n > 1 else ''} tournoi{'s' if n > 1 else ''} de padel"
    corps = "\n\n".join(ligne(t) for t in tournois[:MAX_LIGNES])
    if n > MAX_LIGNES:
        corps += f"\n\n… et {n - MAX_LIGNES} autre{'s' if n - MAX_LIGNES > 1 else ''}"
    if site:
        corps += f"\n\nTous les tournois : {site}"
    return titre, corps


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    veille, jour = lire(argv[0]), lire(argv[1])
    if not jour:
        sys.exit(f"{argv[1]} illisible")
    niveaux = {n.strip() for n in (os.environ.get("ALERTE_NIVEAUX") or "P25,P50,Hors FFT").split(",") if n.strip()}
    rayon = float(os.environ.get("ALERTE_RAYON") or 30)
    liste = nouveaux(veille, jour, niveaux, rayon, date.today())
    if not liste:
        print("Aucun nouveau tournoi" + ("" if veille else " (pas de données de la veille)"))
        return
    titre, corps = message(liste, os.environ.get("ALERTE_SITE", ""))
    print(f"{titre}\n\n{corps}")
    urls = os.environ.get("ALERTE_URLS", "").split()
    if not urls:
        print("\nALERTE_URLS non défini : rien n'est envoyé.")
        return
    import apprise
    ap = apprise.Apprise()
    for u in urls:
        ap.add(u)
    if not ap.notify(title=titre, body=corps):
        sys.exit("Échec de l'envoi de l'alerte")


if __name__ == "__main__":
    main(sys.argv[1:])
