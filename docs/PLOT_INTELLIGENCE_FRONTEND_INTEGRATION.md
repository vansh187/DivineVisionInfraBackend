# Plot Intelligence — Chatbot Frontend Integration

**Goal:** Add a click-triggered "Find My Plot" flow inside the existing chatbot
widget. When tapped, the bot asks a couple of quick questions (or accepts a
free-text description), calls the new `/inventory` APIs, shows matching plots
as cards, and — once the visitor opens/views a plot — proactively surfaces
AI-picked recommendations and similar alternatives.

This sits **alongside** the existing menu-driven flow
(`docs/CHATBOT_FRONTEND_INTEGRATION.md`) — it does not replace it. Reuse the
same `session_id`/`lead_id` you already get from `POST /chatbot/session/init`;
every inventory call below should be tagged with them so the recommendation
engine has browsing history to work from.

## 1. Entry point

Add one new button wherever it fits best in the current menu structure —
recommended: as an extra option on the main menu, and also offered inside
"Only Browsing":

```json
{"label": "🏡 Find My Perfect Plot", "value": "plot_intelligence_start", "action": "plot_intelligence"}
```

New button `action` value: **`"plot_intelligence"`**. Unlike `"chatbot_menu"`
(which posts back into `/chatbot/message`), this action is handled **entirely
on the frontend** — it opens a dedicated plot-search panel/sheet inside the
widget instead of continuing the LLM conversation. (Rationale: filtering/
search/recommendations are deterministic REST calls, not something that needs
to round-trip through the chat LLM.)

## 2. Conversation/UI flow

```
[Find My Plot tapped]
        │
        ▼
"How would you like to search?"
  ┌─────────────────────┐   ┌───────────────────────────┐
  │ Describe it to me    │   │ Guide me through it        │
  │ (free text)           │   │ (quick questions)          │
  └─────────────────────┘   └───────────────────────────┘
        │                              │
        ▼                              ▼
  Text input box              Q1: City? (chips, populated
  "e.g. 150 sq yd plot         from known projects: Sonipat
   near NH-1 under 40 lakh"    / Suraksha Enclave, etc.)
        │                              │
        │                       Q2: Type? (Plot / Floor /
        │                        Flat / Commercial chips)
        │                              │
        │                       Q3: Size range? (chips:
        │                        <100, 100–150, 150–200,
        │                        200+ sq yd)
        │                              │
        │                       Q4: Budget range? (chips,
        │                        or "skip")
        │                              │
        ▼                              ▼
  POST /inventory/search/nl     GET /inventory/search
        │                              │
        └──────────────┬───────────────┘
                        ▼
              Render result cards
                        │
              (visitor taps a card)
                        │
                        ▼
        POST /inventory/{id}/view  (fire-and-forget)
                        │
                        ▼
        Show plot detail panel, AND
        GET /inventory/recommendations
        → render "Similar plots" strip using
          similar_alternatives[thatPlotId]
```

