import { useState, useEffect, useRef, useCallback } from "react";
import type { SearchHit } from "../../api/types";
import { api } from "../../api/client";
import { getCached, fetchSession } from "../../api/sessionCache";
import { useAppStore } from "../../store";
import { navigateTo } from "../../router";
import { formatRelativeDate } from "./SessionItem";
import { useDebounce } from "../../utils/useDebounce";
import { useArchivedSet } from "../../utils/sessionMeta";
import { renderSnippet } from "../../utils/renderSnippet";
import { ScrollContainer } from "../ScrollContainer";

const SEARCH_PAGE_SIZE = 50;

export default function SearchOverlay() {
  const closeSearch = useAppStore((s) => s.closeSearch);
  const setPendingScrollMessageId = useAppStore((s) => s.setPendingScrollMessageId);

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const archived = useArchivedSet();

  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const debouncedQuery = useDebounce(query, 300);

  // Scroll active result into view when navigating with keyboard
  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const active = list.children[activeIndex] as HTMLElement | undefined;
    active?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);

  // Auto-focus on mount
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Close on Escape, trap Tab within dialog
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { closeSearch(); return; }
      if (e.key !== "Tab" || !dialogRef.current) return;
      const focusable = dialogRef.current.querySelectorAll<HTMLElement>(
        'input, button, [tabindex]:not([tabindex="-1"])'
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [closeSearch]);

  // Fetch results when debounced query changes
  useEffect(() => {
    if (debouncedQuery.length < 2) {
      setResults([]);
      setHasMore(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    api
      .search(debouncedQuery, undefined, SEARCH_PAGE_SIZE)
      .then((data) => {
        if (!cancelled) {
          setResults(data);
          setHasMore(data.length === SEARCH_PAGE_SIZE);
          setActiveIndex(0);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setResults([]);
          setHasMore(false);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [debouncedQuery]);

  const handleLoadMore = useCallback(() => {
    if (loadingMore) return;
    setLoadingMore(true);
    api
      .search(debouncedQuery, undefined, SEARCH_PAGE_SIZE, results.length)
      .then((data) => {
        setResults((prev) => [...prev, ...data]);
        setHasMore(data.length === SEARCH_PAGE_SIZE);
      })
      .catch(() => setHasMore(false))
      .finally(() => setLoadingMore(false));
  }, [debouncedQuery, results.length, loadingMore]);

  const handleSelect = useCallback(
    (hit: SearchHit) => {
      // If the session is archived, unarchive it so it surfaces in the sidebar
      if (archived.has(hit.session_id)) {
        archived.remove(hit.session_id);
      }
      setPendingScrollMessageId(hit.message_id);

      const navigate = () => { navigateTo(`/chat/${hit.session_id}`); closeSearch(); };

      if (getCached(hit.session_id)) {
        // Already in cache — navigate immediately, no loading flash.
        navigate();
      } else {
        // Pre-fetch so MessageList renders from cache and skips the loading spinner.
        fetchSession(hit.session_id, api.getSession)
          .then(navigate)
          .catch(navigate); // on error, navigate anyway and let MessageList retry
      }
    },
    [closeSearch, setPendingScrollMessageId, archived]
  );

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((i) => Math.min(i + 1, results.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter" && results[activeIndex]) {
      handleSelect(results[activeIndex]);
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Search conversations"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 1000,
        background: "var(--bg-modal-overlay)",
        display: "flex",
        alignItems: "flex-start",
        justifyContent: "center",
        paddingTop: "180px",
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) closeSearch();
      }}
    >
      <div
        ref={dialogRef}
        style={{
          width: "100%",
          maxWidth: "600px",
          background: "var(--bg-modal)",
          borderRadius: "var(--radius-lg)",
          boxShadow: "var(--shadow-lg)",
          overflow: "hidden",
          margin: "0 16px",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Input */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            padding: "16px",
            borderBottom:
              results.length > 0 || loading
                ? "1px solid var(--border-subtle)"
                : "none",
            gap: "12px",
          }}
        >
          <span style={{ fontSize: "16px", color: "var(--text-tertiary)" }}>⌕</span>
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={'Search conversations…  "phrase"  -exclude  role:user'}
            style={{
              flex: 1,
              border: "none",
              outline: "none",
              background: "transparent",
              fontSize: "15px",
              color: "var(--text-primary)",
              fontFamily: "var(--font-ui)",
            }}
          />
          {loading && (
            <span style={{ fontSize: "11px", color: "var(--text-tertiary)" }}>
              Searching…
            </span>
          )}
          <kbd
            onClick={closeSearch}
            style={{
              fontSize: "11px",
              color: "var(--text-tertiary)",
              background: "var(--bg-tool-block)",
              border: "1px solid var(--border-default)",
              borderRadius: "4px",
              padding: "2px 6px",
              cursor: "pointer",
              fontFamily: "var(--font-ui)",
            }}
          >
            Esc
          </kbd>
        </div>

        {/* Results */}
        {results.length > 0 && (
          <ScrollContainer
            style={{
              maxHeight: "400px",
            }}
          >
          <ul
            ref={listRef}
            role="listbox"
            style={{
              listStyle: "none",
              margin: 0,
              padding: "8px 0",
            }}
          >
            {results.map((hit, i) => (
              <li
                key={hit.message_id}
                role="option"
                aria-selected={i === activeIndex}
                onClick={() => handleSelect(hit)}
                onMouseEnter={() => setActiveIndex(i)}
                style={{
                  padding: "10px 16px",
                  cursor: "pointer",
                  background: i === activeIndex ? "var(--bg-sidebar-hover)" : "transparent",
                  transition: "background var(--transition-fast)",
                }}
              >
                {/* Title row */}
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "6px",
                    marginBottom: "3px",
                    minWidth: 0,
                  }}
                >
                  <span
                    style={{
                      fontSize: "13px",
                      fontWeight: 500,
                      color: "var(--text-primary)",
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      flex: 1,
                    }}
                  >
                    {hit.session_title ?? "Untitled"}
                  </span>
                  {archived.has(hit.session_id) && (
                    <span
                      style={{
                        fontSize: "10px",
                        color: "var(--text-tertiary)",
                        background: "var(--bg-tool-block)",
                        border: "1px solid var(--border-subtle)",
                        borderRadius: "var(--radius-pill)",
                        padding: "1px 6px",
                        flexShrink: 0,
                        fontFamily: "var(--font-ui)",
                      }}
                    >
                      archived
                    </span>
                  )}
                  {hit.source === "ephemeral" && (
                    <span
                      aria-label={`Ephemeral ${hit.kind ?? "message"}`}
                      style={{
                        fontSize: "10px",
                        color: "var(--accent-orange)",
                        background: "var(--accent-orange-subtle)",
                        border: "1px solid var(--border-accent)",
                        borderRadius: "var(--radius-pill)",
                        padding: "1px 6px",
                        flexShrink: 0,
                        fontFamily: "var(--font-ui)",
                        fontWeight: 600,
                        letterSpacing: 0,
                      }}
                    >
                      {hit.kind?.toUpperCase() ?? "EPHEMERAL"}
                    </span>
                  )}
                </div>
                {/* Snippet */}
                <div
                  style={{
                    fontSize: "12px",
                    color: "var(--text-secondary)",
                    display: "-webkit-box",
                    WebkitLineClamp: 2,
                    WebkitBoxOrient: "vertical",
                    overflow: "hidden",
                    lineHeight: 1.4,
                    marginBottom: "4px",
                  }}
                >
                  {renderSnippet(hit.snippet)}
                </div>
                {/* Date */}
                {hit.timestamp && (
                  <div style={{ fontSize: "11px", color: "var(--text-tertiary)" }}>
                    {formatRelativeDate(hit.timestamp)}
                  </div>
                )}
              </li>
            ))}
          </ul>
          {hasMore && (
            <button
              onClick={handleLoadMore}
              disabled={loadingMore}
              style={{
                display: "block",
                width: "100%",
                padding: "10px 16px",
                border: "none",
                background: "transparent",
                color: "var(--text-tertiary)",
                fontSize: "12px",
                fontFamily: "var(--font-ui)",
                cursor: loadingMore ? "default" : "pointer",
              }}
            >
              {loadingMore ? "Loading…" : "Load more"}
            </button>
          )}
          </ScrollContainer>
        )}

        {/* Empty state when query typed but no results */}
        {!loading && query.length >= 2 && results.length === 0 && (
          <div
            style={{
              padding: "24px 16px",
              textAlign: "center",
              fontSize: "13px",
              color: "var(--text-tertiary)",
            }}
          >
            No results for "{query}"
          </div>
        )}

        {/* Hint when query is too short */}
        {query.length > 0 && query.length < 2 && (
          <div
            style={{
              padding: "16px",
              textAlign: "center",
              fontSize: "12px",
              color: "var(--text-tertiary)",
            }}
          >
            Type at least 2 characters…
          </div>
        )}
      </div>
    </div>
  );
}
