# News ciblées — version 3

Le module produit des articles sourcés, pas des conseils financiers ni une explication automatique des pertes.
Les données personnelles du client, ses montants et ses notes ne sont jamais transmis à Apify.

## Parcours

1. `context.py` réutilise les analyses de positions, comptes, allocation et fonds ; aucune simulation ni comparaison de pairs.
2. `planning.py` choisit les recherches à partir des expositions observées et conserve leur provenance.
3. `apify.py` appelle l’Actor existant `scrapeai/yahoo-news-scraper`.
4. `cache.py` partage les résultats publics entre clients et limite les nouveaux runs.
5. `service.py` filtre, classe et déduplique les articles pour chaque portefeuille.
6. `news_integration.py` contrôle l’identité avant l’ajout au briefing.

Les règles de vocabulaire sont communes aux portefeuilles ; aucun identifiant CASE ne déclenche de logique spéciale.

## Voir les recherches, sans appel payant

Depuis la racine du projet :

```bash
uv run python -m news CASE-002 --plan
uv run python -m news CASE-006 --portfolio-id 376 --plan
uv run python -m news.audit --plan --output outputs/news-plans-all.json
```

Le dossier de sortie doit exister et le fichier ne doit pas déjà exister.
Un plan ne contient pas d’articles et ne peut pas être attaché au briefing comme résultat news.

Pour réutiliser un briefing déjà calculé :

```bash
uv run python -m news CASE-002 --plan --briefing outputs/briefing-CASE-002.json
```

L’identité client/portefeuille et la devise sont vérifiées. Sans `--briefing`, seuls les calculs utiles aux news sont exécutés.
Sans `--portfolio-id`, le sélecteur partagé choisit un portefeuille du client, pas tous ses portefeuilles.

## Collecte réelle

Exporter APIFY_TOKEN dans le même terminal, sans le mettre dans le code ou un fichier versionné.

```bash
uv run python -m news CASE-002 --live --max-runs 4 --output outputs/news-CASE-002-v3.json
uv run python build_briefing.py CASE-002 --news outputs/news-CASE-002-v3.json --output outputs/briefing-CASE-002-v3.json
```

Attention : contrairement à la commande news, `build_briefing.py --output` peut remplacer un fichier existant ; choisir un nouveau nom.

- 4 recherches au maximum par défaut (`--max-queries`, 1 à 12).
- 10 candidats par recherche, au plus 3 articles retenus.
- Fenêtre de 7 jours, configurable avec `--lookback-days` de 1 à 30.
- `--max-runs` limite strictement le nombre de nouveaux runs ; 0 autorise seulement les cache hits.
- Le fournisseur demande à Apify un plafond de 0,05 USD par run. Ce n’est pas une mesure du coût facturé.
- Aucun retry automatique du POST créant un run payant.

Pour auditer les résultats réels sur tous les portefeuilles :

```bash
uv run python -m news.audit --live --max-runs 10 --output outputs/news-audit-live.json
```

Cette commande est payante. Le budget de runs est GLOBAL, non multiplié par le nombre de clients.
Avec un budget limité, certains portefeuilles peuvent rester partiellement couverts ; le rapport le montre.
L’audit inclut les vues consolidées séparément mais ne somme jamais leurs avoirs.
Un audit avec échecs/budget épuisé renvoie le code 1 et conserve le rapport.

## Ciblage et priorités

- Titres : nom réel, ISIN en recherche, identité conservée par SecurityId.
- Fonds : recherche du produit et des secteurs/régions classifiés par l’analyse sous-jacente.
- Liquidités : devise explicitement renseignée et événements monétaires ; paire de change pour un compte étranger.
- Crypto : codes explicitement reconnus, jamais une devise inconnue convertie arbitrairement en crypto.
- Secteur/région : seuil de poids 10 %, classe d’actifs 25 %, devise agrégée 15 %, compte liquide 5 % si le poids est connu.
- Une devise de compte connue reste recherchable lorsque son poids est inconnu.
- Les catégories non classées ou non prises en charge sont signalées, pas remplacées par une recherche fourre-tout.

Les poids sont ceux du moteur. Leurs dénominateurs sont conservés : une répartition des titres n’est pas présentée comme celle du portefeuille total.
Le classement de recherche est une heuristique documentée : poids × spécificité (secteur 0,9 ; change 0,85 ; devise 0,65 ; région 0,6 ; classe d’actifs 0,35).
Une exposition connue sans poids reçoit une priorité de recherche conventionnelle de 0,25 avant ce facteur, jamais un poids financier inventé.
Les positions directes utilisent les poids disponibles (facteur fonds 0,6), sinon la priorité relative historique.

