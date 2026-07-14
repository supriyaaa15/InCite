import { Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./context/AuthContext";
import LoginPage from "./pages/LoginPage";

function ProtectedRoute({ children }) {
  const { token, loading } = useAuth();
  if (loading) return null; // avoid a flash-redirect while we're still checking the token
  if (!token) return <Navigate to="/login" replace />;
  return children;
}

function HomePlaceholder() {
  // Stands in until Day 20 builds the real Collections page.
  const { user, logout } = useAuth();
  return (
    <div className="app-shell">
      <h2>Logged in as {user?.email}</h2>
      <button className="btn-link" onClick={logout}>
        Log out
      </button>
    </div>
  );
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/"
        element={
          <ProtectedRoute>
            <HomePlaceholder />
          </ProtectedRoute>
        }
      />
    </Routes>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <AppRoutes />
    </AuthProvider>
  );
}
