# Shopping — Channel Routing Rules

> Rules for deciding which shopping channel to use for each item. The Shopping agent applies these rules when processing a shopping request.

## Amazon Subscribe & Save

- **Items:** {recurring non-perishables — toilet paper, tissue, diapers, detergent, etc.}
- **Cadence:** {monthly, bi-monthly, etc.}
- **Notes:** {preferred brands, size preferences, etc.}

## Costco

- **Items:** {bulk purchases — snacks, beverages, household supplies, etc.}
- **Cadence:** {ad hoc, monthly trip, etc.}
- **Notes:** {membership details, preferred location, etc.}

## Local Grocery

- **Store:** {Trader Joe's, Safeway, etc.}
- **Items:** {perishables — milk, produce, bread, etc.}
- **Cadence:** {weekly, as needed}
- **Notes:** {delivery vs. pickup preferences, etc.}

## Routing Logic

Default rules (override per-item as needed):

1. **Amazon S&S** for recurring non-perishables with predictable consumption
2. **Costco** for bulk/value items or one-off large purchases
3. **Local grocery** for perishables or items needed within 48 hours
