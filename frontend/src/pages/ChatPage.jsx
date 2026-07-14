import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../context/AuthContext";
import Layout from "../components/Layout";

export default function ChatPage() {
  const { collectionId } = useParams();
  const { token } = useAuth();

  const [collection, setCollection] = useState(null);
  const [messages, setMessages] = useState([]); // [{role, content, citations?, reasoning?}]
  const [sessionId, setSessionId] = useState(null);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState(null);

  const bottomRef = useRef(null);

  useEffect(() => {
    api.getCollection(token, collectionId).then(setCollection).catch(() => {});
  }, [token, collectionId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function handleSubmit(e) {
    e.preventDefault();
    const text = input.trim();
    if (!text || sending) return;

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
        { role: "assistant", content: res.message, reasoning: res.reasoning, citations: res.citations },
      ]);
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

      <div className="chat-window">
        <div className="chat-messages">
          {messages.length === 0 && (
            <p className="muted">
              Ask a question about the documents in this collection to get started.
            </p>
          )}

          {messages.map((m, i) => (
            <div key={i} className={`chat-message chat-message-${m.role}`}>
              <p className="chat-message-content">{m.content}</p>

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
    </Layout>
  );
}
