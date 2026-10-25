import React, { useState, useEffect, useRef, useCallback } from "react";
import { api } from "../../api/client";
import { getCached, setCached } from "../../api/sessionCache";
import type { SearchHit } from "../../api/types";
import { useAppStore } from "../../store";
import { useDebounce } from "../../utils/useDebounce";
import { useArchivedSet } from "../../utils/sessionMeta";
import { formatRelative } from "../../utils/formatRelative";
import { navigateTo } from "../../router";
import { renderSnippet } from "../../utils/renderSnippet";
import { ScrollContainer } from "../ScrollContainer";

const SEARCH_PAGE_SIZE = 50;

export function DashboardSearch() {
  const setPendingScrollMessageId = useAppStore((s) => s.setPendingScrollMessageId);
  const archived = useArchivedSet();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [open, setOpen] = useState(false);
  const debouncedQuery = useDebounce(query, 300);

  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  useEffect(() => {
    const onMouseDown = (e: MouseEvent) => {
      if (!containerRef.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onMouseDown);
    return () => window.removeEventListener("mousedown", onMouseDown);
  }, []);

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

  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const active = list.children[activeIndex] as HTMLElement | undefined;
    active?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);

  const handleSelect = useCallback(
    (hit: SearchHit) => {
      if (archived.has(hit.session_id)) {
        archived.remove(hit.session_id);
      }
      setPendingScrollMessageId(hit.message_id);

      const navigate = () => navigateTo(`/chat/${hit.session_id}`);
      if (getCached(hit.session_id)) {
        navigate();
      } else {
        api.getSession(hit.session_id)
          .then((d) => { setCached(hit.session_id, d); navigate(); })
          .catch(navigate);
      }
    },
    [setPendingScrollMessageId, archived],
  );

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((i) => Math.min(i + 1, Math.max(results.length - 1, 0)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter" && results[activeIndex]) {
      handleSelect(results[activeIndex]);
    } else if (e.key === "Escape") {
      if (query) {
        setQuery("");
      } else {
        inputRef.current?.blur();
        setOpen(false);
      }
    }
  };

  const showDropdown = open && query.length > 0;

  return (
    <div
      ref={containerRef}
      style={{
        position: "relative",
        width: "100%",
        maxWidth: "520px",
        alignSelf: "center",
      }}
    >
      <div
        onClick={() => inputRef.current?.focus()}
        style={{
          display: "flex",
          alignItems: "center",
          gap: "10px",
          padding: "11px 14px",
          background: "var(--bg-tool-block)",
          border: "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-md)",
          transition: "border-color 0.15s",
          cursor: "text",
        }}
        onMouseEnter={(e) => { e.currentTarget.style.borderColor = "var(--border-default)"; }}
        onMouseLeave={(e) => { e.currentTarget.style.borderColor = "var(--border-subtle)"; }}
      >
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, opacity: 0.6, color: "var(--text-secondary)" }}>
          <circle cx="11" cy="11" r="7" />
          <path d="m20 20-3.5-3.5" />
        </svg>
        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
          onFocus={() => setOpen(true)}
          onKeyDown={handleKeyDown}
          placeholder={'Search sessions, messages, code…  "phrase"  -exclude'}
          style={{
            flex: 1,
            border: "none",
            outline: "none",
            background: "transparent",
            fontSize: "13.5px",
            color: "var(--text-primary)",
            fontFamily: "inherit",
            minWidth: 0,
          }}
        />
        {loading && (
          <span style={{ fontSize: "11px", color: "var(--text-tertiary)" }}>Searching…</span>
        )}
      </div>

      {showDropdown && (
        <div
          style={{
            position: "absolute",
            top: "calc(100% + 6px)",
            left: 0,
            right: 0,
            background: "var(--bg-modal)",
            border: "1px solid var(--border-subtle)",
            borderRadius: "var(--radius-md)",
            boxShadow: "var(--shadow-lg)",
            overflow: "hidden",
            zIndex: 50,
          }}
        >
          {results.length > 0 && (
            <ScrollContainer style={{ maxHeight: "360px" }}>
              <ul
                ref={listRef}
                role="listbox"
                style={{ listStyle: "none", margin: 0, padding: "6px 0" }}
              >
                {results.map((hit, i) => (
                  <li
                    key={hit.message_id}
                    role="option"
                    aria-selected={i === activeIndex}
                    onMouseDown={(e) => { e.preventDefault(); handleSelect(hit); }}
                    onMouseEnter={() => setActiveIndex(i)}
                    style={{
                      padding: "10px 14px",
                      cursor: "pointer",
                      background: i === activeIndex ? "var(--bg-sidebar-hover)" : "transparent",
                      transition: "background var(--transition-fast)",
                    }}
                  >
                    <div style={{
                      fontSize: "13px",
                      fontWeight: 500,
                      color: "var(--text-primary)",
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      marginBottom: "3px",
                    }}>
                      {hit.session_title ?? "Untitled"}
                    </div>
                    <div style={{
                      fontSize: "12px",
                      color: "var(--text-secondary)",
                      display: "-webkit-box",
                      WebkitLineClamp: 2,
                      WebkitBoxOrient: "vertical",
                      overflow: "hidden",
                      lineHeight: 1.4,
                      marginBottom: hit.timestamp ? "4px" : 0,
                    }}>
                      {renderSnippet(hit.snippet)}
                    </div>
                    {hit.timestamp && (
                      <div style={{ fontSize: "11px", color: "var(--text-tertiary)" }}>
                        {formatRelative(hit.timestamp)}
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
                    padding: "10px 14px",
                    border: "none",
                    background: "transparent",
                    color: "var(--text-tertiary)",
                    fontSize: "12px",
                    fontFamily: "inherit",
                    cursor: loadingMore ? "default" : "pointer",
                  }}
                >
                  {loadingMore ? "Loading…" : "Load more"}
                </button>
              )}
            </ScrollContainer>
          )}

          {!loading && query.length >= 2 && results.length === 0 && (
            <div style={{ padding: "20px 14px", textAlign: "center", fontSize: "13px", color: "var(--text-tertiary)" }}>
              No results for "{query}"
            </div>
          )}

          {query.length > 0 && query.length < 2 && (
            <div style={{ padding: "14px", textAlign: "center", fontSize: "12px", color: "var(--text-tertiary)" }}>
              Type at least 2 characters…
            </div>
          )}
        </div>
      )}
    </div>
  );
}
