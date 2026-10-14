# Pokéfada

Surveille des fiches produit Pokémon TCG et poste sur Discord quand un produit repasse en stock
au bon prix. Tourne gratuitement sur GitHub Actions (toutes les ~10 min).

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
