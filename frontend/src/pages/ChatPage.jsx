import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import { api } from "../api/client";
import { useAuth } from "../context/AuthContext";
import Layout from "../components/Layout";

export default function ChatPage() {
  const { collectionId } = useParams();
  const { token } = useAuth();

  const [collection, setCollection] = useState(null);
  const [sessions, setSessions] = useState([]);
  const [messages, setMessages] = useState([]); // [{role, content, citations?, reasoning?}]
  const [sessionId, setSessionId] = useState(null);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState(null);

  const bottomRef = useRef(null);

  useEffect(() => {
    document.title = collection ? `Chat · ${collection.name} · InCite` : "InCite";
  }, [collection]);

  useEffect(() => {
    api.getCollection(token, collectionId).then(setCollection).catch(() => {});

    // GET /sessions returns every session across all of the user's
    // collections — filter down to just this collection's history.
    api
      .listSessions(token)
      .then((all) => setSessions(all.filter((s) => s.collection_id === collectionId)))
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, collectionId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function loadSession(session) {
    if (sending) return; // switching sessions mid-request would let the
    // pending response land in the wrong conversation once it resolves
    setError(null);
    setSessionId(session.id);
    try {
      const msgs = await api.getSessionMessages(token, session.id);
      // Note: reasoning isn't persisted server-side (only returned live at
      // generation time), so resumed history shows content + citations,
      // but no reasoning line — expected, not a bug.
      setMessages(msgs.map((m) => ({ role: m.role, content: m.content, citations: m.citations })));
    } catch (err) {
      setError(err.message);
    }
  }

  function startNewChat() {
    if (sending) return; // same reasoning as loadSession above
    setSessionId(null);
    setMessages([]);
    setError(null);
  }

  async function handleDeleteSession(e, id) {
    e.stopPropagation(); // don't also trigger loadSession
    if (sending) return;

    const confirmed = window.confirm("Delete this chat? This can't be undone.");
    if (!confirmed) return;

    try {
      await api.deleteSession(token, id);
      setSessions((prev) => prev.filter((s) => s.id !== id));
      // If the deleted session was the one currently open, fall back to a
      // blank new-chat state rather than leaving a transcript on screen
      // for a session that no longer exists.
      if (id === sessionId) {
        setSessionId(null);
        setMessages([]);
      }
    } catch (err) {
      setError(err.message);
    }
  }

  async function handleSubmit(e) {
    e.preventDefault();
    const text = input.trim();
    if (!text || sending) return;

    const wasNewSession = sessionId === null;

    setError(null);
    setInput("");
    // Optimistic: show the user's message immediately, don't wait on the
    // round trip — the assistant's reply gets appended once it arrives.
    setMessages((prev) => [...prev, { role: "user", content: text }]);
    setSending(true);

    try {
      const res = await api.sendMessage(token, collectionId, text, sessionId);
      setSessionId(res.session_id);
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: res.message,
          reasoning: res.reasoning,
          citations: res.citations,
          confidence: res.confidence,
        },
      ]);

      // First message of a brand-new session — add it to the sidebar
      // immediately instead of waiting on a refetch.
      if (wasNewSession) {
        setSessions((prev) => [
          { id: res.session_id, collection_id: collectionId, title: text.slice(0, 60), created_at: new Date().toISOString() },
          ...prev,
        ]);
      }
    } catch (err) {
      setError(err.message);
      // Roll back the optimistic user message — it was never actually
      // answered, so leaving it in place would misrepresent the history.
      setMessages((prev) => prev.slice(0, -1));
    } finally {
      setSending(false);
    }
  }

  return (
    <Layout>
      <div className="page-header">
        <h2>{collection?.name ?? "Loading..."}</h2>
      </div>

      <div className="chat-layout">
        <aside className="chat-sidebar">
          <button
            className="btn-secondary btn-compact chat-new-btn"
            onClick={startNewChat}
            disabled={sending}
          >
            + New chat
          </button>
          {sessions.length === 0 && <p className="muted-small">No previous chats yet.</p>}
          <div className="session-list">
            {sessions.map((s) => (
              <div
                key={s.id}
                className={`session-item ${s.id === sessionId ? "session-item-active" : ""}`}
                onClick={() => loadSession(s)}
                onKeyDown={(e) => e.key === "Enter" && loadSession(s)}
                role="button"
                tabIndex={0}
                aria-disabled={sending}
              >
                <span className="session-item-title">{s.title || "Untitled chat"}</span>
                <button
                  className="btn-icon-delete btn-icon-delete-sm"
                  onClick={(e) => handleDeleteSession(e, s.id)}
                  title="Delete chat"
                  aria-label={`Delete chat: ${s.title || "Untitled chat"}`}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        </aside>

        <div className="chat-window">
          <div className="chat-messages">
            {messages.length === 0 && (
              <p className="muted">
                Ask a question about the documents in this collection to get started.
              </p>
            )}

            {messages.map((m, i) => (
              <div key={i} className={`chat-message chat-message-${m.role}`}>
                {m.confidence && m.confidence !== "high" && (
                  <div className={`confidence-badge confidence-${m.confidence}`}>
                    {m.confidence === "none"
                      ? "⚠ Not found in your documents"
                      : "⚠ Low confidence — sources are only loosely related"}
                  </div>
                )}

                {m.role === "assistant" ? (
                  <div className="chat-message-content markdown-body">
                    <ReactMarkdown>{m.content}</ReactMarkdown>
                  </div>
                ) : (
                  <p className="chat-message-content">{m.content}</p>
                )}

                {m.reasoning && <p className="chat-reasoning">{m.reasoning}</p>}

                {m.citations && m.citations.length > 0 && (
                  <div className="citation-list">
                    {m.citations.map((c, ci) => (
                      <div key={ci} className="citation-chip">
                        <div className="citation-chip-header">
                          <span className="citation-doc">{c.document_name}</span>
                          <span className="mono muted-small">p.{c.page_number}</span>
                          <span className="mono muted-small">{c.score.toFixed(2)}</span>
                        </div>
                        <p className="citation-excerpt">{c.excerpt}</p>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ))}

            {sending && <p className="chat-thinking">Thinking...</p>}
            <div ref={bottomRef} />
          </div>

          {error && <div className="error-banner">{error}</div>}

          <form className="chat-input-row" onSubmit={handleSubmit}>
            <input
              type="text"
              placeholder="Ask a question about this collection..."
              value={input}
              onChange={(e) => setInput(e.target.value)}
              disabled={sending}
            />
            <button className="btn-primary btn-compact" type="submit" disabled={sending || !input.trim()}>
              Send
            </button>
          </form>
        </div>
      </div>
    </Layout>
  );
}
