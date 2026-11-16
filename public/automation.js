'use strict';

(function expose(root) {
  const minuteMs = 60_000;
  const freshnessMs = 5 * minuteMs;
  const defaultPeriod = 60;
  const periods = Object.freeze([15,30,60,120,240,480,1440]);
  const legacyTime = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
  const automatic = Object.freeze({
    humidifier:{metric:'humidity',threshold:50},
    purifier:{metric:'air_quality',threshold:85}
  });

  function autoMetric(device) {
    return Object.prototype.hasOwnProperty.call(automatic,device?.kind) ? automatic[device.kind].metric : undefined;
  }

  function supportsMode(device,mode) {
    const hasPower = Array.isArray(device?.capabilities) &&
      device.capabilities.some(item => item?.id === 'power' && item.type === 'switch' && item.momentary !== true);
    return hasPower && (mode === 'interval' || mode === 'auto' && autoMetric(device) !== undefined);
  }

  function validThreshold(value) {
    return Number.isFinite(value) && value >= 1 && value <= 100;
  }

  function autoThreshold(device,schedule) {
    if (validThreshold(schedule?.threshold)) return schedule.threshold;
    if (device?.kind === 'humidifier' && validThreshold(device.state?.target_humidity)) return device.state.target_humidity;
    return autoMetric(device) ? automatic[device.kind].threshold : undefined;
  }

  function measurementValue(measurement,now) {
    const value = typeof measurement === 'number' ? measurement : measurement?.value;
    if (!Number.isFinite(value) || value < 0 || value > 100) return undefined;
    if (typeof measurement === 'number') return value;
    const updatedAt = measurement.updatedAt;
    if (!Number.isSafeInteger(updatedAt) || updatedAt < 0 || updatedAt > now || now - updatedAt > freshnessMs) return undefined;
    return value;
  }

  function periodMs(period) {
    return (periods.includes(period) ? period : defaultPeriod) * minuteMs;
  }

  function planInterval(schedule,now) {
    const period = periodMs(schedule.period);
    const deadline = schedule.nextRun;
    if (!Number.isSafeInteger(deadline) || deadline <= 0 || deadline > now + period) {
      return {nextRun:now + period};
    }
    if (now < deadline) return {};
    const intervals = Math.floor((now - deadline) / period) + 1;
    return {power:true,nextRun:deadline + intervals * period};
  }

  function planLegacy(schedule,now,previousMinute) {
    if (Math.floor(now / minuteMs) === previousMinute) return;
    const at = new Date(now);
    const time = [at.getHours(),at.getMinutes()].map(part => String(part).padStart(2,'0')).join(':');
    if (typeof schedule.off === 'string' && legacyTime.test(schedule.off) && schedule.off === time) return false;
    if (typeof schedule.on === 'string' && legacyTime.test(schedule.on) && schedule.on === time) return true;
  }

  function evaluate(device,schedule,measurements,now,previousMinute) {
    const unchanged = {power:undefined,nextRun:undefined};
    if (!Number.isSafeInteger(now) || now < 0 || now > 8_640_000_000_000_000 ||
        !device?.online || !schedule || !supportsMode(device,'interval')) return unchanged;

    let power;
    let nextRun;
    const mode = schedule.mode;
    if (mode === 'interval') {
      ({power,nextRun} = planInterval(schedule,now));
    } else if (mode === 'auto' && supportsMode(device,mode)) {
      const reading = measurementValue(measurements?.[autoMetric(device)],now);
      if (reading === undefined) return unchanged;
      const threshold = autoThreshold(device,schedule);
      const hysteresis = Number.isFinite(schedule.hysteresis) ? Math.max(0,Math.min(20,schedule.hysteresis)) : 0;
      // With hysteresis enabled, only raise the turn-off point.
      const limit = device.state?.power === true ? Math.min(100,threshold + hysteresis) : threshold;
      power = reading < limit;
    } else if (mode === undefined) {
      power = planLegacy(schedule,now,previousMinute);
    }

    return {power:power === device.state?.power ? undefined : power,nextRun};
  }

  const api = {evaluate,supportsMode,autoMetric,autoThreshold,periodMs,periods,freshnessMs};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.konturAutomation = api;
})(globalThis);
