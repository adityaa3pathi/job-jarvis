# job-jarvis helper (private Chrome extension)

Two jobs, both on your click only:

- **Form helper**: reads the questions on a job site's form and shows a suggested answer under
  each one, drawn from your job-jarvis profile. **You always press Submit or Save.**
- **LinkedIn helper**: fills your own job-jarvis notes into LinkedIn's Connect and Message boxes.
  **You always press LinkedIn's Send.**

It never submits, sends, connects, likes or posts by itself.

## Install (once)
1. job-jarvis → Outreach → LinkedIn helper → **Make token**. Copy it.
2. Chrome → `chrome://extensions` → turn on **Developer mode** → **Load unpacked** → pick this `extension` folder.
3. Open the extension's **settings**: paste your job-jarvis address and the token, fill your contact
   details and saved answers (notice period, CTC, city…), then **Save** and allow access.
4. Put your career summary on the job-jarvis **Profile** page. Answers come only from it.

## Form helper
- On Naukri, Instahyre, LinkedIn, Wellfound, YC, Cutshort, Hirist and company career pages
  (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Keka, Kula, Darwinbox, Zoho, Freshteam,
  Workday), a small **job-jarvis** bar shows at the bottom right when the page has a form.
- Any other site: click the extension → **Help with this page's form**.
- **Suggest answers** reads the questions and shows a card under each. New questions (next step of a
  multi-step form) get answers as they appear.
- **Fill** puts one answer in; **Copy** copies it; **Fill all** fills only answers from your profile
  or your saved answers.
- Card colours: green = from your profile or saved answers; amber **Check this** = an AI suggestion
  (pay, notice, location) or a number your profile doesn't state; grey = answer it yourself
  (gender, caste, disability and other personal questions are never drafted).
- On LinkedIn it reads only an open popup (Easy Apply, edit profile).
- Each **Suggest answers** uses your daily AI budget in job-jarvis.

## LinkedIn helper
- **LinkedIn job page** → popup → *Send this job to job-jarvis* → reach out from there.
- **Someone's profile** → popup → *Save as contact* for a job. Write and edit the note in job-jarvis.
- Back on their profile → *Fill invite note* → check it → press **Send** in LinkedIn. job-jarvis
  records it and reminds you in 7 days to check if they accepted.
- After they accept → *They accepted* → *Fill message* → press **Send**. One follow-up comes 5 days later.

## Limits and risk
- Job sites' terms limit bots. This stays read-and-fill on your click, and never submits.
  Keep to the 20 LinkedIn invites a day job-jarvis allows.
- Custom dropdowns and tag boxes (skills, cities) may need you to pick the item from the site's
  list after a fill. If a site changes its layout, use **Copy** instead.
- Tests for the page reader live in `tests/extension` (`npm ci && npx vitest run`).
