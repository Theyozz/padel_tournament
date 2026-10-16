# Tournois de padel autour de Lyon

Script personnel qui regroupe les tournois de padel amateur autour de Lyon (Ten'Up, Playtomic, 4PADEL), fusionne les doublons et génère une page HTML locale.

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Playwright est facultatif. Il ne sert que si l'API 4PADEL se remet à publier des tournois :

```bash
.venv/bin/pip install playwright && .venv/bin/python -m playwright install chromium
```

## Utilisation

```bash
.venv/bin/python padel_lyon.py                       # défauts : DM, Senior, P25/P50, 30 km, 4 mois
.venv/bin/python padel_lyon.py --rayon 40 --niveaux P25,P50,P100
.venv/bin/python padel_lyon.py --debut 2026-12-01 --mois 2 --sans-hors-fft
.venv/bin/python padel_lyon.py --csv tournois.csv --no-open
```

| Option | Rôle |
|---|---|
| `--rayon KM` | rayon autour de Lyon (30) |
| `--debut AAAA-MM-JJ`, `--mois N` | période (aujourd'hui, 4 mois) |
| `--niveaux P25,P50` | niveaux FFT retenus (`tous` : la page filtre) |
| `--epreuve DM\|DD\|DX` | double messieurs (défaut), dames, mixte |
| `--sans-hors-fft` | exclure les tournois sans niveau P (americanos, tournois maison) |
| `--sources tenup,playtomic,4padel` | sources interrogées |
| `--playtomic-clubs slug1,slug2` | clubs Playtomic à ajouter à ceux trouvés automatiquement |
| `--csv FICHIER` | export CSV (séparateur `;`, UTF-8 avec BOM pour Excel) |
| `--json FICHIER` | données pour le site (`public/data.json`) |
| `--html FICHIER` | page autonome avec les données intégrées (par défaut `tournois_padel_lyon.html`, sauf si `--json` est donné) |
| `--no-open` | ne pas ouvrir la page dans le navigateur |
| `--rafraichir` | ignorer le cache `.cache/` |

Un lancement prend environ une minute, et quelques secondes de moins quand les fiches Ten'Up sont en cache.

## Site en ligne (Vercel + GitHub Actions)

- `public/index.html` : le front (filtres par niveau, Hors FFT, distance, recherche et tri ; les choix sont mémorisés dans le navigateur). Il charge `public/data.json`.
- `.github/workflows/actualiser.yml` : chaque jour à 5 h UTC (et à la demande, via « Run workflow »), lance `padel_lyon.py --niveaux tous --rayon 40 --json public/data.json`, puis commite `data.json` s'il a changé. Le cache `.cache/` (fiches Ten'Up, clubs Playtomic) est conservé d'un jour à l'autre.
- `vercel.json` : site statique servi depuis `public/`, sans build ; `data.json` n'est jamais mis en cache.

Mise en place :

1. Pousser le projet sur un dépôt GitHub. `.gitignore` exclut `.venv` et `.cache`.
2. Sur Vercel : « Add New Project », importer le dépôt, déployer. Les réglages viennent de `vercel.json`.
3. Dans l'onglet Actions du dépôt, lancer « Actualiser les tournois » une première fois pour vérifier.

Chaque commit de l'Action redéploie le site. Si aucune source ne répond, le script sort avec le code 2 et l'Action ne publie rien : le site garde les données de la veille. Si une seule source échoue, le site l'indique en bas de page. Un bandeau prévient si les données ont plus de 36 h.

En local : `.venv/bin/python -m http.server -d public` puis http://localhost:8000, ou bien `padel_lyon.py` sans `--json` pour générer une page autonome (`tournois_padel_lyon.html`, ouvrable directement).

## Alerte « nouveaux tournois »

Après chaque mise à jour, l'Action lance `alerte.py`. Le script compare `data.json` avec celui de la veille et envoie les tournois apparus depuis. Les notifications passent par [Apprise](https://github.com/caronc/apprise/wiki), qui gère Telegram, Discord, ntfy, l'e-mail, Slack, Signal, Pushover et d'autres.

Dans GitHub : Settings > Secrets and variables > Actions.

| Nom | Type | Rôle |
|---|---|---|
| `ALERTE_URLS` | secret | adresse(s) Apprise, séparées par des espaces. Sans elle, le message est seulement affiché dans le log |
| `ALERTE_NIVEAUX` | variable | niveaux suivis (`P25,P50` par défaut, ajouter `Hors FFT` pour les autres) |
| `ALERTE_RAYON` | variable | distance max en km (30) |
| `ALERTE_SITE` | variable | adresse du site, ajoutée en fin de message |

Pas d'alerte au premier lancement, ni pour une source qui revient après une panne : sinon tous ses tournois seraient signalés comme nouveaux d'un coup. Si l'envoi échoue, la mise à jour du site n'est pas touchée.

En local : `python alerte.py ancien.json public/data.json`.

## Fonctionnement des sources

- **Ten'Up** : l'API publique `POST /back/public/v1/tournois` (paginée) donne la liste des tournois. Pour chaque candidat (bonne épreuve, niveau compatible), la fiche `GET …/{id}/fiche-tournoi` donne le niveau par épreuve, la catégorie (« Senior » ou « 45 ans ») et le tarif. Les fiches restent 48 h en cache. Lien public : `https://tenup.fft.fr/tournoi/{numéro}`.
- **Playtomic** : il n'y a plus d'API publique (`api.playtomic.io` n'existe plus). Les clubs sont trouvés par la recherche du site (`/search?q=Lyon`), la distance est calculée depuis les coordonnées de la page du club (cache 30 jours), puis la page `/clubs/{slug}/competitions/tournaments` est lue. La recherche porte sur le nom des clubs : un club dont le nom ne contient pas « Lyon » doit être ajouté avec `--playtomic-clubs`.
- **4PADEL** : la page utilise l'API JSON `api2-front.lefive.fr/competitions`. Au 16/10/2026, elle ne renvoie aucun tournoi, ni à venir ni passé. Si elle en renvoie un jour, la page est lue avec Playwright (seul le club de Saint-Priest est gardé).

Chaque source est isolée : une erreur est affichée dans le terminal et en bas de page, et les autres sources continuent.

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest
```

Les tests utilisent les réponses réelles enregistrées dans `samples/`, dont les données personnelles des juges-arbitres ont été retirées.
