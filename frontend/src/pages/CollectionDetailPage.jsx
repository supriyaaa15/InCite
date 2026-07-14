import { useEffect, useRef, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../context/AuthContext";
import Layout from "../components/Layout";

const POLL_INTERVAL_MS = 3000;

export default function CollectionDetailPage() {
  const { collectionId } = useParams();
  const { token } = useAuth();
  const navigate = useNavigate();

  const [collection, setCollection] = useState(null);
  const [documents, setDocuments] = useState([]);
  const [error, setError] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState(null);

  const fileInputRef = useRef(null);
  const pollIntervals = useRef({}); // documentId -> intervalId, so we can stop polling once a doc is done

  function startPolling(documentId) {
    if (pollIntervals.current[documentId]) return; // already polling this one

    pollIntervals.current[documentId] = setInterval(async () => {
      try {
        const updated = await api.getDocument(token, documentId);
        setDocuments((prev) => prev.map((d) => (d.id === documentId ? updated : d)));

        if (updated.status !== "processing") {
          clearInterval(pollIntervals.current[documentId]);
          delete pollIntervals.current[documentId];
        }
      } catch {
        // Document lookup failed (e.g. deleted mid-poll) — stop polling rather
        // than retry forever against something that's gone.
        clearInterval(pollIntervals.current[documentId]);
        delete pollIntervals.current[documentId];
      }
    }, POLL_INTERVAL_MS);
  }

  useEffect(() => {
    api
      .getCollection(token, collectionId)
      .then(setCollection)
      .catch((err) => setError(err.message));

    api
      .listDocuments(token, collectionId)
      .then((docs) => {
        setDocuments(docs);
        // Resume polling anything still processing from before a page refresh —
        // otherwise a reload mid-ingestion would leave the status frozen.
        docs.forEach((d) => {
          if (d.status === "processing") startPolling(d.id);
        });
      })
      .catch((err) => setError(err.message));

    // Stop every active poll on unmount — leaving intervals running after
    // navigating away would keep firing state updates on an unmounted
    // component and leak memory.
    return () => {
      Object.values(pollIntervals.current).forEach(clearInterval);
      pollIntervals.current = {};
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, collectionId]);

  async function handleFileChange(e) {
    const file = e.target.files[0];
    if (!file) return;

    setUploading(true);
    setUploadError(null);
    try {
      const doc = await api.uploadDocument(token, collectionId, file);
      setDocuments((prev) => [doc, ...prev]);
      startPolling(doc.id);
    } catch (err) {
      setUploadError(err.message);
    } finally {
      setUploading(false);
      e.target.value = ""; // allow re-selecting the same filename later
    }
  }

  return (
    <Layout>
      {error && <div className="error-banner">{error}</div>}

      <div className="page-header">
        <h2>{collection?.name ?? "Loading..."}</h2>
        <div className="page-header-actions">
          <button
            className="btn-secondary btn-compact"
            onClick={() => navigate(`/collections/${collectionId}/chat`)}
          >
            Chat
          </button>
          <button
            className="btn-primary btn-compact"
            onClick={() => fileInputRef.current.click()}
            disabled={uploading}
          >
            {uploading ? "Uploading..." : "+ Upload PDF"}
          </button>
        </div>
        <input
          ref={fileInputRef}
          type="file"
          accept="application/pdf"
          style={{ display: "none" }}
          onChange={handleFileChange}
        />
      </div>

      {uploadError && <div className="error-banner">{uploadError}</div>}

      {documents.length === 0 && (
        <p className="muted">No documents yet. Upload a PDF to start chatting with it.</p>
      )}

      <div className="document-list">
        {documents.map((doc) => (
          <div key={doc.id} className="document-row">
            <span className="document-filename">{doc.filename}</span>
            <span className={`status-badge status-${doc.status}`}>{doc.status}</span>
            {doc.status === "ready" && (
              <span className="mono muted-small">{doc.page_count} pages</span>
            )}
          </div>
        ))}
      </div>
    </Layout>
  );
}
