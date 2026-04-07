# Job Agent — Agent IA de Recherche d'Emploi

Agent de recherche d'emploi automatisé basé sur l'IA. Il collecte les offres France Travail, les score selon votre profil via Claude, et adapte votre CV pour les meilleures opportunités.

## Fonctionnement

```
Votre profil
    │
    ▼
collector.py  ──► API France Travail  ──► Base SQLite
                                               │
                                          scorer.py  ──► Claude API
                                               │
                                         dashboard.py  (top 10 offres)
                                               │
                                        cv_adapter.py  ──► CV adapté .docx
```

## Installation

**Prérequis :** Python 3.10+

```bash
# 1. Installer les dépendances
pip install -r requirements.txt

# 2. Configurer les variables d'environnement
cp .env.example .env
# Éditer .env avec vos clés API (voir section "Clés API" ci-dessous)

# 3. Adapter votre profil
# Éditer config/profile.yaml avec vos compétences et critères

# 4. Ajouter votre CV
# Placer votre CV au format PDF dans : cv/cv_base.pdf
```

## Clés API nécessaires

### Adzuna (collecte des offres)
1. Créer un compte gratuit sur [developer.adzuna.com](https://developer.adzuna.com)
2. Créer une application
3. Copier le **App ID** et le **App Key** dans `.env`

> Quota gratuit : 250 requêtes/jour, amplement suffisant pour un usage personnel.

### Anthropic (Claude)
1. Créer un compte sur [console.anthropic.com](https://console.anthropic.com)
2. Aller dans **API Keys** → Créer une clé
3. Copier la clé dans `.env`

## Utilisation

### 1. Collecter les offres
```bash
python src/collector.py
```
Récupère jusqu'à 50 offres depuis France Travail selon vos critères, filtre les deal-breakers, et les stocke en base.

### 2. Scorer les offres avec Claude
```bash
python src/scorer.py              # Score les 20 prochaines offres
python src/scorer.py --limite 50  # Score jusqu'à 50 offres
python src/scorer.py --rescorer   # Rescorer les offres en erreur
```

### 3. Afficher le dashboard
```bash
python src/dashboard.py              # Top 10 offres scorées
python src/dashboard.py --top 20     # Top 20
python src/dashboard.py --toutes     # Toutes les offres
python src/dashboard.py --detail <ID>  # Détail d'une offre
```

### 4. Adapter le CV pour une offre
```bash
python src/cv_adapter.py --offre-id <ID>
# Le CV adapté est sauvegardé dans cv/cv_adapte_<ID>.docx
```

## Configuration du profil (`config/profile.yaml`)

Le fichier `profile.yaml` contrôle tout le comportement de l'agent :

| Section | Rôle |
|---|---|
| `candidat.poste_cible` | Mot-clé principal de recherche |
| `competences.techniques` | Compétences pour le scoring IA |
| `criteres.localisation` | Code INSEE de votre ville |
| `criteres.types_contrat` | CDI, CDD, etc. |
| `criteres.salaire_min_annuel` | Filtre de scoring IA |
| `deal_breakers` | Mots-clés → offres automatiquement ignorées |

**Trouver votre code INSEE :** [geo.api.gouv.fr/communes](https://geo.api.gouv.fr/communes)

## Structure du projet

```
.
├── config/
│   ├── profile.yaml        # Votre profil et vos critères
│   └── sources.yaml        # Configuration API France Travail
├── cv/
│   ├── cv_base.pdf         # Votre CV de base (à ajouter)
│   └── cv_adapte_*.docx    # CVs générés (ignorés par git)
├── src/
│   ├── db.py               # Module SQLite partagé
│   ├── collector.py        # Collecte des offres
│   ├── scorer.py           # Scoring IA
│   ├── cv_adapter.py       # Adaptation du CV
│   └── dashboard.py        # Interface CLI
├── data/
│   └── offers.db           # Base SQLite (créée automatiquement)
├── .env                    # Vos clés API (ne pas committer)
├── .env.example            # Template des variables d'environnement
└── requirements.txt        # Dépendances Python
```

## Workflow typique

```bash
# Chaque matin ou chaque semaine :
python src/collector.py      # Nouvelles offres
python src/scorer.py         # Évaluation IA
python src/dashboard.py      # Voir les meilleures

# Pour une offre intéressante :
python src/dashboard.py --detail <ID>   # Lire l'analyse
python src/cv_adapter.py --offre-id <ID> # Générer le CV adapté
# Ouvrir cv/cv_adapte_<ID>.docx dans Word, exporter en PDF, postuler
```

## Dépendances

| Librairie | Usage |
|---|---|
| `anthropic` | Claude API (scoring + adaptation CV) |
| `requests` | Appels API France Travail |
| `pyyaml` | Lecture des fichiers de configuration |
| `pdfplumber` | Extraction du texte du CV PDF |
| `python-docx` | Génération du CV adapté en .docx |
| `rich` | Interface CLI colorisée |
| `python-dotenv` | Chargement des variables d'environnement |

## Feuille de route (v2)

- [ ] Automatisation via cron job (scheduler)
- [ ] Sources supplémentaires (LinkedIn, Welcome to the Jungle)
- [ ] Interface web Streamlit
- [ ] Historique des candidatures
- [ ] Feedback loop pour affiner le scoring
