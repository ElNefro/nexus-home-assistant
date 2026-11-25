'use strict';

(function expose(root) {
  const origins = ['http://127.0.0.1:8080','http://localhost:8080'];
  const validId = /^[a-z0-9][a-z0-9_-]{0,63}$/;
  const available = () => origins.includes(root.location?.origin);

  async function request(route,body) {
    if (!available()) throw new Error('local-service');
    const timeout = route === 'discovery' ? 14000 : route === 'command' ? 8000 : 5000;
    const options = {cache:'no-store',signal:AbortSignal.timeout(timeout)};
    if (body !== undefined) {
      options.method = 'POST';
      options.headers = {'Content-Type':'application/json'};
      options.body = JSON.stringify(body);
    }
    const response = await fetch('./api/' + route,options);
    if (response.status === 401) throw new Error('device-token');
    if (!response.ok) throw new Error('device-unavailable');
    return response.json();
  }

  function acceptManifest(item) {
    if (!item || typeof item.id !== 'string' || !validId.test(item.id) ||
        typeof item.kind !== 'string' || !/^[a-z][a-z0-9_-]{0,39}$/.test(item.kind)) return false;
    if (item.requiresPassword !== undefined && typeof item.requiresPassword !== 'boolean' ||
        item.requiresPassword === true && item.requiresToken !== false) return false;
    if (item.legacy === true) return true;
    if (item.schemaVersion !== 1 || typeof item.name !== 'string' || !item.name.trim() ||
        item.name.length > 60 || !Array.isArray(item.capabilities) || !item.capabilities.length ||
        item.capabilities.length > 20 || !item.capabilities.every(root.konturStorage.capability) ||
        !root.konturStorage.deviceMetadata(item)) return false;
    return new Set(item.capabilities.map(capability => capability.id)).size === item.capabilities.length;
  }

  async function discover() {
    const {devices} = await request('discovery');
    return devices.filter(acceptManifest);
  }

  async function pair(deviceId,secret,requiresPassword = false) {
    const credentials = requiresPassword ? {password:secret} : {token:secret};
    const {session,manifest,state} = await request('pair',{id:deviceId,...credentials});
    if (typeof session !== 'string' || !acceptManifest(manifest) || manifest.legacy || manifest.id !== deviceId ||
        !state || typeof state !== 'object' || Array.isArray(state)) throw new Error('device-response');
    const actionIds = new Set(manifest.capabilities.filter(item => item.momentary === true).map(item => item.id));
    function readings(values) {
      if (!actionIds.size) return values;
      return Object.fromEntries(Object.entries(values).filter(([id]) => !actionIds.has(id)));
    }
    return {
      manifest,state:readings(state),
      async read() {
        const result = await request('state',{session});
        return readings(result.state);
      },
      async send(capability,value) {
        const {state:confirmed} = await request('command',{session,capability,value});
        if (confirmed[capability] !== value) throw new Error('not-confirmed');
        return readings(confirmed);
      }
    };
  }

  root.konturLan = {available,discover,pair,acceptManifest};
})(globalThis);
