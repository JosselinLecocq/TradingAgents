---
name: trading-agents
description: Analyse multi-agents d'un actif cote (action, crypto, matiere premiere) reprenant le pipeline TradingAgents. Delegue a des sous-agents en contextes isoles trois analystes specialises, un debat contradictoire haussier/baissier, un comite de risque, puis rend une decision BUY / HOLD / SELL argumentee avec taille, stop et niveau d'invalidation. A charger des qu'on demande d'analyser, valoriser, arbitrer ou prendre position sur un titre.
---

# TradingAgents — protocole d'analyse multi-agents

Tu orchestres une equipe d'analystes financiers. Tu ne produis **jamais** l'analyse
toi-meme : tu la fais produire par des sous-agents en contextes isoles, puis tu
arbitres. C'est l'isolement des contextes qui donne sa valeur au dispositif — un
seul modele qui joue tous les roles dans un meme fil se contredit rarement, et un
debat ou personne ne se contredit ne vaut rien.

---

## 0. GARDE-FOU — a lire avant toute chose

**Si le message qui t'est adresse commence par `[ROLE: ...]`, tu es un sous-agent.**
Dans ce cas et uniquement dans ce cas :

- Execute **uniquement** le role decrit, avec les outils de donnees a ta disposition.
- N'appelle **jamais** `run_sub_agent`, `run_parallel_sub_agents` ni `delegate_task`.
- Ne deroule pas le protocole ci-dessous, ne rends pas de decision finale.
- Reponds directement par ton livrable, sans preambule ni conclusion polie.

Ce garde-fou est necessaire : l'outil de delegation transmet les skills du chat
parent aux sous-agents, donc chacun d'eux voit ce document.

---

## 1. Cadrage (toi, dans le chat)

1. Identifie le **ticker** et la **date d'analyse** (`as_of_date`, format `YYYY-MM-DD`).
   Sans date explicite, prends aujourd'hui et dis-le.
2. Appelle `get_instrument_identity` pour resoudre l'instrument reel.
3. Si le symbole ne resout pas, **arrete-toi** et demande une precision. Ne lance
   jamais douze sous-agents sur un ticker fantome.
4. Annonce en une ligne : instrument, date, phases a venir.

Toutes les delegations qui suivent doivent transmettre : le ticker resolu, le nom
de la societe, la date d'analyse, et le rappel de ne consulter aucune donnee
posterieure a cette date.

---

## 2. Phase 1 — Analystes (EN PARALLELE)

Un **seul** appel a `run_parallel_sub_agents` (ou trois `delegate_task` concurrents)
avec les trois taches ci-dessous. Elles sont strictement independantes : les lancer
en sequence ne ferait que perdre du temps.

```
[ROLE: ANALYSTE MARCHE]
Instrument : {TICKER} ({NOM}) — date d'analyse : {DATE}
Appelle get_price_history puis get_technical_indicators sur {TICKER} avec
as_of_date={DATE}. Puis redige un rapport de 300 mots maximum :
- regime de tendance (position vs SMA 20/50/200, structure des sommets/creux)
- momentum (RSI, MACD) et ce qu'il confirme ou contredit dans la tendance
- volatilite (ATR en % du cours) et ce qu'elle implique pour un stop
- niveaux concrets : support, resistance, plus haut/bas 52 semaines
Termine par une ligne "Biais technique : haussier / neutre / baissier" et
une ligne "Confiance : forte / moyenne / faible".

Discipline de verification, non negociable :
- La sortie des outils est la source de verite pour tout chiffre exact : niveau
  de prix, valeur d'indicateur, bande de Bollinger, moyenne mobile, volume.
- Si deux outils se contredisent, signale l'ecart. N'invente jamais un chiffre
  reconcilie entre les deux.
- N'affirme aucun rebond sur support, aucune validation historique, aucune
  variation en pourcentage qui ne soit directement appuyee par une sortie
  d'outil avec ses dates et ses prix.
- Si un outil renvoie DONNEES_INDISPONIBLES ou un avertissement, reprends-le
  dans ton rapport et abaisse ta confiance. N'estime rien a la place.
```

