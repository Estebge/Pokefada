# Pokéfada

Surveille les ETB et bundles Pokémon dans plusieurs magasins et poste sur Discord quand un
produit repasse en stock sous le prix maximum. Tourne gratuitement sur GitHub Actions
(toutes les ~10 min).

## Fichiers

| Fichier | Rôle |
|---|---|
| `monitor.py` | le script |
| `products.json` | les produits, les magasins et les prix maximum |
| `etat.json` | mémoire du script (remis à `{}` pour repartir de zéro) |
| `requirements.txt` | dépendance Python (`requests`) |
| `.github/workflows/monitor.yml` | planification GitHub (dossier caché !) |

## Lancer un test

Onglet *Actions* → *Pokéfada monitor* → *Run workflow* → mode **test**.
Discord reçoit un message par produit (🟢 en stock, 🔴 rupture, ⛔ bloqué, ❓ illisible,
🔗 lien non résolu) et un diagnostic par magasin. Le détail complet (adresse exacte de
chaque page, ce que le script y a lu) est dans le passage, rubrique *Summary*.

## products.json

Chaque produit a un `id`, un `name`, un `max_price` et une liste d'`offers`.
Chaque offre a :

- `store` : nom affiché ;
- `url` (adresse directe de la fiche) **ou** `link` (lien raccourci Chocobonplan :
  le script le suit une seule fois jusqu'au magasin, retire le suivi d'affiliation
  et garde l'adresse dans `etat.json`) ;
- `status` : `actif` (surveillé en permanence), `test` (vérifié seulement en mode test :
  pages liste, accès secondaires), `off` (jamais).

Une page **liste** mélange plusieurs produits : « en stock » peut concerner un autre
article. Passe-la en `actif` seulement si le test montre qu'elle est fiable.

Pour un site dont le stock n'est pas lisible, tu peux ajouter dans l'offre
`in_stock_regex` / `out_of_stock_regex` (texte à repérer, en minuscules).

## Pas encore couverts

- **Amazon** : `off`. Interdit par ses conditions, bloque les serveurs GitHub, et vend
  ces produits sur invitation : garde la demande d'invitation sur le site.
- **Auchan, Rakuten, Figurines Goodies** : aucun lien trouvé pour ces produits.
  Ajoute une offre avec l'`url` de la fiche quand tu en trouves une.
- **Règne Delta (ME06)** : sortie le 6 novembre 2026. Seul Smyths Toys a déjà des fiches ;
  ajoute les autres magasins dès que leurs fiches apparaissent (Chocobonplan les liste).
- **Accès généraux** (JouéClub, KingJouet) : pages d'accueil ou de catégorie, pas
  spécifiques à un produit, donc non surveillables.

## Surveillance toutes les 30 secondes

GitHub Actions ne peut pas descendre à 30 s : le minimum est 5 min, et en pratique GitHub
lance ce workflow toutes les 3 à 7 h. Pour 30 s, il faut une machine allumée en permanence
(PC, Raspberry Pi, petit serveur) :

```
pip install -r requirements.txt
set DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...   (PowerShell : $env:DISCORD_WEBHOOK_URL="...")
python monitor.py --loop
```

- produit qui passe dispo sous son `max_price` (ou en file d'attente) : alerte `@everyone` immédiate ;
- produit déjà signalé et toujours dispo : rappel groupé toutes les 12 h ;
- les magasins sont recherchés toutes les 10 min pour repérer les nouvelles fiches.

Si tu lances `--loop`, désactive le workflow GitHub (onglet *Actions* → *Pokéfada monitor* →
*Disable workflow*), sinon tu reçois les alertes en double.
