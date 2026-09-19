# News directement reliées au portefeuille

Version de sortie : news-2.0. Entrée : ClientDossier produit par les collègues.
Les fichiers pipeline.py et processing/contracts.py restent inchangés.

## Recherche réelle

Dans le terminal où APIFY_TOKEN est déjà exporté :

~~~bash
uv run python -m news CASE-016 --live
~~~

Pour créer un fichier JSON séparé :

~~~bash
uv run python -m news CASE-016 --live --output outputs/news-CASE-016-direct.json
~~~

Le fichier doit être nouveau ; aucun ancien résultat n'est écrasé.
Sans --output, le JSON est écrit dans le terminal. --format text produit une
version compacte des mêmes faits pour l'agrégateur, sans paragraphe rédigé.

Le programme ne contient plus de fournisseur de démonstration. Il exige --live
ou --plan. Sans token, --live échoue explicitement et ne génère aucun article.

## Examiner les mots-clés avant de payer

~~~bash
uv run python -m news CASE-016 --plan
uv run python -m news CASE-023 --plan
uv run python -m news CASE-004 --plan
~~~

--plan lit les vraies données : requêtes, positions associées, preuves, éléments
ignorés et requêtes reportées. Il ne contacte pas Apify et ne crée aucune news.

## Ce qui personnalise la recherche

- Noms des titres réellement détenus, joints à reference.json par SecurityId.
- Montants positifs en devise du portefeuille pour prioriser ces positions.
- Nom identifiable de l'émetteur pour une obligation ; le lien conserve le nom
  et l'identifiant de l'obligation exacte.
- Nom spécifique du fonds ou ETF : jamais son gestionnaire seul.
- ISIN exact quand disponible et non contradictoire avec la référence.
- Bitcoin/Ethereum seulement si les comptes exportés indiquent BTC/ETH détenus.

Les recherches « equity markets », « bond markets », pays/secteur seul et devises
génériques sont supprimées. Un portefeuille uniquement en cash peut donc ne
produire aucune requête. Les émetteurs publics aux noms très ambigus ont un
contrôle supplémentaire de contexte économique dans le titre/extrait.

Quatre entités maximum sont recherchées par défaut ; les autres sont listées dans
deferred_queries. On peut utiliser --max-queries 1 à 12 pour ajuster explicitement
la couverture et le budget. Aucun AUM manquant n'est remplacé par zéro.

Un fonds au nom d'export trop complexe peut nécessiter un alias vérifié, conservant
l'identité du produit. Le code ne devine ni son ticker ni ses sous-jacents.
Un fichier --aliases peut contenir un objet JSON reliant les SecurityId aux noms
publics vérifiés. Les noms génériques sont refusés. Il faut vérifier soi-même que
chaque alias désigne bien l'instrument, surtout pour les fonds.

## Sélection et preuves

Chaque article retenu contient matches avec :
- term : entreprise, fonds, crypto ou ISIN effectivement trouvé ;
- field : title ou snippet ;
- source_excerpt : le titre/extrait fourni par la source, utilisé pour ce lien ;
- holdings : identifiant et nom du titre, montant, devise, relation et chemin source ;
- evidence : provenance des champs du portefeuille et du passage de l'article.

Aucune relevance_reason préécrite, synthèse de marché ou recommandation automatique.
Les titres/extraits sont fournis par Apify (espaces normalisés, longueurs limitées).
La sortie ne prétend pas avoir lu le corps complet de l'article.

Le score est transparent dans score_components : mention dans le titre ou l'extrait,
valeur de la position relative à la plus grande position recherchée et récence.
C'est un classement déterministe de pertinence textuelle, pas une estimation
d'impact financier ou une validation de la véracité de l'article.

Les doublons sont regroupés et conservent leurs liens aux positions.
La sélection privilégie la couverture de positions différentes avant de répéter
une même entité, dans la limite de trois articles au total.

Des clients détenant le même titre peuvent recevoir légitimement la même actualité.
Aucune différence artificielle n'est générée pour faire paraître les sorties uniques.

## Résultats et limites

0 à 3 articles, jamais de remplissage. Les articles sans date exploitable, trop
anciens/futurs, sponsorisés ou sans nom d'une exposition recherchée sont exclus.
Les dates relatives sont signalées date_is_estimated=true. Fenêtre : 7 jours,
tolérance d'horloge : 5 minutes. portfolio_snapshot_at et history_as_of restent
séparés de la date de recherche. La fraîcheur des news ne confirme pas la fraîcheur
des positions de portefeuille.

- ok : au moins un article qualifié et aucune requête en échec.
- no_results : appels réussis, aucun article qualifié.
- partial : certaines requêtes ont échoué, avec leur texte dans warnings.
- unavailable : aucune recherche possible ou tous les appels ont échoué.

Un nom absent de l'extrait peut faire manquer un article pertinent.
Le rapprochement ne prouve pas l'impact financier et ne détecte pas parfaitement
les homonymes. Les liens sont inspectables précisément pour cette raison.
La comparaison House View et le branchement au pipeline commun restent à faire.

## Apify et budget

Actor : https://apify.com/scrapeai/yahoo-news-scraper
Adaptateur propre à cet Actor ; un autre Actor nécessite son propre mapping.

Seuls les noms publics/ISIN partent dans les requêtes. Les identités clients, notes
et montants restent locaux. Le token est lu depuis APIFY_TOKEN, envoyé en en-tête,
jamais écrit dans le JSON. Le module ne charge aucun .env.

Un appel par entité, 10 candidats par appel, plafond demandé de 0,05 USD par run.
Donc 0,20 USD maximum demandé pour quatre requêtes ; avec --max-queries 12,
le plafond demandé atteint 0,60 USD. Aucun nouvel essai automatique d'un POST
échoué. Aucun cache persistant : relancer --live crée de nouveaux runs.

Docs : https://apify.com/scrapeai/yahoo-news-scraper/input-schema
et https://docs.apify.com/api/v2/actors-runs-post

## Intégration sans fichier

~~~python
from news import collect_news, render_news_context
from news.apify import ApifyNewsProvider
from processing.contracts import to_jsonable

result = collect_news(dossier, ApifyNewsProvider())
payload = to_jsonable(result)
context = render_news_context(result)
~~~

Changement de schéma : relevance_reason / matched_keywords / matched_security_ids
sont remplacés par matches. Les mocks d'articles existent seulement dans les tests.

## Tests

~~~bash
uv run pytest -q
~~~

Régressions : les trois titres réels hors sujet obtenus précédemment pour CASE-016
sont rejetés ; les mêmes candidats produisent des résultats différents pour deux
portefeuilles distincts ; un article sur le gestionnaire seul ne suffit pas pour
un fonds ; aucune erreur API n'est remplacée par une fausse actualité.
