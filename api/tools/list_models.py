"""List the Gemini models your key can use for generateContent, one name per line. Run it yourself:

    python -m api.tools.list_models

The key comes from the environment or the .env file (same loader as the provider). The key and the raw answer are
never printed. Use the names to fill GEMINI_MODEL_CHAIN (see docs/API.md).
"""
import os
import sys
from typing import Callable, Optional

import requests
from dotenv import load_dotenv

URL = "https://generativelanguage.googleapis.com/v1beta/models"
TIMEOUT_SECONDS = 30
MAX_PAGES = 20


def main(out: Callable[[str], None] = print) -> int:
    load_dotenv()  # real environment variables win over the .env file
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        out("GEMINI_API_KEY is not set. Put it in your .env file or set it as an environment variable.")
        return 2
    names: list[str] = []
    token: Optional[str] = None
    for _ in range(MAX_PAGES):
        params = {"pageSize": 100, **({"pageToken": token} if token else {})}
        try:
            response = requests.get(URL, params=params, headers={"x-goog-api-key": key}, timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:  # only the type: the text could contain the URL
            out(f"Could not reach Gemini ({type(exc).__name__}). Check your internet connection and try again.")
            return 1
        if response.status_code != 200:
            out(f"Gemini answered HTTP {response.status_code}. Check the key and try again.")
            return 1
        try:
            data = response.json()
            for model in data.get("models", []):
                if "generateContent" in model.get("supportedGenerationMethods", []):
                    names.append(str(model["name"]).removeprefix("models/"))
            token = data.get("nextPageToken")
        except (ValueError, AttributeError, KeyError, TypeError):
            out("Gemini sent an answer this tool could not read.")
            return 1
        if not token:
            break
    for name in names:
        out(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
