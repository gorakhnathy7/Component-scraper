"""Claude calls: build the cached system prompt, generate the summary, answer chat turns."""

import re

import anthropic

from .prompts import PAGE_LABELS, SUMMARY_REQUEST, SYSTEM_RULES

MODEL = "claude-sonnet-5-5"
MAX_HISTORY = 20  # recent messages kept, in addition to the opening summary turn

_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY from the environment
    return _client


SUGGESTIONS_RE = re.compile(r"<suggestions>(.*?)</suggestions>", re.DOTALL)


class LLMError(Exception):
    pass


def build_system(site: dict) -> list[dict]:
    status_lines, page_blocks = [], []
    for key, label in PAGE_LABELS.items():
        page = site.get(key)
        if page:
            status_lines.append(f"- {label}: {page['url']}")
            page_blocks.append(f'<page name="{label}" url="{page["url"]}">\n{page["markdown"]}\n</page>')
        else:
            status_lines.append(f"- {label}: NOT FOUND (this page could not be located on the site)")

    text = SYSTEM_RULES.format(page_status="Pages:\n" + "\n".join(status_lines)) + "\n\n" + "\n\n".join(page_blocks)
    # The system prompt is identical on every turn for a site, so it's cached:
    # follow-up questions read the website content from cache instead of re-paying for it.
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _split_suggestions(text: str) -> tuple[str, list[str]]:
    match = SUGGESTIONS_RE.search(text)
    if not match:
        return text.strip(), []
    suggestions = [line.strip(" -•\t") for line in match.group(1).strip().splitlines() if line.strip(" -•\t")]
    return SUGGESTIONS_RE.sub("", text).strip(), suggestions[:3]


def _trim(history: list[dict]) -> list[dict]:
    """Keep the opening summary turn (later questions refer back to it) plus the most recent messages."""
    if len(history) <= MAX_HISTORY + 2:
        return history
    recent = history[-MAX_HISTORY:]
    if recent[0]["role"] == "assistant":  # messages must start with a user turn after the summary pair
        recent = recent[1:]
    return history[:2] + recent


async def complete(system, messages: list[dict], effort: str = "low", max_tokens: int = 16000, label: str = "claude") -> str:
    """One Claude call (streamed, so long code generations don't hit HTTP timeouts). Returns the text."""
    try:
        async with get_client().beta.messages.stream(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            output_config={"effort": effort},
            # If a safety classifier declines, retry server-side on Anthropic's recommended fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            response = await stream.get_final_message()
    except anthropic.AuthenticationError:
        raise LLMError("Invalid Anthropic API key. Check ANTHROPIC_API_KEY in .env.")
    except anthropic.RateLimitError:
        raise LLMError("Rate limited by the Claude API. Wait a moment and retry.")
    except anthropic.APIStatusError as e:
        raise LLMError(f"Claude API error ({e.status_code}): {e.message}")
    except anthropic.APIConnectionError:
        raise LLMError("Couldn't reach the Claude API. Check your internet connection.")

    if response.stop_reason == "refusal":
        raise LLMError("Claude declined this request.")
    if response.stop_reason == "max_tokens":
        raise LLMError("Claude's response was cut off (too long). Try again or simplify the request.")

    u = response.usage
    print(f"[{label}] in={u.input_tokens} cache_write={u.cache_creation_input_tokens} "
          f"cache_read={u.cache_read_input_tokens} out={u.output_tokens}")
    return "".join(b.text for b in response.content if b.type == "text")


async def _ask(site: dict, messages: list[dict]) -> tuple[str, list[str]]:
    # Q&A over provided text: low effort is fast and sufficient.
    text = await complete(build_system(site), messages, effort="low")
    return _split_suggestions(text)


async def summarize(site: dict) -> tuple[str, list[str]]:
    return await _ask(site, [{"role": "user", "content": SUMMARY_REQUEST}])


async def chat(site: dict, history: list[dict], question: str) -> tuple[str, list[str]]:
    messages = _trim(history) + [{"role": "user", "content": question}]
    return await _ask(site, messages)
