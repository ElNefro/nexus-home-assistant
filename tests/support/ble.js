'use strict';

(function expose(root) {
  const serviceId = '933f3720-bc7f-4dd1-b631-ab694240dd81';
  const identityId = '933f3721-bc7f-4dd1-b631-ab694240dd81';
  const telemetryId = '933f3722-bc7f-4dd1-b631-ab694240dd81';
  const commandId = '933f3723-bc7f-4dd1-b631-ab694240dd81';

  function parseIdentity(bytes) {
    const value = JSON.parse(new TextDecoder().decode(bytes));
    if (value?.v !== 1 || value.kind !== 'humidifier' ||
        typeof value.id !== 'string' || !/^[0-9a-f]{12}$/.test(value.id)) {
      throw new Error('invalid-identity');
    }
    return value;
  }

  function parseTelemetry(bytes) {
    if (bytes.byteLength !== 5) throw new Error('invalid-telemetry');
    const [version, power, target, humidity, water] = new Uint8Array(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    if (version !== 1 || power > 1 || target < 30 || target > 70 || humidity > 100 || water > 100) {
      throw new Error('invalid-telemetry');
    }
    return {power: Boolean(power), target_humidity: target, humidity, water_level: water};
  }

  function encodeCommand(capability, value) {
    if (capability === 'power' && typeof value === 'boolean') return Uint8Array.of(1, Number(value));
    if (capability === 'target_humidity' && Number.isInteger(value) && value >= 30 && value <= 70) {
      return Uint8Array.of(2, value);
    }
    throw new Error('invalid-command');
  }

  async function choose(bluetooth, expectedId) {
    const peripheral = await bluetooth.requestDevice({filters:[{services:[serviceId]}]});
    try {
      const server = await peripheral.gatt.connect();
      const service = await server.getPrimaryService(serviceId);
      const identity = parseIdentity(await (await service.getCharacteristic(identityId)).readValue());
      if (expectedId && identity.id !== expectedId) throw new Error('wrong-device');
      if (identity.telemetry !== 1) {
        peripheral.gatt.disconnect();
        return {identity};
      }

      const telemetry = await service.getCharacteristic(telemetryId);
      const commands = await service.getCharacteristic(commandId);
      let queue = Promise.resolve();
      function send(capability, value) {
        const bytes = encodeCommand(capability,value);
        const operation = queue.then(async () => {
          if (!peripheral.gatt.connected) throw new Error('disconnected');
          if (commands.writeValueWithResponse) await commands.writeValueWithResponse(bytes);
          else await commands.writeValue(bytes);
          for (let attempt = 0; attempt < 5; attempt += 1) {
            const state = parseTelemetry(await telemetry.readValue());
            if (state[capability] === value) return state;
            await new Promise(resolve => setTimeout(resolve,70));
          }
          throw new Error('not-confirmed');
        });
        queue = operation.catch(() => {});
        return operation;
      }
      await telemetry.startNotifications();
      const state = parseTelemetry(await telemetry.readValue());
      return {
        identity, state, send,
        subscribe(onState, onDisconnect) {
          const onValue = event => {
            try { onState(parseTelemetry(event.target.value)); }
            catch (error) { console.warn('Kontur ignored invalid BLE telemetry',error); }
          };
          const disconnect = () => {
            telemetry.removeEventListener('characteristicvaluechanged',onValue);
            onDisconnect();
          };
          telemetry.addEventListener('characteristicvaluechanged',onValue);
          peripheral.addEventListener('gattserverdisconnected',disconnect,{once:true});
          if (!peripheral.gatt.connected) disconnect();
        },
        disconnect() { peripheral.gatt.disconnect(); }
      };
    } catch (error) {
      if (peripheral.gatt.connected) peripheral.gatt.disconnect();
      throw error;
    }
  }

  const api = {choose, parseIdentity, parseTelemetry, encodeCommand};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.konturBle = api;
})(globalThis);
