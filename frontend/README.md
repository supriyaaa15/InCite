# InCite frontend

React (Vite) frontend. Talks to the FastAPI backend at `http://localhost:8000`.

## Setup

```bash
cd frontend
npm install
npm run dev
```

Opens at `http://localhost:5173`. Make sure the backend (`docker compose up`)
is running first — this frontend doesn't work standalone.

## Day 19 status

Login/register page working against `/auth/register`, `/auth/login`,
`/auth/me`. Token stored in localStorage, validated on load. Successful
login/register redirects to a placeholder home screen (real Collections
page comes Day 20).

## Structure

```
src/
├── api/client.js        All HTTP calls go through here — one place for
│                         base URL, auth headers, error handling.
├── context/AuthContext.jsx   Token + user state, available anywhere via useAuth().
├── pages/                Each route's top-level screen.
├── components/           Shared UI pieces (empty until Day 20+).
├── App.jsx                Routing + ProtectedRoute guard.
└── index.css              Design tokens + global styles.
```