Le budget réserve des places aux positions directes et aux différentes dimensions. Un portefeuille de fonds réserve une seule place directe quand des recherches d’exposition existent.
Les sujets différés restent visibles. Ces choix optimisent un budget de recherche, pas un impact économique mesuré.

## Sélection des articles

Exigences communes : titre, éditeur, URL HTTP(S) sans identifiants intégrés, date admissible, absence de sponsoring.
Les dates relatives sont ancrées à la collecte d’origine, même après un cache hit.

- Une news instrument doit mentionner l’entité dans le titre ou l’extrait ; les noms ambigus nécessitent un contexte financier. Un ISIN seul ne suffit pas, car le référentiel contient des doublons.
- Une news de marché doit mentionner le sujet lié à l’exposition ET un événement financier.
- Les passages justificatifs et leurs sources sont conservés.
- Le score additionne mention titre/extrait (50/30), priorité (jusqu’à 25), fraîcheur (jusqu’à 15) et présence d’un événement (10).
- Déduplication par URL nettoyée ou titre normalisé, puis diversification des expositions.

Il ne s’agit pas d’une lecture sémantique exhaustive des articles : le fournisseur donne des titres/extraits.
`impact_status: not_assessed` interdit d’interpréter le score comme une prévision ou une preuve de causalité.
Le même événement peut légitimement être pertinent pour plusieurs clients exposés au même sujet.

## Cache et confidentialité

Cache SQLite local : `.cache/news.sqlite3`, ignoré par Git.
Durée de validité : 1 heure ; les dates de publication sont toujours filtrées à nouveau.
Clé : fournisseur, mode test/réel, requête, fenêtre et limite. Aucune identité client, aucun token, aucun montant.
Une réservation évite deux lancements simultanés identiques ; un échec incertain impose un délai de 10 minutes.
`--no-cache` utilise seulement un cache mémoire pour cette invocation ; relancer peut donc coûter à nouveau.
Le classement client n’est jamais mis en cache avec les articles publics.

## Contrat et compatibilité

Sortie `schema_version: news-3.0` ; les champs principaux de news-2.0 restent présents :
`client_ref`, `portfolio_id`, `status`, `articles`, `queries`, `warnings`.
Ajouts : `exposures`, `supporting_terms`, `relevance`, `event_terms`, `impact_status`, `collection_stats`.
Les composantes de score ont changé : utiliser `score_components` sans présumer les anciens noms.

Statuts : `ok`, `no_results`, `partial`, `unavailable`. Un résultat vide honnête est acceptable.
Le JSON détaillé conserve la traçabilité ; `--format text` fournit aussi du JSON, dans une forme plus compacte.
`news_integration` accepte news-2.0 et news-3.0 avec identité valide. Les plans, formats inconnus et résultats d’un autre portefeuille sont refusés.
Le moteur IA consomme le bloc attaché ; la recherche NewsAPI concurrente a été retirée.

API :
```python
from news import build_news_context, collect_news
context = build_news_context(store, dossier)
result = collect_news(dossier, provider, context=context)
```

L’appel historique sans `context` reste direct-only. La CLI utilise par défaut les nouvelles expositions.
`--direct-only` permet de retrouver le ciblage historique.

## Limites et vérification

- Recherche actuelle uniquement : l’Actor n’est pas utilisé pour attribuer une baisse ancienne.
- L’historique fourni est non corrigé des flux et ses dates peuvent être décalées.
- Les positions actuelles ne prouvent pas les positions historiques.
- Les compositions de fonds n’ont ni noms d’entreprises sous-jacentes ni date ; certains libellés géographiques sont peu fiables.
- Les recherches de devises utilisent les catégories rapportées, pas une mesure garantie du risque de change après couverture.
- Aucun article, sentiment, impact, House View ou citation n’est inventé.
- Les devises/secteurs inconnus et positions courtes sont signalés comme limites de couverture.

```bash
uv run pytest -q
uv run python -m news.audit --plan --output outputs/news-coverage.json
```

Les tests hors ligne utilisent des fournisseurs simulés uniquement dans tests/. Ils ne prouvent pas la qualité du flux réel.
La validation réelle doit examiner, par famille de portefeuille, les liens de pertinence, erreurs d’identité, fraîcheur, couverture, temps et coût.
L’interface utilise le contexte du briefing déjà calculé, avec quatre recherches
en parallèle, un délai global de 45 secondes et le cache SQLite partagé.
Le cache protège les réservations et le budget entre threads et conserve les
résultats publics pendant une heure. Le cache de session de 30 minutes reste actif.
