# Form spike, 2026-10-08

Method: loaded 11 real application pages in headless Chromium, read the fields, typed nothing, clicked nothing, submitted nothing. 3 seconds between pages. Workable not tested (no keyless job list).

| ATS | Pages | Loaded | Fields (visible) | Required | Notes |
|---|---|---|---|---|---|
| Greenhouse | groww (EU host), anthropic, databricks, stripe | 3 of 4 | 17 to 31 | 14 to 22 | Stripe's company-hosted page showed no form after 4.5 s |
| Lever | cred, paytm, meesho, zeta | 4 of 4 | 11 to 45 | 3 to 38 | Hidden `opportunityLocationId` field; "Apply with LinkedIn" button present |
| Ashby | sarvam, linear, notion | 3 of 3 | 8 to 33 | 4 to 10 | Two file inputs (resume, cover letter) |

## What it means for U7 and U8
- No login wall on any page. Public forms work in a clean browser context.
- Every form has pause-worthy questions: notice period, relatives at the company, work authorisation, relocation, pronouns and demographic radios, "why us" free text. Expect most applications to pause at least once, so the one-message batch pause is needed.
- Country, phone and location are typeahead widgets, not native selects (0 native selects on Greenhouse). The filler needs click-and-pick handling for them.
- Captcha scripts are present on 10 of 11 pages. Lever pages carry a `data-sitekey` widget in the page. Whether a challenge appears only shows on submit, which this spike did not do. The first live runs must measure it.
- Stripe-style company-hosted Greenhouse pages need the URL normalised to `job-boards.greenhouse.io/{token}/jobs/{id}` (KTD, U7).
- EU Greenhouse boards use `job-boards.eu.greenhouse.io`.
- Never click "Apply with LinkedIn" on Lever.

## Not covered
- Submit behaviour and challenge pages. Workable. Saved fixtures for tests (next step).
