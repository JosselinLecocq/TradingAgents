# TradingAgents pour Open WebUI

Portage simplifie du pipeline TradingAgents dans Open WebUI, en trois pieces qui
respectent la separation prevue par la plateforme : **un skill decrit la methode,
un tool fournit la capacite, un troisieme outil fournit les sous-agents.**

| Fichier | Type Open WebUI | Role | Equivalent dans ce depot |
| --- | --- | --- | --- |
| `trading-agents.skill.md` | Skill | Orchestration : ordre des phases, prompts de delegation, format du rapport | `tradingagents/graph/setup.py` + les prompts de `tradingagents/agents/` |
| `tradingagents_data.py` | Tool | Donnees de marche : identite, cours, indicateurs, actualites, fondamentaux, macro | `tradingagents/dataflows/` + `tradingagents/agents/utils/*_tools.py` |
| *(externe)* Sub Agent | Tool | Contextes isoles pour chaque role | Les noeuds LangGraph et `AgentState` |

## Fournisseurs de donnees

Le tool reprend la logique `data_vendors` du depot : Alpha Vantage en priorite,
yfinance en repli sans cle.

| Categorie | Fournisseur | Pourquoi |
| --- | --- | --- |
| Actualites | **Alpha Vantage** | `NEWS_SENTIMENT` borne le fil cote serveur avec `time_from`/`time_to`, et fournit un label de sentiment par titre. yfinance oblige a filtrer apres coup ce qu'il a bien voulu renvoyer |
| Fondamentaux | **Alpha Vantage** | Les etats financiers portent un `fiscalDateEnding`, donc le trimestre retenu est celui clos **avant** la date d'analyse. C'est du point-in-time, impossible avec yfinance |
| Cours et indicateurs | **yfinance sur le palier gratuit AV** | `TIME_SERIES_DAILY_ADJUSTED` est un **endpoint premium** (verifie sur l'API en direct). Le seul endpoint gratuit restant, `TIME_SERIES_DAILY`, cote les prix « tels que traites », donc non ajustes des splits — inexploitable pour des indicateurs. Le tool bascule donc sur yfinance, qui ajuste toujours des splits. Les indicateurs sont calcules localement : les endpoints AV couteraient un appel chacun |
| Contexte macro | **yfinance** | Sept indices = sept appels AV, soit un tiers du quota gratuit quotidien pour un simple tableau de fond |
| Crypto, futures, indices | **yfinance** | Hors perimetre des endpoints actions d'Alpha Vantage |
| Actions europeennes et asiatiques (`.PA`, `.DE`, `.L`, `.MI`, `.T`...) | **yfinance** | Les deux fournisseurs ne partagent pas la convention de suffixe : Londres est `TSCO.L` chez Yahoo et `TSCO.LON` chez Alpha Vantage. Envoyer `AIR.PA` a AV ne fait que depenser une requete pour s'entendre dire que le symbole n'existe pas |

**Budget d'appels** sur le palier gratuit : environ **sept** appels Alpha Vantage
par analyse (OVERVIEW, NEWS_SENTIMENT par ticker, NEWS_SENTIMENT thematique,
INSIDER_TRANSACTIONS sur une action americaine, et les trois etats financiers), plus une
seule tentative sur l'endpoint ajuste pour la duree du processus. Les cours
venant de yfinance, ils ne consomment rien. Le palier gratuit etant limite a
**25 requetes par jour**, cela laisse environ trois analyses quotidiennes. Retirer les appels
`get_global_news` et `get_insider_transactions` du prompt de l'analyste
actualites en rend deux de plus.
Au-dela, le tool signale explicitement le quota epuise et bascule sur yfinance.

### Le piege des cours non ajustes

Sur un plan sans acces a `TIME_SERIES_DAILY_ADJUSTED`, il serait tentant de se
rabattre sur `TIME_SERIES_DAILY`. C'est un piege : cet endpoint cote les prix
tels qu'ils ont ete traites, sans ajustement des divisions du nominal. Une
fenetre couvrant le split 10 pour 1 de NVDA en juin 2024 contiendrait une chute
de 90 % qui n'a jamais eu lieu, et **toutes** les statistiques calculees dessus
— moyennes mobiles, RSI, ATR, drawdown, volatilite — seraient fausses sans que
rien ne le signale.

Le tool prefere donc yfinance, qui ajuste toujours des splits. Seul le mode
`alpha_vantage` strict, ou le repli est interdit par construction, sert la serie
non ajustee : elle porte alors un avertissement en tete du rapport de cours
**et** du rapport d'indicateurs.

La valve `DATA_VENDOR` accepte `auto` (defaut), `alpha_vantage` (aucun repli
silencieux : l'erreur remonte) et `yfinance` (ignore AV completement).

### Places europeennes et asiatiques

Le routage se fait sur le suffixe : les codes de place Yahoo (`PA`, `DE`, `L`,
`MI`, `AS`, `SW`, `T`...) partent chez yfinance, les codes Alpha Vantage (trois
lettres : `LON`, `FRK`...) chez Alpha Vantage. `get_instrument_identity` annonce
explicitement le motif quand une ligne europeenne bascule sur yfinance, pour que
la perte des fondamentaux point-in-time ne passe pas inapercue.

Pour recuperer Alpha Vantage sur une valeur europeenne, passe **son** symbole a
lui plutot que celui de Yahoo — `TSCO.LON` au lieu de `TSCO.L`. Le symbole exact
se trouve via l'endpoint `SYMBOL_SEARCH` d'Alpha Vantage. A verifier au cas par
cas : la couverture AV des places continentales est inegale, et une ligne cotee
en EUR peut renvoyer un ADR cote en USD.

Le **contexte macro s'adapte a la region** du titre, dans l'esprit du
`benchmark_map` du depot :

| Suffixe | Indices interroges |
| --- | --- |
| `.PA` `.DE` `.AS` `.MI` `.MC` `.BR` `.LS` `.HE` `.ST` `.OL` `.CO` `.VI` | CAC 40, Euro Stoxx 50, DAX, EUR/USD |
| `.L` | FTSE 100, Euro Stoxx 50, GBP/USD, S&P 500 |
| `.SW` | SMI, Euro Stoxx 50, USD/CHF, S&P 500 |
| aucun suffixe (US) et autres | S&P 500, Nasdaq, taux 10 ans US, dollar index |

VIX, petrole et or figurent dans tous les profils : ce sont des variables
globales. L'analyste actualites doit passer `symbol={TICKER}` a
`get_market_context` pour declencher le bon profil ; le prompt du skill le fait.

## Installation

### 1. Le tool de donnees

**Workspace > Tools > `+`**, coller le contenu de `tradingagents_data.py`, puis
**Save**. Depuis la v0.9.6, l'editeur pre-remplit seul le nom, l'ID et la
description a partir du frontmatter.

Renseigne ensuite la valve `ALPHA_VANTAGE_API_KEY` (ou laisse le tool lire la
variable d'environnement du meme nom). Sans cle, tout fonctionne quand meme sur
yfinance, avec les limites decrites plus haut.

La ligne `requirements: yfinance` declenche un `pip install` au moment du Save.
`requests` est deja fourni par Open WebUI.
Si ton instance tourne avec `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS=False`
(recommande en production), installe la dependance dans ton image :

```dockerfile
FROM ghcr.io/open-webui/open-webui:main
RUN pip install --no-cache-dir yfinance
```

Valves du tool de donnees :

| Valve | Defaut | Effet |
| --- | --- | --- |
| `ALPHA_VANTAGE_API_KEY` | vide | Cle Alpha Vantage. A defaut, la variable d'environnement `ALPHA_VANTAGE_API_KEY` est lue |
| `DATA_VENDOR` | `auto` | `auto`, `alpha_vantage` (strict) ou `yfinance` |
| `LOOKBACK_DAYS` | `180` | Fenetre d'historique. Les indicateurs forcent un minimum de 400 jours pour que la SMA 200 existe |
| `NEWS_LOOKBACK_DAYS` | `14` | Profondeur de la fenetre d'actualites avant la date d'analyse |
| `NEWS_LIMIT` | `8` | Nombre maximal de titres par appel |
| `MAX_OUTPUT_CHARS` | `6000` | Plafond par reponse, pour ne pas saturer le contexte d'un sous-agent |
| `REQUEST_TIMEOUT` | `30` | Secondes avant d'abandonner un appel fournisseur. Le budget est triple automatiquement pour les appels groupes : `get_market_context` (sept symboles) et `get_fundamentals` (quatre endpoints Alpha Vantage) |

Une `UserValves.LOOKBACK_DAYS` permet a chaque utilisateur de surcharger la fenetre.

### 2. Le skill

**Workspace > Skills > Import**, selectionner `trading-agents.skill.md`. Le
frontmatter YAML remplit automatiquement le nom et la description.

### 3. Les sous-agents

Deux options selon ton installation :

- **Open WebUI Computer** : `delegate_task` est natif. Regle **Settings > Admin >
  Subagents** — laisser *Max concurrent* a 20 et monter *Max iterations per
  sub-agent* si les analystes sont coupes en cours de route.
- **Open WebUI standard** : installer le [Sub Agent Tool](https://openwebui.com/posts/sub_agent_7bfeb0b7)
  (`run_sub_agent` / `run_parallel_sub_agents`), qui requiert la v0.7.0 minimum.
  La propagation des skills aux sous-agents demande la v0.8.2.

Valves conseillees pour le Sub Agent Tool :

| Valve | Valeur | Pourquoi |
| --- | --- | --- |
| `MAX_PARALLEL_AGENTS` | `3` ou plus | Les phases 1 et 5 lancent trois taches simultanees |
| `MAX_ITERATIONS` | `10` a `12` | L'analyste actualites enchaine jusqu'a quatre appels (titre, marche, inities, macro), et le compteur inclut les tentatives ratees |
| `EXCLUDED_TOOL_IDS` | l'ID du Sub Agent lui-meme | **Indispensable** : empeche un sous-agent de deleguer a son tour |
| `DEFAULT_MODEL` | un modele rapide | Reproduit le couple `quick_think_llm` / `deep_think_llm` du depot |
| `AVAILABLE_TOOL_IDS` | vide | Les sous-agents heritent alors des outils coches dans le chat |

### 4. Le modele

**Workspace > Models > `+`** :

1. Modele de base : ton modele le plus solide en suivi d'instructions — c'est lui
   qui arbitre, l'equivalent de `deep_think_llm`.
2. Section **Skills** : cocher `trading-agents`.
3. Section **Tools** : cocher `TradingAgents Data` et le Sub Agent.
4. **Advanced Params > Function Calling : Native**. Non negociable : le mode Legacy
   n'est plus supporte, et le chargement paresseux des skills passe par le builtin
   `view_skill`, qui exige le function calling natif.

Ensuite il suffit d'ecrire `Analyse NVDA` ou `Analyse AIR.PA au 2024-05-10`.

## Articulation d'un run

```
Chat parent (deep model)
├── get_instrument_identity                        cadrage, 1 appel d'outil
├── run_parallel_sub_agents  ×3   ── marche · actualites+macro · fondamentaux
├── run_sub_agent            ×4   ── Bull → Bear → Bull → Bear   (sequentiel)
├── run_sub_agent            ×1   ── directeur de la recherche
├── run_sub_agent            ×1   ── trader
├── run_parallel_sub_agents  ×3   ── risque agressif · conservateur · neutre
└── decision finale                                rendue dans le chat
```

Soit douze contextes isoles. Chaque sous-agent recoit un prompt auto-portant
(ticker, nom, date, consignes) parce qu'il **n'a aucun acces a l'historique du
chat** : c'est la contrainte de la plateforme, et elle se trouve correspondre au
fonctionnement de `InvestDebateState` dans le depot, ou `bull_history` et
`bear_history` circulent sous forme de chaines.

### Le garde-fou sous-agent

Le Sub Agent Tool transmet les skills du chat parent aux sous-agents
(`extract_skill_manifest`, `extract_user_skill_tags`). Sans precaution, chaque
sous-agent lirait le protocole et tenterait de ré-orchestrer un run complet.
La section 0 du skill neutralise cela : tout prompt commencant par `[ROLE: ...]`
identifie un sous-agent, qui execute son seul role et ne delegue jamais.
La valve `EXCLUDED_TOOL_IDS` en est la seconde ligne de defense.

## Quand un fournisseur tombe

Les quatre mecanismes de resilience de `tradingagents/dataflows/` sont repris :

| Mecanisme | Amont | Ici |
| --- | --- | --- |
| Backoff sur throttle | `yf_retry`, 3 essais, base 2 s | 2 essais, base 1,5 s (le budget doit tenir dans `REQUEST_TIMEOUT`) |
| Chaine de fournisseurs | `data_vendors="yfinance,alpha_vantage"`, ordre explicite | `DATA_VENDOR` avec repli Alpha Vantage -> yfinance |
| Garde de fraicheur | `_assert_ohlcv_not_stale`, `MAX_OHLCV_STALE_DAYS = 10` | Meme seuil : une serie s'arretant plus de 10 jours avant la date d'analyse est traitee comme absente |
| Sentinelle explicite | `NO_DATA_AVAILABLE` | `DONNEES_INDISPONIBLES`, meme consigne : ne pas estimer, signaler l'indisponibilite |

Le principe que l'amont a paye pour apprendre (#988, #289) est respecte : **la
liste configuree est la chaine**, jamais de bascule vers un fournisseur que
l'utilisateur n'a pas choisi. Et le cas dangereux n'est pas l'absence de donnee
mais la donnee presente et fausse (#1021) : une reponse dont la derniere seance
est vieille de plusieurs mois ressemble a un vrai cours, d'ou le rejet actif.

**Ce qui n'est pas repris** : le cache OHLCV sur disque de l'amont (cinq ans
d'historique par symbole, avec rafraichissement le jour meme). Un tool Open WebUI
n'a pas de repertoire de donnees garanti ; le cache est en memoire du processus,
donc perdu au redemarrage.

**Le dernier recours volontairement refuse** : quand yfinance est throttle et que
seul `TIME_SERIES_DAILY` reste disponible, le tool renvoie la sentinelle plutot
que la serie non ajustee des splits. Servir des indicateurs faux sans le dire
serait pire que ne rien servir. Pour forcer cette serie malgre tout, passer
`DATA_VENDOR` sur `alpha_vantage` : elle arrive alors avec son avertissement.

## Depannage

**Les sous-agents rendent des rapports sans aucun chiffre.** C'est le mode d'echec
le plus probable, et le plus insidieux : le modele produit une analyse plausible et
entierement inventee. Il vient de ce que les sous-agents n'heritent pas du tool de
donnees. Avec `AVAILABLE_TOOL_IDS` vide, ils ne recoivent que les outils **coches
dans l'interface de chat** — ce qui n'est pas la meme chose que les outils attaches
au modele. Deux remedes : cocher le tool via le menu `+` de la conversation, ou
renseigner explicitement `AVAILABLE_TOOL_IDS` avec l'ID du tool de donnees et celui
du Sub Agent. La regle 7 du skill fait remonter le probleme au lieu de le masquer.

**Un analyste est coupe en cours de route.** Monter `MAX_ITERATIONS` : chaque
analyste enchaine deux a trois appels d'outils, et le compteur inclut les tentatives
ratees.

**Le modele repond sans deleguer.** Le function calling natif n'est pas actif sur le
modele, ou le skill n'est pas charge. Verifier **Advanced Params > Function Calling :
Native**, puis que le skill est bien coche dans la section Skills du modele.

## Garde-fous anti-hallucination

- **Aucune anticipation, la ou elle est possible.** `get_price_history`,
  `get_technical_indicators`, `get_company_news` et `get_market_context` prennent un
  `as_of_date` et ne renvoient rien au-dela. C'est la discipline du `trade_date` du
  graphe amont, portee jusque dans le tool. Une date future est ramenee a aujourd'hui.
- **Fondamentaux point-in-time avec Alpha Vantage.** `get_fundamentals` retient le
  dernier trimestre dont le `fiscalDateEnding` precede la date d'analyse, et l'affiche
  sous un titre qui nomme la periode. Reserve honnete : la cloture d'un trimestre
  n'est pas sa publication, donc un trimestre clos trois jours avant la date d'analyse
  n'etait pas encore public. C'est une approximation, bien meilleure qu'un instantane
  actuel, mais pas une certitude.
- **Ce qui ne peut pas etre rembobine est declare.** Les multiples de valorisation
  (PER, PEG, VE/EBITDA, objectif de cours) restent l'instantane courant chez les deux
  fournisseurs. Des que l'analyse porte sur une date de plus d'une semaine, la reponse
  est prefixee d'un avertissement explicite, repercute par le prompt de l'analyste.
  `get_instrument_identity` renvoie des attributs stables, sans enjeu d'anticipation.
- **Aucun chiffre sans outil.** Le skill l'exige, et le tool ecrit explicitement
  « donnee indisponible » plutot que de renvoyer une valeur vide qu'un modele
  comblerait de lui-meme.
- **Sortie plafonnee.** `MAX_OUTPUT_CHARS` (6000 par defaut) borne chaque reponse,
  pour que douze contextes isoles ne saturent pas leur fenetre.
- **Instruments sans fondamentaux.** Sur une crypto, un future ou un indice,
  `get_fundamentals` renvoie une explication au lieu de ratios vides.

## Ce que cette version ne reprend pas

- **La memoire reflexive.** `TradingMemoryLog` recalcule l'alpha realise apres N
  jours et reinjecte les lecons dans les runs suivants. Hors de portee d'un skill :
  il faudrait un stockage persistant et un declencheur planifie.
- **Le checkpoint et la reprise.** `graph/checkpointer.py` reprend un run
  interrompu au dernier noeud reussi. Ici, une coupure oblige a tout relancer,
  et les sous-agents en arriere-plan ne survivent pas a un redemarrage serveur.
- **La garantie du flux.** LangGraph *impose* les aretes du graphe ; ici le modele
  orchestrateur *suit des instructions*. Avec un modele faible en suivi
  d'instructions, une phase peut sauter. C'est la limite structurelle du portage.
- **Une partie des vendeurs du depot.** FRED (macro), Reddit et Stocktwits (sentiment
  social), Polymarket (marches predictifs) ne sont pas cables. Le contexte macro se
  limite a un tableau d'indices Yahoo au lieu des series FRED. Ajouter FRED demande
  une valve et une methode supplementaire.
- **L'analyste sentiment social.** Le pipeline d'origine en compte un ; ici ses
  signaux sont absorbes par l'analyste actualites, via le sentiment par titre
  d'Alpha Vantage.
- **Les sorties structurees.** L'amont contraint certains agents a un schema
  (`agents/schemas.py`, `agents/utils/structured.py`) ; un skill ne peut
  qu'imposer un format en langage naturel.

## Couts et limites operationnelles

Un run complet, c'est douze conversations de sous-agents, chacune avec sa boucle
d'outils : compte plusieurs minutes et un volume de tokens comparable au pipeline
d'origine. Chaque message declenche un run entier — pour une question de suivi,
mieux vaut desactiver le skill le temps de l'echange.

Yahoo Finance limite le debit par adresse IP. En cas de rafale de `Too Many
Requests`, espacer les runs ; le tool degrade proprement en signalant la donnee
manquante plutot qu'en echouant.

## Etat de validation

- **Skill** : frontmatter YAML valide, cles `name` et `description` exactement,
  ~2,4k tokens de contenu.
- **Conformite du tool**, verifiee par analyse de l'AST : six methodes publiques,
  toutes `async`, toutes annotees, toutes documentees en reST `:param:` sans
  qu'aucun argument reserve ne fuite dans le schema JSON. Events `status`
  uniquement, les seuls pleinement supportes en mode Native.
- **Calculs** : RSI(14) Wilder, MACD et SMA confrontes a une implementation
  independante sur serie synthetique — ecart nul. Bornes verifiees : RSI 0 sur
  une baisse monotone, 100 sur une hausse monotone, 50 sur un marche plat.
- **Parseur d'actualites** : les deux schemas yfinance (imbrique et plat), avec
  titre, source et lien preserves dans les deux cas, plus le filtre
  anti-anticipation.
- **Couche fournisseur** : chemin Alpha Vantage exerce sur charges simulees — le
  trimestre point-in-time correct est retenu et le suivant ecarte, les cours
  posterieurs a la date d'analyse n'apparaissent pas, le sentiment par titre
  remonte. Cache verifie (deux appels identiques = une requete HTTP, plafond a 64
  entrees, expiration au-dela du TTL), classification des erreurs AV (quota /
  premium / cle invalide), repli automatique en mode `auto` et remontee d'erreur
  en mode `alpha_vantage` strict.
- **Resilience** : backoff verifie (succes au 3e essai apres 4,5 s d'attente,
  throttle permanent propage, erreur non-throttle propagee sans retry), garde de
  fraicheur verifiee (serie de 16 mois rejetee, serie de J-2 servie), sentinelle
  instructive verifiee sur les deux outils de cours.
- **Un appel reel** (autorise, budget d'une requete) : cle valide et
  authentifiee ; `TIME_SERIES_DAILY_ADJUSTED` confirme comme endpoint premium sur
  le palier gratuit ; le libelle de refus renvoye par l'API est bien classe en
  « premium » par le tool, et non en quota ou cle invalide. Le repli a ete
  volontairement non declenche pour tenir le budget.
- **Timeout** : verifie sur boucle persistante — l'appelant est libere a l'echeance
  et l'appel suivant est servi normalement.
- **Non verifie en conditions reelles** : Yahoo Finance a renvoye `429 Too Many
  Requests` sur cette machine pendant toute la mise au point, donc le chemin
  nominal (donnees live) n'a pas pu etre execute de bout en bout. Les chemins
  d'erreur, eux, ont ete valides par ce meme incident. Le premier run reel reste
  a faire.

### Defauts trouves en revue et corriges

| Defaut | Consequence si non corrige |
| --- | --- |
| Fournisseur : yfinance retenu alors que le dossier dispose d'une cle Alpha Vantage | Actualites filtrees apres coup au lieu d'etre bornees a la source, et aucun fondamental point-in-time |
| `outputsize=compact` choisi sur la largeur de fenetre au lieu de la distance a aujourd'hui | Analyse historique a fenetre courte : Alpha Vantage renvoie les 100 derniers jours **depuis aujourd'hui**, la fenetre ressort vide et le run retombe silencieusement sur yfinance |
| `annualReports` ignore par le filtre point-in-time | Aucun etat financier date pour les societes a publication annuelle, frequentes hors des Etats-Unis |
| Cache Alpha Vantage sans plafond ni TTL | Croissance memoire dans un processus Open WebUI de longue duree, et une seconde analyse le meme jour ne voit jamais de seance plus recente |
| Message « aucune actualite » annoncant une fenetre glissante meme en repli Yahoo | Decrit a l'agent un filtrage qui n'a pas eu lieu |
| Repli sur endpoint premium non memorise | Si `TIME_SERIES_DAILY_ADJUSTED` est premium sur le plan, chaque appel de cours gaspille une requete refusee — deux par analyse sur un quota de 25/jour |
| `DATA_VENDOR` en texte libre | Une faute de frappe (`alphavantage`) retombait silencieusement en mode `auto` au lieu d'etre rejetee |
| Cle API affichee en clair dans le formulaire | La doc Open WebUI recommande un champ `password` pour tout identifiant |
| Tableau d'ouverture du README casse par une insertion de section | Ligne de tableau orpheline, rendu illisible |
| Skill annoncant « onze sous-agents » | Decompte reel : douze (3 + 4 + 1 + 1 + 3) |
| Tickers europeens routes vers Alpha Vantage malgre une convention de suffixe incompatible | Requete depensee pour rien, degradation silencieuse en mode `auto`, et **echec de l'analyse** en mode strict |
| Contexte macro fige sur les indices americains | Une valeur parisienne jugee contre le Nasdaq et le dollar index, cadre de reference sans rapport |
| `BRK.B` non converti en `BRK-B` (convention Yahoo des categories d'actions) | Aucune donnee pour les actions a categories multiples |
| Mode strict + ticker europeen : message « verifie le symbole » | Accuse un ticker parfaitement valide d'etre faux, alors que c'est le fournisseur qui ne couvre pas la place — et le mode strict est justement celui qu'on active pour diagnostiquer |
| Normalisation remplacant **tous** les points d'un symbole | `A.B.C` transforme en `A-B-C` au lieu de `A.B-C` |
| `DATA_VENDOR=yfinance` n'empechait pas les appels Alpha Vantage des transactions d'inities | La valve documentee comme « ignore AV completement » laissait partir des requetes vers AV |
| Message de refus attribuant toujours l'echec a une cle manquante | Repondait « il faut une cle » a quelqu'un ayant deliberement desactive le fournisseur |
| Trois outils amont oublies : actualites de marche, transactions d'inities, discipline de verification du prompt analyste | L'analyste ne voyait que les nouvelles du titre sans la toile de fond du marche, ignorait les operations des dirigeants, et n'avait pas l'interdiction explicite d'affirmer un rebond sur support ou une variation en pourcentage non appuyee par un outil |
| Repli sur `TIME_SERIES_DAILY`, non ajuste des splits | **Trouve grace a l'appel reel.** Toutes les statistiques techniques faussees sur toute fenetre couvrant une division du nominal, sans aucun signal |
| Ancien schema yfinance : `publisher` et `link` perdus | Actualites sans source ni lien verifiable |
| `dividendYield` lu sans tenir compte du changement de convention | Un rendement de 2,15 % affiche a **215 %** |
| `debtToEquity` affiche brut | `172.50` lu comme un ratio au lieu de 172,5 % (1,73x) |
| RSI a 0 traite comme valeur falsy | La survente la plus extreme etiquetee « neutre » |
| RSI sur hausse monotone (division par zero neutralisee) | `n/a` au lieu de 100 |
| Fenetre « 52 semaines » sur 30 seances | Un plus-haut mensuel presente comme annuel |
| `UserValves` transmis en dict | Surcharge utilisateur silencieusement ignoree |
| `REQUEST_TIMEOUT` declaree mais jamais utilisee | Valve mensongere dans l'UI ; sous-agent bloque sur un fournisseur muet |
| `get_fundamentals` sans date, alors que le skill lui en donne une | Ratios d'aujourd'hui presentes comme connus a une date passee, sans que l'agent le sache |
| Date d'analyse future acceptee telle quelle | En-tete de rapport date d'un jour qui n'a pas eu lieu |
| Parametre `limit` des actualites non borne | Requete inutilement large, tronquee ensuite en silence |
| Aucun controle des rapports sans chiffres | Une analyse entierement inventee integree sans signalement |
