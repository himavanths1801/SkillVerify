import { useCallback, useEffect, useState } from "react";
import { NETWORK_ERROR, getQuestions, retryQuestions, startQuestions } from "./client";
import { POLL_MS } from "./useAnalysis";
import type { ApiError, QuestionsStatus } from "./types";

export type UseQuestions = {
  /** The latest status. `null` until the first answer. */
  status: QuestionsStatus | null;
  /** The server does not know this analysis (404). */
  notFound: boolean;
  /** Retry a failed run (starts it again on the server unless the server was unreachable), then poll again. */
  retry: () => void;
};

function failed(id: string, error: ApiError): QuestionsStatus {
  return { id, state: "failed", stage: null, result: null, error };
}

/**
 * Loads the question run of one analysis. An idle run is started (a 409 "already started", for example from React
 * StrictMode's double effect, just means: ask again). It polls every POLL_MS while the run is idle, queued or running
 * and stops when it is done or failed, and on unmount. One failed poll is tolerated; the second in a row is `network`.
 * `id` null means "not yet": nothing is requested.
 */
export function useQuestions(id: string | null): UseQuestions {
  const [status, setStatus] = useState<QuestionsStatus | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [round, setRound] = useState(0);

  useEffect(() => {
    if (id === null) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let misses = 0;
    let started = false;
    const later = (step: () => void) => {
      timer = setTimeout(step, POLL_MS);
    };

    const step = async () => {
      const answer = await getQuestions(id);
      if (cancelled) return;
      if (!answer.ok) {
        if (answer.error.code === "network" && misses < 1) {
          misses += 1;
          later(() => void step());
          return;
        }
        if (answer.error.code === "unknown") setNotFound(true);
        else setStatus(failed(id, answer.error.code === "network" ? NETWORK_ERROR : answer.error));
        return;
      }
      misses = 0;
      let current = answer.value;
      if (current.state === "idle" && !started) {
        started = true;
        const begun = await startQuestions(id);
        if (cancelled) return;
        if (begun.ok) {
          current = begun.value;
        } else if (begun.error.code === "questions_already_started") {
          void step(); // someone else started it: read it again
          return;
        } else {
          setStatus(failed(id, begun.error.code === "network" ? NETWORK_ERROR : begun.error));
          return;
        }
      }
      setStatus(current);
      if (current.state === "idle" || current.state === "queued" || current.state === "running") later(() => void step());
    };

    void step();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [id, round]);

  const retry = useCallback(() => {
    const error = status?.error;
    void (async () => {
      if (id !== null && error && error.code !== "network") await retryQuestions(id);
      setStatus(null);
      setNotFound(false);
      setRound((n) => n + 1);
    })();
  }, [id, status]);

  return { status, notFound, retry };
}
