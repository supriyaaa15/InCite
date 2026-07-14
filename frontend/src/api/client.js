const API_BASE_URL = "http://localhost:8000";

async function request(path, { method = "GET", body, formData, token } = {}) {
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;

  let requestBody;
  if (formData) {
    requestBody = formData; // browser sets Content-Type (with boundary) automatically — don't set it ourselves
  } else if (body) {
    headers["Content-Type"] = "application/json";
    requestBody = JSON.stringify(body);
  }

  const res = await fetch(`${API_BASE_URL}${path}`, { method, headers, body: requestBody });

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

  listDocuments: (token, collectionId) =>
    request(`/collections/${collectionId}/documents`, { token }),
  uploadDocument: (token, collectionId, file) => {
    const formData = new FormData();
    formData.append("file", file);
    return request(`/collections/${collectionId}/documents`, {
      method: "POST",
      formData,
      token,
    });
  },
  getDocument: (token, documentId) => request(`/documents/${documentId}`, { token }),

  sendMessage: (token, collectionId, message, sessionId) =>
    request(`/collections/${collectionId}/chat`, {
      method: "POST",
      body: { message, session_id: sessionId },
      token,
    }),
  listSessions: (token) => request("/sessions", { token }),
  getSessionMessages: (token, sessionId) => request(`/sessions/${sessionId}/messages`, { token }),
};
