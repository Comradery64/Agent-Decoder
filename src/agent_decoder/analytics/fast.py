"""SQL-backed analytics over the full message corpus.

The Python scanners (``stats.py`` / ``service.py``) answer the dashboard's
all-message questions by loading every row — ``SELECT *`` pulls ~500 MB of
``content_json`` and ~145k rows become pydantic ``Message`` objects (~9 s) —
then scanning in memory. These functions compute the **identical shapes**
from data materialized at ingest:

  - per-message token columns (``input_tokens`` … ``cache_read_tokens``)
  - the ``message_blocks`` table (one row per tool_use / tool_result block,
    with tool name, file path, and result char-count already extracted)

So the dashboard never reads ``content_json`` at query time — it aggregates
small indexed columns. Empirically ~1 s cold vs ~9 s, and the existing
signature cache keeps warm loads at ~3 ms.

Verified byte-for-byte against the scanners in
``tests/analytics/test_fast_parity.py``. Per-session/small-list analytics
keep using the scanners — these are only for the whole-corpus dashboard.
"""

from __future__ import annotations

from typing import Any

from agent_decoder.models import Message, TokenUsage, ToolResultBlock, ToolUseBlock

from .stats import compute_stats
from .tips import (
    _SEVERITY_ORDER,
    _WRITE_TOOLS,
    LowCacheHitRule,
    OversizedToolResultRule,
    RepeatedFileReadRule,
)

# Read + write tool names that drive RepeatedFileReadRule's state machine;
# every other tool_use is a no-op there, so we only fetch these.
_RW_TOOLS = ("Read", *sorted(_WRITE_TOOLS))


async def _rows(conn, sql: str, params: tuple = ()) -> list:
    async with conn.execute(sql, params) as cur:
        return await cur.fetchall()


async def _build_usage(conn) -> None:
    """Materialize a small per-message usage temp table in ONE scan, so the
    four usage aggregates (daily / models / prompt_stats / cache rollup)
    don't each re-scan the 145k-row messages table. The parent role/meta
    flags that ``daily`` and ``prompt_stats`` need are resolved here via a
    single PK self-join instead of one join per aggregate.
    """
    await conn.execute("PRAGMA temp_store=MEMORY")
    await conn.execute("DROP TABLE IF EXISTS temp._usage")
    await conn.execute(
        """
        CREATE TEMP TABLE _usage AS
        SELECT date(m.timestamp) AS day, m.session_id AS session_id, m.role AS role,
               m.is_sidechain AS is_sidechain, m.is_meta AS is_meta,
               CASE WHEN m.usage_json IS NOT NULL THEN 1 ELSE 0 END AS has_usage,
               m.model AS model,
               COALESCE(m.input_tokens,0) AS it, COALESCE(m.output_tokens,0) AS ot,
               COALESCE(m.cache_creation_tokens,0) AS cc,
               COALESCE(m.cache_read_tokens,0) AS cr,
               CASE WHEN p.id IS NOT NULL AND p.role='user' THEN 1 ELSE 0 END
                   AS parent_is_user,
               CASE WHEN p.id IS NOT NULL AND p.is_meta=1 THEN 1 ELSE 0 END
                   AS parent_is_meta
        FROM messages m LEFT JOIN messages p ON m.parent_id = p.id
        """
    )


async def _drop_usage(conn) -> None:
    await conn.execute("DROP TABLE IF EXISTS temp._usage")


async def daily(conn) -> list[dict[str, Any]]:
    """Per-UTC-day token totals + prompt/session counts (from ``_usage``).
    Mirrors ``DailyAggregator``: only non-zero-usage messages contribute;
    ``prompt_count`` counts non-sidechain/non-meta assistant messages whose
    parent is a user message; ``session_count`` is the distinct sessions."""
    sql = """
      SELECT day,
             SUM(it) AS it, SUM(ot) AS ot, SUM(cc) AS cc, SUM(cr) AS cr,
             COUNT(DISTINCT session_id) AS sessions,
             SUM(CASE WHEN role='assistant' AND is_sidechain=0 AND is_meta=0
                       AND parent_is_user=1 THEN 1 ELSE 0 END) AS prompts
      FROM _usage
      WHERE day IS NOT NULL AND (it+ot+cc+cr) > 0
      GROUP BY day ORDER BY day ASC
    """
    out = []
    for r in await _rows(conn, sql):
        it, ot, cc, cr = r["it"], r["ot"], r["cc"], r["cr"]
        out.append(
            {
                "day": r["day"],
                "input_tokens": it,
                "output_tokens": ot,
                "cache_creation_tokens": cc,
                "cache_read_tokens": cr,
                "total": it + ot + cc + cr,
                "prompt_count": r["prompts"],
                "session_count": r["sessions"],
            }
        )
    return out


