import { useCallback, useEffect, useState } from "react";
import { NETWORK_ERROR, getAnalysis } from "./client";
import type { JobStatus } from "./types";

export const POLL_MS = 1500;

/**
 * Polls one analysis every POLL_MS while it is queued or running. Stops on done, failed and on unmount.
 * One failed poll (the server not answering) is tolerated; a second in a row is reported as a `network` failure.
 * `restart()` polls again, for example after a retry.
 */
export function useAnalysis(id: string | null): { status: JobStatus | null; restart: () => void } {
  const [status, setStatus] = useState<JobStatus | null>(null);
  const [round, setRound] = useState(0);

  const restart = useCallback(() => {
    setStatus(null);
    setRound((n) => n + 1);
  }, []);

  useEffect(() => {
    if (id === null) {
      setStatus(null);
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let misses = 0;

    const poll = async () => {
      const answer = await getAnalysis(id);
      if (cancelled) return;
      if (!answer.ok) {
        if (answer.error.code === "network" && misses < 1) {
          misses += 1;
          timer = setTimeout(poll, POLL_MS);
          return;
        }
        setStatus({ id, state: "failed", stage: "", result: null, error: answer.error.code === "network" ? NETWORK_ERROR : answer.error });
        return;
      }
      misses = 0;
      setStatus(answer.value);
      if (answer.value.state === "queued" || answer.value.state === "running") timer = setTimeout(poll, POLL_MS);
    };

    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [id, round]);

  return { status, restart };
}
