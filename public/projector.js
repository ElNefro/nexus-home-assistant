'use strict';

(function expose(root) {
  const channels = ['r','g','b','w'];
  const actions = {r:'red_toggle',g:'green_toggle',b:'blue_toggle',w:'white_toggle'};
  const laserStates = ['unknown','off','on','steady','pulse'];
  const palettes = Object.freeze([
    {id:'red',label:'Красный',hex:'#ff516e',channels:{r:true,g:false,b:false,w:false}},
    {id:'yellow',label:'Жёлтый',hex:'#ffd76a',channels:{r:true,g:true,b:false,w:false}},
    {id:'green',label:'Зелёный',hex:'#57efac',channels:{r:false,g:true,b:false,w:false}},
    {id:'cyan',label:'Голубой',hex:'#63e6f4',channels:{r:false,g:true,b:true,w:false}},
    {id:'blue',label:'Синий',hex:'#718bff',channels:{r:false,g:false,b:true,w:false}},
    {id:'purple',label:'Фиолетовый',hex:'#d47eff',channels:{r:true,g:false,b:true,w:false}},
    {id:'white',label:'Белый',hex:'#f5f0ff',channels:{r:false,g:false,b:false,w:true}}
  ].map(item => Object.freeze({...item,channels:Object.freeze(item.channels)})));

  function isColor(value) {
    return Boolean(value) && typeof value === 'object' && !Array.isArray(value) &&
      channels.every(channel => Object.prototype.hasOwnProperty.call(value,channel) &&
        typeof value[channel] === 'boolean') && Object.keys(value).length === channels.length;
  }

  function copyColor(value) {
    return Object.fromEntries(channels.map(channel => [channel,value[channel]]));
  }

  function initial() {
    return {color:null,laser:'unknown'};
  }

  function supports(device) {
    if (device?.commandFeedback !== 'transmitted' || !Array.isArray(device.capabilities)) return false;
    return [...Object.values(actions),'laser_toggle'].every(id => device.capabilities.some(capability =>
      capability.id === id && capability.type === 'switch' && capability.momentary === true));
  }

  function colorId(value) {
    if (!isColor(value)) return null;
    return palettes.find(item => channels.every(channel => item.channels[channel] === value[channel]))?.id ?? null;
  }

  // This is a plan from a manually synchronized or last-requested color, not telemetry.
  function colorPlan(current,target) {
    const requested = typeof target === 'string' ? palettes.find(item => item.id === target)?.channels : target;
    if (!isColor(requested)) throw new Error('invalid-color');
    if (!isColor(current)) throw new Error('sync-needed');
    const desired = channels.filter(channel => requested[channel]);
    if (!desired.length) throw new Error('unsupported-color-off');
    if (desired.length > 2) throw new Error('invalid-color');
    const active = new Set(channels.filter(channel => current[channel]));
    const planned = [];
    const toggle = channel => {
      planned.push(actions[channel]);
      if (active.has(channel)) active.delete(channel);
      else active.add(channel);
    };

    // The projector can ignore turning off its last color, and only supports two
    // colors together. Keep an illuminated anchor while replacing the channels.
    // A manually entered mix may contain more than two colors: remove extras
    // before adding anything, without discarding a requested channel.
    while (active.size > 2) {
      toggle(channels.find(channel => active.has(channel) && !requested[channel]));
    }
    const common = desired.find(channel => active.has(channel));
    if (common) {
      for (const channel of channels) {
        if (active.has(channel) && !requested[channel]) toggle(channel);
      }
      for (const channel of desired) {
        if (!active.has(channel)) toggle(channel);
      }
    } else if (active.size) {
      const anchor = channels.find(channel => active.has(channel));
      for (const channel of channels) {
        if (active.has(channel) && channel !== anchor) toggle(channel);
      }
      toggle(desired[0]);
      toggle(anchor);
      for (const channel of desired.slice(1)) toggle(channel);
    } else {
      for (const channel of desired) toggle(channel);
    }
    return {actions:planned,next:copyColor(requested)};
  }

  // 5E is verified from off/steady; 5A starts pulsing from steady on this BL-XK01.
  function laserPlan(current,target) {
    if (!laserStates.includes(current) || !['off','on','steady','pulse'].includes(target)) {
      throw new Error('invalid-laser');
    }
    if (current === 'unknown') throw new Error('sync-needed');
    if (target === 'off') {
      return {actions:current === 'off' ? [] : ['laser_toggle'],next:'off'};
    }
    if (target === 'on') {
      return current === 'off'
        ? {actions:['laser_steady'],next:'steady'} : {actions:[],next:current};
    }
    if (target === 'steady') {
      return {actions:current === 'steady' ? [] : current === 'off'
        ? ['laser_steady'] : ['laser_toggle','laser_steady'],next:'steady'};
    }
    if (current === 'pulse') return {actions:[],next:'pulse'};
    return {actions:current === 'steady' ? ['laser_pulse'] : current === 'off'
      ? ['laser_steady','laser_pulse'] : ['laser_toggle','laser_steady','laser_pulse'],next:'pulse'};
  }

  function lightingState(state) {
    if (state?.lighting !== undefined) {
      return ['on','off','unknown'].includes(state.lighting) ? state.lighting : 'unknown';
    }
    if (!isColor(state?.color)) return 'unknown';
    return channels.some(channel => state.color[channel]) ? 'on' : 'off';
  }

  // LED power is a separate native command; color buttons cannot turn off the
  // last illuminated channel. The result describes requested settings only.
  function powerPlan(state,target) {
    if (!['on','off'].includes(target)) throw new Error('invalid-power');
    const lighting = lightingState(state);
    if (lighting === 'unknown' || !laserStates.includes(state?.laser) || state.laser === 'unknown') {
      throw new Error('sync-needed');
    }
    const color = isColor(state.color) ? copyColor(state.color) : null;
    const resume = state.resume ? {
      color:isColor(state.resume.color) ? copyColor(state.resume.color) : null,
      laser:state.resume.laser
    } : undefined;
    const next = {...state,color,lighting,laser:state.laser};
    if (resume) next.resume = resume;

    if (target === 'off') {
      if (lighting === 'off' && state.laser === 'off') return {actions:[],next};
      const laser = laserPlan(state.laser,'off');
      const planned = lighting === 'on' ? ['light_toggle',...laser.actions] : laser.actions;
      next.lighting = 'off';
      next.laser = 'off';
      // Preserve an already recorded color when only the remaining laser is on.
      next.resume = {color:color && channels.some(channel => color[channel])
        ? copyColor(color) : resume?.color ?? null,laser:state.laser};
      return {actions:planned,next};
    }

    if (lighting === 'on') return {actions:[],next};
    const planned = ['light_toggle'];
    if (state.laser === 'off') {
      const laser = laserPlan('off',resume?.laser ?? 'off');
      planned.push(...laser.actions);
      next.laser = laser.next;
    }
    next.lighting = 'on';
    // A dark observation alone does not reveal what the native LED switch will
    // restore. Retain a known lit color, otherwise ask for synchronization later.
    const rememberedColor = resume?.color ?? color;
    next.color = rememberedColor && channels.some(channel => rememberedColor[channel])
      ? copyColor(rememberedColor) : null;
    return {actions:planned,next};
  }

  function invalidate(state,action) {
    const next = {
      color:isColor(state?.color) ? copyColor(state.color) : null,
      laser:laserStates.includes(state?.laser) ? state.laser : 'unknown'
    };
    if (Object.values(actions).includes(action) || action === 'purple_from_white') {
      next.color = null;
    } else if (['laser_toggle','laser_steady','laser_pulse'].includes(action)) {
      next.laser = 'unknown';
    } else {
      // Automatic color effects may also affect the laser (44 has done so).
      next.color = null;
      next.laser = 'unknown';
    }
    return next;
  }

  const api = {palettes,initial,supports,isColor,colorId,colorPlan,laserPlan,lightingState,powerPlan,invalidate};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.konturProjector = api;
})(globalThis);
