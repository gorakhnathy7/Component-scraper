"""Component Lab: find the component in the user's screenshot, rebuild it with Claude, check the rebuild
against the original, and apply edit requests."""

import base64
import json
import re

from . import browser, llm
from .prompts import BUILD_PROMPT, COMPARE_PROMPT, COMPONENT_SYSTEM, EDIT_PROMPT, FIX_PROMPT, LOCATE_PROMPT

GOOD_ENOUGH = 8          # similarity score (1-10) at which we stop fixing
MAX_EXTRACT_CHARS = 90_000
BUILD_EFFORT = "medium"  # code generation: medium balances quality against the wait in a live demo
BUILD_MAX_TOKENS = 32000


def _img(jpeg: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(jpeg).decode()}}


def data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.standard_b64encode(jpeg).decode()


def _json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise llm.LLMError("Claude's reply wasn't in the expected format. Try again.")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        raise llm.LLMError("Claude's reply wasn't valid JSON. Try again.")


def _tag(text: str, name: str) -> str | None:
    start, end = text.find(f"<{name}>"), text.rfind(f"</{name}>")
    if start == -1 or end == -1 or end < start:
        return None
    return text[start + len(name) + 2:end].strip()


def _clean_params(raw) -> list[dict]:
    out = []
    for p in raw if isinstance(raw, list) else []:
        if not isinstance(p, dict) or not str(p.get("name", "")).startswith("--"):
            continue
        kind = p.get("type")
        if kind == "range":
            try:
                p["min"], p["max"] = float(p["min"]), float(p["max"])
                p["step"] = float(p.get("step") or (p["max"] - p["min"]) / 100)
                p["default"] = float(p["default"])
            except (KeyError, TypeError, ValueError):
                continue
            p["unit"] = p.get("unit") or ""
        elif kind == "color":
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(p.get("default", ""))):
                p["default"] = "#888888"
        elif kind == "select":
            if not isinstance(p.get("options"), list) or not p["options"]:
                continue
            p["default"] = p.get("default") if p.get("default") in p["options"] else p["options"][0]
        else:
            continue
        p["label"] = p.get("label") or p["name"].lstrip("-")
        out.append(p)
    return out[:10]


def parse_component(text: str) -> dict:
    code = _tag(text, "component")
    if not code:
        raise llm.LLMError("Claude didn't return a component. Try again.")
    code = re.sub(r"^```\w*\n|\n```$", "", code.strip())
    try:
        params = _clean_params(json.loads(_tag(text, "params") or "[]"))
    except json.JSONDecodeError:
        params = []
    return {"code": code, "params": params,
            "notes": _tag(text, "notes") or "", "reply": _tag(text, "reply") or ""}


# ---------- locate ----------

async def locate(job: dict, user_jpeg: bytes) -> dict:
    content = [{"type": "text", "text": "User's screenshot:"}, _img(user_jpeg)]
    for i, s in enumerate(job["slices"], 1):
        content += [{"type": "text", "text": f"Homepage slice {i} of {len(job['slices'])} ({s['w']}x{s['h']} px):"},
                    _img(s["jpeg"])]
    content.append({"type": "text", "text": LOCATE_PROMPT})
    text = await llm.complete("You locate UI components in website screenshots.",
                              [{"role": "user", "content": content}], effort="medium", max_tokens=4000,
                              label="locate")
    answer = _json(text)
    if not answer.get("found"):
        return {"found": False, "reason": answer.get("reason", "")}

    try:
        idx = int(answer["slice"]) - 1
        s = job["slices"][max(0, min(idx, len(job["slices"]) - 1))]
        x1, y1, x2, y2 = (float(v) / browser.SLICE_SCALE for v in answer["box"])
    except (KeyError, TypeError, ValueError):
        return {"found": False, "reason": "Claude's answer didn't include a usable position."}
    box = {"x": min(x1, x2), "y": s["top"] + min(y1, y2), "w": abs(x2 - x1), "h": abs(y2 - y1)}
    picked = await browser.pick(job, box)
    if not picked:
        return {"found": False, "reason": "No element on the page lines up with that area."}
    return {"found": True, "confidence": answer.get("confidence", "medium"),
            "description": answer.get("description", picked["desc"]), "reason": answer.get("reason", ""),
            **_selection_view(picked)}


def _selection_view(picked: dict) -> dict:
    return {"element": picked["desc"], "text": picked["text"], "rect": picked["rect"],
            "crop": data_url(picked["crop"])}


async def select_box(job: dict, box: dict) -> dict:
    picked = await browser.pick(job, box)
    if not picked:
        raise llm.LLMError("Nothing on the page lines up with that area. Try drawing the box again.")
    return {"found": True, "confidence": "manual", "description": "Your selection", "reason": "",
            **_selection_view(picked)}


