# onboarding

5-step brand wizard that onboards a new tenant.

## Flow
1. **Brand** (`/onboarding/brand`) — scrape website, extract colors/fonts/vocab
2. **Papers** (`/onboarding/papers`) — upload prior credit papers
3. **Voice** (`/onboarding/voice`) — generate voice profile from papers
4. **Preview** (`/onboarding/preview`) — section ordering
5. **Save** (`/onboarding/save`) — persist tenant profile

## DB tables owned
- `brand_profile` — extracted brand
- `onboarding_documents` — uploaded papers
- `voice_profile` — house-style voice
- `section_orders` — section order preference
- `onboarding_state` — state machine

## Routes
| Method | Path | Purpose |
|--------|------|---------|
| POST | /onboarding/brand/analyze | Scrape and analyze website |
| GET  | /onboarding/brand | Get current brand profile |
| POST | /onboarding/brand/manual | Save manual brand |
| POST | /onboarding/papers | Upload paper |
| GET  | /onboarding/papers | List papers |
| DELETE | /onboarding/papers/{id} | Remove paper |
| POST | /onboarding/voice/generate | Build voice profile |
| GET  | /onboarding/voice | Get voice profile |
| GET  | /onboarding/preview | Section preview |
| GET  | /onboarding/preview-doc | Preview document |
| GET  | /onboarding/state | Current step state |
| POST | /onboarding/complete | Mark onboarding done |

## Owner
Onboarding team, #onboarding channel.
