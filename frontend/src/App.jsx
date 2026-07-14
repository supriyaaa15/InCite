import { Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./context/AuthContext";
import LoginPage from "./pages/LoginPage";
import CollectionsPage from "./pages/CollectionsPage";
import CollectionDetailPage from "./pages/CollectionDetailPage";

function ProtectedRoute({ children }) {
  const { token, loading } = useAuth();
  if (loading) return null; // avoid a flash-redirect while we're still checking the token
  if (!token) return <Navigate to="/login" replace />;
  return children;
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/"
        element={
          <ProtectedRoute>
            <CollectionsPage />
          </ProtectedRoute>
        }
      />
      <Route
        path="/collections/:collectionId"
        element={
          <ProtectedRoute>
            <CollectionDetailPage />
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