# ---------- build ----------

def _frames_content(original: dict, rebuilt: dict) -> list[dict]:
    imgs = [_img(f) for f in original["frames"]] + [_img(original["hover"])]
    imgs += [_img(f) for f in rebuilt["frames"]] + [_img(rebuilt["hover"])]
    return imgs


async def _compare(original: dict, rebuilt: dict) -> dict:
    content = _frames_content(original, rebuilt) + [{"type": "text", "text": COMPARE_PROMPT}]
    text = await llm.complete("You compare UI screenshots precisely.", [{"role": "user", "content": content}],
                              effort="low", max_tokens=3000, label="compare")
    result = _json(text)
    try:
        score = max(1, min(10, int(result.get("score", 0))))
    except (TypeError, ValueError):
        score = 1
    diffs = [str(d) for d in result.get("differences", [])][:5]
    return {"score": score, "differences": diffs}


async def build(job: dict, user_jpeg: bytes):
    """Async generator of progress events; the last one carries the result (or an error)."""
    try:
        yield {"step": "capture", "label": "Reloading the page and recording how the component moves"}
        original = await browser.capture(job)
        data, assets = original["data"], original["assets"]

        yield {"step": "rebuild", "label": "Claude is rebuilding it as a standalone component"}
        extracted = json.dumps(data, ensure_ascii=False)[:MAX_EXTRACT_CHARS]
        content = [_img(user_jpeg)] + [_img(f) for f in original["frames"]] + [_img(original["hover"])]
        content.append({"type": "text", "text": BUILD_PROMPT.format(url=job["url"], extracted=extracted)})
        text = await llm.complete(COMPONENT_SYSTEM, [{"role": "user", "content": content}],
                                  effort=BUILD_EFFORT, max_tokens=BUILD_MAX_TOKENS, label="rebuild")
        comp = parse_component(text)
        width = data["size"]["w"]

        yield {"step": "verify", "label": "Rendering the rebuild and comparing it with the original"}
        rebuilt = await browser.render(job, comp["code"], width, assets)
        check = await _compare(original, rebuilt)

        if check["score"] < GOOD_ENOUGH:
            yield {"step": "fix", "label": f"Match is {check['score']}/10, so Claude is fixing the differences"}
            errors = ("JavaScript errors in the rebuild:\n" + "\n".join(rebuilt["errors"])) if rebuilt["errors"] else ""
            prompt = FIX_PROMPT.format(score=check["score"], differences="\n".join(f"- {d}" for d in check["differences"]),
                                       errors=errors, code=comp["code"], params=json.dumps(comp["params"]))
            content = _frames_content(original, rebuilt) + [{"type": "text", "text": prompt}]
            fixed = parse_component(await llm.complete(
                COMPONENT_SYSTEM, [{"role": "user", "content": content}],
                effort=BUILD_EFFORT, max_tokens=BUILD_MAX_TOKENS, label="fix"))

            yield {"step": "recheck", "label": "Re-checking the fixed version"}
            rebuilt2 = await browser.render(job, fixed["code"], width, assets)
            check2 = await _compare(original, rebuilt2)
            if check2["score"] > check["score"]:  # keep the fix only if it actually scored better
                comp, check = fixed, check2

        yield {"result": {
            **comp,
            "score": check["score"],
            "differences": check["differences"],
            "width": width,
            "element": data["element"],
            "libraries": data["libraries"],
            "assets": assets,
            # Short description of pre-loaded files, sent back with edit requests (the files themselves are not).
            "context": json.dumps({"lottieFiles": data.get("lottieFiles", [])}) if data.get("lottieFiles") else "",
            "original": [data_url(f) for f in original["frames"]] + [data_url(original["hover"])],
        }}
    except (browser.BrowserError, llm.LLMError) as e:
        yield {"error": str(e)}
    except Exception as e:  # never leave the client's progress stream hanging
        print(f"[component] build failed: {e!r}")
        yield {"error": f"Something went wrong while building the component ({type(e).__name__})."}


# ---------- edit ----------

async def edit(code: str, params: list, instruction: str, context: str = "") -> dict:
    prompt = EDIT_PROMPT.format(code=code, params=json.dumps(params), instruction=instruction)
    if context:
        prompt = f"Files the component loads from window.__clAssets:\n{context[:4000]}\n\n" + prompt
    text = await llm.complete(COMPONENT_SYSTEM, [{"role": "user", "content": prompt}],
                              effort=BUILD_EFFORT, max_tokens=BUILD_MAX_TOKENS, label="edit")
    return parse_component(text)
