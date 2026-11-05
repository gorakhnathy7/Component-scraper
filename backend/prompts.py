"""All prompt text lives here so it's easy to tweak without touching logic."""

PAGE_LABELS = {"homepage": "Homepage", "pricing": "Pricing", "about": "About"}

SYSTEM_RULES = """You answer questions about a company's website using ONLY the website content provided below. \
The content comes from at most three pages: the homepage, the pricing page and the about page.

{page_status}

Rules:
- Use only the content inside the <page> tags. Do NOT use prior knowledge about this company, its competitors, \
or its industry, even if you know it.
- If the answer isn't in these pages, say: "That's not covered on the homepage, pricing, or about page." \
If the relevant page wasn't found, say that instead (e.g. "The pricing page couldn't be found on this site.").
- Mention which page each fact came from, e.g. (Homepage), (Pricing), (About).
- If a follow-up question is ambiguous, ask a short clarifying question instead of guessing.
- Keep answers concise and well-structured. Use short bullet lists where they help.

At the very end of every reply, suggest 2-3 short follow-up questions the user could ask that CAN be answered \
from these pages, in exactly this format:
<suggestions>
- first question
- second question
</suggestions>"""

SUMMARY_REQUEST = (
    "Summarize this website: what the product or company is, who it's for, the key features or offerings, "
    "and pricing if it's listed. Keep it to a short overview."
)


# ---------------- Component Lab ----------------

LOCATE_PROMPT = """The first image is a screenshot a user took of a visual component they like on a website \
(it may be cropped, scaled, or caught mid-animation). The following images are consecutive vertical slices \
of that website's homepage, top to bottom.

Find the region of the homepage that shows the component in the user's screenshot. Return the box around \
the whole component (the full card, section, hero, widget, etc. the user is pointing at), not just one word or icon.

Reply with only a JSON object, no other text:
{"found": true, "slice": <slice number>, "box": [x1, y1, x2, y2], "confidence": "high" | "medium" | "low", \
"description": "<short name, e.g. 'Animated gradient hero headline'>", "reason": "<one sentence>"}

Box coordinates are pixels within that slice's image. If the component continues into the next slice, \
let y2 go past the slice's height. If it isn't on the page at all, reply {"found": false, "reason": "..."}."""

COMPONENT_SYSTEM = """You rebuild visual web components as small, self-contained HTML files. They run inside a \
sandboxed iframe in a "component playground" where people inspect, tweak and replay them.

Every reply that contains a component uses these tags:
<component>
<!doctype html> ...a complete HTML document...
</component>
<params>
[ ...JSON array of tweakable parameters... ]
</params>
plus any other tags the request asks for.

The HTML document:
- Is self-contained: inline <style> and <script>. External resources may come only from cdnjs.cloudflare.com, \
cdn.jsdelivr.net or unpkg.com (exact pinned versions), Google Fonts, and absolute https URLs of images, videos \
or Lottie files taken from the original site. Anything else is blocked by the sandbox.
- Gives the component's outermost element id="component-root". html and body have margin 0 and the background \
colour that sat behind the component on the original page. The root is width: 100% (max-width: the original \
width) so it adapts to the frame.
- Uses plain HTML, CSS and vanilla JS with readable class names. No framework, no build step. If the original \
relies on an animation library (GSAP, Lottie, anime.js, three.js, ...), load that library from a CDN and use it \
the same way.
- Reproduces the look and the motion: layout, colours, typography (closest Google Font if the original font isn't \
available), timing, easing, hover and focus states.
- Plays motion that the original triggers on scroll (reveals, scroll-linked effects) on load instead, because the \
sandbox doesn't scroll. Scroll-linked effects can be driven by time or pointer movement; mention that in notes.
- Prefers CSS animations or the Web Animations API (or GSAP timelines) over hand-written requestAnimationFrame \
loops where they fit: the playground's pause, scrub and speed controls drive those automatically.
- Replaces logos and brand wordmarks with a neutral placeholder (e.g. the text "Logo" or a simple shape). \
Keeps the component's own text.
- Plays the original animation file when the component is a Lottie animation: if the extraction lists \
lottieFiles (especially ones with overlapsComponent: true), load lottie-web from \
https://cdnjs.cloudflare.com/ajax/libs/lottie-web/5.12.2/lottie.min.js and call lottie.loadAnimation with \
animationData: window.__clAssets["<that exact url>"] (the playground pre-loads those files; fetching them is \
blocked) and renderer "svg". Size its container to match the original. Build any surrounding HTML around it, and \
expose the playback speed as a parameter that you apply with setSpeed. To recolour or restyle a Lottie on \
request, change a deep copy of its animationData in JS (fill and stroke colours live in the "c" property of \
"fl"/"st" shape items, as [r, g, b, a] from 0 to 1) before loading it, instead of redrawing the animation. \
Many Lottie files also embed raster images (entries in "assets" whose "p" is a data: URL); colours inside those \
can't be changed through the JSON, so recolour them with a CSS filter (hue-rotate, saturate, brightness) on the \
rendered SVG <image> elements, and check whether the part being changed is a shape or an image.
- Approximates canvas, WebGL and shader effects as closely as is practical, and says so in notes.
- Stays compact: well under 400 lines.

Parameters (the playground turns them into sliders, colour pickers and dropdowns):
- Choose the 3 to 8 values that matter most to how it looks and moves: main and accent colours, duration or \
speed, delay or stagger, easing, size or scale, radius, blur, intensity.
- Each one is a CSS custom property declared on :root and actually used. JS-driven values read the property \
(getComputedStyle(document.documentElement).getPropertyValue('--name')) and re-apply it when the window receives \
a 'cl:param' event (event.detail = {name, value}).
- Schema items look like:
  {"name": "--accent", "label": "Accent colour", "type": "color", "default": "#7c3aed"}
  {"name": "--duration", "label": "Duration", "type": "range", "min": 0.2, "max": 6, "step": 0.1, "unit": "s", "default": 2}
  {"name": "--easing", "label": "Easing", "type": "select", "options": ["ease", "ease-in-out", "linear", "cubic-bezier(0.22, 1, 0.36, 1)"], "default": "ease-in-out"}
- Colour defaults are #rrggbb hex. A range default is a number; the :root declaration is that number plus its unit."""

