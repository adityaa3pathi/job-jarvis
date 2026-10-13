# Job Agent web

Next.js frontend for the job agent API (`src/jobagent/web`).

```bash
npm install
API_URL=http://127.0.0.1:8000 npm run dev
```

`/api/*` is proxied to `API_URL`, so the login cookie stays first-party.
`API_URL` is read when the app is built, so set it before `npm run build`.
