'use strict';

(function expose(root) {
  const base = '7ac2';
  const uuid = number => base + number + '-9f61-4e58-9d61-67d571076102';
  const serviceId = uuid('0001');
  const infoId = uuid('0002');
  const secretsId = uuid('0003');
  const statusId = uuid('0004');
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();

  function available() {
    return root.isSecureContext && Boolean(root.navigator?.bluetooth && root.crypto?.subtle);
  }

  const toHex = bytes => Array.from(bytes,byte => byte.toString(16).padStart(2,'0')).join('');
  const fromHex = value => Uint8Array.from(value.match(/.{2}/g),part => parseInt(part,16));
  const delay = milliseconds => new Promise(resolve => setTimeout(resolve,milliseconds));

  async function packet(key,nonce,ssid,password) {
    const subtle = root.crypto.subtle;
    const hmac = {name:'HMAC',hash:'SHA-256'};
    const master = await subtle.importKey('raw',fromHex(key),hmac,false,['sign']);
    const encryptionBytes = await subtle.sign('HMAC',master,encoder.encode('kontur:enc:' + nonce));
    const authenticationBytes = await subtle.sign('HMAC',master,encoder.encode('kontur:mac:' + nonce));
    const encryption = await subtle.importKey('raw',encryptionBytes,'AES-CBC',false,['encrypt']);
    const authentication = await subtle.importKey('raw',authenticationBytes,hmac,false,['sign']);
    const iv = root.crypto.getRandomValues(new Uint8Array(16));
    const plaintext = encoder.encode(JSON.stringify({ssid,password}));
    const data = new Uint8Array(await subtle.encrypt({name:'AES-CBC',iv},encryption,plaintext));
    const signed = new Uint8Array(iv.length + data.length);
    signed.set(iv);
    signed.set(data,iv.length);
    const mac = new Uint8Array(await subtle.sign('HMAC',authentication,signed));
    return encoder.encode(JSON.stringify({iv:toHex(iv),data:toHex(data),mac:toHex(mac)}) + '\n');
  }

  async function provision({setupKey,ssid,password},onStage) {
    if (!available()) throw Error('ble-unavailable');
    if (!/^[0-9a-f]{64}$/.test(setupKey)) throw Error('invalid-setup-key');
    const ssidLength = encoder.encode(ssid).length;
    const passwordLength = encoder.encode(password).length;
    if (!ssidLength || ssidLength > 32 || passwordLength > 64) throw Error('invalid-wifi');
    let peripheral;
    try {
      peripheral = await root.navigator.bluetooth.requestDevice({filters:[{services:[serviceId]}]});
      onStage('transfer');
      const server = await peripheral.gatt.connect();
      const service = await server.getPrimaryService(serviceId);
      const info = await service.getCharacteristic(infoId);
      const secrets = await service.getCharacteristic(secretsId);
      const status = await service.getCharacteristic(statusId);
      if (decoder.decode(await status.readValue()) !== 'ready') throw Error('ble-not-ready');
      const {id,name,nonce} = JSON.parse(decoder.decode(await info.readValue()));
      if (typeof id !== 'string' || !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(id) ||
          typeof name !== 'string' || !name.trim() || name.length > 60 ||
          typeof nonce !== 'string' || !/^[0-9a-f]{32}$/.test(nonce)) throw Error('ble-response');
      const bytes = await packet(setupKey,nonce,ssid,password);
      for (let offset = 0; offset < bytes.length; offset += 20) {
        const chunk = bytes.slice(offset,offset + 20);
        if (typeof secrets.writeValueWithResponse === 'function') await secrets.writeValueWithResponse(chunk);
        else await secrets.writeValue(chunk);
      }
      onStage('wifi');
      const deadline = Date.now() + 45_000;
      while (Date.now() < deadline) {
        const current = decoder.decode(await status.readValue());
        if (current === 'connected') return {id,name};
        if (current === 'invalid-code') throw Error('device-token');
        if (current === 'wifi-error') throw Error('wifi-error');
        if (current !== 'ready' && current !== 'connecting') throw Error('ble-response');
        await delay(500);
      }
      throw Error('wifi-error');
    } finally {
      if (peripheral?.gatt?.connected) peripheral.gatt.disconnect();
    }
  }

  root.konturProvisioning = {available,provision};
})(globalThis);
