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
      setMessages(
        msgs.map((m) => ({
          role: m.role,
          content: m.content,
          citations: m.citations,
          recommendedDocuments: m.recommended_documents ?? [],
          comparisonSections: m.comparison_sections ?? [],
        }))
      );
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
          followUpQuestions: res.follow_up_questions ?? [],
          recommendedDocuments: res.recommended_documents ?? [],
          comparisonSections: res.comparison_sections ?? [],
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

  // Clicking a suggestion chip behaves exactly like the user typing the
  // question and pressing Send — sets input, then immediately submits.
  async function handleSuggestionClick(question) {
    if (sending) return;
    setInput(question);
    // We need the input value in handleSubmit, but setInput is async.
    // Simulate the submit directly with the question text.
    const text = question.trim();
    if (!text) return;

    const wasNewSession = sessionId === null;
    setError(null);
    setInput("");
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
          followUpQuestions: res.follow_up_questions ?? [],
          recommendedDocuments: res.recommended_documents ?? [],
          comparisonSections: res.comparison_sections ?? [],
        },
      ]);
      if (wasNewSession) {
        setSessions((prev) => [
          { id: res.session_id, collection_id: collectionId, title: text.slice(0, 60), created_at: new Date().toISOString() },
          ...prev,
        ]);
      }
    } catch (err) {
      setError(err.message);
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
                    {m.confidence === "none" && "⚠ Not found in your documents"}
                    {m.confidence === "low" && "⚠ Low confidence — sources are only loosely related"}
                    {m.confidence === "medium" && "ℹ Medium confidence"}
                    {m.confidence === "error" && "⚠ AI service temporarily unavailable"}
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

                {/* Recommendation cards — only for RECOMMEND responses */}
                {m.role === "assistant" &&
                  m.recommendedDocuments &&
                  m.recommendedDocuments.length > 0 && (
                    <div className="recommendations-section">
                      <span className="recommendations-label">Document Recommendations</span>
                      <div className="recommendation-cards">
                        {m.recommendedDocuments.map((doc, di) => (
                          <div key={di} className="recommendation-card">
                            <div className="recommendation-card-header">
                              <span className="recommendation-rank">#{doc.rank}</span>
                              <span className="recommendation-doc-name">{doc.document_name}</span>
                            </div>
                            <div className="recommendation-label">{doc.suitability_label}</div>
                            <p className="recommendation-reason">{doc.reason}</p>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                {/* Comparison sections — only for COMPARE responses */}
                {m.role === "assistant" &&
                  m.comparisonSections &&
                  m.comparisonSections.length > 0 && (
                    <div className="comparison-section">
                      <span className="comparison-label">Comparison</span>
                      {m.comparisonSections.map((section, si) => (
                        <div key={si} className="comparison-block">
                          <div className="comparison-block-title">{section.title}</div>
                          <div className="comparison-block-content markdown-body">
                            <ReactMarkdown>{section.content}</ReactMarkdown>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}

                {/* Follow-up suggestion chips — only for assistant messages,
                    only when not loading, only when the API returned questions */}
                {m.role === "assistant" &&
                  !sending &&
                  m.followUpQuestions &&
                  m.followUpQuestions.length > 0 && (
                    <div className="suggestions-section">
                      <span className="suggestions-label">Suggested questions</span>
                      <div className="suggestions-chips">
                        {m.followUpQuestions.map((q, qi) => (
                          <button
                            key={qi}
                            className="suggestion-chip"
                            onClick={() => handleSuggestionClick(q)}
                            disabled={sending}
                            title={q}
                          >
                            {q}
                          </button>
                        ))}
                      </div>
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