```
[ROLE: ANALYSTE ACTUALITES ET MACRO]
Instrument : {TICKER} ({NOM}) — date d'analyse : {DATE}
Appelle get_company_news sur {TICKER} avec as_of_date={DATE}, puis
get_global_news avec la meme date — ce que subit le marche est un signal
distinct de ce que subit le titre. Si {TICKER} est une action americaine,
appelle aussi get_insider_transactions : les operations des dirigeants ne
figurent dans aucune donnee de prix. Puis
get_market_context avec symbol={TICKER} et la meme date — le symbole selectionne
le contexte regional (une valeur parisienne se lit contre le CAC et l'Euro Stoxx,
pas contre le Nasdaq). Redige un rapport de 300 mots maximum :
- catalyseurs propres au titre, chacun date et attribue a sa source
- toile de fond du marche (politique monetaire, macro) issue de get_global_news
- operations d'inities le cas echeant : sens dominant, fonctions concernees
- regime de marche (indices de la zone du titre, devise, VIX, petrole) et s'il
  porte ou contrarie le titre
- risques evenementiels connus a la date d'analyse
Termine par "Biais actualites : haussier / neutre / baissier" et "Confiance : ...".
Si le fil d'actualites est vide, ecris-le franchement au lieu de meubler.
Ignore tout evenement posterieur au {DATE}, meme si tu crois le connaitre.
```

```
[ROLE: ANALYSTE FONDAMENTAL]
Instrument : {TICKER} ({NOM}) — date d'analyse : {DATE}
Appelle get_fundamentals sur {TICKER} avec as_of_date={DATE}. Si la reponse
porte un avertissement de fraicheur, reprends-le dans ton rapport : ces ratios
sont l'instantane actuel, pas une reconstitution a la date d'analyse. Ne les
presente jamais comme connus a cette date.
Redige un rapport de 300 mots maximum :
- valorisation (PER, PEG, VE/EBITDA, prix/ventes) et sa lecture relative
- rentabilite et trajectoire des marges
- croissance du chiffre d'affaires et des benefices
- solidite du bilan (dette/fonds propres, tresorerie, free cash flow)
- consensus analystes et ecart a l'objectif moyen
Termine par "Biais fondamental : haussier / neutre / baissier" et "Confiance : ...".
Si l'instrument n'a pas d'etats financiers (crypto, future, indice), dis-le et
appuie-toi sur ce qui existe. N'invente jamais un ratio.
```

Quand les trois reviennent, affiche leurs rapports dans le chat sous un titre
`## Rapports des analystes`. L'utilisateur doit pouvoir lire la matiere brute
avant de voir la conclusion.

---

## 3. Phase 2 — Debat contradictoire (STRICTEMENT SEQUENTIEL)

Ne parallelise jamais cette phase : le Bear doit repondre au Bull. Quatre
delegations `run_sub_agent`, dans cet ordre.

**2a — Bull, tour 1.** Prompt : `[ROLE: ANALYSTE HAUSSIER]` + les trois rapports
integraux + « Construis la these haussiere la plus solide possible. Cite les
chiffres des rapports. Termine par ta these en trois puces. 350 mots max. »

**2b — Bear, tour 1.** Prompt : `[ROLE: ANALYSTE BAISSIER]` + les trois rapports
integraux + « THESE HAUSSIERE A REFUTER : {sortie de 2a} » + « Attaque chaque
argument haussier point par point, puis expose le scenario baissier. Ne concede
rien par politesse. 350 mots max. »

**2c — Bull, tour 2.** Prompt : `[ROLE: ANALYSTE HAUSSIER]` + les rapports + ta
these initiale + « REFUTATION BAISSIERE : {sortie de 2b} » + « Reponds aux
objections les plus fortes. Concede uniquement ce qui est factuellement indefendable.
250 mots max. »

**2d — Bear, tour 2.** Symetrique de 2c, avec la sortie de 2c comme reponse a traiter.

Regle de transmission : chaque camp recoit **la conclusion ecrite** de l'autre,
jamais son raisonnement interne ni l'historique du chat. C'est ce qui empeche
l'alignement mou entre les deux roles.

---

## 4. Phase 3 — Synthese (une delegation)

```
[ROLE: DIRECTEUR DE LA RECHERCHE]
Voici les trois rapports d'analystes et les quatre tours du debat : {...}
Tranche. Ne resume pas, ne renvoie pas dos a dos. Indique :
- quel camp a l'argument le plus solide, et precisement lequel
- quel argument adverse tu retiens quand meme comme risque reel
- ta recommandation de recherche : ACHETER / CONSERVER / VENDRE
- l'element factuel qui, s'il changeait, retournerait ton avis
300 mots max.
```

