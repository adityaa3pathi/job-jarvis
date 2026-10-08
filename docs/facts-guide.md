# Fact sheet guide

`facts.yaml` is the only source the agent may claim from. Review it line by line once.

- `confirmed: false` means the agent will not use the item. Flip it to `true` only if you can defend it in an interview.
- Every line marked "(from your resume - confirm)" in the two resumes starts as `false`, as do numbers tied to them.
- Contact details and standard answers start `false` too. Confirm them so forms can be filled.
- A skill needs `evidence` (a confirmed fact id, or `resume:<file>#<section>`). A confirmed skill needs at least one confirmed evidence entry.
- Each bullet lists the scope words it allows (`lead`, `own`, `production`, `end_to_end`, ...). A resume may use a scope word only if the cited bullet lists it.
- Every number in a bullet needs a `numbers` entry. A confirmed bullet cannot contain an unconfirmed number.
- Not seeded on purpose: salary, notice period, visa or work authorisation, demographics. Add only if you want them answered automatically.
- `fallback` names a confirmed bullet to use while the first one is unconfirmed.

Check your edits with `uv run pytest tests/test_facts.py`.
