// Chat UI helpers for miployees mocks.
//  - Autogrow textareas inside .chat-composer / .agent-composer
//  - Scroll the log to the bottom on load
//  - Lazy-render older messages as the user scrolls up
//  - Swap WhatsApp-style mic <-> send icon based on input state
//  - Manager sidebar collapse/expand

(function () {
  "use strict";

  const CHUNK = 20;

  function scrollToBottom(el, { instant = true } = {}) {
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    if (!instant) {
      // ensure final layout after images/fonts
      requestAnimationFrame(() => {
        el.scrollTop = el.scrollHeight;
      });
    }
  }

  // Lazy load: keep a "window" of messages visible; reveal older ones
  // when the user scrolls near the top. Works against a log that has
  // all children pre-rendered (mocks mode) by collapsing them behind
  // a data-hidden attribute.
  function setupLazyChat(log) {
    if (!log) return;
    const all = Array.from(log.children).filter(
      (c) => c.classList.contains("chat-msg") || c.classList.contains("agent-msg"),
    );
    const pageSize = parseInt(log.dataset.chatPageSize || CHUNK, 10);

    // Show the last pageSize messages, hide the rest until scroll-up.
    let visibleFrom = Math.max(0, all.length - pageSize);
    for (let i = 0; i < visibleFrom; i++) {
      all[i].hidden = true;
    }

    // First paint: force scroll to bottom without animation.
    requestAnimationFrame(() => scrollToBottom(log));
    // Re-pin on fonts/images settling.
    window.addEventListener("load", () => scrollToBottom(log));

    log.addEventListener("scroll", () => {
      if (visibleFrom === 0) return;
      if (log.scrollTop > 40) return;
      const prevHeight = log.scrollHeight;
      const nextFrom = Math.max(0, visibleFrom - pageSize);
      for (let i = nextFrom; i < visibleFrom; i++) {
        all[i].hidden = false;
      }
      visibleFrom = nextFrom;
      // Preserve the user's scroll position across the new content.
      const delta = log.scrollHeight - prevHeight;
      log.scrollTop = log.scrollTop + delta;
    });
  }

  function setupAutogrow(textarea) {
    if (!textarea) return;
    const max = 140;
    const resize = () => {
      textarea.style.height = "auto";
      textarea.style.height = Math.min(textarea.scrollHeight, max) + "px";
    };
    textarea.addEventListener("input", resize);
    // Enter sends, Shift+Enter = newline (WhatsApp pattern on desktop).
    textarea.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        const form = textarea.closest("form");
        if (form) form.requestSubmit();
      }
    });
    resize();
  }

  function setupSendIcon(form) {
    if (!form) return;
    const textarea = form.querySelector("[data-chat-textarea]");
    const send = form.querySelector("[data-chat-send]");
    if (!textarea || !send) return;
    const update = () => {
      const hasText = textarea.value.trim().length > 0;
      send.classList.toggle("chat-composer__send--ready", hasText);
    };
    textarea.addEventListener("input", update);
    update();
  }

  function setupAgentCollapse() {
    const toggle = document.querySelector("[data-agent-toggle]");
    const sidebar = document.querySelector("[data-agent-sidebar]");
    if (!toggle || !sidebar) return;
    // Initial collapsed/expanded state is set by the server from the
    // per-user cookie, so the first paint matches the user's last
    // choice without a flash. Toggling POSTs back to record the new
    // preference against the session (§14 "persisted per user").
    toggle.addEventListener("click", () => {
      const collapsed = !sidebar.classList.contains("desk__agent--collapsed");
      sidebar.classList.toggle("desk__agent--collapsed", collapsed);
      toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
      const url = "/agent/sidebar/" + (collapsed ? "collapsed" : "open");
      // Persist even if the user immediately navigates away: sendBeacon
      // is queued by the browser and survives unload; the fallback
      // uses fetch with keepalive for the same property.
      let delivered = false;
      if (navigator.sendBeacon) {
        try { delivered = navigator.sendBeacon(url, new Blob([], { type: "text/plain" })); }
        catch (_) { /* fall through */ }
      }
      if (!delivered) {
        fetch(url, {
          method: "POST",
          credentials: "same-origin",
          keepalive: true,
          headers: { "X-Requested-With": "fetch" },
        }).catch(() => { /* preference is best-effort */ });
      }
    });
  }

  function syncBannerHeight() {
    const banner = document.querySelector(".preview-banner");
    if (!banner) return;
    const h = banner.getBoundingClientRect().height;
    document.documentElement.style.setProperty("--banner-h", h + "px");
  }

  function init() {
    syncBannerHeight();
    window.addEventListener("resize", syncBannerHeight);
    document.querySelectorAll("[data-lazy-chat]").forEach(setupLazyChat);
    document.querySelectorAll("[data-chat-textarea]").forEach(setupAutogrow);
    document.querySelectorAll("[data-chat-composer]").forEach(setupSendIcon);
    setupAgentCollapse();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
