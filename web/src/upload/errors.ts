import type { ApiError } from "../api/types";

export type BannerText = { title: string; message: string; retry: boolean };

const TEXTS: Record<string, { title: string; message: string }> = {
  ai_busy: { title: "The AI service is busy right now.", message: "Please try again." },
  ai_rate_limited: { title: "The AI service's request limit was reached.", message: "Wait a little while and try again." },
  ai_all_limited: { title: "All available AI models have reached their limits.", message: "Wait a few minutes and try again." },
  ai_not_configured: { title: "The AI service is not set up on this server.", message: "Check the server configuration." },
  github_user_not_found: { title: "That GitHub username was not found.", message: "Check the spelling and try again." },
  github_unavailable: {
    title: "GitHub could not be reached or its request limit was reached.",
    message: "Wait a few minutes and try again.",
  },
  cv_unreadable: { title: "We couldn't read the CV.", message: "Try a different file." },
  jd_unreadable: { title: "We couldn't read the job description.", message: "Try a different file or paste the text." },
  timeout: { title: "This is taking too long.", message: "Please try again." },
  internal_error: { title: "Something went wrong on the server.", message: "Please try again." },
  analysis_not_ready: { title: "The analysis isn't finished yet.", message: "" },
  network: { title: "Can't reach the SkillVerify server.", message: "Make sure it is running, then try again." },
};

/** Codes whose Retry button is shown, exactly as listed in the design. */
const RETRY = new Set(["ai_busy", "ai_rate_limited", "ai_all_limited", "github_unavailable", "timeout", "internal_error", "network"]);

/** Title, body and whether to offer Retry for an error. `invalid_input` and unknown codes show the API's message. */
export function bannerText(error: ApiError): BannerText {
  if (error.code === "ai_rate_limited") {
    const message = error.fallback_available ? "Retry will use a backup AI model." : "Wait a little while and try again.";
    return { title: TEXTS.ai_rate_limited.title, message, retry: true };
  }
  const known = TEXTS[error.code];
  if (known) return { ...known, retry: RETRY.has(error.code) };
  if (error.code === "invalid_input") return { title: "Please check what you entered.", message: error.message, retry: false };
  return { title: "Something went wrong.", message: error.message, retry: false };
}
