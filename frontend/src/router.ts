import { useEffect, useState } from "react";

export type AppRoute = "/" | "/runs" | `/runs/${string}` | `/chat/${string}`;

function parseHash(): AppRoute {
  const h = window.location.hash.replace(/^#/, "") || "/";
  if (h === "/runs" || h.startsWith("/runs/")) return h as AppRoute;
  if (h.startsWith("/chat/")) {
    const id = h.slice("/chat/".length);
    if (id) return `/chat/${id}`;
  }
  return "/";
}

export function getChatIdFromRoute(route: AppRoute): string | null {
  return route.startsWith("/chat/") ? route.slice("/chat/".length) : null;
}

export function navigateTo(route: AppRoute): void {
  if (window.location.hash.replace(/^#/, "") === route) return;
  window.location.hash = route;
}

export function useRoute(): AppRoute {
  const [route, setRoute] = useState<AppRoute>(parseHash);

  useEffect(() => {
    const onChange = () => setRoute(parseHash());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  return route;
}
