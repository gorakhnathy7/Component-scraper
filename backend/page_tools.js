// Injected into the live page by browser.py (via Playwright's evaluate, which page CSP can't block).
// Defines window.__cl with helpers to locate a component and extract what it's made of.
(() => {
  const LIMITS = { html: 40000, css: 60000, elements: 400, animations: 30, styled: 30, assets: 20 };
  const TARGET_ATTR = "data-cl-target";

  const pageRect = (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height };
  };

  const iou = (a, b) => {
    const x1 = Math.max(a.x, b.x), y1 = Math.max(a.y, b.y);
    const x2 = Math.min(a.x + a.w, b.x + b.w), y2 = Math.min(a.y + a.h, b.y + b.h);
    const inter = Math.max(0, x2 - x1) * Math.max(0, y2 - y1);
    const union = a.w * a.h + b.w * b.h - inter;
    return union > 0 ? inter / union : 0;
  };

  // Share of a's area that lies inside b.
  const overlapShare = (a, b) => {
    const r = a.getBoundingClientRect(), s = b.getBoundingClientRect();
    const ix = Math.max(0, Math.min(r.right, s.right) - Math.max(r.left, s.left));
    const iy = Math.max(0, Math.min(r.bottom, s.bottom) - Math.max(r.top, s.top));
    return r.width * r.height > 0 ? (ix * iy) / (r.width * r.height) : 0;
  };

  const describe = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += "#" + el.id;
    const cls = [...el.classList].slice(0, 3);
    if (cls.length) s += "." + cls.join(".");
    return s;
  };

  // A selector we can re-find after reloading the page (ids where unique, nth-of-type otherwise).
  const cssPath = (el) => {
    const parts = [];
    while (el && el.nodeType === 1 && el !== document.documentElement) {
      if (el.id && document.querySelectorAll("#" + CSS.escape(el.id)).length === 1) {
        parts.unshift("#" + CSS.escape(el.id));
        break;
      }
      let i = 1, s = el;
      while ((s = s.previousElementSibling)) if (s.tagName === el.tagName) i++;
      parts.unshift(el.tagName.toLowerCase() + ":nth-of-type(" + i + ")");
      el = el.parentElement;
    }
    return parts.join(" > ");
  };

  const mark = (el) => {
    document.querySelectorAll("[" + TARGET_ATTR + "]").forEach((e) => e.removeAttribute(TARGET_ATTR));
    el.setAttribute(TARGET_ATTR, "1");
    const text = (el.innerText || "").replace(/\s+/g, " ").trim().slice(0, 80);
    return { rect: pageRect(el), desc: describe(el), selector: cssPath(el), text };
  };

  const target = () => document.querySelector("[" + TARGET_ATTR + "]");

  // Pick the element whose box best overlaps `box` (page coordinates).
  function pickByBox(box) {
    let best = null, bestScore = 0;
    for (const el of document.body.querySelectorAll("*")) {
      const r = pageRect(el);
      if (r.w * r.h < 400) continue;
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden") continue;
      const score = iou(r, box);
      if (score > bestScore) { best = el; bestScore = score; }
    }
    if (!best) return null;
    // Prefer the outermost of a chain of same-sized wrappers: it carries the wrapper's styles too.
    while (best.parentElement && best.parentElement !== document.body &&
           iou(pageRect(best.parentElement), box) >= bestScore - 0.02) {
      best = best.parentElement;
    }
    return { ...mark(best), iou: bestScore };
  }

  function findBySelector(sel) {
    try {
      const el = document.querySelector(sel);
      return el ? mark(el) : null;
    } catch { return null; }
  }

  function viewportRect() {
    const el = target();
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: r.left, y: r.top, w: r.width, h: r.height };
  }

  function scrollTargetIntoView() {
    const el = target();
    if (!el) return false;
    const r = el.getBoundingClientRect();
    // Center it if it fits, otherwise align its top a little below the viewport top.
    const y = r.height < innerHeight ? r.top + scrollY - (innerHeight - r.height) / 2 : r.top + scrollY - 40;
    window.scrollTo({ top: Math.max(0, y), behavior: "instant" });
    return true;
  }

  function blockedSheets() {
    const out = [];
    for (const sheet of document.styleSheets) {
      try { void sheet.cssRules; } catch { if (sheet.href) out.push(sheet.href); }
    }
    return out;
  }

  // ---------- extraction ----------

  const PSEUDO_RE = /::?(before|after|hover|focus-visible|focus-within|focus|active|visited|placeholder|marker|selection|first-letter|first-line|backdrop)(\([^)]*\))?/g;

  // Does any selector in the (comma-separated) list match an element, ignoring state/pseudo parts?
  const matchesAny = (selectorText, els) => {
    for (let part of selectorText.split(",")) {
      part = part.replace(PSEUDO_RE, "").trim();
      if (!part || /[>+~]$/.test(part)) part = (part + " *").trim();
      try {
        for (const el of els) if (el.matches(part)) return true;
      } catch { /* unsupported selector */ }
    }
    return false;
  };

  function extract(crossOriginCss) {
    const root = target();
    if (!root) return null;
    const els = [root, ...root.querySelectorAll("*")].slice(0, LIMITS.elements);

    // 1. Markup (scripts removed, giant attributes trimmed)
    const clone = root.cloneNode(true);
    clone.querySelectorAll("script, noscript").forEach((n) => n.remove());
    clone.removeAttribute(TARGET_ATTR);
    for (const n of [clone, ...clone.querySelectorAll("*")]) {
      for (const a of [...n.attributes]) {
        if (a.value.length > 2000) n.setAttribute(a.name, a.value.slice(0, 2000) + "…");
        if (a.name.startsWith("on")) n.removeAttribute(a.name);
      }
    }
    const html = clone.outerHTML.slice(0, LIMITS.html);

    // 2. Animation usage, so we keep the matching @keyframes
    const animNames = new Set();
    const fontFamilies = new Set();
    for (const el of els) {
      for (const pseudo of [null, "::before", "::after"]) {
        const cs = getComputedStyle(el, pseudo);
        cs.animationName.split(",").map((s) => s.trim()).filter((s) => s && s !== "none").forEach((n) => animNames.add(n));
        cs.fontFamily.split(",").forEach((f) => fontFamilies.add(f.trim().replace(/["']/g, "").toLowerCase()));
      }
    }

    // 3. Stylesheet rules that apply to the subtree
    const out = [];
    let size = 0;
    const push = (text) => { if (size < LIMITS.css) { out.push(text); size += text.length; } };

    const walk = (rules, wrap) => {
      for (const rule of rules) {
        if (rule instanceof CSSStyleRule) {
          if (matchesAny(rule.selectorText, els)) push(wrap(rule.cssText));
        } else if (rule instanceof CSSMediaRule) {
          walk(rule.cssRules, (t) => wrap(`@media ${rule.conditionText} { ${t} }`));
        } else if (typeof CSSSupportsRule !== "undefined" && rule instanceof CSSSupportsRule) {
          walk(rule.cssRules, (t) => wrap(`@supports ${rule.conditionText} { ${t} }`));
        } else if (rule instanceof CSSKeyframesRule) {
          if (animNames.has(rule.name)) push(rule.cssText);
        } else if (rule instanceof CSSFontFaceRule) {
          const fam = (rule.style.getPropertyValue("font-family") || "").replace(/["']/g, "").trim().toLowerCase();
          if (fontFamilies.has(fam)) push(rule.cssText);
        } else if (rule.cssRules) {
          try { walk(rule.cssRules, wrap); } catch { /* layer/container rules etc. */ }
        }
      }
    };

    for (const sheet of document.styleSheets) {
      let rules;
      try { rules = sheet.cssRules; } catch { continue; }
      walk(rules, (t) => t);
    }
    for (const [href, text] of Object.entries(crossOriginCss || {})) {
      try {
        const s = new CSSStyleSheet();
        s.replaceSync(text); // parsed off-document, so the page's own styling is untouched
        walk(s.cssRules, (t) => t);
      } catch { /* unparsable sheet */ }
    }
    // Inline style attributes are already in the markup.

    // 4. CSS variables referenced by those rules, resolved at the component root
    const cssText = out.join("\n");
    const vars = {};
    const rootCs = getComputedStyle(root);
    for (const m of new Set(cssText.match(/var\(\s*(--[\w-]+)/g) || [])) {
      const name = m.replace(/var\(\s*/, "");
      const v = rootCs.getPropertyValue(name).trim();
      if (v) vars[name] = v;
    }

    // 5. Inherited context (what the component looks like sitting on the page)
    let bg = "transparent";
    for (let e = root; e; e = e.parentElement) {
      const c = getComputedStyle(e).backgroundColor;
      if (c && c !== "transparent" && !/rgba\(.+,\s*0\)$/.test(c)) { bg = c; break; }
    }
    const context = {
      backgroundBehind: bg,
      color: rootCs.color, fontFamily: rootCs.fontFamily, fontSize: rootCs.fontSize,
      lineHeight: rootCs.lineHeight, letterSpacing: rootCs.letterSpacing,
    };

    // 6. Running animations (CSS animations, transitions and Web Animations API, incl. ones made by JS libraries)
    const animations = [];
    for (const a of root.getAnimations({ subtree: true }).slice(0, LIMITS.animations)) {
      const eff = a.effect;
      let keyframes = [];
      try { keyframes = eff.getKeyframes(); } catch { }
      const t = eff ? eff.getComputedTiming() : {};
      animations.push({
        kind: a.constructor.name,
        name: a.animationName || a.transitionProperty || a.id || "",
        target: eff && eff.target ? describe(eff.target) : "",
        duration: t.duration, delay: t.delay, iterations: t.iterations, easing: t.easing,
        direction: t.direction, fill: t.fill, playState: a.playState,
        keyframes: JSON.stringify(keyframes).slice(0, 1500),
      });
    }

    // 7. Computed styles of the elements that move or are visually notable
    const PROPS = ["display", "position", "width", "height", "transform", "transition", "animation", "opacity",
      "filter", "backdrop-filter", "background", "color", "border-radius", "box-shadow", "mix-blend-mode",
      "clip-path", "mask-image", "will-change"];
    const styled = [];
    for (const el of els) {
      if (styled.length >= LIMITS.styled) break;
      const cs = getComputedStyle(el);
      const moves = cs.animationName !== "none" || cs.transitionDuration.split(",").some((d) => parseFloat(d) > 0) ||
        cs.transform !== "none" || cs.willChange !== "auto";
      if (el !== root && !moves) continue;
      const entry = { el: describe(el) };
      for (const p of PROPS) {
        const v = cs.getPropertyValue(p);
        if (v && !["none", "auto", "normal", "0s", "all 0s ease 0s", "visible"].includes(v)) entry[p] = v;
      }
      styled.push(entry);
    }

    // 8. Assets and media
    const abs = (u) => { try { return new URL(u, location.href).href; } catch { return u; } };
    const images = new Set();
    for (const el of els) {
      if (images.size >= LIMITS.assets) break;
      if (el.tagName === "IMG" && (el.currentSrc || el.src)) images.add(abs(el.currentSrc || el.src));
      const bgImg = getComputedStyle(el).backgroundImage;
      for (const m of bgImg.matchAll(/url\(["']?([^"')]+)["']?\)/g)) images.add(abs(m[1]));
    }
    const media = {
      canvas: root.querySelectorAll("canvas").length + (root.tagName === "CANVAS" ? 1 : 0),
      video: [...root.querySelectorAll("video")].map((v) => abs(v.currentSrc || v.src || (v.querySelector("source") || {}).src || "")).slice(0, 5),
      svg: root.querySelectorAll("svg").length,
      // Lottie players inside the component, or laid over it (often positioned as a sibling).
      lottie: [...document.querySelectorAll("lottie-player, dotlottie-player, dotlottie-wc, [data-animation-path], [data-src$='.json'], [data-src$='.lottie']")]
        .filter((e) => root.contains(e) || e.contains(root) || overlapShare(e, root) > 0.5)
        .map((e) => abs(e.getAttribute("src") || e.getAttribute("data-animation-path") || e.getAttribute("data-src") || ""))
        .filter(Boolean).slice(0, 5),
      iframes: [...root.querySelectorAll("iframe")].map((f) => abs(f.src)).slice(0, 5),
    };

    // 9. Animation / UI libraries on the page
    const scripts = [...document.scripts].map((s) => s.src).filter(Boolean);
    const has = (re) => scripts.some((s) => re.test(s));
    const libraries = Object.entries({
      gsap: !!window.gsap || has(/gsap|greensock/i),
      "gsap ScrollTrigger": !!window.ScrollTrigger || has(/ScrollTrigger/i),
      lottie: !!(window.lottie || window.bodymovin) || has(/lottie|bodymovin/i) || media.lottie.length > 0,
      three: !!window.THREE || has(/three(\.module)?(\.min)?\.js/i),
      "framer motion / Framer site": !!document.querySelector("[data-framer-name],[data-framer-component-type]") || has(/framer/i),
      spline: !!document.querySelector("spline-viewer") || has(/splinetool/i),
      animejs: !!window.anime || has(/anime(\.min)?\.js/i),
      swiper: !!window.Swiper || !!root.querySelector(".swiper"),
      lenis: !!window.Lenis || document.documentElement.classList.contains("lenis"),
      aos: !!root.querySelector("[data-aos]") || has(/aos/i),
      webflow: !!window.Webflow || document.documentElement.hasAttribute("data-wf-page"),
      "motion one": has(/motion(\.min)?\.js|@motionone/i),
    }).filter(([, v]) => v).map(([k]) => k);

    return {
      element: describe(root), size: pageRect(root), html, css: cssText, cssVariables: vars, context,
      animations, styledElements: styled, images: [...images], media, libraries,
    };
  }

  // Cookie/consent banners cover the page in screenshots; hide fixed overlays that talk about cookies.
  function hideConsentBanners() {
    let hidden = 0;
    for (const el of document.body.querySelectorAll("div, section, aside, dialog, footer, form")) {
      const cs = getComputedStyle(el);
      if (cs.position !== "fixed" && cs.position !== "sticky") continue;
      const r = el.getBoundingClientRect();
      if (r.height > innerHeight * 0.6) continue;
      if (/cookie|consent|gdpr|privacy choices/i.test(el.innerText || "") || /cookie|consent|onetrust/i.test(el.id + " " + el.className)) {
        el.style.setProperty("display", "none", "important");
        hidden++;
      }
    }
    return hidden;
  }

  window.__cl = { hideConsentBanners, pickByBox, findBySelector, viewportRect, scrollTargetIntoView, blockedSheets, extract };
})();
