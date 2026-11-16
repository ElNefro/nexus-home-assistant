'use strict';

(function expose(root) {
  const panelOrigin = 'https://nexus-home-assistant.onrender.com';
  const apiOrigin = 'https://nexus-home-assistant-api.onrender.com';
  const maxWait = 32_000;
  const validCommandId = /^[a-zA-Z0-9_-]{1,128}$/;
  let ownerCode;
  let connecting = false;

  const available = () => root.location?.origin === panelOrigin;
  const connected = () => available() && Boolean(ownerCode);
  const wait = milliseconds => new Promise(resolve => setTimeout(resolve,milliseconds));

  async function request(path,code,body,timeout) {
    if (!available() || !code) throw Error('cloud-sign-in');
    const headers = {'Authorization':'Bearer ' + code, 'Accept':'application/json'};
    const options = {
      headers, credentials:'omit', cache:'no-store', redirect:'error',
      signal:AbortSignal.timeout(timeout)
    };
    if (body !== undefined) {
      options.method = 'POST';
      headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const response = await fetch(apiOrigin + '/api/cloud/' + path,options);
    if (response.status === 401 || response.status === 403) {
      if (ownerCode === code) ownerCode = undefined;
      throw Error('cloud-sign-in');
    }
    if (response.status === 409) throw Error('cloud-offline');
    if (!response.ok) throw Error('cloud-unavailable');
    return response.json();
  }

  async function devices(code=ownerCode,timeout=12_000) {
    const result = await request('devices',code,undefined,timeout);
    if (!Array.isArray(result?.devices)) throw Error('cloud-unavailable');
    const ids = new Set();
    return result.devices.flatMap(item => {
      const {manifest,online} = item ?? {};
      if (manifest?.legacy || !root.konturLan.acceptManifest(manifest) || ids.has(manifest.id)) return [];
      const capabilities = manifest.capabilities.filter(item => item.type === 'switch' && item.momentary === true)
        .map(({id,label,description}) => ({id,label,description,type:'switch',momentary:true}));
      if (!capabilities.length) return [];
      ids.add(manifest.id);
      return [{id:manifest.id,name:manifest.name.trim(),kind:manifest.kind,capabilities,online:online === true}];
    });
  }

  async function connect(code) {
    if (connecting || typeof code !== 'string' || !/^[!-~]{1,256}$/.test(code)) throw Error('cloud-sign-in');
    connecting = true;
    try {
      const result = await devices(code,75_000);
      ownerCode = code;
      return result;
    } finally {
      connecting = false;
    }
  }

  function disconnect() { ownerCode = undefined; }

  async function command(deviceId,capability) {
    const code = ownerCode;
    const result = await request('commands',code,{deviceId,capability,value:true},12_000);
    if (typeof result?.id !== 'string' || !validCommandId.test(result.id)) throw Error('cloud-unconfirmed');
    const deadline = Date.now() + maxWait;
    while (ownerCode === code && Date.now() < deadline) {
      await wait(750);
      if (ownerCode !== code) break;
      let current;
      try {
        current = await request('commands/' + result.id,code,undefined,5_000);
      } catch (error) {
        if (error.message === 'cloud-sign-in') throw error;
        continue;
      }
      if (current?.status === 'applied') return;
      if (current?.status !== 'pending') throw Error('cloud-unconfirmed');
    }
    throw Error('cloud-unconfirmed');
  }

  root.konturCloud = {available,connected,connect,disconnect,devices,command};
})(globalThis);
