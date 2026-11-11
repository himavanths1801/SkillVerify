import type { ApiError, JobStatus, QuestionsStatus, ReplaceResponse } from "./types";

export const API_URL: string = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export type ApiResult<T> = { ok: true; value: T } | { ok: false; error: ApiError };

export const NETWORK_ERROR: ApiError = {
  code: "network",
  message: "The SkillVerify server could not be reached.",
  retryable: true,
};

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The API sends `{error: {...}}` for validation errors and `{message}` for 404/409. Anything else is `unknown`. */
function toApiError(body: unknown): ApiError {
  if (isObject(body) && isObject(body.error) && typeof body.error.code === "string") {
    const { code, message, retryable, fallback_available, stage } = body.error;
    const error: ApiError = { code, message: typeof message === "string" ? message : "", retryable: retryable === true };
    if (typeof fallback_available === "boolean") error.fallback_available = fallback_available;
    if (typeof stage === "string") error.stage = stage;
    return error;
  }
  const message = isObject(body) && typeof body.message === "string" ? body.message : "The request did not work.";
  return { code: "unknown", message, retryable: false };
}

/** One call. A thrown `fetch` (server not running) becomes the `network` error. Request bodies are never logged. */
async function call<T>(path: string, init?: RequestInit): Promise<ApiResult<T>> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, init);
  } catch {
    return { ok: false, error: NETWORK_ERROR };
  }
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // not JSON: handled below
  }
  if (response.ok && isObject(body)) return { ok: true, value: body as T };
  return { ok: false, error: toApiError(body) };
}

export function createAnalysis(form: FormData): Promise<ApiResult<{ id: string }>> {
  return call("/api/analyses", { method: "POST", body: form });
}

export function getAnalysis(id: string): Promise<ApiResult<JobStatus>> {
  return call(`/api/analyses/${encodeURIComponent(id)}`);
}

export function retryAnalysis(id: string): Promise<ApiResult<{ id: string }>> {
  return call(`/api/analyses/${encodeURIComponent(id)}/retry`, { method: "POST" });
}

const questionsPath = (id: string, tail = "") => `/api/analyses/${encodeURIComponent(id)}/questions${tail}`;

export function getQuestions(id: string): Promise<ApiResult<QuestionsStatus>> {
  return call(questionsPath(id));
}

export function startQuestions(id: string): Promise<ApiResult<QuestionsStatus>> {
  return call(questionsPath(id), { method: "POST" });
}

export function retryQuestions(id: string): Promise<ApiResult<QuestionsStatus>> {
  return call(questionsPath(id, "/retry"), { method: "POST" });
}

/** One new question. `afterId` is context only (the dropped question's id); without it the question is simply added. */
export function replaceQuestion(id: string, afterId?: string): Promise<ApiResult<ReplaceResponse>> {
  const init: RequestInit =
    afterId === undefined
      ? { method: "POST" }
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ after: afterId }) };
  return call(questionsPath(id, "/replace"), init);
}
