import { useEffect, useRef } from "react";
import { useLocation } from "react-router-dom";

/** After a route change (not on first load) moves focus to the page's <h1>, so keyboard and screen reader users start at the top. */
export function useFocusHeading() {
  const { pathname } = useLocation();
  const previous = useRef(pathname);
  useEffect(() => {
    if (previous.current === pathname) return;
    previous.current = pathname;
    document.querySelector<HTMLElement>("main h1")?.focus();
  }, [pathname]);
}
