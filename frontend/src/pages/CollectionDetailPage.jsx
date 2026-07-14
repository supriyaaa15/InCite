import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../context/AuthContext";
import Layout from "../components/Layout";

export default function CollectionDetailPage() {
  const { collectionId } = useParams();
  const { token } = useAuth();

  const [collection, setCollection] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api
      .getCollection(token, collectionId)
      .then(setCollection)
      .catch((err) => setError(err.message));
  }, [token, collectionId]);

  if (error) {
    return (
      <Layout>
        <div className="error-banner">{error}</div>
      </Layout>
    );
  }

  return (
    <Layout>
      <h2>{collection?.name ?? "Loading..."}</h2>
      <p className="muted">Document upload and chat coming next.</p>
    </Layout>
  );
}
