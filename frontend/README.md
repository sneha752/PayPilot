# PayPilot Frontend

React + TypeScript + Vite + Tailwind CSS client for the PayPilot backend.

> **Your money. Your rules.**

## Quick start

```bash
cd frontend
npm install
cp .env.example .env   # optional — defaults to http://localhost:8000
npm run dev            # serves on http://localhost:5173
```

The dev server is pinned to port **5173** (`strictPort`) because the
backend's CORS allowlist permits `http://localhost:5173`.

Start the backend first (`uvicorn app.main:app --reload` from `backend/`);
the header shows a dev-only **Backend connected / Backend unavailable**
pill based on a read-only `GET /products` check.

## Configuration

| Variable            | Default                 | Purpose                  |
| ------------------- | ----------------------- | ------------------------ |
| `VITE_API_BASE_URL` | `http://localhost:8000` | FastAPI backend base URL |

**Never put secrets here.** `VITE_`-prefixed variables ship to the
browser. PayPal credentials and Gemini API keys live only in
`backend/.env`.

## Structure

```
src/
├── components/   # Reusable UI (header, status pill, panels)
├── pages/        # Page-level compositions
├── services/     # Centralized API client (the only place fetch happens)
├── types/        # TS mirrors of the backend Pydantic schemas
├── hooks/        # React hooks (e.g. useBackendStatus)
├── lib/          # Config (API base URL)
├── App.tsx       # App shell
└── main.tsx      # Entry point
```

- All backend calls go through `services/api.ts` → `services/httpClient.ts`.
- Errors are normalized into `ApiError` (`services/apiError.ts`), which
  preserves the HTTP status and the backend's `detail` message.
- Types in `src/types/` mirror `backend/app/schemas/` — keep them in sync
  with the backend; never invent fields.

## Scripts

| Command           | What it does                     |
| ----------------- | -------------------------------- |
| `npm run dev`     | Dev server on `localhost:5173`   |
| `npm run build`   | Type-check (`tsc -b`) + build    |
| `npm run preview` | Preview the production build     |
