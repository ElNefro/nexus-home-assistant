'use strict';

(function expose(root) {
  const rememberedRooms = new Map();

  function sceneState(devices) {
    let light = 0;
    let humidifying = false;
    let purifying = false;
    for (const device of devices) {
      if (!device.online || device.state?.power !== true) continue;
      if (device.kind === 'light') {
        const brightness = device.capabilities?.find(item => item.id === 'brightness' && item.type === 'slider');
        if (brightness && Number.isFinite(device.state.brightness) && brightness.max > brightness.min) {
          const ratio = (device.state.brightness - brightness.min) / (brightness.max - brightness.min);
          light = Math.max(light, Math.min(1, Math.max(0, ratio)));
        } else if (!brightness) {
          light = 1;
        }
      } else if (device.kind === 'humidifier' && device.state.water_level !== 0) {
        humidifying = true;
      } else if (device.kind === 'purifier') {
        purifying = true;
      }
    }
    return {light,humidifying,purifying};
  }

  function apply(visual, state) {
    visual.style.setProperty('--room-light-level',state.light.toFixed(3));
    visual.dataset.humidifying = String(state.humidifying);
    visual.dataset.purifying = String(state.purifying);
  }

  function attach(hero, devices, roomId) {
    const art = hero.querySelector('.room-art');
    if (!art) return;
    const visual = hero.ownerDocument.createElement('div');
    visual.className = 'room-visual';
    visual.setAttribute('aria-hidden','true');
    visual.append(art);
    for (const effect of ['light','mist','air']) {
      const layer = hero.ownerDocument.createElement('div');
      layer.className = 'room-effect room-effect-' + effect;
      visual.append(layer);
    }
    hero.append(visual);
    const state = sceneState(devices);
    const previous = roomId ? rememberedRooms.get(roomId) : undefined;
    if (roomId) {
      rememberedRooms.delete(roomId);
      rememberedRooms.set(roomId,state);
      if (rememberedRooms.size > 64) rememberedRooms.delete(rememberedRooms.keys().next().value);
    }
    const changed = previous && (
      previous.light !== state.light || previous.humidifying !== state.humidifying || previous.purifying !== state.purifying
    );
    if (changed && typeof root.requestAnimationFrame === 'function' &&
        !root.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      apply(visual,previous);
      root.requestAnimationFrame(() => root.requestAnimationFrame(() => apply(visual,state)));
    } else {
      apply(visual,state);
    }
    return visual;
  }

  const api = { attach, sceneState };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.konturRoomScene = api;
})(globalThis);
