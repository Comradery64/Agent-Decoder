"""Search query language → FTS5 MATCH expression + SQL filters.

Supported syntax:
    "exact phrase"        phrase match
    -word / -"phrase"     exclude
    a OR b                alternation (AND is implicit)
    word*                 prefix match
    role:user             filter by role        (-role:assistant to exclude)
    project:foo           project id contains   (-project:foo to exclude)
    session:<id>          one session
    before:YYYY-MM-DD     timestamp < value
    after:YYYY-MM-DD      timestamp >= value

Every term is emitted as a quoted FTS5 string, so punctuation inside a word
(``eye-candy``, ``a.b``, ``http://x``) stays literal and user input can never
produce an FTS5 syntax error. Unknown ``key:`` prefixes are treated as text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(r'(-)?(?:(\w+):)?("[^"]*"|\S+)')
_FILTER_KEYS = {"role", "project", "session", "before", "after"}


@dataclass
class ParsedQuery:
    match: str = ""  # FTS5 MATCH expression; empty → nothing searchable
    roles: list[str] = field(default_factory=list)
    not_roles: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    not_projects: list[str] = field(default_factory=list)
    session: str | None = None
    before: str | None = None
    after: str | None = None


def _fts_term(raw: str, *, quoted: bool) -> str | None:
    text = raw.replace('"', " ").strip() if not quoted else raw[1:-1].strip()
    text = text.replace('"', " ")
    if not re.search(r"\w", text):
        return None
    prefix = not quoted and text.endswith("*")
    text = text.rstrip("*").strip() if prefix else text
    if not re.search(r"\w", text):
        return None
    return f'"{text}"' + ("*" if prefix else "")


def parse_query(query: str) -> ParsedQuery:
    pq = ParsedQuery()
    groups: list[list[str]] = [[]]
    negs: list[str] = []

    for m in _TOKEN_RE.finditer(query):
        neg, key, val = m.group(1), m.group(2), m.group(3)
        key = key.lower() if key else None

        if key in _FILTER_KEYS:
            v = val.strip('"')
            if not v:
                continue
            if key == "role":
                (pq.not_roles if neg else pq.roles).append(v)
            elif key == "project":
                (pq.not_projects if neg else pq.projects).append(v)
            elif key == "session":
                pq.session = v
            elif key == "before":
                pq.before = v
            else:
                pq.after = v
            continue

        # Not a known filter: re-join an unknown "key:" prefix back into text.
        if key:
            val = f"{key}:{val}"
        if not neg and val == "OR":
            if groups[-1]:
                groups.append([])
            continue

        term = _fts_term(val, quoted=val.startswith('"') and val.endswith('"') and len(val) > 1)
        if term is None:
            continue
        (negs if neg else groups[-1]).append(term)

    groups = [g for g in groups if g]
    if not groups:
        return pq  # negation-only / filter-only → no match expression
    pos = " OR ".join("(" + " ".join(g) + ")" for g in groups)
    pq.match = pos + (f" NOT ({' OR '.join(negs)})" if negs else "")
    return pq


def filter_sql(
    pq: ParsedQuery, *, role: str, project: str, session: str, ts: str
) -> tuple[str, list]:
    """Return (``" AND ..."`` clause string, params) for the non-FTS filters."""
    sql: list[str] = []
    params: list = []
    if pq.roles:
        sql.append(f"{role} IN ({','.join('?' * len(pq.roles))})")
        params += pq.roles
    if pq.not_roles:
        sql.append(f"{role} NOT IN ({','.join('?' * len(pq.not_roles))})")
        params += pq.not_roles
    for p in pq.projects:
        sql.append(f"instr({project}, ?) > 0")
        params.append(p)
    for p in pq.not_projects:
        sql.append(f"instr({project}, ?) = 0")
        params.append(p)
    if pq.session:
        sql.append(f"{session} = ?")
        params.append(pq.session)
    if pq.before:
        sql.append(f"{ts} < ?")
        params.append(pq.before)
    if pq.after:
        sql.append(f"{ts} >= ?")
        params.append(pq.after)
    return "".join(f" AND {c}" for c in sql), params
