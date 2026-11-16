'use strict';

(function expose(root) {
  const storageKey = 'kontur-pending-enrollment-1.7';
  const maxAge = 15 * 60_000;
  const fields = ['enroll','setupNonce','deviceId','proof'];
  const ticketPattern = /^[A-Za-z0-9_-]{1,320}\.[A-Za-z0-9_-]{43}$/;
  const noncePattern = /^[0-9a-f]{32}$/;
  const devicePattern = /^[a-z0-9][a-z0-9_-]{0,63}$/;
  const proofPattern = /^[0-9a-f]{64}$/;
  let pending;

  function valid(record) {
    if (typeof record?.deviceId !== 'string' || !devicePattern.test(record.deviceId) ||
        typeof record.proof !== 'string' || !proofPattern.test(record.proof)) return false;
    return record.nonce === undefined
      ? typeof record.ticket === 'string' && ticketPattern.test(record.ticket)
      : record.ticket === undefined && typeof record.nonce === 'string' && noncePattern.test(record.nonce);
  }

  function remember(record) {
    pending = record;
    try {
      if (record) root.sessionStorage.setItem(storageKey,JSON.stringify(record));
      else root.sessionStorage.removeItem(storageKey);
    } catch {}
  }

  function restore() {
    try {
      const record = JSON.parse(root.sessionStorage.getItem(storageKey));
      const age = Date.now() - record?.capturedAt;
      if (valid(record) && Number.isFinite(age) && age >= 0 && age < maxAge) pending = record;
      else if (record) remember();
    } catch {}
  }

  function capture(params) {
    if (!params.has('enroll') && !params.has('setupNonce') && !params.has('proof')) return '';
    const callbackField = params.has('setupNonce') ? 'setupNonce' : 'enroll';
    const required = [callbackField,'deviceId','proof'];
    const record = {
      [callbackField === 'setupNonce' ? 'nonce' : 'ticket']:params.get(callbackField),
      deviceId:params.get('deviceId'),proof:params.get('proof'),capturedAt:Date.now()
    };
    const unambiguous = !params.has('enroll') || !params.has('setupNonce');
    const accepted = unambiguous && required.every(field => params.getAll(field).length === 1) && valid(record);
    if (accepted) remember(record);
    for (const field of fields) params.delete(field);
    if (!params.has('page')) params.set('page','devices');
    const url = new URL(root.location.href);
    url.hash = params.toString();
    root.history.replaceState(root.history.state,'',url);
    return accepted ? 'captured' : 'invalid-enrollment';
  }

  function portal(ticket,locale) {
    if (ticket !== undefined && (typeof ticket !== 'string' || !ticketPattern.test(ticket))) throw Error('invalid-enrollment');
    const url = new URL('http://192.168.4.1/');
    const params = new URLSearchParams({lang:locale === 'en' ? 'en' : 'ru'});
    if (ticket !== undefined) params.set('ticket',ticket);
    url.hash = params.toString();
    return url.href;
  }

  const clear = record => { if (record === pending) remember(); };

  restore();
  root.konturEnrollment = {capture,portal,clear,get pending() { return pending; }};
})(globalThis);
