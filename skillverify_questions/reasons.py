"""The fixed "why this question" sentences. Written by code from templates, never by Gemini."""
from skillverify_questions import config

MUST_HAVE, NICE_TO_HAVE = "must_have", "nice_to_have"  # the tier values of score_candidate's skills[]

# Used when the file is NOT rank 1 for the skill (a reserve pick, or a second-best file): no claim of being top.
REASON_MUST_CLAIMED_OTHER = "Asked because the CV claims {skill} and this file shows it in the candidate's repos."
REASON_NICE_TO_HAVE_OTHER = ("Asked because the job lists {skill} as a nice-to-have and this file shows it in the "
                             "candidate's repos.")
REASON_MUST_NOT_CLAIMED_OTHER = ("Asked because the job requires {skill} and this file shows it, although the CV "
                                 "does not list it.")


def reason_for(skill: str, tier: str, claimed: bool, top_ranked: bool = True) -> str:
    if tier == NICE_TO_HAVE:
        template = config.REASON_NICE_TO_HAVE if top_ranked else REASON_NICE_TO_HAVE_OTHER
    elif claimed:
        template = config.REASON_MUST_CLAIMED if top_ranked else REASON_MUST_CLAIMED_OTHER
    else:
        template = config.REASON_MUST_NOT_CLAIMED if top_ranked else REASON_MUST_NOT_CLAIMED_OTHER
    return template.format(skill=skill)
