import json
import re
from typing import Optional, Tuple, Dict, Any


def parse_llm_response(raw: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Future-use parser:
    Splits assistant-visible text from an optional WM_UPDATE block.

    Safe even if your current app does not yet use a real LLM.
    """
    pattern = r"<WM_UPDATE>\s*(.*?)\s*</WM_UPDATE>"
    match = re.search(pattern, raw, re.DOTALL)

    if not match:
        return raw.strip(), None

    reply_text = raw[:match.start()].strip()

    try:
        wm_update = json.loads(match.group(1))
    except json.JSONDecodeError:
        wm_update = None

    return reply_text, wm_update