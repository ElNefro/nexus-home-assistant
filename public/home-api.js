'use strict';

(function setup(root) {
  const api = {};
  async function request(path,body) {
    const options = {cache:'no-store',signal:AbortSignal.timeout(8000)};
    if (body !== undefined) {
      options.method = 'POST';
      options.headers = {'Content-Type':'application/json'};
      options.body = JSON.stringify(body);
    }
    const response = await fetch('./api/' + path,options);
    const value = await response.json();
    if (!response.ok) throw new Error(value.error ?? 'request-failed');
    return value;
  }
  const local = () => ['http://127.0.0.1:8080','http://localhost:8080'].includes(location.origin);
  Object.assign(api,{request,local});
  root.konturHome = api;
})(globalThis);
