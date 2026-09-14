# etilbudsavis-notion

Flags ingredients on a Notion database as "on sale" by checking eTilbudsavis
(Tjek) for current grocery discounts, on a schedule, from a Docker container
on your Raspberry Pi.

## 1. Set up Notion

1. Create an integration at https://www.notion.so/my-integrations, copy its
   **Internal Integration Token** -> `NOTION_TOKEN`.
2. Open your ingredient database in Notion, click **... -> Connections ->
   Connect to** and add your integration.
3. Copy the database ID from its URL:
   `https://www.notion.so/<workspace>/<DATABASE_ID>?v=...` -> `NOTION_DATABASE_ID`.
4. Add these properties to the database if they don't already exist
   (exact names matter, or edit them in `sync.py`):
   - `On Sale` — Checkbox
   - `Store` — Text
   - `Sale Price` — Number
   - `Sale Ends` — Date
   Your ingredient name should be the database's **Title** property
   (default name `Name` — set `NOTION_NAME_PROPERTY` if yours is different).

## 2. Configure

```bash
cp .env.example .env
# edit .env: NOTION_TOKEN, NOTION_DATABASE_ID, and your lat/lng
```

Your lat/lng just need to be roughly where you shop, so the offer search
can filter to nearby stores.

## 3. Run on the Raspberry Pi

```bash
docker compose up -d --build
docker compose logs -f
```

It runs once immediately, then loops every `RUN_INTERVAL_HOURS`.

## 4. Fixing the Tjek request (likely needed)

`sync.py`'s `search_offers()` is a best-effort implementation — eTilbudsavis
(now branded "Tjek") has never published an API spec, and the exact
endpoint/params have moved before. If your first run logs "no offer" for
things you know are discounted, the request shape has drifted from what's
in the script. To fix it:

1. Open https://etilbudsavis.dk in a desktop browser (Chrome/Firefox).
2. Open DevTools (F12) -> **Network** tab -> filter to **Fetch/XHR**.
3. Search for a product you know is currently on sale (e.g. "æg").
4. Look through the requests for one whose response is JSON containing a
   list of offers (heading, price, dealer/store name, dates). Right-click
   it -> **Copy -> Copy as cURL**.
5. Send me that cURL command (strip any cookies/auth you're not comfortable
   sharing — the offer-search endpoint is normally public with no auth).

I'll update `search_offers()` to match the real endpoint, params, and
response shape exactly.

## Notes

- This depends on an unofficial, undocumented API. It can break without
  warning — the container logs errors rather than crashing outright, but
  keep an eye on it after eTilbudsavis does any redesign.
- Runs are politely rate-limited (0.5s between ingredient lookups) — avoid
  cranking `RUN_INTERVAL_HOURS` down much further or hammering it with a
  huge ingredient list.
