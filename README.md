# Pokéfada

Surveille des fiches produit Pokémon TCG et poste sur Discord quand un produit repasse en stock
au bon prix. Tourne gratuitement sur GitHub Actions (toutes les ~10 min).

## Mise en place

1. Crée un dépôt GitHub **public** et envoie-y tout ce dossier (y compris `.github/`).
2. Dépôt → *Settings → Secrets and variables → Actions → New repository secret* :
   nom `DISCORD_WEBHOOK_URL`, valeur = l'URL du webhook Discord.
3. Onglet *Actions* → *Pokéfada monitor* → *Run workflow* en mode **test** :
   un récap arrive sur Discord avec ce que le script lit sur chaque page.
4. Si une ligne est ⛔ ou ❓, le site bloque ou n'est pas lisible : voir plus bas.
5. Quand le test est bon, plus rien à faire : le mode normal tourne tout seul.

## Ajouter un produit

Ajoute un bloc dans `products.json` : `name`, `store`, `url` (lien de la fiche produit) et
`max_price` (alerte seulement si le prix est ≤ à cette valeur, `null` = pas de limite).
Relance un test pour vérifier que la page est lisible.

## Si une page n'est pas lisible

- Le script cherche d'abord les données de stock cachées dans la page, puis le texte.
  Sur certains sites ça ne suffit pas : ajoute `in_stock_regex` / `out_of_stock_regex`
  (texte à repérer, en minuscules) dans le bloc du produit.
- Si le site bloque (⛔ HTTP 403/429), c'est son anti-bot : il faut changer d'enseigne
  ou passer par les alertes Dealabs / Chocobonplan.
- Après 3 échecs de suite (~30 min), le script te le dit sur Discord, une seule fois.

## Sécurité

Ne mets jamais l'URL du webhook dans un fichier du dépôt : le dépôt est public.
Si elle a fuité, supprime le webhook dans Discord et crée-en un nouveau.