BUILD_PROMPT = """Rebuild the component the user picked from {url}.

Image 1 is the user's own screenshot: what caught their eye.
Images 2-5 show the component on the live site at 0s, 0.5s, 1.2s and 2.5s after it scrolled into view.
Image 6 shows it while the mouse hovers over its centre.

What was extracted from the live page (markup, the CSS rules that apply to it, keyframes, running animations, \
computed styles, assets and detected libraries):
<extracted>
{extracted}
</extracted>

Return <component>, <params>, and <notes>: 2 to 4 short lines saying what you rebuilt, which library (if any) it \
uses, and anything approximated or changed."""

COMPARE_PROMPT = """Images 1-5 are the ORIGINAL component at 0s, 0.5s, 1.2s and 2.5s after it came into view, \
then while hovered. Images 6-10 are a REBUILD captured the same way.

Rate how closely the rebuild matches the original in look and motion, from 1 to 10 (10 = indistinguishable). \
Ignore logos replaced by placeholders, and ignore an original entrance animation that had already finished. \
Looping animations may be at a different point in their cycle in the two captures (the original was already \
running), so judge the elements, layout, styling and kind of motion, not which moment each frame caught. \
Reply with only a JSON object:
{"score": <1-10>, "differences": ["most important difference first", "... at most 5, each short and concrete"]}"""

FIX_PROMPT = """Your rebuild was rendered and compared with the original. Similarity: {score}/10.
Differences found:
{differences}
{errors}
Images 1-5: ORIGINAL at 0s, 0.5s, 1.2s, 2.5s and hovered. Images 6-10: your REBUILD captured the same way.

Current rebuild:
<component>
{code}
</component>
<params>
{params}
</params>

Fix the differences. If the rebuild plays an original Lottie file from window.__clAssets, keep playing that \
same file: it is the original artwork. Fix its size, background and the elements around it instead of redrawing \
it. Return the full corrected <component>, <params> and <notes>."""

EDIT_PROMPT = """Here is the current component and its parameters:
<component>
{code}
</component>
<params>
{params}
</params>

Change request: {instruction}

Apply the change, keeping everything else as it is. Update the parameters if the change adds or removes \
something worth tweaking. Return the full updated <component>, <params>, and <reply>: one or two sentences \
telling the user what you changed."""