async def models(conn) -> list[dict[str, Any]]:
    """Token usage per model (from ``_usage``). Mirrors ``ModelUsageScanner``:
    assistant messages with a model and a usage block, sorted by total desc."""
    sql = """
      SELECT model, COUNT(*) AS message_count,
             SUM(it) AS input_tokens, SUM(ot) AS output_tokens,
             SUM(it+ot+cc+cr) AS total_tokens
      FROM _usage
      WHERE role='assistant' AND model IS NOT NULL AND model != '' AND has_usage=1
      GROUP BY model ORDER BY total_tokens DESC, model ASC
    """
    return [
        {
            "model": r["model"],
            "message_count": r["message_count"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "total_tokens": r["total_tokens"],
        }
        for r in await _rows(conn, sql)
    ]


async def prompt_stats(conn) -> dict[str, Any]:
    """Distribution of per-prompt token counts (from ``_usage``). Mirrors
    ``PromptStatsScanner`` + ``PromptIterator``: one prompt per non-sidechain
    assistant message whose parent is a non-meta user message; percentiles
    reuse the canonical ``compute_stats``."""
    sql = """
      SELECT it, ot, (it+ot+cc+cr) AS tot FROM _usage
      WHERE role='assistant' AND is_sidechain=0
            AND parent_is_user=1 AND parent_is_meta=0
    """
    inputs, outputs, totals = [], [], []
    for r in await _rows(conn, sql):
        inputs.append(r["it"])
        outputs.append(r["ot"])
        totals.append(r["tot"])
    return {
        "prompt_count": len(inputs),
        "input_tokens": compute_stats(inputs),
        "output_tokens": compute_stats(outputs),
        "total_tokens": compute_stats(totals),
    }


async def _low_cache_totals(conn) -> tuple[int, int]:
    """(total_input, total_cache_read) over assistant messages with usage
    (from ``_usage``) — the inputs LowCacheHitRule needs."""
    row = (
        await _rows(
            conn,
            "SELECT SUM(it+cr+cc) AS total_input, SUM(cr) AS cache_read "
            "FROM _usage WHERE role='assistant' AND has_usage=1",
        )
    )[0]
    return row["total_input"] or 0, row["cache_read"] or 0


def _fake_msg(blocks: list) -> Message:
    """Minimal Message carrying just content blocks, so the real tip rules
    run unchanged on materialized data (guarantees byte-for-byte parity)."""
    return Message(id="x", session_id="x", role="assistant", content_blocks=blocks)


async def _tips(conn, cache_totals: tuple[int, int]) -> list[dict[str, Any]]:
    """Reproduce ``TipRegistry`` by running the real rule classes on minimal
    messages rebuilt from ``message_blocks`` + the cache rollup, so
    heuristics, copy, and ordering stay identical to the scanners.

    ``cache_totals`` is ``(total_input, total_cache_read)`` from the usage
    pass, reused here so LowCacheHit needs no extra scan.
    """
    placeholders = ",".join("?" * len(_RW_TOOLS))
    rw_rows = await _rows(
        conn,
        f"""SELECT mb.tool_name AS name, mb.file_path AS path
            FROM message_blocks mb JOIN messages m ON mb.message_id = m.id
            WHERE mb.btype='tool_use' AND mb.tool_name IN ({placeholders})
            ORDER BY m.timestamp, m.rowid, mb.block_index""",
        _RW_TOOLS,
    )
    repeated = RepeatedFileReadRule().check(
        [
            _fake_msg(
                [
                    ToolUseBlock(
                        id="t", name=r["name"], input={"file_path": r["path"] or ""}
                    )
                ]
            )
            for r in rw_rows
        ]
    )

    over_rows = await _rows(
        conn,
        """SELECT mb.tool_use_id AS tid, mb.result_chars AS chars
           FROM message_blocks mb JOIN messages m ON mb.message_id = m.id
           WHERE mb.btype='tool_result' AND mb.result_chars >= ?
           ORDER BY m.timestamp, m.rowid, mb.block_index""",
        (OversizedToolResultRule()._threshold,),
    )
    oversized = OversizedToolResultRule().check(
        [
            _fake_msg(
                [ToolResultBlock(tool_use_id=r["tid"] or "", content="x" * r["chars"])]
            )
            for r in over_rows
        ]
    )

    total_input, cache_read = cache_totals
    usage_msg = _fake_msg([])
    usage_msg.usage = TokenUsage(
        input_tokens=total_input - cache_read, cache_read_input_tokens=cache_read
    )
    low_cache = LowCacheHitRule().check([usage_msg])

    tips = sorted(
        repeated + oversized + low_cache,
        key=lambda t: _SEVERITY_ORDER.get(t.severity, 99),
    )
    return [
        {
            "rule_id": t.rule_id,
            "severity": t.severity,
            "title": t.title,
            "detail": t.detail,
            "evidence": t.evidence,
        }
        for t in tips
    ]


async def compute_bundle(conn, files_top_n: int = 20) -> dict[str, Any]:
    """The whole-corpus analytics bundle, entirely from materialized data.

    No ``content_json`` is read: token metrics come from per-message columns,
    tool/file/tip facts from ``message_blocks``. Verified against the Python
    scanners in ``tests/analytics/test_fast_parity.py``.

    Counts match the scanners exactly; the order *among equal counts* uses a
    deterministic name tiebreak rather than the scanners' first-seen order
    (matching it needs a join + per-row composite key that cost ~600ms for a
    purely cosmetic chart ordering).
    """
    # One scan into a per-message temp table feeds all four usage aggregates.
    await _build_usage(conn)
    try:
        daily_r = await daily(conn)
        models_r = await models(conn)
        stats_r = await prompt_stats(conn)
        cache_totals = await _low_cache_totals(conn)
    finally:
        await _drop_usage(conn)

    tool_rows = await _rows(
        conn,
        """SELECT tool_name AS name, COUNT(*) AS n FROM message_blocks
           WHERE btype='tool_use' AND tool_name IS NOT NULL
           GROUP BY tool_name ORDER BY n DESC, tool_name ASC""",
    )
    file_rows = await _rows(
        conn,
        """SELECT file_path AS path, COUNT(*) AS n FROM message_blocks
           WHERE btype='tool_use' AND file_path IS NOT NULL AND file_path != ''
           GROUP BY file_path ORDER BY n DESC, file_path ASC LIMIT ?""",
        (files_top_n,),
    )
    return {
        "daily": daily_r,
        "models": models_r,
        "stats": stats_r,
        "tools": [{"tool": r["name"], "count": r["n"]} for r in tool_rows],
        "files": [{"file": r["path"], "count": r["n"]} for r in file_rows],
        "tips": await _tips(conn, cache_totals),
    }
