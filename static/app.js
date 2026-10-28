/* RouterX Python UI — shell behaviour + shared helpers.
 * Vanilla JS over HTMX; Alpine is available to tab modules for local state.
 */
(() => {
  'use strict';

  const store = {
    tab: 'overview',
    expanded: true,
    autoRefresh: false,
    base_url: null,
  };

  try { store.expanded = localStorage.getItem('nova.sidebar.expanded') !== '0'; } catch {}
  /* Auto-refresh is OFF by default (opt-in via the header toggle): periodic
   * live swaps used to close the on-screen keyboard while typing on mobile. */
  try { store.autoRefresh = localStorage.getItem('nova.autorefresh') === '1'; } catch {}

  /* ----------------------------- Sidebar ----------------------------- */

  function applySidebar() {
    document.body.classList.toggle('sb-expanded', store.expanded);
    document.body.classList.toggle('sb-collapsed', !store.expanded);
    document.querySelectorAll('#sidebar .nav-item').forEach((btn) => {
      const active = btn.dataset.tab === store.tab;
      btn.classList.toggle('is-active', active);
      btn.setAttribute('aria-current', active ? 'page' : 'false');
    });
    const activeBtn = document.querySelector(`#sidebar .nav-item[data-tab="${store.tab}"]`);
    if (activeBtn) {
      const t = document.getElementById('tab-title');
      const s = document.getElementById('tab-subtitle');
      if (t) t.textContent = activeBtn.dataset.label || activeBtn.dataset.tab;
      if (s) s.textContent = activeBtn.dataset.subtitle || '';
    }
  }

  /* A tab request must finish through HTMX: direct innerHTML insertion skips
   * cleanup, hx-trigger registration, fragment scripts and afterSwap hooks.
   * Keep the old DOM until HTMX cleans it up, but hide it while navigating so
   * a slow request cannot make the new sidebar selection show old tab data. */
  let navigationVersion = 0;
  let navigationPending = true; // the initial overview hx-trigger="load"
  const tabRequests = new WeakMap();
  let activeTabRequest = null;

  function requestTab(tab) {
    // htmx.ajax defaults to body as its source and queues all body requests.
    // Use the tab's own button instead, and abort only the superseded tab GET.
    if (activeTabRequest && activeTabRequest.readyState !== 4) activeTabRequest.abort();
    const source = document.querySelector(`#sidebar .nav-item[data-tab="${tab}"]`);
    window.htmx.ajax('GET', '/partials/tab/' + tab, {
      source, target: '#tab-content', swap: 'innerHTML',
    });
  }

  function showTabLoading() {
    const target = document.getElementById('tab-content');
    if (target) {
      target.hidden = true;
      target.setAttribute('aria-busy', 'true');
    }
    const loading = document.getElementById('tab-loading');
    if (loading) {
      loading.hidden = false;
      loading.querySelector('[data-tab-error]')?.classList.add('hidden');
      loading.querySelector('[data-tab-skeleton]')?.classList.remove('hidden');
    }
  }

  function finishTabLoading(error = false) {
    navigationPending = false;
    const loading = document.getElementById('tab-loading');
    if (loading) {
      loading.hidden = !error;
      loading.querySelector('[data-tab-error]')?.classList.toggle('hidden', !error);
      loading.querySelector('[data-tab-skeleton]')?.classList.toggle('hidden', error);
    }
    const target = document.getElementById('tab-content');
    if (target) {
      target.hidden = error;
      target.setAttribute('aria-busy', 'false');
    }
  }

  function nav(tab) {
    if (!tab || !document.querySelector(`#sidebar .nav-item[data-tab="${tab}"]`)) return;
    /* The merged RouterX Console entry is a standalone cloud workspace page —
     * it opens in its own browser tab instead of swapping the dashboard. */
    if (tab === 'terminal') {
      window.open('/terminal', '_blank', 'noopener');
      return;
    }
    if (tab === store.tab) return;
    navigationVersion += 1;
    navigationPending = true;
    store.tab = tab;
    applySidebar();
    const target = document.getElementById('tab-content');
    if (!window.htmx || !target) return;
    showTabLoading();
    requestTab(tab);
    window.scrollTo({ top: 0, behavior: 'auto' });
  }

  /* ----------------------------- Clocks ----------------------------- */

  function tick() {
    const now = new Date();
    const c = document.getElementById('clock');
    if (c) c.textContent = now.toLocaleTimeString('en-US', { hour12: false });
    const d = document.getElementById('dhaka-clock');
    if (d) d.textContent = now.toLocaleTimeString('en-US', { hour12: false, timeZone: 'Asia/Dhaka' });
  }

  /* ----------------------------- Toasts ----------------------------- */

  function toast(message, type = 'info') {
    const wrap = document.getElementById('toaster');
    if (!wrap || !message) return;
    const el = document.createElement('div');
    el.className = `nova-toast nova-toast-${type}`;
    el.setAttribute('role', 'status');
    el.innerHTML =
      '<span class="nova-toast-dot mt-1 inline-block size-1.5 shrink-0 rounded-full"></span>' +
      '<span class="min-w-0 flex-1 break-words"></span>';
    el.querySelector('span:nth-child(2)').textContent = String(message);
    const dismiss = () => {
      el.classList.add('leaving');
      setTimeout(() => el.remove(), 160);
    };
    el.addEventListener('click', dismiss);
    wrap.appendChild(el);
    while (wrap.children.length > 5) wrap.firstElementChild.remove();
    setTimeout(dismiss, 4600);
  }

  /* ----------------------------- Clipboard / refresh / downloads ----------------------------- */

  async function copy(text, message = 'Copied to clipboard') {
    try {
      await navigator.clipboard.writeText(text);
      toast(message, 'success');
    } catch {
      toast('Clipboard is not available', 'error');
    }
  }

  function refresh() {
    if (window.htmx && !navigationPending) window.htmx.trigger(document.body, 'nova:refresh');
  }

  async function downloadFile(id, name) {
    try {
      const res = await fetch('/api/admin/storage/files/' + id);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const json = await res.json();
      const bin = atob(json.file.data_base64);
      const bytes = Uint8Array.from(bin, (ch) => ch.charCodeAt(0));
      const url = URL.createObjectURL(new Blob([bytes], { type: json.file.mime || 'application/octet-stream' }));
      const a = document.createElement('a');
      a.href = url;
      a.download = name || json.file.name || 'download';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch {
      toast('Download failed', 'error');
    }
  }

  function copyFromSelector(sel) {
    const el = document.querySelector(sel);
    if (el && el.textContent) copy(el.textContent.trim(), 'Base URL copied to clipboard');
  }

  window.NovaUI = { nav, toast, copy, refresh, downloadFile, copyFromSelector, store };

  /* ----------------------------- HTMX wiring ----------------------------- */

  const requests = new Set();
  document.addEventListener('htmx:beforeRequest', (e) => {
    const { xhr, target } = e.detail;
    requests.add(xhr);
    if (target?.id === 'tab-content') {
      tabRequests.set(xhr, navigationVersion);
      activeTabRequest = xhr;
    }
    document.getElementById('refresh-btn')?.classList.add('nova-spin');
  });
  const settle = (e) => {
    requests.delete(e.detail?.xhr);
    if (activeTabRequest === e.detail?.xhr) activeTabRequest = null;
    if (requests.size === 0) document.getElementById('refresh-btn')?.classList.remove('nova-spin');
  };
  document.addEventListener('htmx:afterRequest', settle);
  document.addEventListener('htmx:timeout', settle);
  document.addEventListener('htmx:sendError', settle);

  document.addEventListener('htmx:beforeSwap', (e) => {
    if (e.detail.target?.id !== 'tab-content') return;
    if (tabRequests.get(e.detail.xhr) !== navigationVersion) {
      e.detail.shouldSwap = false;
    }
  });

  function tabRequestFailed(e) {
    const version = tabRequests.get(e.detail?.xhr);
    if (version === navigationVersion && navigationPending) finishTabLoading(true);
  }
  document.addEventListener('htmx:afterRequest', (e) => {
    if (e.detail.successful === false) tabRequestFailed(e);
  });
  document.addEventListener('htmx:timeout', tabRequestFailed);
  document.addEventListener('htmx:sendError', tabRequestFailed);

  document.addEventListener('htmx:afterSwap', (e) => {
    if (e.target?.id === 'tab-content') {
      finishTabLoading();
      window.scrollTo({ top: 0 });
    }
    if (window.lucide) window.lucide.createIcons();
    applySidebar();
    /* Swapped fragments may carry .nx-reveal cards or a Playground. */
    try { initWorldFX(); } catch {}
    try { initPlayground(); } catch {}
  });

  document.addEventListener('nova:toast', (e) => {
    const d = e.detail || {};
    if (typeof d === 'string') return toast(d, 'info');
    toast(d.message || 'Done', d.type || 'info');
  });

  /* ----------------------------- Global listeners ----------------------------- */

  document.addEventListener('click', (e) => {
    const navBtn = e.target.closest('#sidebar .nav-item');
    if (navBtn) { e.preventDefault(); nav(navBtn.dataset.tab); return; }
    const copyBtn = e.target.closest('[data-copy-selector]');
    if (copyBtn) { e.preventDefault(); copyFromSelector(copyBtn.dataset.copySelector); }
  });

  document.getElementById('tab-retry')?.addEventListener('click', () => {
    navigationVersion += 1;
    navigationPending = true;
    showTabLoading();
    requestTab(store.tab);
  });

  document.getElementById('sidebar-toggle')?.addEventListener('click', () => {
    store.expanded = !store.expanded;
    try { localStorage.setItem('nova.sidebar.expanded', store.expanded ? '1' : '0'); } catch {}
    applySidebar();
  });
  document.getElementById('sb-expand-bottom')?.addEventListener('click', () => {
    store.expanded = true;
    try { localStorage.setItem('nova.sidebar.expanded', '1'); } catch {}
    applySidebar();
  });

  const ar = document.getElementById('auto-refresh');
  function applyAr() {
    const on = ar?.checked ?? store.autoRefresh;
    const dot = document.getElementById('ar-dot');
    const ping = document.getElementById('ar-ping');
    if (dot) { dot.classList.toggle('bg-green-400', on); dot.classList.toggle('bg-slate-600', !on); }
    if (ping) ping.classList.toggle('hidden', !on);
  }
  if (ar) {
    ar.checked = store.autoRefresh;
    ar.addEventListener('change', () => {
      store.autoRefresh = ar.checked;
      try { localStorage.setItem('nova.autorefresh', ar.checked ? '1' : '0'); } catch {}
      applyAr();
    });
    applyAr();
  }

  document.getElementById('refresh-btn')?.addEventListener('click', () => {
    refresh();
  });

  let lastActivity = 0;
  const bumpActivity = () => { lastActivity = Date.now(); };
  ['keydown', 'input', 'pointerdown', 'touchstart'].forEach((evt) =>
    document.addEventListener(evt, bumpActivity, { capture: true, passive: true }));

  const isEditing = () => {
    const a = document.activeElement;
    if (!a) return false;
    const tag = (a.tagName || '').toUpperCase();
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || a.isContentEditable === true;
  };

  /* Live polling — 5s, only when auto-refresh is explicitly on, the tab has
   * live widgets, and the user is not typing/interacting. Every guard exists
   * to guarantee a background refresh can never steal focus or close the
   * mobile keyboard mid-sentence.
   *
   * Dispatches `nova:auto-refresh`, NOT `nova:refresh`: the Models and
   * Providers tabs never listen to the polling event — a background swap
   * used to abort a running Sync Catalogue / Health check sweep mid-flight.
   * Those two tabs refresh on user actions only (manual refresh button,
   * post-mutation HX-Trigger from the server).
   * The Playground is exempt too: a poll must never wipe an open conversation. */
  const AUTO_REFRESH_EXEMPT_TABS = new Set(['models', 'providers', 'playground']);
  setInterval(() => {
    if (!store.autoRefresh) return;
    if (document.hidden) return;
    if (AUTO_REFRESH_EXEMPT_TABS.has(store.tab)) return;
    if (navigationPending || requests.size > 0) return;
    if (isEditing()) return;
    if (Date.now() - lastActivity < 8000) return;
    if (!document.querySelector('#tab-content [data-live]')) return;
    if (window.htmx) window.htmx.trigger(document.body, 'nova:auto-refresh');
  }, 5000);

  /* Base URL chip (meta descriptor) */
  fetch('/api/admin/meta')
    .then((r) => (r.ok ? r.json() : null))
    .then((meta) => {
      if (meta && meta.base_url) {
        store.base_url = meta.base_url;
        const span = document.getElementById('base-url');
        /* The chip itself stays `hidden md:flex` (template) — removing the
           hidden class un-hid it on phones too and caused horizontal
           overflow at 390px. Only the text needs populating. */
        if (span) span.textContent = meta.base_url;
      }
    })
    .catch(() => {});

  /* ----------------------------- Dark AI World FX -----------------------------
   * Presentation only. Every entry point is idempotent (safe after repeated
   * HTMX swaps) and guarded, so a missing element or an old browser can never
   * throw into the navigation code above. */

  function reduceMotion() {
    try { return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); } catch { return false; }
  }

  function initWorldFX() {
    try {
      const reduce = reduceMotion();

      /* Reveal-on-scroll: .nx-reveal starts hidden (CSS) and is released the
         first time it intersects the viewport, or immediately when motion is
         reduced or IntersectionObserver is unavailable. */
      const pending = Array.from(document.querySelectorAll('.nx-reveal:not([data-nx-seen])'));
      const release = (el) => { el.dataset.nxSeen = '1'; el.classList.add('responded'); };
      if (pending.length) {
        const height = window.innerHeight || 800;
        if (reduce || typeof window.IntersectionObserver !== 'function') {
          pending.forEach(release);
        } else {
          try {
            const io = new window.IntersectionObserver((entries) => {
              entries.forEach((entry) => {
                if (entry.isIntersecting) { release(entry.target); io.unobserve(entry.target); }
              });
            }, { rootMargin: '0px 0px -6% 0px', threshold: 0.04 });
            pending.forEach((el) => {
              /* Above the fold should not wait for a scroll event. */
              if (el.getBoundingClientRect().top < height * 1.1) release(el);
              else io.observe(el);
            });
          } catch (e) {
            pending.forEach(release);
          }
        }
      }

      /* Pointer tilt (.nx-tilt) + cursor-follow glow (.nx-glow): one delegated,
         rAF-throttled listener for the whole document. */
      if (!reduce && !window.__nxPointerBound && typeof document.addEventListener === 'function') {
        window.__nxPointerBound = true;
        let frame = 0;
        let last = null;
        document.addEventListener('pointermove', (e) => {
          last = { x: e.clientX, y: e.clientY, target: e.target };
          if (frame) return;
          frame = window.requestAnimationFrame(() => {
            frame = 0;
            const t = last && last.target;
            if (!t || !t.closest) return;
            const host = t.closest('.nx-tilt, .nx-glow');
            if (!host) return;
            const box = host.getBoundingClientRect();
            if (!box.width || !box.height) return;
            const px = (last.x - box.left) / box.width;
            const py = (last.y - box.top) / box.height;
            host.style.setProperty('--mx', (px * 100).toFixed(1) + '%');
            host.style.setProperty('--my', (py * 100).toFixed(1) + '%');
            if (host.classList.contains('nx-tilt')) {
              host.style.setProperty('--tilt-x', ((0.5 - py) * 5).toFixed(2) + 'deg');
              host.style.setProperty('--tilt-y', ((px - 0.5) * 5).toFixed(2) + 'deg');
            }
          });
        }, { passive: true });
        document.addEventListener('pointerout', (e) => {
          const host = e.target && e.target.closest ? e.target.closest('.nx-tilt') : null;
          if (!host) return;
          host.style.removeProperty('--tilt-x');
          host.style.removeProperty('--tilt-y');
        }, { passive: true });
      }
    } catch (e) { /* decorative only */ }
  }

  /* ----------------------------- Playground -----------------------------
   * Chat against this app's own gateway (POST /v1/chat/completions). The
   * streaming toggle reads text/event-stream with fetch (not EventSource), so
   * the request can carry a JSON body and an abort signal.
   *
   * Routing metadata: the gateway's SSE stream never carries the `_nova` block
   * (only a non-streaming body does), so chips cannot come from the stream.
   * The non-streaming path reads `body._nova` directly. The streamed path
   * asks /ui/playground/meta with the request's correlation id once the stream
   * ends; when no row matches, it shows no provider chips at all rather than
   * a guessed one. Never block the answer on that lookup. */

  const pgState = { history: [], busy: false, abort: null };

  function pgEsc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function pgChips(nova, runtime) {
    if (!nova) return '';
    const chip = (cls, inner) => '<span class="inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 font-mono text-[10px] ' + cls + '">' + inner + '</span>';
    const out = [];
    if (nova.provider) out.push(chip('border-slate-800 bg-slate-900/70 text-slate-400', '<span class="sr-only">Provider: </span>' + pgEsc(nova.provider)));
    if (nova.upstream_model) out.push(chip('border-slate-800 bg-slate-900/70 text-slate-400', '<span class="sr-only">Served model: </span>' + pgEsc(nova.upstream_model)));
    out.push(chip(nova.stage ? 'border-slate-800 bg-slate-900/70 text-slate-400' : 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300',
      'stage ' + pgEsc(nova.stage == null ? '-' : nova.stage)));
    if (nova.fallback) out.push(chip('border-amber-500/30 bg-amber-500/10 text-amber-300', 'fallback'));
    if (nova.cached) out.push(chip('border-green-500/30 bg-green-500/10 text-green-300', 'cached'));
    if (nova.spoofed) out.push(chip('border-purple-500/30 bg-purple-500/10 text-purple-300', 'identity spoofed'));
    if (runtime) out.push('<span class="ml-auto font-mono text-[10px] text-slate-500">' + pgEsc(runtime) + '</span>');
    return '<div class="mt-2.5 flex flex-wrap items-center gap-1.5">' + out.join('') + '</div>';
  }

  function pgSetStatus(text) {
    const el = document.getElementById('pg-status');
    if (el) el.textContent = text;
  }

  function pgSetBusy(busy) {
    pgState.busy = busy;
    const stream = document.getElementById('pg-stream');
    if (stream) stream.setAttribute('aria-busy', busy ? 'true' : 'false');
    const send = document.getElementById('pg-send');
    if (send) send.disabled = busy;
    const busyEl = document.getElementById('pg-busy');
    if (busyEl) {
      busyEl.classList.toggle('hidden', !busy);
      busyEl.classList.toggle('flex', busy);
    }
  }

  /* Append an HTML block to the conversation and pin the scroll to the end. */
  function pgAppend(html) {
    const host = document.getElementById('pg-result');
    if (!host) return null;
    const wrap = document.createElement('div');
    wrap.className = 'flex flex-col gap-3 py-1 nx-reveal';
    wrap.innerHTML = html;
    wrap.dataset.nxSeen = '1';
    wrap.classList.add('responded');
    host.appendChild(wrap);
    const empty = document.getElementById('pg-empty');
    if (empty) empty.remove();
    const stream = document.getElementById('pg-stream');
    if (stream) stream.scrollTop = stream.scrollHeight;
    if (window.lucide) window.lucide.createIcons();
    return wrap;
  }

  function pgUserBubble(text) {
    return '<div class="ml-auto max-w-[85%] rounded-2xl rounded-tr-md border border-emerald-500/25 bg-emerald-500/10 px-3.5 py-2.5 text-sm text-slate-100">' +
      '<p class="whitespace-pre-wrap break-words">' + pgEsc(text) + '</p></div>';
  }

  function pgAnswerBubble(model, content, chips) {
    return '<div class="max-w-[92%] rounded-2xl rounded-tl-md border border-slate-800 bg-[#0b0b0b] px-3.5 py-2.5 text-sm text-slate-200">' +
      '<div class="mb-1 nx-kicker">' + pgEsc(model) + '</div>' +
      '<div class="md-body whitespace-pre-wrap break-words">' + pgEsc(content || '(empty answer)') + '</div>' +
      (chips || '') + '</div>';
  }

  function pgErrorBubble(message, status) {
    return '<div class="max-w-[92%] rounded-2xl rounded-tl-md border border-rose-500/40 bg-rose-500/10 px-3.5 py-2.5 text-sm text-rose-200" role="alert">' +
      '<div class="flex items-center gap-2 font-medium"><i data-lucide="triangle-alert" class="size-3.5 shrink-0" aria-hidden="true"></i>' +
      (status ? 'Gateway returned HTTP ' + pgEsc(status) : 'Request failed') + '</div>' +
      '<p class="mt-1 break-words font-mono text-xs text-rose-200/90">' + pgEsc(message) + '</p></div>';
  }

  async function pgSend() {
    const input = document.getElementById('pg-message');
    const modelEl = document.getElementById('pg-model');
    const systemEl = document.getElementById('pg-system');
    const tempEl = document.getElementById('pg-temperature');
    const maxEl = document.getElementById('pg-max-tokens');
    const streamEl = document.getElementById('pg-streaming');
    const text = input ? input.value.trim() : '';
    if (!text || pgState.busy) return;
    const model = (modelEl && modelEl.value) || 'auto';
    const system = systemEl ? systemEl.value.trim() : '';
    const temperature = tempEl ? Number(tempEl.value) : 0.7;
    const maxTokens = maxEl ? Number(maxEl.value) : 1024;
    const streaming = !!(streamEl && streamEl.checked);
    const messages = [];
    if (system) messages.push({ role: 'system', content: system });
    pgState.history.forEach((m) => messages.push(m));
    messages.push({ role: 'user', content: text });

    pgSetBusy(true);
    pgSetStatus('waiting');
    pgAppend(pgUserBubble(text));
    pgState.history.push({ role: 'user', content: text });
    if (input) input.value = '';

    const started = Date.now();
    try {
      if (streaming) {
        await pgStream({ model, messages, temperature, max_tokens: maxTokens }, model, started);
      } else {
        const res = await fetch('/v1/chat/completions', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model, messages, temperature, max_tokens: maxTokens, stream: false }),
        });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw Object.assign(new Error((body && body.error) || ('HTTP ' + res.status)), { status: res.status });
        const choice = (body.choices || [])[0] || {};
        const content = (choice.message && choice.message.content) || '';
        const usage = body.usage || {};
        const runtime = (Date.now() - started) + ' ms · ' + (usage.prompt_tokens == null ? '—' : usage.prompt_tokens) +
          ' in / ' + (usage.completion_tokens == null ? '—' : usage.completion_tokens) + ' out · ' + (choice.finish_reason || 'stop');
        pgAppend(pgAnswerBubble(body.model || model, content, pgChips(body._nova, runtime)));
        pgState.history.push({ role: 'assistant', content });
        pgSetStatus('done · ' + (Date.now() - started) + ' ms');
      }
    } catch (err) {
      if (err && err.name === 'AbortError') {
        pgSetStatus('stopped');
      } else {
        pgAppend(pgErrorBubble((err && err.message) || 'Request failed', err && err.status));
        pgSetStatus('error');
      }
    } finally {
      pgState.abort = null;
      pgSetBusy(false);
    }
  }

  async function pgStream(payload, model, started) {
    const ctrl = new AbortController();
    pgState.abort = ctrl;
    const res = await fetch('/v1/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(Object.assign({}, payload, { stream: true })),
      signal: ctrl.signal,
    });
    if (!res.ok || !res.body) {
      const body = await res.json().catch(() => ({}));
      throw Object.assign(new Error((body && body.error) || ('HTTP ' + res.status)), { status: res.status });
    }
    const wrap = pgAppend('<div class="max-w-[92%] rounded-2xl rounded-tl-md border border-slate-800 bg-[#0b0b0b] px-3.5 py-2.5 text-sm text-slate-200">' +
      '<div class="mb-1 nx-kicker">' + pgEsc(model) + '</div>' +
      '<div class="pg-body md-body whitespace-pre-wrap break-words"><span class="nova-caret text-emerald-400" aria-hidden="true">▌</span></div>' +
      '<div class="pg-meta"></div></div>');
    const bodyEl = wrap && wrap.querySelector('.pg-body');
    const metaEl = wrap && wrap.querySelector('.pg-meta');
    const scrollEl = document.getElementById('pg-stream');
    let content = '';
    let done = false;
    const paint = (caret) => {
      if (!bodyEl) return;
      bodyEl.innerHTML = pgEsc(content) + (caret ? '<span class="nova-caret text-emerald-400" aria-hidden="true">▌</span>' : '');
      if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;
    };
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    pgSetStatus('streaming');
    while (!done) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      const events = buffer.split('\n\n');
      buffer = events.pop() || '';
      for (const raw of events) {
        const line = raw.split('\n').find((l) => l.indexOf('data:') === 0);
        if (!line) continue;
        const data = line.slice(5).trim();
        if (data === '[DONE]') { done = true; break; }
        let json = null;
        try { json = JSON.parse(data); } catch (e) { continue; }
        if (json && json.error) throw new Error((json.error && json.error.message) || 'The gateway stream failed');
        const choice = json && json.choices && json.choices[0];
        const delta = choice && choice.delta;
        if (delta && typeof delta.content === 'string' && delta.content) { content += delta.content; paint(true); }
      }
    }
    paint(false);
    pgState.history.push({ role: 'assistant', content });
    pgSetStatus('done');
    if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;

    /* Streams carry no _nova block. Ask for this request's own log row by
       correlation id (bounded: a few quick polls, then give up). */
    const runtime = (Date.now() - started) + ' ms · streamed';
    pgLoadStreamMeta(metaEl, res.headers.get('x-request-id'), runtime);
  }

  async function pgLoadStreamMeta(metaEl, requestId, runtime) {
    if (!metaEl) return;
    if (!requestId) {
      /* No correlation id: show the runtime line only, never a guessed chip. */
      metaEl.innerHTML = '<div class="mt-2.5 flex items-center gap-1.5"><span class="ml-auto font-mono text-[10px] text-slate-500">' + pgEsc(runtime) + '</span></div>';
      return;
    }
    for (let attempt = 0; attempt < 4; attempt++) {
      await new Promise((r) => setTimeout(r, attempt === 0 ? 250 : 700));
      try {
        const res = await fetch('/ui/playground/meta?request_id=' + encodeURIComponent(requestId));
        if (!res.ok) continue;
        const meta = await res.json().catch(() => null);
        if (meta && meta.ok && meta.nova) {
          if (metaEl.isConnected) metaEl.innerHTML = pgChips(meta.nova, runtime);
          return;
        }
      } catch (err) { /* retry */ }
    }
  }

  function initPlayground() {
    try {
      const root = document.getElementById('tab-playground');
      if (!root) return;
      if (root.dataset.pgBound === '1') return;
      root.dataset.pgBound = '1';

      const send = document.getElementById('pg-send');
      const message = document.getElementById('pg-message');
      const streamEl = document.getElementById('pg-streaming');
      const tempEl = document.getElementById('pg-temperature');
      const tempVal = document.getElementById('pg-temperature-val');
      const form = document.getElementById('pg-form');

      if (send) send.addEventListener('click', (e) => { e.preventDefault(); pgSend(); });
      if (form) form.addEventListener('submit', (e) => { if (e.target === form) { e.preventDefault(); pgSend(); } });
      if (message) message.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); pgSend(); }
      });

      if (streamEl) {
        try { streamEl.checked = localStorage.getItem('nova.playground.stream') !== '0'; } catch (e) { streamEl.checked = true; }
        streamEl.addEventListener('change', () => {
          try { localStorage.setItem('nova.playground.stream', streamEl.checked ? '1' : '0'); } catch (e) { /* ignore */ }
        });
      }
      if (tempEl && tempVal) {
        const paint = () => { tempVal.textContent = Number(tempEl.value).toFixed(1); };
        tempEl.addEventListener('input', paint);
        paint();
      }

      const reload = document.getElementById('pg-models-refresh');
      if (reload) reload.addEventListener('click', async () => {
        reload.disabled = true;
        try {
          const res = await fetch('/v1/models');
          if (!res.ok) throw new Error('HTTP ' + res.status);
          const json = await res.json();
          const pick = document.getElementById('pg-model');
          const rows = (json && json.data) || [];
          if (pick && rows.length) {
            const keep = pick.value;
            pick.innerHTML = '<option value="auto">auto — let the gateway route</option>' + rows.map((m) =>
              '<option value="' + pgEsc(m.id) + '">' + pgEsc(m.id) + (m.owned_by ? ' · ' + pgEsc(m.owned_by) : '') + '</option>').join('');
            if (Array.prototype.some.call(pick.options, (o) => o.value === keep)) pick.value = keep;
            const note = document.getElementById('pg-models-note');
            if (note) note.classList.add('hidden');
          }
        } catch (e) {
          if (window.NovaUI) NovaUI.toast('Could not reload the model list', 'error');
        } finally {
          reload.disabled = false;
        }
      });

      const clear = document.getElementById('pg-clear');
      if (clear) clear.addEventListener('click', () => {
        const host = document.getElementById('pg-result');
        if (host) host.innerHTML = '<div class="m-auto max-w-sm text-center">' +
          '<p class="text-sm font-medium text-slate-200">Conversation cleared</p>' +
          '<p class="mt-1 text-xs text-slate-500">The next message starts without context.</p></div>';
        pgState.history = [];
        pgSetStatus('idle');
      });
    } catch (e) { /* never break the shell */ }
  }

  window.NovaUI.initWorldFX = initWorldFX;
  window.NovaUI.initPlayground = initPlayground;

  /* Init */
  document.addEventListener('DOMContentLoaded', () => {
    try { document.documentElement.classList.remove('no-js'); } catch (e) { /* legacy */ }
    applySidebar();
    if (window.lucide) window.lucide.createIcons();
    tick();
    setInterval(tick, 1000);
    try { initWorldFX(); } catch {}
    try { initPlayground(); } catch {}
  });
})();
