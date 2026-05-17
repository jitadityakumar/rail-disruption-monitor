function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

async function fetchJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) {
    let detail;
    try { detail = (await resp.json()).detail; } catch {}
    const err = new Error(typeof detail === 'string' ? detail : `HTTP ${resp.status}`);
    err.detail = detail;
    throw err;
  }
  return resp.json();
}

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    let detail;
    try { detail = (await resp.json()).detail; } catch {}
    const err = new Error(typeof detail === 'string' ? detail : `HTTP ${resp.status}`);
    err.detail = detail;
    throw err;
  }
  return resp.status === 204 ? null : resp.json();
}

async function patchJSON(url, body) {
  const resp = await fetch(url, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    let detail;
    try { detail = (await resp.json()).detail; } catch {}
    const err = new Error(typeof detail === 'string' ? detail : `HTTP ${resp.status}`);
    err.detail = detail;
    throw err;
  }
  return resp.status === 204 ? null : resp.json();
}

async function deleteReq(url) {
  const resp = await fetch(url, { method: 'DELETE' });
  if (!resp.ok) {
    let detail;
    try { detail = (await resp.json()).detail; } catch {}
    const err = new Error(typeof detail === 'string' ? detail : `HTTP ${resp.status}`);
    err.detail = detail;
    throw err;
  }
  return null;
}
