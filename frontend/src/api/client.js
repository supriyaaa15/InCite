const API_BASE_URL = "http://localhost:8000";

async function request(path, { method = "GET", body, token } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;

  const res = await fetch(`${API_BASE_URL}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    let detail = "Something went wrong. Please try again.";
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch {
      // response wasn't JSON — keep the generic message
    }
    throw new Error(detail);
  }

  if (res.status === 204) return null;
  return res.json();
}

export const api = {
  register: (email, password) =>
    request("/auth/register", { method: "POST", body: { email, password } }),
  login: (email, password) =>
    request("/auth/login", { method: "POST", body: { email, password } }),
  me: (token) => request("/auth/me", { token }),

  listCollections: (token) => request("/collections", { token }),
  createCollection: (token, name) =>
    request("/collections", { method: "POST", body: { name }, token }),
  getCollection: (token, collectionId) => request(`/collections/${collectionId}`, { token }),
};
