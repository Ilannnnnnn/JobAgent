# Rapport Technique — JobAgent

> Analyse exhaustive du codebase. Basée sur la lecture intégrale de tous les fichiers sources.

---

## Table des matières

1. [Vue d'ensemble](#1-vue-densemble)
2. [Architecture de l'agent LangGraph](#2-architecture-de-lagent-langgraph)
3. [Sources de scraping](#3-sources-de-scraping)
4. [Pipeline de données complet](#4-pipeline-de-données-complet)
5. [Base de données SQLite](#5-base-de-données-sqlite)
6. [Dashboard Streamlit](#6-dashboard-streamlit)
7. [Adaptation du CV](#7-adaptation-du-cv)
8. [Configuration](#8-configuration)
9. [Flows d'erreur et résilience](#9-flows-derreur-et-résilience)
10. [Limitations connues et dette technique](#10-limitations-connues-et-dette-technique)
11. [Guide de lancement](#11-guide-de-lancement)
12. [Anomalies détectées](#12-anomalies-détectées)

---

## 1. Vue d'ensemble

### Objectif

JobAgent est un agent autonome de recherche d'emploi. Il collecte des offres depuis quatre sources (Adzuna, APEC, Indeed, Welcome to the Jungle), les score automatiquement avec Google Gemini, et présente les résultats dans un dashboard Streamlit interactif. Le candidat peut suivre ses candidatures, comparer les salaires au marché, et générer un CV HTML adapté à chaque offre via LLM.

### Philosophie de conception

**Pourquoi LangGraph ?** L'orchestration est conditionnelle : si aucune nouvelle offre n'est collectée, le scoring est inutile et doit être sauté. LangGraph permet d'exprimer cette logique comme un graphe d'états avec arêtes conditionnelles, plutôt que comme une suite de `if/else` dans un script monolithique. L'état (`AgentState`) est explicite et circule entre les nœuds, rendant le flux auditable.

**Pourquoi LangChain ?** Deux raisons : le `structured output` via Pydantic (validation automatique du JSON retourné par Gemini, sans `json.loads()` fragile), et le streaming pour l'adaptation de CV (`llm.stream()`).

**Pourquoi SQLite ?** Base zéro-infra pour un usage solo. La déduplication par contrainte `UNIQUE` sur l'URL est native et fiable. Les migrations sont des `ALTER TABLE` + `UPDATE` inline dans `init_db()`, idempotentes.

### Structure des fichiers

```
JobAgent/
├── src/
│   ├── pipeline.py      # Graphe LangGraph + scrapers APEC/WTTJ/Indeed/Adzuna
│   ├── collector.py     # Scraper Adzuna (API REST) + helpers partagés
│   ├── scorer.py        # Scoring Gemini via LangChain structured output
│   ├── db.py            # Schéma SQLite, connexions, migrations
│   ├── dashboard.py     # Interface Streamlit + export TXT
│   └── cv_adapter.py    # Adaptation HTML du CV via Gemini streaming
├── config/
│   ├── profile.yaml     # Profil candidat, critères, deal-breakers
│   └── sources.yaml     # Paramètres de scraping par source
├── cv/
│   └── cv_base.html     # CV de base (non versionné)
├── data/                # DB SQLite + rapports générés (non versionné)
├── .env                 # Secrets API (non versionné en prod)
└── requirements.txt
```

---

## 2. Architecture de l'agent LangGraph

### Graphe d'exécution

```
START
  │
  ▼
┌─────────────┐
│  collecter  │  Nœud 1 — scraping parallèle des 4 sources
└──────┬──────┘
       │
       ▼
  should_score()  ← edge conditionnel
       │
  ┌────┴─────────────────┐
  │ new_offers_count > 0  │ new_offers_count == 0
  ▼                       ▼
┌──────────────┐    ┌─────────────────┐
│ scorer_batch │    │ generer_rapport │
└──────┬───────┘    └────────┬────────┘
       │                     │
       └──────────┬──────────┘
                  ▼
         ┌─────────────────┐
         │ generer_rapport │  Nœud 3
         └────────┬────────┘
                  ▼
         ┌─────────────────┐
         │ generer_excel   │  Nœud 4
         └────────┬────────┘
                  ▼
                 END
```

### AgentState (`pipeline.py`, ligne 68)

```python
class AgentState(TypedDict):
    profile: dict           # Non utilisé activement (réservé pour usage futur)
    new_offers_count: int   # Nombre d'offres nouvellement insérées en DB
    scored_count: int       # Nombre d'offres scorées avec succès lors de ce run
    report_path: Optional[str]  # Chemin absolu du rapport .txt généré
    excel_path: Optional[str]   # Chemin absolu du fichier .xlsx généré
```

Chaque nœud reçoit l'état complet via `state: AgentState` et retourne un dictionnaire partiel `{**state, "clé": valeur}` — LangGraph fusionne les clés.

### Nœud 1 — `collecter` (ligne 415)

**Rôle :** Lancer les quatre scrapers en parallèle, dédupliquer, filtrer les deal-breakers, sauvegarder en DB.

**Inputs :** Variables d'environnement (`ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `APIFY_API_TOKEN`, `DB_PATH`), `profile.yaml`, `sources.yaml`.

**Outputs :** `state["new_offers_count"]` = total nouvelles offres insérées.

**Logique interne :**
1. `ThreadPoolExecutor(max_workers=4)` lance simultanément `_scraper_adzuna`, `_scraper_apec`, `_scraper_wttj`, `_scraper_jobspy`.
2. Adzuna insère directement en DB et retourne un `int`. Les trois autres retournent des `list[dict]` au format unifié.
3. Déduplication en mémoire des offres APEC/WTTJ/Indeed par URL via un `set`.
4. Filtrage deal-breakers : concaténation `titre + description` en minuscules, test `any(kw in texte for kw in deal_breakers)`.
5. Conversion au format DB via `_unifier_vers_db()` (génère l'ID par `md5(url)[:12]`).
6. Pré-filtre SQL : `_filtrer_urls_existantes()` interroge la DB pour ne pas tenter d'insérer ce qui existe déjà.
7. `sauvegarder_offres()` fait des `INSERT OR IGNORE` et compte les `rowcount > 0`.

### Nœud 2 — `scorer_batch` (ligne 516)

**Rôle :** Scorer toutes les offres dont `score IS NULL` avec Gemini.

**Inputs :** `GOOGLE_AI_STUDIO_KEY`, `PROFILE_PATH`, `DB_PATH`.

**Outputs :** `state["scored_count"]`.

**Logique interne :**
1. Charge `profile.yaml`, formate le profil via `formater_profil()` (chaîne texte structurée).
2. Instancie `ChatGoogleGenerativeAI(model="gemini-3.1-flash-lite-preview", temperature=0.2, max_output_tokens=1024)`.
3. Pour chaque offre : appelle `scorer_offre()` → `mettre_a_jour_score()`.
4. `time.sleep(4)` entre chaque appel pour respecter les quotas Gemini.

### Edge conditionnel — `should_score` (ligne 504)

```python
def should_score(state: AgentState) -> str:
    if state["new_offers_count"] > 0:
        return "scorer_batch"
    return "generer_rapport"
```

C'est la seule décision agentique du graphe. Si zéro nouvelle offre → on saute le scoring (inutile de re-scorer ce qui l'est déjà) et on passe directement à la génération du rapport.

### Nœud 3 — `generer_rapport` (ligne 571)

**Rôle :** Exporter toutes les offres scorées (`score >= 0`) en fichier `.txt` horodaté.

**Inputs :** `DB_PATH`.

**Outputs :** `state["report_path"]` = `"data/offres_YYYYMMDD_HHMM.txt"`.

**Logique :** Requête `WHERE score IS NOT NULL AND score >= 0 ORDER BY score DESC`, puis appel à `exporter_txt()` (définie dans `dashboard.py`).

### Nœud 4 — `generer_excel` (ligne 600)

**Rôle :** Exporter toutes les offres scorées dans `data/offres.xlsx` (fichier permanent, écrasé à chaque run).

**Inputs :** `DB_PATH`.

**Outputs :** `state["excel_path"]` = `"data/offres.xlsx"`.

**Logique :**
- 12 colonnes : Score, Priorité, Poste, Entreprise, Lieu, Contrat, Salaire, Source, Analyse, Points forts, Points faibles, URL.
- Code couleur via `PatternFill` : vert (`#C6EFCE`) si score ≥ 85, jaune (`#FFEB9C`) si ≥ 60, gris (`#F2F2F2`) sinon.
- `ws.auto_filter.ref = ws.dimensions` active les filtres sur toutes les colonnes.
- `ws.freeze_panes = "A2"` fige la ligne d'en-tête.
- Largeurs de colonnes définies manuellement : `[8, 20, 35, 25, 20, 15, 20, 12, 60, 40, 40, 50]`.

---

## 3. Sources de scraping

### 3.1 Adzuna — API REST

**Fichier :** `src/collector.py` + `_scraper_adzuna()` dans `pipeline.py` (ligne 101).

**Mécanisme :** Appels HTTP GET paginés à `https://api.adzuna.com/v1/api/jobs/{pays}/search/{page}`.

**Paramètres configurables dans `sources.yaml` :**
| Paramètre | Chemin YAML | Défaut | Effet |
|---|---|---|---|
| Cibles | `adzuna.cibles[]` | 7 cibles | Pays + ville + mots-clés à rechercher |
| Max résultats | `adzuna.search_params.max_results_par_cible` | 25 | Limite par cible (avant filtrage) |
| Timeout | `adzuna.timeout_seconds` | 30 | Timeout HTTP en secondes |

**Paramètres injectés depuis `profile.yaml` :**
- `criteres.distance_km` → param `distance` de l'API Adzuna.
- `criteres.salaire_min_annuel` → param `salary_min`.

**Format brut retourné (champs utilisés) :**
```json
{
  "id": "...",
  "title": "...",
  "description": "...",
  "company": {"display_name": "..."},
  "location": {"display_name": "..."},
  "contract_time": "full_time|part_time",
  "contract_type": "permanent|contract",
  "salary_min": 45000,
  "salary_max": 60000,
  "created": "2024-01-15T...",
  "redirect_url": "https://..."
}
```

**Mapping vers format interne (`normaliser_offre()`, ligne 141) :**
- `id` → `adzuna_{pays}_{id_brut}`
- `title` → `intitule`
- `company.display_name` → `entreprise_nom`
- `location.display_name` → `lieu_travail`
- `contract_time` + `contract_type` → `type_contrat` (logique combinatoire : `permanent` écrase `full_time`)
- `salary_min`/`salary_max` → `salaire_libelle` formaté (ex : `"45 000 – 60 000 €/an"`)
- `redirect_url` → `url`

**Limites :** 25 offres par cible (configurable), 7 cibles × 2 mots-clés = ~14 appels API par run. L'API Adzuna a une limite gratuite de 250 requêtes/jour. Pause de 1 s entre pages et entre cibles.

### 3.2 APEC — Apify actor

**Fichier :** `_scraper_apec()` dans `pipeline.py` (ligne 139).

**Mécanisme :** POST vers l'API Apify pour lancer l'actor `easyapi~apec-jobs-scraper` (configurable via `APIFY_ACTOR_APEC`). Polling toutes les 5 s pendant 180 s max. Récupération du dataset via GET.

**Paramètres configurables dans `sources.yaml` :**
| Paramètre | Chemin YAML | Défaut | Effet |
|---|---|---|---|
| Localisation | `apec.location` | `"France"` | Filtre géographique dans l'URL APEC |
| Termes de recherche | `apec.search_terms[]` | 4 termes | Un run Apify unique avec toutes les searchUrls |

**Construction des URLs :** Pour chaque terme, une URL APEC est construite avec les 4 types de contrat (`typesConvention=143684&143685&143686&143687`). Toutes les URLs sont passées dans un seul run Apify avec `maxItems: 30`.

**Format brut retourné (champs utilisés) :**
```json
{
  "numeroOffre": "140HTBN",
  "intitule": "...",
  "entreprise": {"nom": "..."},
  "lieuTexte": "...",
  "texteOffre": "...",
  "datePublication": "...",
  "salaireTexte": "...",
  "typeContrat": 101888
}
```

**Mapping type de contrat :**
| Code APEC | Libellé |
|---|---|
| 101887 | CDD |
| 101888 | CDI |
| 101889 | Interim |
| 101890 | Freelance |
| 101906 | Alternance |
| 597137 | CDI |

**URL générée :** `https://www.apec.fr/candidat/recherche-emploi.html/emploi/detail-offre/{numeroOffre}`

**Limites :** `maxItems: 30` hardcodé dans le payload Apify. Timeout Apify : 180 s. Dépend du plan Apify (run payant).

### 3.3 Welcome to the Jungle — Apify actor

**Fichier :** `_scraper_wttj()` dans `pipeline.py` (ligne 259).

**Mécanisme :** Identique à APEC — actor `bebity~welcome-to-the-jungle-jobs-scraper` (configurable via `APIFY_ACTOR_WTTJ`).

**Paramètres configurables dans `sources.yaml` :**
| Paramètre | Chemin YAML | Défaut | Effet |
|---|---|---|---|
| Max items | `wttj.max_items` | 30 | Limite Apify |

**URL de recherche construite :** `https://www.welcometothejungle.com/fr/jobs?query={search_term}&refinementList[contract_type][]=CDI`

Le `search_term` est `profil["candidat"]["poste_cible"]` = `"Ingénieur IA"`. Le filtre `CDI` est hardcodé dans l'URL.

**Champs mappés depuis le dataset Apify :**
- `name` ou `title` → `titre`
- `company.name` → `entreprise`
- `office.city` → `localisation`
- `description` → `description`
- `url` → `url`
- `publishedAt` → `date_publication`
- `salary` → `salaire`

**Limites :** 30 items max. Filtre CDI hardcodé. Un seul terme de recherche.

### 3.4 Indeed + Google Jobs — JobSpy

**Fichier :** `_scraper_jobspy()` dans `pipeline.py` (ligne 346).

**Mécanisme :** Bibliothèque `python-jobspy` qui scrape Indeed et Google Jobs via leur HTML. Retourne un DataFrame pandas converti en `list[dict]`.

**Paramètres configurables dans `sources.yaml` :**
| Paramètre | Chemin YAML | Défaut | Effet |
|---|---|---|---|
| Résultats | `indeed.results_wanted` | 50 | Nombre d'offres cibles |
| Fraîcheur | `indeed.hours_old` | 72 | Offres publiées dans les dernières N heures |
| Localisation | `indeed.location` | `"France"` | Région de recherche |

**Appel interne :**
```python
scrape_jobs(
    site_name=["indeed", "google"],
    search_term=search_term,           # "Ingénieur IA"
    google_search_term=f"{search_term} jobs {location}",
    location=location,
    results_wanted=results_wanted,
    hours_old=hours_old,
    country_indeed="France",
)
```

**Champs mappés depuis le DataFrame :**
- `title` → `titre`
- `company` → `entreprise`
- `location` → `localisation`
- `description`/`job_description` → `description`
- `job_url`/`url` → `url`
- `site` → `source` (`"indeed"` ou `"google"`)
- `date_posted` → `date_publication`

**Limites :** Dépend de la disponibilité du scraping HTML d'Indeed/Google (peut casser si les sites changent leur structure). Pas de rate limiting explicite dans le code.

---

## 4. Pipeline de données complet

### Chemin d'une offre de la collecte à l'affichage

```
1. COLLECTE (nœud collecter)
   └─ Scraper source → list[dict] (format brut)
   └─ normaliser_offre() ou _unifier_vers_db() → format DB unifié
   └─ contient_deal_breaker() → filtre exclusion
   └─ _filtrer_urls_existantes() → filtre DB (évite double INSERT)
   └─ sauvegarder_offres() → INSERT OR IGNORE → SQLite

2. SCORING (nœud scorer_batch)
   └─ SELECT * FROM offres WHERE score IS NULL
   └─ formater_profil(profil) → chaîne texte du profil
   └─ formater_offre(offre) → chaîne texte de l'offre
   └─ Concaténation : profil_texte + "\n\n---\n\n" + offre_texte
   └─ llm.with_structured_output(ScoringResult, method="json_mode")
   └─ structured_llm.invoke([SystemMessage, HumanMessage])
   └─ ScoringResult validé par Pydantic (score: int ge=0 le=100)
   └─ mettre_a_jour_score() → UPDATE offres SET score=?, ...

3. EXPORT RAPPORT (nœud generer_rapport)
   └─ SELECT * FROM offres WHERE score >= 0 ORDER BY score DESC
   └─ exporter_txt() → data/offres_YYYYMMDD_HHMM.txt

4. EXPORT EXCEL (nœud generer_excel)
   └─ Même requête
   └─ openpyxl Workbook → data/offres.xlsx (écrasé)

5. AFFICHAGE DASHBOARD (dashboard.py)
   └─ charger_offres() → SELECT * WHERE score >= 0 → DataFrame
   └─ Filtres Streamlit (score, source, contrat, statut)
   └─ Tableau interactif avec sélection de ligne
   └─ Panneau détail : badges HTML, graphique salaire, info entreprise
```

### Construction du prompt de scoring

Le prompt envoyé à Gemini est une concaténation en deux blocs séparés par `---` :

**Bloc 1 — Profil candidat** (généré par `formater_profil()`, `scorer.py` ligne 82) :
```
## PROFIL CANDIDAT
Poste cible : Ingénieur IA
Expérience : 2 ans
Compétences techniques : Python, PyTorch, HuggingFace, LangGraph, ...
Soft skills : Autonomie, Curiosité technique, ...

## CRITÈRES
Types de contrat acceptés : CDI
Salaire minimum : 45000 €/an brut
Télétravail souhaité : Oui
Localisation acceptée : Paris / Île-de-France, Bruxelles, ...
Secteurs préférés : Intelligence artificielle, ...

## PRÉFÉRENCES ENTREPRISE
Taille : Toutes tailles
Culture : Innovation / R&D, Agilité / Scrum, ...
```

**Bloc 2 — Offre** (généré par `formater_offre()`, `scorer.py` ligne 112) :
```
## OFFRE D'EMPLOI
Titre : ...
Entreprise : ...
Lieu : ...
Type de contrat : ...
Salaire : ...

### Description de l'offre
[description[:3000]]
```

Le `SYSTEM_PROMPT_SCORING` définit le barème (90-100 parfait, 70-89 bon, 50-69 partiel, 30-49 faible, 0-29 inadapté) et les critères d'évaluation par ordre d'importance.

**Format de réponse (Pydantic `ScoringResult`) :**
```python
score: int           # ge=0, le=100 — validé automatiquement
explication: str     # Résumé 2-3 phrases
points_forts: list[str]
points_faibles: list[str]
```

---

## 5. Base de données SQLite

### Schéma complet de la table `offres`

| Colonne | Type | Valeur par défaut | Description |
|---|---|---|---|
| `id` | TEXT PRIMARY KEY | — | `adzuna_{pays}_{id}` ou `{source}_{md5(url)[:12]}` |
| `intitule` | TEXT | — | Titre du poste |
| `description` | TEXT | — | Description complète de l'offre |
| `entreprise_nom` | TEXT | — | Nom de l'entreprise |
| `lieu_travail` | TEXT | — | Localisation |
| `type_contrat` | TEXT | — | CDI, CDD, Freelance, Interim, Alternance, Temps plein, ... |
| `salaire_libelle` | TEXT | — | Salaire lisible (ex : `"45 000 – 60 000 €/an"`) |
| `date_creation` | TEXT | — | Date de publication côté source |
| `url` | TEXT | — | URL directe vers l'offre (**contrainte UNIQUE**) |
| `raw_json` | TEXT | — | JSON brut de la source pour débogage |
| `score` | INTEGER | NULL | Score Gemini 0-100, NULL si non scoré, -1 si erreur |
| `score_explication` | TEXT | NULL | Analyse Gemini en 2-3 phrases |
| `score_points_forts` | TEXT | NULL | JSON array de strings |
| `score_points_faibles` | TEXT | NULL | JSON array de strings |
| `score_date` | TEXT | NULL | Timestamp du scoring (`datetime('now')`) |
| `collected_at` | TEXT | `datetime('now')` | Timestamp d'insertion |
| `statut` | TEXT | `'À postuler'` | État de la candidature |
| `source` | TEXT | NULL | `apec`, `adzuna`, `indeed`, `wttj`, `google` |

### Index

```sql
CREATE INDEX IF NOT EXISTS idx_score ON offres(score DESC)
-- Accélère ORDER BY score DESC dans dashboard et rapport

CREATE UNIQUE INDEX IF NOT EXISTS idx_url ON offres(url)
-- Prévient les doublons cross-sources
-- Précédé d'un DELETE des doublons (garde le MIN(rowid))
```

### Migrations dans `init_db()` (`db.py` ligne 24)

Toutes idempotentes, exécutées à chaque démarrage dans cet ordre :

1. **`CREATE TABLE IF NOT EXISTS offres`** — création initiale avec 16 colonnes.
2. **`CREATE INDEX IF NOT EXISTS idx_score`**
3. **Déduplication pré-unique** : `DELETE FROM offres WHERE rowid NOT IN (SELECT MIN(rowid) FROM offres GROUP BY url)` — nécessaire pour que l'index unique ne lève pas `IntegrityError` si des doublons existent.
4. **`CREATE UNIQUE INDEX IF NOT EXISTS idx_url`**
5. **Migration `statut`** : `ALTER TABLE offres ADD COLUMN statut TEXT DEFAULT 'À postuler'` — ignoré si colonne déjà présente (`except sqlite3.OperationalError: pass`).
6. **Migration `source`** : `ALTER TABLE offres ADD COLUMN source TEXT` — idem.
7. **Inférence `source`** : `UPDATE offres SET source = CASE WHEN id LIKE 'apec%' THEN 'apec' ...` — remplit la colonne `source` pour les offres sans valeur.
8. **Migration codes contrat APEC** : remplace `'101887'` → `'CDD'`, `'101888'` → `'CDI'`, etc. — corrige les données brutes insérées avant la normalisation dans le scraper.
9. **Suppression URLs APEC invalides** : `DELETE FROM offres WHERE source = 'apec' AND url LIKE '%typesConvention%'` — purge les offres avec URLs de recherche au lieu d'URLs de détail.
10. **Correction URLs APEC manquantes** : insère le segment `/detail-offre/` dans les URLs qui en seraient dépourvues.

### Déduplication

Trois couches successives :
1. **En mémoire (Adzuna)** : `offres_par_id[offre.get("id", "")] = offre` — dict indexé par ID Adzuna.
2. **En mémoire (APEC/WTTJ/Indeed)** : `seen_urls: set[str]` dans `collecter()`.
3. **SQL** : `_filtrer_urls_existantes()` interroge la DB avant insertion, puis `INSERT OR IGNORE` + contrainte `UNIQUE` sur `url`.

---

## 6. Dashboard Streamlit

**Lancement :** `streamlit run src/dashboard.py`

### Filtres (sidebar)

| Filtre | Widget | Comportement |
|---|---|---|
| Score minimum | `st.sidebar.slider(0, 100, défaut=60)` | Exclut les offres sous le seuil |
| Source | `st.sidebar.multiselect(["Adzuna","Indeed","Apec","Wttj","Google"])` | Filtre par `source.str.lower()` |
| Contrat | `st.sidebar.multiselect(["CDI","CDD","Freelance","N/A"])` | `"N/A"` capture les valeurs vides |
| Statut | `st.sidebar.multiselect(["À postuler","Postulé","Refusé","Entretien"])` | Filtre sur colonne `statut` |

### Métriques (4 colonnes)

- **Total offres scorées** : `len(df_complet)` (avant filtres)
- **Prioritaires (≥ 85)** : `(df_complet["score"] >= 85).sum()`
- **Postulées** : `(df_complet["statut"] == "Postulé").sum()`
- **Score moyen** : `df_complet["score"].mean()` arrondi à 1 décimale

### Tableau principal

10 colonnes affichées : Score, Priorité, Poste, Entreprise, Lieu, Contrat, Salaire, Source, Statut, URL.

- Coloration via `df.style.apply(colorier_texte, axis=1)` : vert (`#1a6b3a`) si ≥ 85, orange (`#7a4f00`) si ≥ 60, gris (`#666666`) sinon.
- Colonne Score en gras via `.map(lambda _: "font-weight: bold", subset=["Score"])`.
- URL affichée comme lien cliquable via `st.column_config.LinkColumn`.
- Sélection ligne unique : `on_select="rerun"`, `selection_mode="single-row"`.

### Vue détail — les 5 blocs

**Bloc 1 — En-tête** (`st.container(border=True)`) :
- Titre du poste en `## heading`.
- Badges HTML colorés : score, priorité, source (via `rendre_badges_html()`).
- Ligne infos : entreprise · lieu · contrat · salaire.
- Bouton **"Ouvrir l'offre"** (`st.link_button`) → ouvre l'URL dans le navigateur.
- Bouton **"Postuler (adapter CV)"** → `subprocess.run(["python", "src/cv_adapter.py", "--offre-id", offre_id])`. Affiche `cv/cv_adapte_{id_court}.html` en cas de succès.
- **Selectbox Statut** : met à jour `statut` en DB via `mettre_a_jour_statut()` si la valeur change, puis `st.rerun()`.

**Bloc 2 — Analyse IA + Points clés** (2 colonnes) :
- Colonne gauche : `score_explication` (texte Gemini).
- Colonne droite : `score_points_forts` en vert (`#198754`), `score_points_faibles` en rouge (`#dc3545`), parsés depuis JSON.

**Bloc 3 — Salaire vs marché + Entreprise** (2 colonnes) :
- Colonne gauche : graphique HTML généré par `construire_graphique_salaire()` — barre de marché, trait vert pour l'offre courante, médiane en bleu. Données issues de `SELECT salaire_libelle FROM offres WHERE salaire_libelle IS NOT NULL`.
- Colonne droite : info entreprise via `obtenir_info_entreprise()` (Tavily API) ou simplement le nom si pas de clé.

**Bloc 4 — Description** :
- Premiers 400 caractères affichés + `"…"`.
- `st.expander("Voir la description complète")` pour le texte intégral.

**Bloc 5 — Zone dangereuse** (`st.expander`) :
- Bouton "Supprimer cette offre" → stocke `confirm_key` dans `st.session_state`.
- Si confirmé → `DELETE FROM offres WHERE id = ?` + `st.rerun()`.

### Bouton "Lancer le pipeline"

```python
proc = subprocess.Popen(
    ["python", "src/pipeline.py"],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True, encoding="utf-8", errors="replace",
)
for line in proc.stdout:
    lines.append(line.rstrip())
    log_placeholder.code("\n".join(lines[-50:]))  # affiche les 50 dernières lignes
proc.wait()
```

Affichage en temps réel dans un `st.expander("Logs pipeline")`. `st.rerun()` à la fin pour rafraîchir le tableau.

---

## 7. Adaptation du CV

**Fichier :** `src/cv_adapter.py`

### Fonctionnement général

1. Récupère l'offre depuis la DB via `SELECT * FROM offres WHERE id = ?`.
2. Lit le CV HTML de base (`CV_PATH` = `cv/cv_base.html`).
3. Charge le portfolio HTML local (`PORTFOLIO_PATH` = `C:/repos/Portfolio`) — tous les `.html` récursivement.
4. Construit le contexte candidat = `profile.yaml` brut + texte extrait du portfolio.
5. Envoie à Gemini en streaming le `SYSTEM_PROMPT_CV` + le prompt construit.
6. Nettoie la réponse (supprime les blocs markdown éventuels).
7. Sauvegarde dans `cv/cv_adapte_{id_court}.html`.

### Base de connaissance

**`profile.yaml` :** Lu tel quel (YAML brut) et injecté dans le contexte candidat — Gemini voit les compétences, l'expérience, les critères.

**Portfolio HTML (`charger_portfolio()`, ligne 49) :**
- `Path(portfolio_path).rglob("*.html")` — tous les fichiers HTML récursivement.
- Suppression des balises `script`, `style`, `nav`, `footer`, `head` via BeautifulSoup.
- Texte tronqué à 8 000 caractères par page.
- Format : `=== nom_fichier.html ===\n{texte}`.

### Prompt de l'adaptation

`construire_prompt()` (ligne 134) envoie :
- Fiche offre complète (titre, entreprise, lieu, contrat, salaire, description[:3000]).
- Contexte candidat (`profile.yaml` + portfolio).
- Le HTML intégral du CV à adapter.

**Ce que Gemini peut modifier :**
- `div.cv-title` — titre du poste
- `div.cv-accroche` — accroche
- `li` dans `cv-entry-bullets` — bullet points d'expériences
- Liste des compétences — réordonnancement

**Ce que Gemini ne doit pas toucher :**
- `div.cv-name`, `div.cv-contact`, dates, entreprises, diplômes, CSS, attributs HTML.

### Paramètres LLM pour le CV

```python
ChatGoogleGenerativeAI(
    model="gemini-3.1-flash-lite-preview",
    temperature=0.2,
    max_output_tokens=8192,  # Plus élevé que pour le scoring (1024)
)
```

Le streaming (`llm.stream()`) accumule les chunks pour éviter un timeout sur un long HTML.

### Nettoyage post-génération

`nettoyer_html()` (ligne 183) supprime les blocs markdown que Gemini ajoute parfois malgré les instructions :
```python
re.sub(r"^```(?:html)?\s*\n?", "", html.strip())
re.sub(r"\n?```\s*$", "", html.strip())
```

---

## 8. Configuration

### Variables d'environnement (fichier `.env`)

| Variable | Obligatoire | Valeur par défaut dans le code | Usage |
|---|---|---|---|
| `ADZUNA_APP_ID` | Oui | — | Auth API Adzuna — vérifié dans `pipeline.main()` et `collector.main()` |
| `ADZUNA_APP_KEY` | Oui | — | Auth API Adzuna |
| `APIFY_API_TOKEN` | Non | `""` | Scraping APEC et WTTJ — si absent, ces sources sont ignorées avec warning |
| `APIFY_ACTOR_APEC` | Non | `"easyapi~apec-jobs-scraper"` | ID de l'actor Apify APEC |
| `APIFY_ACTOR_WTTJ` | Non | `"bebity~welcome-to-the-jungle-jobs-scraper"` | ID de l'actor Apify WTTJ |
| `GOOGLE_AI_STUDIO_KEY` | Oui | — | Auth Gemini — vérifié dans `pipeline.main()`, `scorer.main()`, `cv_adapter.main()` |
| `DB_PATH` | Non | `"data/offers.db"` | Chemin de la base SQLite |
| `PROFILE_PATH` | Non | `"config/profile.yaml"` | Chemin du profil candidat |
| `SOURCES_PATH` | Non | `"config/sources.yaml"` | Chemin de la config sources |
| `CV_PATH` | Non | `"cv/cv_base.html"` | Chemin du CV HTML de base |
| `OUTPUT_DIR` | Non | `"cv/"` | Dossier de sortie des CV adaptés |
| `TAVILY_API_KEY` | Non | `""` | Info entreprise dans le dashboard — si absent, affiche juste le nom |
| `PORTFOLIO_PATH` | Non | `""` | Chemin du portfolio HTML local pour l'adaptation CV |

### Paramètres `profile.yaml`

| Paramètre | Effet dans le code |
|---|---|
| `candidat.poste_cible` | Terme de recherche WTTJ et JobSpy ; affiché dans `formater_profil()` |
| `competences.techniques[]` | Injecté dans le prompt de scoring |
| `competences.soft_skills[]` | Injecté dans le prompt de scoring |
| `experience.annees_totales` | Affiché dans le prompt de scoring |
| `criteres.distance_km` | Paramètre `distance` de l'API Adzuna |
| `criteres.salaire_min_annuel` | Paramètre `salary_min` de l'API Adzuna + affiché dans le prompt de scoring |
| `criteres.types_contrat[]` | Affiché dans le prompt de scoring (pas utilisé pour filtrer) |
| `criteres.teletravail_souhaite` | Affiché dans le prompt de scoring |
| `criteres.localisations_acceptees[]` | Affiché dans le prompt de scoring |
| `criteres.secteurs_preferes[]` | Affiché dans le prompt de scoring |
| `preferences_entreprise.*` | Injectés dans le prompt de scoring |
| `deal_breakers[]` | Filtre actif dans `contient_deal_breaker()` — offres exclues si un mot-clé est trouvé dans titre+description |

### Paramètres `sources.yaml`

| Paramètre | Effet dans le code |
|---|---|
| `adzuna.base_url` | URL de base des appels API Adzuna |
| `adzuna.cibles[]` | Boucle de recherche dans `_scraper_adzuna()` et `collector.main()` |
| `adzuna.search_params.max_results_par_cible` | Limite par cible dans `rechercher_offres_cible()` |
| `adzuna.timeout_seconds` | Timeout HTTP des appels Adzuna |
| `apec.location` | Filtre géographique dans les URLs APEC construites |
| `apec.search_terms[]` | Un URL APEC par terme → un seul run Apify avec toutes les URLs |
| `indeed.results_wanted` | Paramètre `results_wanted` de `scrape_jobs()` |
| `indeed.hours_old` | Paramètre `hours_old` de `scrape_jobs()` |
| `indeed.location` | Paramètre `location` de `scrape_jobs()` |
| `wttj.max_items` | Paramètre `maxItems` dans le payload Apify WTTJ |
| `google_jobs.enabled` | **Non utilisé dans le code** — présent dans le YAML mais jamais lu |

---

## 9. Flows d'erreur et résilience

### Adzuna

- **Token manquant :** Vérifié dans `pipeline.main()` avant tout — `raise SystemExit(1)`.
- **Erreur HTTP :** `reponse.status_code != 200` → log rich `[red]Erreur ({code})[/red]` + `break` de la pagination. La cible est abandonnée, les suivantes continuent.
- **Exception réseau :** Attrapée dans `as_completed()` dans `collecter()` → log `logging.error()` + message rich. Les autres sources ne sont pas impactées.

### APEC / WTTJ (Apify)

- **Token manquant :** `if not apify_token: logging.warning(...); return []` — source ignorée silencieusement.
- **Timeout Apify (180 s)** : la boucle de polling se termine, la fonction retourne `[]`.
- **Statut FAILED/ABORTED/TIMED-OUT :** `logging.error(...)` + `return []`.
- **`raise_for_status()`** sur les appels HTTP — si Apify est indisponible, l'exception remonte jusqu'à `as_completed()` qui la catch.

### Indeed / Google (JobSpy)

- **Package absent :** `except ImportError: logging.error(...); return []`.
- **DataFrame vide :** `if df is None or df.empty: return []`.
- **Exception générale :** remonte jusqu'à `as_completed()`.

### Scoring Gemini

- **Clé manquante :** Vérifié dans `pipeline.main()` avant tout — `raise SystemExit(1)`.
- **Erreur LLM / validation Pydantic :** `except Exception as e: return -1, f"Erreur : {str(e)}", [], []`. Le score `-1` est inséré en DB. Le flag `--rescorer` de `scorer.main()` permet de re-scorer ces offres.
- **Rate limiting :** `time.sleep(4)` entre chaque appel — atténue le risque mais ne garantit pas le respect des quotas Gemini.

### Niveaux de logging

- `logging.basicConfig(level=logging.INFO)` dans `pipeline.py`.
- Format : `"%(levelname)s — %(message)s"`.
- Messages clés : `logging.warning()` pour sources ignorées, `logging.error()` pour erreurs Apify et scrapers, `logging.info()` pour diagnostics APEC (nombre d'items bruts, structure du 1er item).
- Les logs diagnostics APEC (`items[0].keys()`, `items[0]`) sont en production — potentiellement verbeux.

---

## 10. Limitations connues et dette technique

### Hardcodé

| Élément | Localisation | Impact |
|---|---|---|
| `maxItems: 30` (APEC) | `pipeline.py` ligne 183 | Limite APEC à 30 offres, non configurable dans `sources.yaml` |
| Filtre `CDI` dans l'URL WTTJ | `pipeline.py` ligne 281 | Offres CDD/Freelance WTTJ impossibles à récupérer |
| `country_indeed="France"` | `pipeline.py` ligne 371 | JobSpy uniquement configuré pour la France côté Indeed |
| `google_search_term=f"{search_term} jobs {location}"` | `pipeline.py` ligne 367 | Terme Google hardcodé, peu localisé |
| Seuils de couleur Excel (85/60) | `pipeline.py` lignes 650-655 | Seuils identiques au dashboard mais dupliqués |
| Seuils de priorité (85/60) | `dashboard.py` `deriver_priorite()`, `generer_excel()`, `exporter_txt()` | Définis à 3 endroits distincts — incohérence potentielle |
| Seuil `80` pour "PRIORITAIRE" dans TXT | `dashboard.py` ligne 53 | `exporter_txt()` utilise 80 (pas 85) — désynchronisé avec le dashboard |

### Dépendances externes fragiles

- **JobSpy** : scraping HTML d'Indeed/Google, susceptible de casser à chaque changement de structure des sites.
- **Actors Apify** (`easyapi~apec-jobs-scraper`, `bebity~welcome-to-the-jungle-jobs-scraper`) : maintenus par des tiers, peuvent être dépréciés ou changer de comportement.
- **Gemini `gemini-3.1-flash-lite-preview`** : modèle en preview, potentiellement retiré ou modifié.

### Cas non gérés

- `google_jobs.enabled` dans `sources.yaml` est lu mais jamais vérifié dans le code — Google Jobs est toujours activé via JobSpy.
- La colonne `profile` de `AgentState` est initialisée à `{}` et jamais peuplée ni lue par aucun nœud.
- `teletravail_minimum_jours_par_semaine` dans `profile.yaml` est chargé mais jamais utilisé dans le prompt de scoring.
- `formation` dans `profile.yaml` n'est pas injecté dans `formater_profil()` — Gemini ne voit pas le niveau d'études.
- `preferences_entreprise.stack_preferee` n'est pas injecté dans `formater_profil()`.
- `experience.domaines` n'est pas injecté dans `formater_profil()`.
- `deriver_source()` dans `dashboard.py` ne gère pas WTTJ (manque `"welcometothejungle"`) — retourne `"N/A"` pour ces offres.
- Le chemin de sortie du CV adapté dans le dashboard (`cv/cv_adapte_{offre_id.replace('adzuna_', '')[:12]}.html`) ne correspond pas exactement à la logique de `cv_adapter.main()` (`offre_id.replace("adzuna_", "")[:12]`) — fonctionnellement équivalent mais fragile.

### Dette technique

- Pas de tests automatisés (unitaires ni d'intégration).
- Les logs de debug APEC (`logging.info("APEC — 1er item complet : %s", items[0])`) sont laissés en production.
- `db.py` ligne 135 référence `logging` sans l'importer (`logging.warning("Migration URL APEC : %s", e)`) — lèverait un `NameError` si la migration échoue.
- Le rapport `.txt` est généré dans `data/` avec horodatage — accumulation de fichiers sans nettoyage automatique.
- `data/offres.xlsx` est écrasé à chaque run — pas d'historique Excel.

---

## 11. Guide de lancement

### Prérequis

- Python 3.10+
- Comptes API : Adzuna (gratuit), Google AI Studio (gratuit), Apify (payant pour APEC/WTTJ)

### Installation

```bash
# Cloner le repo
git clone <url_du_repo>
cd JobAgent

# Créer l'environnement virtuel
python -m venv .venv
source .venv/bin/activate        # Linux/Mac
.venv\Scripts\activate           # Windows

# Installer les dépendances
pip install -r requirements.txt
```

### Configuration

```bash
# Copier et remplir le fichier .env
cp .env.example .env   # si présent, sinon créer manuellement

# Contenu minimal obligatoire :
ADZUNA_APP_ID=<votre_id>
ADZUNA_APP_KEY=<votre_clé>
GOOGLE_AI_STUDIO_KEY=<votre_clé>

# Optionnel (APEC + WTTJ) :
APIFY_API_TOKEN=<votre_token>

# Optionnel (info entreprise) :
TAVILY_API_KEY=<votre_clé>

# Chemin du portfolio pour l'adaptation CV :
PORTFOLIO_PATH=<chemin/absolu/vers/portfolio>
```

```bash
# Placer votre CV HTML dans :
cv/cv_base.html
```

### Adapter le profil

Éditer `config/profile.yaml` : nom, poste cible, compétences, critères, deal-breakers.

Éditer `config/sources.yaml` : cibles Adzuna, termes APEC, paramètres Indeed/WTTJ.

### Lancer le pipeline complet

```bash
python src/pipeline.py
```

### Visualiser le graphe agentique (sans exécuter)

```bash
python src/pipeline.py --visualiser
# Génère pipeline_graph.png et l'ouvre automatiquement
```

### Lancer le dashboard

```bash
streamlit run src/dashboard.py
# Ouvre http://localhost:8501
```

### Scorer manuellement (sans pipeline)

```bash
# Score les 20 prochaines offres non scorées
python src/scorer.py

# Score jusqu'à 50 offres
python src/scorer.py --limite 50

# Re-scorer les offres en erreur (score = -1)
python src/scorer.py --rescorer
```

### Adapter un CV manuellement

```bash
# Récupérer l'ID depuis le dashboard ou le rapport .txt
python src/cv_adapter.py --offre-id adzuna_fr_1234567890

# Avec chemin de sortie personnalisé
python src/cv_adapter.py --offre-id <id> --output cv/mon_cv_startup_x.html
```

---

## 12. Anomalies détectées

### Bug confirmé — `logging` non importé dans `db.py`

**Fichier :** `src/db.py`, ligne 135.

```python
except Exception as e:
    logging.warning("Migration URL APEC : %s", e)  # NameError !
```

`logging` n'est pas importé dans `db.py`. Si la migration URL APEC échoue (ce qui peut arriver sur de vieilles DB), le `except` lève un `NameError` au lieu du `warning` attendu. Correctif : ajouter `import logging` en tête de fichier.

### Désynchronisation des seuils de priorité

**Fichier :** `src/dashboard.py` vs `src/pipeline.py`.

- `deriver_priorite()` (`dashboard.py` ligne 137) : seuil PRIORITAIRE à **85**.
- `exporter_txt()` (`dashboard.py` ligne 53) : seuil "PRIORITAIRE" à **80** dans le rapport texte.
- `generer_excel()` (`pipeline.py` ligne 650) : seuil PRIORITAIRE à **85**.

Le rapport `.txt` classe comme prioritaire tout ce qui est ≥ 80, mais le dashboard et l'Excel utilisent 85. Une offre à 82 sera "PRIORITAIRE" dans le `.txt` et "À CONSIDÉRER" dans le dashboard.

### `google_jobs.enabled` jamais lu

**Fichier :** `src/pipeline.py`, `config/sources.yaml`.

La clé `google_jobs.enabled: true` est définie dans `sources.yaml` mais n'est jamais lue dans le code. Google Jobs est toujours scrapé via JobSpy, même si on met `enabled: false`.

### `AgentState.profile` jamais peuplé

**Fichier :** `src/pipeline.py`.

Le champ `profile` de `AgentState` est initialisé à `{}` dans `etat_initial` (ligne 787) et n'est jamais rempli ni lu par aucun nœud. Le profil est rechargé indépendamment dans chaque nœud qui en a besoin (`scorer_batch` relit `profile.yaml` directement).

### `deriver_source()` ne reconnaît pas WTTJ

**Fichier :** `src/dashboard.py`, ligne 117.

```python
def deriver_source(url: str) -> str:
    url = (url or "").lower()
    if "adzuna" in url:   return "Adzuna"
    if "apec" in url:     return "APEC"
    if "indeed" in url:   return "Indeed"
    return "N/A"           # WTTJ et Google retournent "N/A"
```

Les offres WTTJ (URLs `welcometothejungle.com`) et Google retournent `"N/A"`. Cette fonction est utilisée comme fallback dans la vue détail ; en pratique la colonne `source` de la DB est utilisée en priorité, mais le fallback est incorrect.

### Champs de profil non injectés dans le scoring

**Fichier :** `src/scorer.py`, `formater_profil()`.

Les champs suivants de `profile.yaml` sont chargés mais absents du prompt de scoring :
- `formation.niveau` et `formation.domaines` — Gemini ne sait pas que le candidat est Bac+5.
- `experience.domaines` — les domaines d'expérience ne sont pas transmis.
- `preferences_entreprise.stack_preferee` — non injecté.
- `criteres.teletravail_minimum_jours_par_semaine` — chargé mais non utilisé.

### Logs de debug APEC en production

**Fichier :** `src/pipeline.py`, lignes 250-253.

```python
logging.info("APEC — %d items bruts reçus du dataset", len(items))
if items:
    logging.info("APEC — structure du 1er item : %s", list(items[0].keys()))
    logging.info("APEC — 1er item complet : %s", items[0])  # potentiellement long
```

Ces logs de débogage sont laissés actifs en production. `items[0]` complet peut contenir une description longue et polluer les logs.
