import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../context/AuthContext";
import Layout from "../components/Layout";

export default function CollectionsPage() {
  const { token } = useAuth();
  const navigate = useNavigate();

  const [collections, setCollections] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [showForm, setShowForm] = useState(false);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState(null);
  const [deletingId, setDeletingId] = useState(null);

  useEffect(() => {
    document.title = "Collections · InCite";
  }, []);

  useEffect(() => {
    api
      .listCollections(token)
      .then(setCollections)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [token]);

  async function handleCreate(e) {
    e.preventDefault();
    if (!newName.trim()) return;
    setCreating(true);
    setCreateError(null);
    try {
      const collection = await api.createCollection(token, newName.trim());
      setCollections((prev) => [collection, ...prev]);
      setNewName("");
      setShowForm(false);
    } catch (err) {
      setCreateError(err.message);
    } finally {
      setCreating(false);
    }
  }

  async function handleDelete(e, collectionId, name) {
    e.stopPropagation(); // don't also trigger the card's navigate
    const confirmed = window.confirm(
      `Delete "${name}"? This permanently deletes every document and chat inside it.`,
    );
    if (!confirmed) return;

    setDeletingId(collectionId);
    try {
      await api.deleteCollection(token, collectionId);
      setCollections((prev) => prev.filter((c) => c.id !== collectionId));
    } catch (err) {
      setError(err.message);
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <Layout>
      <div className="page-header">
        <h2>Collections</h2>
        <button className="btn-primary btn-compact" onClick={() => setShowForm((v) => !v)}>
          {showForm ? "Cancel" : "+ New collection"}
        </button>
      </div>

      {showForm && (
        <form className="inline-form" onSubmit={handleCreate}>
          {createError && <div className="error-banner">{createError}</div>}
          <input
            type="text"
            placeholder="e.g. College Notes, Resume, Research Papers"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            autoFocus
          />
          <button className="btn-primary btn-compact" type="submit" disabled={creating}>
            {creating ? "Creating..." : "Create"}
          </button>
        </form>
      )}

      {loading && <p className="muted">Loading collections...</p>}
      {error && <div className="error-banner">{error}</div>}

      {!loading && !error && collections.length === 0 && (
        <p className="muted">
          No collections yet. Create one to start uploading documents and chatting with them.
        </p>
      )}

      <div className="collection-grid">
        {collections.map((c) => (
          <div
            key={c.id}
            className="collection-card"
            onClick={() => navigate(`/collections/${c.id}`)}
            onKeyDown={(e) => e.key === "Enter" && navigate(`/collections/${c.id}`)}
            role="button"
            tabIndex={0}
          >
            <div className="collection-card-top">
              <h3>{c.name}</h3>
              <button
                className="btn-icon-delete"
                onClick={(e) => handleDelete(e, c.id, c.name)}
                disabled={deletingId === c.id}
                title="Delete collection"
                aria-label={`Delete ${c.name}`}
              >
                {deletingId === c.id ? "..." : "×"}
              </button>
            </div>
            <span className="mono muted-small">
              {new Date(c.created_at).toLocaleDateString()}
            </span>
          </div>
        ))}
      </div>
    </Layout>
  );
}
