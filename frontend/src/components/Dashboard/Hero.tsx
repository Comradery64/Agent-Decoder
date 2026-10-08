import { greeting } from "./fmt";
import { DashboardSearch } from "./DashboardSearch";

export function Hero({ totalSessions }: { totalSessions: number }) {
  return (
    // Gap bumped from 18px → 30px so the space between the heading block and
    // the search bar more closely matches the 36px section gap that follows.
    <section style={{ display: "flex", flexDirection: "column", gap: "30px", paddingTop: "8px" }}>
      <div>
        <div style={{ fontSize: "13px", color: "var(--text-tertiary)", marginBottom: "6px", letterSpacing: "0.02em" }}>
          {greeting()} —
        </div>
        <h1 style={{
          fontFamily: "var(--font-content)",
          fontSize: "34px",
          fontWeight: 600,
          margin: 0,
          lineHeight: 1.15,
          color: "var(--text-primary)",
          letterSpacing: "-0.01em",
        }}>
          Agent <span style={{ color: "var(--accent-orange)" }}>Decoder</span>
        </h1>
        <div style={{ fontSize: "14px", color: "var(--text-secondary)", marginTop: "6px" }}>
          {totalSessions > 0
            ? <>Browse, search, and analyze your sessions.</>
            : <>Your local browser for session history.</>}
        </div>
      </div>

      <DashboardSearch />
    </section>
  );
}
