import { createContext, useContext, useEffect, useState } from "react";
import { api } from "../api/client";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [token, setToken] = useState(() => localStorage.getItem("incite_token"));
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  // On load (or whenever the token changes), confirm it's still valid by
  // fetching the current user. If it's expired/invalid, clear it out
  // instead of leaving the app in a broken "logged in but useless" state.
  useEffect(() => {
    if (!token) {
      setLoading(false);
      return;
    }
    api
      .me(token)
      .then(setUser)
      .catch(() => {
        localStorage.removeItem("incite_token");
        setToken(null);
      })
      .finally(() => setLoading(false));
  }, [token]);

  async function login(email, password) {
    const { access_token } = await api.login(email, password);
    localStorage.setItem("incite_token", access_token);
    setToken(access_token);
  }

  async function register(email, password) {
    await api.register(email, password);
    await login(email, password); // register doesn't return a token, so log in right after
  }

  function logout() {
    localStorage.removeItem("incite_token");
    setToken(null);
    setUser(null);
  }

  return (
    <AuthContext.Provider value={{ token, user, loading, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
