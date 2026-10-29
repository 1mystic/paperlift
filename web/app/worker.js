/* Paperlift browser engine: the real Python code running on Pyodide (WebAssembly), inside a Web Worker. */
const PYODIDE = 'https://cdn.jsdelivr.net/pyodide/v0.29.3/full/';
importScripts(PYODIDE + 'pyodide.js');

const CAPS = {PAPERLIFT_CAP_FILES: '5', PAPERLIFT_CAP_MB: '15', PAPERLIFT_CAP_PAGES: '30'};
let py = null, webhost = null, nativefs = null, pumping = false;
const say = (text) => postMessage({type: 'boot', text});

async function boot({dirHandle, base}) {
  say('Loading Python runtime…');
  py = await loadPyodide({indexURL: PYODIDE});
  say('Loading PDF engine…');
  await py.loadPackage(['PyMuPDF', 'pillow', 'lxml', 'micropip', 'typing-extensions']);
  say('Loading Word export…');
  await py.runPythonAsync("import micropip\nawait micropip.install('python-docx')");

  say('Loading Paperlift…');
  const manifest = await (await fetch(base + 'py/manifest.json', {cache: 'no-cache'})).json();
  py.FS.mkdirTree('/pkg/paperlift');
  for (const f of manifest.files) {
    const r = await fetch(base + 'py/' + f, {cache: 'no-cache'});
    py.FS.writeFile('/pkg/' + f, new Uint8Array(await r.arrayBuffer()));
  }

  let root = '/work';
  py.FS.mkdirTree(root);
  if (dirHandle) {
    root = '/mnt/paperlift';
    py.FS.mkdirTree(root);
    nativefs = await py.mountNativeFS(root, dirHandle);
  }
  py.FS.mkdirTree(root + '/inbox');
  py.FS.mkdirTree(root + '/output');

  py.registerJsModule('plbridge', {
    progress: (name, cur, total) => postMessage({type: 'job', name, cur, total}),
  });
  py.globals.set('PL_ROOT', root);
  py.globals.set('PL_CAPS', py.toPy(CAPS));
  py.runPython(`
import os, sys
sys.path.insert(0, '/pkg')
os.environ['PAPERLIFT_HOME'] = PL_ROOT
os.environ.update(dict(PL_CAPS))
from paperlift import webhost
import plbridge
webhost.set_progress_hook(plbridge.progress)
`);
  webhost = py.pyimport('paperlift.webhost');
  if (nativefs) await nativefs.syncfs();
  postMessage({type: 'ready', root, mounted: !!dirHandle});
}

async function flush() {
  if (nativefs) { try { await nativefs.syncfs(); } catch (e) { postMessage({type: 'warn', text: 'Could not write to the folder: ' + e.message}); } }
}

async function pump() {
  if (pumping) return;
  pumping = true;
  try {
    for (;;) {
      const name = webhost.run_next();
      if (!name) break;
      await flush();
      postMessage({type: 'job-end', name});
      await new Promise(r => setTimeout(r, 0));   // let queued requests (state, cancel, move…) through
    }
  } finally {
    pumping = false;
    postMessage({type: 'idle'});
  }
}

async function handle(m) {
  const res = webhost.js_request(m.method, m.target, py.toPy(m.headers || {}), m.body ? new Uint8Array(m.body) : new Uint8Array(0));
  const [status, headers, body] = res.toJs();
  res.destroy();
  if (m.method !== 'GET') await flush();
  const out = body instanceof Uint8Array ? body : new Uint8Array(body);
  const buf = out.buffer.slice(out.byteOffset, out.byteOffset + out.byteLength);
  postMessage({id: m.id, status, headers: JSON.parse(headers), body: buf}, [buf]);
  if (m.target.startsWith('/api/convert')) pump();
}

onmessage = async (e) => {
  const m = e.data;
  try {
    if (m.type === 'init') await boot(m);
    else if (m.type === 'req') await handle(m);
  } catch (err) {
    if (m.type === 'init') postMessage({type: 'fatal', text: String(err && err.message || err)});
    else postMessage({id: m.id, status: 500, headers: {'Content-Type': 'application/json'},
      body: new TextEncoder().encode(JSON.stringify({error: String(err && err.message || err)})).buffer});
  }
};
