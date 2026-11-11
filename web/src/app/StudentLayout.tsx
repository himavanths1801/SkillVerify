import { useEffect, useState } from "react";
import { Outlet, useSearchParams } from "react-router-dom";
import { Timer } from "../components/Timer";
import { sessionMock } from "../mocks/session";
import { SkipLink } from "./SkipLink";
import { useFocusHeading } from "./useFocusHeading";
import { useModeRoute } from "./useModeRoute";

/** Dev-only: `?t=290` starts the timer at 290 seconds so the low-time state can be seen. Ignored in production builds. */
function startSeconds(param: string | null): number {
  if (import.meta.env.DEV && param !== null && /^\d+$/.test(param)) return Number(param);
  return sessionMock.secondsLeft;
}

export function StudentLayout() {
  useModeRoute("student");
  useFocusHeading();
  const [params] = useSearchParams();
  const [secondsLeft, setSecondsLeft] = useState(() => startSeconds(params.get("t")));

  useEffect(() => {
    const id = window.setInterval(() => setSecondsLeft((s) => Math.max(0, s - 1)), 1000);
    return () => window.clearInterval(id);
  }, []);

  return (
    <>
      <SkipLink />
      <header className="student-bar">
        <span className="t-caption">Question {sessionMock.questionNumber} of {sessionMock.questionTotal}</span>
        <Timer secondsLeft={secondsLeft} />
      </header>
      <main id="main" tabIndex={-1} className="student-main">
        <Outlet />
      </main>
    </>
  );
}
