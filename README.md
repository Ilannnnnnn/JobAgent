![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![LangGraph](https://img.shields.io/badge/LangGraph-0.2%2B-purple)
![Streamlit](https://img.shields.io/badge/Streamlit-1.30%2B-red)

# JobAgent — Agent de Recherche d'Emploi Automatisé

Agent de recherche d'emploi basé sur **LangGraph** qui collecte automatiquement des offres depuis **Adzuna**, **APEC** (via Apify) et **Indeed** (via JobSpy), les score avec **Gemini** en fonction d'un profil candidat, et les présente dans un **dashboard Streamlit** avec suivi des candidatures et adaptation de CV.

---

## Sommaire

1. [Architecture](#architecture)
2. [Prérequis & Installation](#prérequis--installation)
3. [Configuration des APIs](#configuration-des-apis)
4. [Structure du projet](#structure-du-projet)
5. [Utilisation](#utilisation)
6. [Configuration du profil candidat](#configuration-du-profil-candidat)
7. [Dashboard Streamlit](#dashboard-streamlit)

---

## Architecture

Le pipeline est orchestré par un graphe LangGraph composé de 4 nœuds enchaînés avec une décision conditionnelle :

```
START
  └─► collecter
  │     Adzuna + APEC + Indeed collectés en parallèle (ThreadPoolExecutor)
  │     Filtre deal-breakers → déduplication par URL → sauvegarde SQLite
  │
  ├─[nouvelles offres]─► scorer_batch
  │                          Score chaque offre avec Gemini (0–100)
  │                          Explication + points forts + points faibles
  │                          (4 s entre chaque appel pour respecter les quotas)
  │                          │
  │                          ▼
  └─[aucune nouvelle]──► generer_rapport
                              Export texte horodaté → data/offres_YYYYMMDD_HHMM.txt
                              │
                              ▼
                         generer_excel
                              Export data/offres.xlsx
                              Mise en forme colorée : vert ≥85 / jaune ≥60 / gris sinon
                              Filtres automatiques + gel de la première ligne
                              │
                              ▼
                             END
```

### Description des nœuds

| Nœud | Rôle |
|---|---|
| `collecter` | Scrape les 3 sources en parallèle, filtre les deal-breakers (titre + description), déduplique par URL et insère les nouvelles offres en SQLite |
| `scorer_batch` | *(conditionnel)* Score uniquement les offres sans score via Gemini — retourne un entier 0–100, une explication textuelle, une liste de points forts et une liste de points faibles |
| `generer_rapport` | Exporte toutes les offres scorées (score ≥ 0) dans un fichier texte lisible avec classement et analyse |
| `generer_excel` | Génère `data/offres.xlsx` : 12 colonnes, mise en forme couleur par priorité, filtres automatiques, colonne URL cliquable |

**Edge conditionnel `should_score`** : si `new_offers_count > 0` → `scorer_batch`, sinon → `generer_rapport` directement (évite des appels API inutiles).

### Sources de données

| Source | Méthode | Couverture |
|---|---|---|
| **Adzuna** | API REST officielle | Paris, Lyon, Bruxelles, Genève, Amsterdam, Madrid, Barcelone |
| **APEC** | Actor Apify `easyapi~apec-jobs-scraper` | France (offres cadres) |
| **Indeed** | `python-jobspy` (scraping) | France, offres ≤ 72 h |

---

## Prérequis & Installation

**Python 3.10 ou supérieur requis.**

```bash
# 1. Cloner le dépôt
git clone <url-du-repo>
cd JobAgent

# 2. Créer un environnement virtuel (recommandé)
python -m venv .venv
source .venv/bin/activate      # Linux / macOS
.venv\Scripts\activate         # Windows

# 3. Installer les dépendances
pip install -r requirements.txt

# 4. Créer le fichier de variables d'environnement
# Créer un fichier .env à la racine du projet (voir section suivante)
```

Contenu minimal du `.env` :

```env
ADZUNA_APP_ID=votre_app_id
ADZUNA_APP_KEY=votre_app_key
APIFY_API_TOKEN=votre_token_apify
GOOGLE_AI_STUDIO_KEY=votre_cle_gemini

# Optionnel — valeurs par défaut ci-dessous
DB_PATH=data/offers.db
PROFILE_PATH=config/profile.yaml
APIFY_ACTOR_APEC=easyapi~apec-jobs-scraper
```

---

## Configuration des APIs

Trois clés API sont nécessaires pour faire fonctionner l'agent :

### Adzuna API

- **Créer un compte et obtenir les clés** : [https://developer.adzuna.com/](https://developer.adzuna.com/)
- **Variables** : `ADZUNA_APP_ID` et `ADZUNA_APP_KEY`
- Gratuit pour un usage personnel (quota : 250 requêtes/jour)

### Apify — Scraping APEC

- **Créer un compte** : [https://console.apify.com](https://console.apify.com)
- **Obtenir le token** : Settings → API & Integrations → Personal API tokens
- **Variable** : `APIFY_API_TOKEN`
- **Actor utilisé** : `easyapi~apec-jobs-scraper` (configurable via `APIFY_ACTOR_APEC`)
- Plan gratuit disponible (limité en volume de scraping mensuel)

### Google AI Studio — Gemini

- **Créer une clé API** : [https://aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)
- **Variable** : `GOOGLE_AI_STUDIO_KEY`
- Modèle utilisé : `gemini-3.1-flash-lite-preview` (rapide et économique)
- Gratuit dans les limites du quota Google AI Studio

---

## Structure du projet

```
JobAgent/
├── src/
│   ├── pipeline.py       # Orchestration LangGraph — point d'entrée principal
│   ├── dashboard.py      # Dashboard Streamlit + fonction export TXT
│   ├── collector.py      # Scraping Adzuna via API REST
│   ├── scorer.py         # Scoring Gemini via LangChain (réponses structurées Pydantic)
│   ├── cv_adapter.py     # Adaptation du CV HTML pour une offre spécifique
│   └── db.py             # Schéma SQLite centralisé et gestion des connexions
├── config/
│   ├── profile.yaml      # Profil candidat : compétences, critères, deal-breakers
│   └── sources.yaml      # Paramètres des sources : cibles Adzuna, config APEC/Indeed
├── data/                 # Généré automatiquement
│   ├── offers.db         # Base SQLite (toutes les offres)
│   ├── offres.xlsx       # Export Excel permanent (écrasé à chaque run)
│   └── offres_*.txt      # Rapports texte horodatés
├── cv/                   # CV de base (cv_base.html) + CV adaptés générés
├── requirements.txt
└── .env                  # Variables d'environnement (non versionné)
```

---

## Utilisation

### Lancer le pipeline complet

```bash
python src/pipeline.py
```

Le pipeline exécute dans l'ordre :

1. Collecte depuis Adzuna + APEC + Indeed (en parallèle)
2. Scoring des nouvelles offres avec Gemini (si nécessaire)
3. Export du rapport texte dans `data/`
4. Export du fichier Excel `data/offres.xlsx`

Un résumé s'affiche à la fin avec le nombre d'offres collectées, scorées et les chemins des fichiers générés.

### Visualiser le graphe LangGraph

```bash
python src/pipeline.py --visualiser
```

Génère `pipeline_graph.png` à la racine et l'ouvre automatiquement. Si la génération PNG échoue, le diagramme Mermaid est affiché dans le terminal (à coller sur [mermaid.live](https://mermaid.live)).

### Lancer le dashboard

```bash
streamlit run src/dashboard.py
```

Ouvre le dashboard dans le navigateur par défaut (port 8501). Le pipeline peut également être lancé directement depuis la sidebar du dashboard sans quitter l'interface.

---

## Configuration du profil candidat

### `config/profile.yaml`

Ce fichier définit votre identité et vos critères. Il est utilisé à deux endroits :

- Par **Gemini** pour scorer chaque offre (pertinence par rapport à votre profil)
- Par **le filtre deal-breakers** pour exclure les offres indésirables avant même le scoring

```yaml
candidat:
  nom: "Votre Nom"
  poste_cible: "Ingénieur IA"   # Mot-clé de recherche envoyé à APEC et Indeed

competences:
  techniques:
    - Python
    - PyTorch
    - LangGraph
    # Ajouter vos compétences ici

experience:
  annees_totales: 2
  domaines:
    - LLMs et agents autonomes
    # ...

formation:
  niveau: "Bac+5"
  domaines:
    - Intelligence artificielle

criteres:
  localisations_acceptees:
    - "Paris / Île-de-France"
    - "Remote / Télétravail complet"
    # Autres villes acceptées
  types_contrat:
    - CDI
  salaire_min_annuel: 45000
  teletravail_minimum_jours_par_semaine: 2

deal_breakers:
  - "commercial"
  - "call center"
  - "non cadre"
  # Toute offre dont le titre ou la description contient un de ces mots est rejetée
```

**Clés importantes :**

| Clé | Effet |
|---|---|
| `candidat.poste_cible` | Mot-clé envoyé à APEC et Indeed |
| `deal_breakers` | Filtre appliqué avant le scoring — offres rejetées silencieusement |
| `criteres.*` | Transmis à Gemini comme contexte de scoring |
| `competences` + `experience` + `formation` | Profil technique comparé à chaque offre par Gemini |

### `config/sources.yaml`

Ce fichier contrôle ce que chaque source collecte.

```yaml
adzuna:
  cibles:
    - pays: "fr"              # Code pays Adzuna (fr, be, ch, nl, es, gb...)
      localisation: "Paris"
      mots_cles:
        - "Ingénieur IA"
        - "AI Engineer"
    # Ajouter ou supprimer des cibles ici
  search_params:
    max_results_par_cible: 25  # Nombre max d'offres par couple pays+ville

apec:
  location: "France"           # "France" = pas de filtre géo, sinon ex: "Paris"

indeed:
  results_wanted: 50           # Nombre d'offres à récupérer
  hours_old: 72                # Offres publiées dans les N dernières heures
  location: "France"
```

**Codes pays Adzuna supportés :** `fr`, `gb`, `us`, `de`, `nl`, `be`, `ch`, `es`, `it`, `au`, `ca`

---

## Dashboard Streamlit

### Métriques globales

Quatre indicateurs affichés en haut de page :

- **Total offres scorées** — nombre total d'offres avec un score en base
- **Prioritaires (≥ 85)** — offres les plus pertinentes selon Gemini
- **Postulées** — offres dont le statut est "Postulé"
- **Score moyen** — moyenne des scores sur l'ensemble de la base

### Filtres (sidebar)

| Filtre | Type | Description |
|---|---|---|
| Score minimum | Slider 0–100 | Masque les offres sous le seuil choisi |
| Source | Multiselect | Adzuna / Indeed / APEC |
| Contrat | Multiselect | CDI / CDD / Freelance / N/A |
| Statut | Multiselect | À postuler / Postulé / Refusé / Entretien |

Le bouton **"Lancer le pipeline"** en bas de la sidebar exécute `pipeline.py` et affiche les logs en temps réel dans un expander.

### Tableau interactif

- Colonnes : Score · Priorité · Poste · Entreprise · Lieu · Contrat · Salaire · Source · Statut · URL
- Code couleur : vert (≥ 85) / orange (≥ 60) / gris (< 60)
- Cliquer sur une ligne ouvre le panneau de détail

### Panneau de détail

Affiché sous le tableau lors de la sélection d'une ligne :

- Informations complètes de l'offre (poste, entreprise, lieu, contrat, salaire, score)
- **Analyse Gemini** : explication détaillée du score
- **Points forts** (✅) et **points faibles** (❌) identifiés par le LLM
- **Changer le statut** : menu déroulant (À postuler → Postulé → Entretien → Refusé)
- **Ouvrir l'offre** : lien direct vers l'annonce originale
- **Postuler (adapter CV)** : lance `cv_adapter.py` pour générer un CV HTML personnalisé dans `cv/cv_adapte_<id>.html`
- **Supprimer cette offre** : suppression avec confirmation en deux clics
