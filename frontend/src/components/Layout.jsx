import { Link } from "react-router-dom";
import { useAuth } from "../context/AuthContext";

export default function Layout({ children }) {
  const { user, logout } = useAuth();

  return (
    <div className="layout">
      <header className="layout-header">
        <Link to="/" className="layout-brand">
          InCite
        </Link>
        <div className="layout-header-right">
          <span className="layout-user">{user?.email}</span>
          <button className="btn-link" onClick={logout}>
            Log out
          </button>
        </div>
      </header>
      <main className="layout-main">{children}</main>
    </div>
  );
}
