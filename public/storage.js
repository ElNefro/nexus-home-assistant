'use strict';

(function expose(root) {
  const kinds = ['humidifier','purifier','climate','light','relay','feeder','other'];
  const transports = ['ble-test','ble','lan','cloud'];
  const periods = [15,30,60,120,240,480,1440];
  const time = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
  const valueId = /^[a-z][a-z0-9_-]{0,39}$/;

  function text(value, limit) {
    return typeof value === 'string' && value.trim() && value.length <= limit;
  }

  function object(value) {
    return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
  }

  function capability(item) {
    return object(item) && typeof item.id === 'string' && valueId.test(item.id) && text(item.label,60) &&
      ['switch','slider','sensor'].includes(item.type) &&
      (item.momentary === undefined || item.type === 'switch' && typeof item.momentary === 'boolean') &&
      (item.unit === undefined || typeof item.unit === 'string' && item.unit.length <= 12) &&
      (item.description === undefined || typeof item.description === 'string' && item.description.length <= 500) &&
      (item.type !== 'slider' || [item.min,item.max,item.step,item.max - item.min].every(Number.isFinite) &&
        item.max > item.min && item.step > 0 && item.step <= item.max - item.min);
  }

  function cleanSchedule(source) {
    if (!object(source)) return undefined;
    const schedule = {};
    if (['manual','auto','interval'].includes(source.mode)) schedule.mode = source.mode;
    for (const key of ['on','off']) {
      if (typeof source[key] === 'string' && time.test(source[key])) schedule[key] = source[key];
    }
    if (periods.includes(source.period)) schedule.period = source.period;
    if (Number.isInteger(source.threshold) && source.threshold >= 1 && source.threshold <= 100) schedule.threshold = source.threshold;
    if (Number.isSafeInteger(source.nextRun) && source.nextRun > 0) schedule.nextRun = source.nextRun;
    return schedule;
  }

  function uniqueItems(items, check) {
    const ids = new Set();
    for (const item of items) {
      if (!check(item) || ids.has(item.id)) return false;
      ids.add(item.id);
    }
    return true;
  }

  function normalize(input,{trustedLive=false}={}) {
    if (!object(input) || !Array.isArray(input.rooms) || !input.rooms.length ||
        !Array.isArray(input.devices) || !object(input.profile) || !text(input.profile.name,60) ||
        !uniqueItems(input.rooms, item => object(item) && text(item.id,128) && text(item.name,60))) {
      throw new Error('invalid-home');
    }
    const roomIds = new Set(input.rooms.map(room => room.id));
    if (!uniqueItems(input.devices, item => object(item) && text(item.id,128) && text(item.name,60) &&
        roomIds.has(item.roomId) && Array.isArray(item.capabilities) && item.capabilities.length <= 20 &&
        uniqueItems(item.capabilities,capability) && object(item.state))) {
      throw new Error('invalid-device');
    }
    return {
      rooms: input.rooms.map(({id,name}) => ({id,name})),
      devices: input.devices.map(device => {
        const transport = transports.includes(device.transport) ? device.transport : undefined;
        const state = Object.fromEntries(device.capabilities.flatMap(item => {
          if (item.momentary === true) return [];
          const value = Object.hasOwn(device.state,item.id) ? device.state[item.id] : undefined;
          if (item.type === 'switch') return [[item.id,typeof value === 'boolean' ? value : false]];
          if (item.type === 'slider') {
            const safeValue = Number.isFinite(value) && value >= item.min && value <= item.max ? value : item.min;
            return [[item.id,safeValue]];
          }
          return Number.isFinite(value) ? [[item.id,value]] : [];
        }));
        return {
          id:device.id, name:device.name, roomId:device.roomId,
          kind:kinds.includes(device.kind) ? device.kind : 'other',
          online:(!transport || trustedLive) && device.online === true,
          lastSeen:trustedLive && Number.isSafeInteger(device.lastSeen) ? device.lastSeen : undefined,
          transport, state, schedule:device.capabilities.some(item =>
            item.id === 'power' && item.momentary === true) ? undefined : cleanSchedule(device.schedule),
          nameIsDefault:device.nameIsDefault === true,
          capabilities:device.capabilities,
          waterNotificationSent:device.waterNotificationSent === true
        };
      }),
      profile:{name:input.profile.name,role:'Админ',customName:input.profile.customName === true},
      roomId:roomIds.has(input.roomId) ? input.roomId : input.rooms[0].id
    };
  }

  function decodeBackup(json) {
    const envelope = JSON.parse(json);
    if (envelope?.version !== 1 || envelope.format !== 'kontur-home') throw new Error('invalid-backup');
    return normalize(envelope.home);
  }

  const api = {normalize, decodeBackup, capability};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.konturStorage = api;
})(globalThis);
