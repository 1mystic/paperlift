/* Paperlift web build: routes the UI's /api and /out requests to the in-browser Python engine. */
(() => {
  const here = location.pathname.replace(/[^/]*$/, '');            // e.g. /paperlift/app/
  const BASE = here.replace(/app\/$/, '').replace(/\/$/, '');      // e.g. /paperlift   ('' at a domain root)
  window.PL_BASE = BASE;
  window.PL_WEB = true;
  const APP = here;                                                // folder holding worker.js and py/
  const $ = (s) => document.querySelector(s);

  /* ---------- boot screen ---------- */
  const boot = document.createElement('div');
  boot.id = 'plboot';
  boot.innerHTML = '<div><svg viewBox="0 0 64 64" width="44" height="44"><rect x="14" y="20" width="26" height="32" rx="5" fill="none" stroke="#8a8a92" stroke-width="3.5"/><rect x="23" y="11" width="27" height="33" rx="5" fill="#fff"/><path d="M36.5 35V21.5M31 27l5.5-5.5L42 27" fill="none" stroke="#ff5a4e" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
    '<b>Starting Paperlift</b><p id="plboot-t">Preparing…</p><div class="plbar" role="progressbar" aria-valuemin="0" aria-valuemax="100"><i id="plbar-i"></i></div><div id="plbar-n">0%</div><small>First visit downloads about 30 MB. After that it opens from cache.<br>Your PDFs never leave this device.</small></div>';
  const st = document.createElement('style');
  st.textContent = '#plboot{position:fixed;inset:0;background:#0b0b0c;display:grid;place-items:center;z-index:100;text-align:center;color:#ededee;font:14px -apple-system,BlinkMacSystemFont,"Inter",sans-serif}' +
    '#plboot b{display:block;font:400 26px "Iowan Old Style",Palatino,Georgia,serif;margin:14px 0 6px}#plboot p{color:#ff8f86;margin:0 0 14px}#plboot small{color:#6b6b72;line-height:1.6}' +
    '#plboot .plbar{width:260px;height:4px;border-radius:4px;background:#2a2a2e;margin:0 auto 8px;overflow:hidden}#plboot .plbar i{display:block;height:100%;width:0;background:#ff5a4e;border-radius:4px;transition:width .25s ease-out}#plbar-n{color:#9a9aa1;font-size:12px;margin-bottom:16px;font-variant-numeric:tabular-nums}#plboot.err .plbar i{background:#6b6b72}#plboot.err p{color:#ffb3ab}.plws{margin:10px 8px 0;padding:11px 12px;border:1px solid #2a2a2e;border-radius:12px;background:#141416;font-size:12.5px;color:#9a9aa1;line-height:1.45}' +
    '.plws b{display:block;color:#ededee;font-weight:600;margin-bottom:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.plws button{margin-top:8px;width:100%;background:#222226;border:1px solid #3a3a40;border-radius:8px;padding:6px 8px;color:#ededee;cursor:pointer;font:inherit}' +
    '.plws button:hover{background:#2a2a2f}.plws.ok{border-color:#2b5a3d}.plws.ok b{color:#46c27a}';
  document.head.appendChild(st);
  document.addEventListener('DOMContentLoaded', () => { if (!isReady) document.body.appendChild(boot); });
  let pctTarget = 0, pctShown = 0, creepCap = 0;
  const paintPct = () => { const i = $('#plbar-i'), n = $('#plbar-n'); if (i) i.style.width = pctShown + '%'; if (n) n.textContent = Math.round(pctShown) + '%'; const b = boot.querySelector('.plbar'); if (b) b.setAttribute('aria-valuenow', Math.round(pctShown)); };
  /* the bar jumps to each stage, then creeps forward so long downloads never look frozen */
  setInterval(() => { if (!boot.isConnected || boot.classList.contains('err')) return; if (pctShown >= creepCap) return; pctShown = Math.min(creepCap, pctShown + (creepCap - pctShown) * 0.03 + 0.02); paintPct(); }, 200);
  const bootText = (t, err, pct) => {
    const e = $('#plboot-t'); if (e) e.textContent = t; if (err) boot.classList.add('err');
    if (pct != null) { pctTarget = pct; creepCap = pct >= 100 ? 100 : Math.min(99, pct + (pct < 22 ? 17 : pct < 78 ? 50 : 8)); pctShown = Math.max(pctShown, pct); paintPct(); }
  };

  /* ---------- remembered folder (Chrome / Edge) ---------- */
  const idb = () => new Promise((res, rej) => { const r = indexedDB.open('paperlift', 1); r.onupgradeneeded = () => r.result.createObjectStore('kv'); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
  const kv = async (mode, fn) => { const db = await idb(); return new Promise((res, rej) => { const tx = db.transaction('kv', mode); const out = fn(tx.objectStore('kv')); tx.oncomplete = () => res(out.result); tx.onerror = () => rej(tx.error); }); };
  const saveHandle = (h) => kv('readwrite', s => h ? s.put(h, 'dir') : s.delete('dir'));
  const loadHandle = () => kv('readonly', s => s.get('dir')).catch(() => null);
  const canPick = 'showDirectoryPicker' in window;

  /* ---------- worker ---------- */
  let worker = null, waiting = new Map(), nextId = 1, ready, readyRes, readyRej;
  let isReady = false, busy = false, snap = null, overlay = {}, ws = {mounted: false, name: '', pendingHandle: null};

  function startWorker(dirHandle) {
    if (worker) worker.terminate();
    isReady = false; waiting = new Map(); busy = false; overlay = {}; snap = null;
    ready = new Promise((res, rej) => { readyRes = res; readyRej = rej; });
    ready.catch(() => {});
    worker = new Worker(APP + 'worker.js');
    worker.onmessage = (e) => {
      const m = e.data;
      if (m.id) { const w = waiting.get(m.id); if (w) { waiting.delete(m.id); w(m); } return; }
      if (m.type === 'boot') bootText(m.text, false, m.pct);
      else if (m.type === 'ready') { isReady = true; boot.remove(); readyRes(m); restoreSettings(); }
      else if (m.type === 'fatal') { bootText('Could not start the engine: ' + m.text + ' Check your connection and reload.', true); readyRej(new Error(m.text)); }
      else if (m.type === 'job') { busy = true; overlay[m.name] = {status: 'processing', progress: [m.cur, m.total]}; }
      else if (m.type === 'job-end') { delete overlay[m.name]; busy = false; }
      else if (m.type === 'idle') { busy = false; overlay = {}; }
      else if (m.type === 'warn' && window.toast) window.toast(m.text, true);
    };
    worker.postMessage({type: 'init', dirHandle: dirHandle || null, base: APP});
    return ready;
  }

  /* without a mounted folder the engine's files vanish on reload, so keep settings in localStorage */
  async function restoreSettings() {
    if (ws.mounted) return;
    try {
      const saved = localStorage.getItem('pl-settings');
      if (saved) await call('POST', '/api/settings', {'Content-Type': 'application/json'}, new TextEncoder().encode(saved).buffer);
    } catch (e) {}
  }

  function call(method, target, headers, body) {
    return ready.then(() => new Promise((res) => {
      const id = nextId++;
      waiting.set(id, res);
      worker.postMessage({id, type: 'req', method, target, headers, body}, body ? [body] : []);
    }));
  }

  /* serve /api/state from the last snapshot while a file is being extracted, so the UI never stalls */
  function patchedSnapshot() {
    const d = JSON.parse(snap);
    for (const f of d.files) { const o = overlay[f.name]; if (o) { f.status = o.status; f.progress = o.progress; } }
    d.pending = d.files.filter(f => f.status === 'queued' || f.status === 'processing').length;
    return d;
  }

  async function plRequest(method, target, headers, body) {
    if (target.startsWith('/api/state') && busy && snap) {
      return {status: 200, headers: {'Content-Type': 'application/json'}, body: new TextEncoder().encode(JSON.stringify(patchedSnapshot()))};
    }
    const r = await call(method, target, headers, body);
    if (target.startsWith('/api/state') && r.status === 200) snap = new TextDecoder().decode(r.body);
    if (target.startsWith('/api/') && r.status === 200 && target.startsWith('/api/settings') && method === 'POST' && !ws.mounted) {
      try { localStorage.setItem('pl-settings', new TextDecoder().decode(r.body)); } catch (e) {}
    }
    if (BASE && target.startsWith('/api/') && (r.headers['Content-Type'] || '').includes('json')) {
      const t = new TextDecoder().decode(r.body).replaceAll('"/out/', '"' + BASE + '/out/');
      r.body = new TextEncoder().encode(t);
    }
    return r;
  }
  window.plRequest = plRequest;

  const norm = (u) => {
    const url = new URL(u, location.href);
    if (url.origin !== location.origin) return null;
    let p = url.pathname;
    if (BASE && p.startsWith(BASE + '/')) p = p.slice(BASE.length);
    return (p.startsWith('/api/') || p.startsWith('/out/')) ? p + url.search : null;
  };

  /* ---------- fetch override ---------- */
  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const target = norm(typeof input === 'string' ? input : input.url);
    if (!target) return realFetch(input, init);
    const method = (init.method || 'GET').toUpperCase();
    let body = null;
    if (init.body != null) body = init.body instanceof Blob ? await init.body.arrayBuffer() : new TextEncoder().encode(String(init.body)).buffer;
    const r = await plRequest(method, target, init.headers || {}, body);
    return new Response(r.body, {status: r.status, headers: r.headers});
  };

  /* ---------- service worker: lets <iframe>, <img> and download links reach the engine ---------- */
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register(BASE + '/sw.js', {scope: BASE + '/'}).catch(() => {});
    navigator.serviceWorker.addEventListener('message', async (ev) => {
      if (!ev.data || !ev.data.plreq) return;
      const target = norm(ev.data.path);
      const r = target ? await plRequest(ev.data.method, target, {}, null) : {status: 404, headers: {}, body: new ArrayBuffer(0)};
      ev.ports[0].postMessage({status: r.status, headers: r.headers, body: r.body});
    });
  }

  /* ---------- workspace panel ---------- */
  function panel() {
    let el = $('#plws');
    if (!el) {
      el = document.createElement('div'); el.id = 'plws'; el.className = 'plws';
      const foot = $('.foot'); if (!foot) return null;
      foot.insertBefore(el, foot.firstChild);
    }
    return el;
  }
  function paintWorkspace() {
    const el = panel(); if (!el) return;
    el.classList.toggle('ok', ws.mounted);
    if (ws.pendingHandle) {
      el.innerHTML = `<b>${esc(ws.pendingHandle.name)}</b>Allow access again to reopen your files.<button id="plws-b">Reconnect folder</button><button id="plws-t" style="margin-top:6px">Use temporary workspace</button>`;
      $('#plws-b').onclick = reconnect; $('#plws-t').onclick = async () => { ws.pendingHandle = null; await saveHandle(null); paintWorkspace(); };
    } else if (ws.mounted) {
      el.innerHTML = `<b>📁 ${esc(ws.name)}</b>Files are saved in this folder on your computer.<button id="plws-b">Change folder</button>`;
      $('#plws-b').onclick = pickDir;
    } else if (canPick) {
      el.innerHTML = '<b>Temporary workspace</b>Files vanish when you close this tab. Mount a folder to keep them.<button id="plws-b">Use a folder on this computer</button>';
      $('#plws-b').onclick = pickDir;
    } else {
      el.innerHTML = '<b>Temporary workspace</b>Files vanish when you close this tab. Download your results before leaving. (Folder mounting needs Chrome or Edge.)';
    }
  }
  const esc = (s) => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

  async function mount(handle) {
    ws = {mounted: !!handle, name: handle ? handle.name : '', pendingHandle: null};
    if (!boot.isConnected) document.body.appendChild(boot);
    pctShown = pctTarget = 0; bootText('Opening your folder…', false, 0);
    try { await startWorker(handle); } catch (e) { return; }
    paintWorkspace();
    if (window.refresh) window.refresh();
  }
  async function pickDir() {
    try {
      const h = await window.showDirectoryPicker({id: 'paperlift', mode: 'readwrite'});
      await saveHandle(h);
      await mount(h);
    } catch (e) { if (e.name !== 'AbortError' && window.toast) window.toast('Could not open that folder: ' + e.message, true); }
  }
  async function reconnect() {
    const h = ws.pendingHandle;
    if (h && (await h.requestPermission({mode: 'readwrite'})) === 'granted') await mount(h);
  }

  /* ---------- start ---------- */
  document.addEventListener('DOMContentLoaded', async () => {
    ['Open inbox folder', 'Open output folder', 'Rescan inbox'].forEach(t => {
      [...document.querySelectorAll('.foot button')].filter(b => b.textContent.trim() === t).forEach(b => b.remove());
    });
    let handle = null;
    if (canPick) handle = await loadHandle();
    if (handle && (await handle.queryPermission({mode: 'readwrite'})) === 'granted') {
      ws = {mounted: true, name: handle.name, pendingHandle: null};
      startWorker(handle).then(paintWorkspace).catch(() => {});
    } else {
      startWorker(null).catch(() => {});
      if (handle) ws.pendingHandle = handle;
    }
    paintWorkspace();
  });
})();
