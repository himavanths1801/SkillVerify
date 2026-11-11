import { Icon } from "./Icon";

/** At or below this many seconds the timer shows its low-time state. The one place this number lives. */
export const LOW_TIME_SECONDS = 300;

/** 2530 -> "42:10", 299 -> "4:59". Minutes are not padded; seconds always are. */
export function formatTime(totalSeconds: number): string {
  const safe = Math.max(0, Math.floor(totalSeconds));
  const minutes = Math.floor(safe / 60);
  const seconds = safe % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

type TimerProps = { secondsLeft: number };

/**
 * Student timer. Neutral: clock icon + time on a quiet-fill pill. Low (<= LOW_TIME_SECONDS): err-text colour,
 * a small err-accent dot with a slow pulse, and the word "left", so it never relies on colour alone.
 * The pulse period is 1.8 s (never faster than once per second) and stops under prefers-reduced-motion.
 */
export function Timer({ secondsLeft }: TimerProps) {
  const low = secondsLeft <= LOW_TIME_SECONDS;
  const time = formatTime(secondsLeft);
  return (
    <span className={`sv-timer t-timer${low ? " sv-timer--low" : ""}`} role="timer" aria-label="Time remaining">
      {low ? (
        <span className="sv-timer__dot" data-testid="timer-dot" aria-hidden="true" />
      ) : (
        <Icon name="clock" size={16} />
      )}
      <span className="sv-timer__time">{low ? `${time} left` : time}</span>
    </span>
  );
}
