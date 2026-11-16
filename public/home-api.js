'use strict';

(function setup(root) {
  const panelOrigin = 'https://nexus-home-assistant.onrender.com';
  const apiOrigin = 'https://nexus-home-assistant-api.onrender.com';
  const sessionKey = 'kontur-cloud-session';
  const local = () => ['http://127.0.0.1:8080','http://localhost:8080'].includes(root.location?.origin);
  const cloud = () => root.location?.origin === panelOrigin;
  let sessionToken;
  if (cloud()) {
    try { sessionToken = root.sessionStorage.getItem(sessionKey) || undefined; } catch {}
  }

  function remember(token) {
    sessionToken = token;
    try {
      if (token) root.sessionStorage.setItem(sessionKey,token);
      else root.sessionStorage.removeItem(sessionKey);
    } catch {}
  }

  async function request(path,body) {
    if (!local() && !cloud()) throw Error('request-failed');
    const remote = cloud();
    const options = {cache:'no-store',headers:{'Accept':'application/json'}};
    let timer;
    if (typeof AbortController === 'function') {
      const controller = new AbortController();
      options.signal = controller.signal;
      timer = setTimeout(() => controller.abort(),remote ? 80_000 : 8000);
    }
    const previousToken = sessionToken;
    if (remote) {
      options.credentials = 'omit';
      options.redirect = 'error';
      if (previousToken) options.headers.Authorization = 'Bearer ' + previousToken;
    }
    if (body !== undefined) {
      options.method = 'POST';
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    try {
      const response = await fetch((remote ? apiOrigin : '.') + '/api/' + path,options);
      const value = await response.json();
      if (!response.ok) {
        if (remote && path === 'account/session' && response.status === 401 && sessionToken === previousToken) remember();
        throw new Error(value.error ?? 'request-failed');
      }
      if (remote && ['account/register','account/login','account/recover'].includes(path)) {
        if (typeof value.sessionToken !== 'string' || !/^[A-Za-z0-9_-]{16,256}$/.test(value.sessionToken)) {
          throw Error('request-failed');
        }
        remember(value.sessionToken);
        delete value.sessionToken;
      }
      if (remote && (path === 'account/logout' || path === 'account/session' && !value.user) && sessionToken === previousToken) remember();
      return value;
    } finally {
      if (timer !== undefined) clearTimeout(timer);
    }
  }

  root.konturHome = {request,local,cloud};
})(globalThis);
