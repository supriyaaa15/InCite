# InCite frontend

The React and Vite app provides sign-in, registration, collections, PDF uploads and status polling, and collection chat with citations and session history. Chat can also display structured comparison and recommendation results.

## Local setup

Start the FastAPI backend using the [repository setup steps](../README.md#installation--startup). From the repository root:

```bash
cd frontend
npm ci
npm run dev
```

Open [http://localhost:5173](http://localhost:5173). The client defaults to `http://localhost:8000` for the API. Set `VITE_API_BASE_URL` before starting Vite or building if your backend uses another origin. Include the frontend origin in the backend's `ALLOWED_ORIGINS`. Vite exposes `VITE_` values to browsers, so use this variable for the public API URL only.

Run `npm run build` to produce `dist/`. The checked-in Vercel rewrite serves the SPA entry point for client-side routes.

## Source layout

- `src/api/client.js` defines API requests and attaches Bearer tokens.
- `src/context/AuthContext.jsx` manages sign-in state and validates the saved token with `/auth/me`.
- `src/App.jsx` defines public and protected routes.
- `src/pages/` and `src/components/` contain page views and the shared layout.

See the [HTTP API reference](../docs/api.md) for the backend contract.