After any search (guided or NL), also show a persistent "✨ Recommended for
you" section beneath the results, populated from `GET /inventory/recommendations`
— this works even before the visitor taps anything (cold-start falls back to
qualification data or just recent listings, so it's never empty).

## 3. API contracts

Base path: `/inventory`. No auth required (same as `/market-trends`). All
responses are JSON; errors are `{"detail": "<code>"}` with 400 (bad input) or
500.

### a) Guided search — `GET /inventory/search`

Query params (all optional): `project_name, city, unit_type, status,
min_area_sqyd, max_area_sqyd, min_budget, max_budget, limit, offset`

```
GET /inventory/search?city=Sonipat&unit_type=plot&min_area_sqyd=100&max_area_sqyd=150&limit=20
```
```json
{
  "count": 12,
  "units": [
    {
      "id": "27bb1b9f-7545-4218-b145-4a71d55f5c7d",
      "project_name": "Suraksha Enclave",
      "city": "Sonipat",
      "locality": "Sector-15, Ganaur",
      "block": "B",
      "unit_number": "B1-D",
      "unit_type": "plot",
      "width_mtr": 7.312,
      "length_mtr": 13.0,
      "area_sqmt": 95.056,
      "area_sqyd": 113.69,
      "status": "available",
      "estimated_price": 12165000.0
    }
  ]
}
```
`estimated_price` can be `null` — no market-trend data exists for that
city/type yet. **Render as "Price on request", never as ₹0 or blank.**

### b) Natural-language search — `POST /inventory/search/nl`

```json
{ "query": "150 sq yd plot near NH-1 under 40 lakh", "session_id": "sess-123" }
```
```json
{
  "count": 8,
  "units": [ /* same shape as above */ ],
  "parsed_filters": { "min_area_sqyd": 127.5, "max_area_sqyd": 172.5, "unit_type": "plot", "max_budget": 4000000 }
}
```
Show `parsed_filters` back to the visitor as a confirmation chip row
("Searching: plot · 127–172 sq yd · under ₹40L") so they can see what was
understood and correct it — the parser widens a single stated size into a
±15% band rather than an exact match, so this matters for trust. Budget/city
that wasn't mentioned in the query is simply absent from `parsed_filters`,
never guessed.

### c) Record a view — `POST /inventory/{inventory_id}/view`

Call this the moment a visitor opens a plot's detail view (not on
hover/scroll-by).
```json
{ "lead_id": "f4e1d853-9230-4ea1-b5d2-7229a4d4aa56", "session_id": "sess-123" }
```
```json
{ "recorded": true }
```
Fire-and-forget: don't block the UI on this call, and don't show an error if
it comes back `{"recorded": false}` (it silently no-ops on bad/stale ids —
nothing for the visitor to see or retry).

### d) Recommendations — `GET /inventory/recommendations`

```
GET /inventory/recommendations?lead_id=f4e1d853-...&limit=10
```
```json
{
  "best_fit": [ /* top-N ranked units, same card shape as above */ ],
  "similar_alternatives": {
    "27bb1b9f-...": [ /* up to 3 units similar to the plot with this id, which the visitor viewed */ ]
  }
}
```
- Call with `lead_id` (preferred — persists across the visitor's chatbot
  lifetime) or `session_id`.
- `best_fit` → render as the "✨ Recommended for you" strip.
- `similar_alternatives[plotId]` → render as "Similar to this plot" directly
  on that plot's detail panel, keyed by whichever plot the visitor is
  currently viewing.
- Re-fetch this after every `view` call so the strip updates as the visitor
  browses.

## 4. Card content mapping

| Field | Display |
|---|---|
| `project_name` + `unit_number` | Card title, e.g. "Suraksha Enclave · B1-D" |
| `block`, `locality`, `city` | Subtitle, e.g. "Block B · Sector-15, Ganaur, Sonipat" |
| `area_sqyd` (+ `area_sqmt` as secondary) | "113.69 sq yd" |
| `unit_type` | Badge: Plot / Floor / Flat / Commercial |
| `status` | Badge: Available (green) / Held (amber) / Sold (grey, non-clickable or shown as "Sold") |
| `estimated_price` | "₹1.22 Cr" or "Price on request" if `null` |

## 5. Edge cases / copy

- Zero results from either search → "No exact matches — here's what's
  closest" and fall through to showing `recommendations.best_fit` instead of
  a dead end.
- `similar_alternatives` empty for a viewed plot → hide that section rather
  than showing an empty strip.
- Visitor not yet identified (`lead_id` unknown) → still call
  `/inventory/recommendations` with just `session_id`; the chatbot session
  already has a `lead_id` internally from `/chatbot/session/init`, so prefer
  using that even before the visitor has given a name/phone.

## 6. What's unchanged

- Everything in `docs/CHATBOT_FRONTEND_INTEGRATION.md` — the menu flow,
  `chatbot_menu` buttons, auth/booking — is untouched.
- `/inventory/*` is a separate REST surface, not part of the
  `/chatbot/message` LLM loop — no new message types or button actions flow
  through the chat transcript for this feature, only the one new
  `plot_intelligence` entry-point button.
