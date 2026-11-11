import { Link, Outlet, useLocation } from "react-router-dom";
import { Icon } from "../components/Icon";
import { SkipLink } from "./SkipLink";
import { STEPS, STUDENT_PATH } from "./steps";
import { useFocusHeading } from "./useFocusHeading";
import { useModeRoute } from "./useModeRoute";

export function RecruiterLayout() {
  useModeRoute("recruiter");
  useFocusHeading();
  const { pathname } = useLocation();
  const afterReview = pathname.startsWith("/recruiter/assessment");
  const found = STEPS.findIndex((step) => pathname === step.path || pathname.startsWith(`${step.path}/`));
  const current = afterReview ? STEPS.length : found;
  return (
    <>
      <SkipLink />
      <header className="app-bar">
        <div className="app-bar__inner">
          <span className="app-bar__name">SkillVerify</span>
          <nav aria-label="Progress">
            <ol className="steps">
              {STEPS.map((step, index) => {
                const state = index === current ? "current" : index < current ? "done" : "todo";
                return (
                  <li
                    key={step.label}
                    className={`steps__item steps__item--${state} t-body`}
                    aria-current={state === "current" ? "step" : undefined}
                  >
                    <span className="steps__circle" aria-hidden="true">
                      {state === "done" ? <Icon name="check" size={14} /> : index + 1}
                    </span>
                    <span className="steps__label">{step.label}</span>
                    {state === "done" ? <span className="sr-only"> (completed)</span> : null}
                  </li>
                );
              })}
            </ol>
          </nav>
        </div>
      </header>
      <main id="main" tabIndex={-1} className="recruiter-main">
        <Outlet />
      </main>
      <footer className="app-footer">
        <Link className="app-footer__link t-caption" to={STUDENT_PATH}>Preview as student (prototype control)</Link>
      </footer>
    </>
  );
}
