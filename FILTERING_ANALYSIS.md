# Job Filtering Analysis - 21-10-2026

## Your Experience
- ~1.5 years total (DocNow since Dec 2024 + Badaya internship 6 months)
- Current config: max_years_asked = 5

## Problems Found

### 1. **5-Year Threshold Too Strict** (HIGH IMPACT)
**Location**: `src/jobagent/rules.py:175`
```python
if (asked and asked[0] >= max_years_asked) or SENIOR_TITLE.search(...)
```

**Issue**: Jobs asking for "5+ years" OR "5-7 years" are dropped as too_senior.

**Why it matters**: Many mid-level jobs say "3-5 years" where 3 is minimum. The code reads the LOWEST number (3), so these pass. But jobs saying "5-7 years" get dropped even though you could apply with strong projects.

**False negatives**: Jobs that say:
- "5+ years experience"
- "5-7 years in software engineering"
- Jobs that don't specify years but say "Mid-level" or "Staff" (see #2)

### 2. **Senior Title Filter Too Aggressive** (HIGH IMPACT)
**Location**: `src/jobagent/rules.py:41-47, 175-178`

**Drops jobs with these titles**:
- "Senior Software Engineer"
- "Tech Lead"
- "Lead Engineer"
- "Staff Engineer"
- "Principal Engineer"

**Why it matters**: Many companies have inflated titles. A "Senior Engineer" at a startup might be equivalent to "Software Engineer II" at a big company. You have strong AI/LLM experience that could qualify.

### 3. **Remote Location Edge Cases** (MEDIUM IMPACT)
**Location**: `src/jobagent/rules.py:145-159`

**Issue**: Remote job detection might miss:
- "Remote - India/US" (ambiguous format)
- "Remote, India preferred"
- "Anywhere (India timezone)"
- Some variations of "Remote (multiple countries)"

**Current logic**: Drops if has FOREIGN_MARKERS + no INDIA_MARKERS + not open region

### 4. **Missing Search Terms** (LOW-MEDIUM IMPACT)
**Location**: `searches.yaml`

**Currently searches for**: full stack, ai engineer, backend, python, nodejs, react, software engineer, generative ai, llm engineer, react native

**Potentially missing**:
- "machine learning engineer" on some boards (only on instahyre)
- "ml engineer"
- "data engineer" (you have pgvector, embeddings experience)
- "platform engineer"
- "backend engineer" (vs "backend developer")

## Impact Estimate

Based on typical job distribution:
- ~40-60% of "mid-level" jobs ask for 3-5 years (you see these)
- ~20-30% ask for 5+ years (DROPPED but you could apply with strong projects)
- ~30-40% of AI/ML jobs have "Senior" or "Lead" in title even for 3-5 year roles (DROPPED)

**You're likely missing 30-50% of suitable jobs.**

## Recommendations

### Option A: Relax 5-year threshold (Safest)
Change `max_years_asked` from 5 to 6 in config.yaml.
- Lets through "5-7 years" jobs
- You can still skip them manually if too senior
- Low risk

### Option B: Add "possibly_too_senior" status (Better)
Instead of dropping, flag as "possibly_too_senior" so you can review.
- Requires code change in rules.py
- More work but better control

### Option C: Disable senior title filter for AI/ML roles (Aggressive)
Many AI/ML "Senior" roles are actually mid-level due to field being new.
- Risky - more manual filtering needed

## Next Steps

1. Check recent runs: How many jobs dropped as "too_senior" or "outside_india"?
2. Sample dropped jobs to confirm false negatives
3. Adjust filters based on real data