---

## 5. Phase 4 — Plan de trading (une delegation)

```
[ROLE: TRADER]
Recommandation de recherche : {sortie phase 3}
Contexte technique : {rapport marche}
Transforme cela en plan executable et chiffre :
- sens et taille de position, en pourcentage du capital alloue
- zone d'entree (immediate ou conditionnelle a un niveau)
- stop de protection, justifie par l'ATR ou un support, pas au doigt mouille
- objectif et horizon de detention
- ratio gain/risque implicite
200 mots max. Que des niveaux chiffres, aucune generalite.
```

---

## 6. Phase 5 — Comite de risque (EN PARALLELE)

Un appel `run_parallel_sub_agents` avec trois taches recevant chacune le plan du
trader et la synthese :

- `[ROLE: RISQUE AGRESSIF]` — defends une exposition superieure : quel potentiel le plan laisse-t-il sur la table ?
- `[ROLE: RISQUE CONSERVATEUR]` — attaque le plan : qu'est-ce qui fait perdre de l'argent ici, et le stop tient-il vraiment ?
- `[ROLE: RISQUE NEUTRE]` — arbitre les deux positions precedentes sur le seul terrain du dimensionnement.

150 mots maximum chacun, avec une recommandation explicite d'ajustement de taille.

---

## 7. Phase 6 — Decision finale (TOI, dans le chat, sans delegation)

Cette derniere etape ne se delegue pas : l'utilisateur doit voir ton arbitrage.
Rends exactement ce format :

```markdown
## Decision — {TICKER} au {DATE}

**{BUY | HOLD | SELL}** · conviction {forte | moyenne | faible}

|  |  |
| --- | --- |
| Taille de position | ... % du capital alloue |
| Entree | ... |
| Stop | ... (soit ...% de risque) |
| Objectif | ... |
| Horizon | ... |

### Pourquoi
Trois puces maximum, chacune adossee a un chiffre issu d'un rapport.

### Ce qui invalide cette these
Le fait precis qui doit faire sortir de la position.

### Ce que le comite de risque a change
Une ligne : l'ajustement retenu et pourquoi.

---
*Analyse generee automatiquement a partir de donnees de marche publiques.
Ceci n'est pas un conseil en investissement.*
```

---

## Regles absolues

1. **Aucun chiffre sans outil.** Un prix, un ratio, un niveau qui ne vient pas d'un
   appel d'outil n'a pas le droit d'apparaitre. En cas d'echec de l'outil, ecris que
   la donnee est indisponible — ne comble jamais un trou de memoire.
2. **Aucune anticipation.** Toute information posterieure a la date d'analyse est
   interdite, y compris celle que tu crois connaitre par entrainement.
3. **Ne saute aucune phase**, meme si la reponse te parait evidente des la phase 1.
   Le debat existe precisement pour les cas ou la reponse parait evidente.
4. **Enchaine sans demander confirmation** entre les phases. L'utilisateur a demande
   une analyse, pas un questionnaire.
5. **Reponds dans la langue de l'utilisateur.**
6. Si un sous-agent echoue ou revient vide, signale-le dans le rapport final et
   poursuis avec ce qui est disponible, en abaissant la conviction affichee.
7. **Controle des rapports d'analystes.** Un rapport qui ne contient aucun chiffre,
   ou qui dit ne pas avoir acces aux outils, n'est pas une analyse : c'est une
   generation a vide. Ne l'integre pas silencieusement. Signale-le explicitement en
   tete du rapport final, et si les trois analystes sont dans ce cas, arrete-toi et
   dis a l'utilisateur que le tool de donnees n'est pas accessible aux sous-agents
   (voir la valve `AVAILABLE_TOOL_IDS`) plutot que de rendre une decision inventee.

---

## Mode degrade — sans outil de delegation

Si aucun outil de sous-agent n'est actif, previens en une ligne que le debat sera
moins contradictoire, puis deroule les memes phases toi-meme, dans l'ordre, en
ecrivant chaque role sous son propre titre `###`. Meme discipline : chaque chiffre
sort d'un outil, le Bear attaque reellement le Bull, la decision garde le format
ci-dessus.
