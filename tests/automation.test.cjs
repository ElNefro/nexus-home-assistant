'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {evaluate,supportsMode,autoMetric,autoThreshold,periodMs,freshnessMs} = require('../public/automation.js');

const now = new Date(2026,10,15,12,34).getTime();
const currentMinute = Math.floor(now / 60_000);
const noChange = {power:undefined,nextRun:undefined};
function powerDevice(kind,power=false) {
  return {kind,online:true,state:{power},capabilities:[{type:'switch',id:'power'}]};
}

test('only a writable power switch enables scheduling, and only climate appliances have auto mode',() => {
  for (const kind of ['humidifier','purifier','relay','light','other']) {
    assert.equal(supportsMode(powerDevice(kind),'interval'),true);
  }
  assert.equal(supportsMode(powerDevice('relay'),'auto'),false);
  assert.equal(supportsMode(powerDevice('light'),'auto'),false);
  assert.equal(supportsMode({kind:'humidifier',capabilities:[{id:'power',type:'sensor'}]},'interval'),false);
  assert.equal(autoMetric(powerDevice('humidifier')),'humidity');
  assert.equal(autoMetric(powerDevice('purifier')),'air_quality');
  assert.equal(autoMetric({kind:'toString'}),undefined);
});

test('interval initializes a missing or invalid deadline and does not fire early',() => {
  const device = powerDevice('light');
  const schedule = {mode:'interval',period:-15,nextRun:Infinity};
  assert.deepEqual(evaluate(device,schedule,null,now),{power:undefined,nextRun:now + periodMs(60)});
  schedule.nextRun = now + 10_000;
  assert.deepEqual(evaluate(device,schedule,null,now),noChange);
  schedule.nextRun = now + periodMs(1440) * 2;
  assert.deepEqual(evaluate(device,schedule,null,now),{power:undefined,nextRun:now + periodMs(60)});
});

test('missed intervals collapse to one start and preserve cadence after browser sleep',() => {
  const schedule = Object.freeze({mode:'interval',period:15,nextRun:now - periodMs(15) * 4 + 4_000});
  const device = Object.freeze(powerDevice('relay'));
  assert.deepEqual(evaluate(device,schedule,null,now),{power:true,nextRun:now + 4_000});
  assert.deepEqual(evaluate(device,schedule,null,now + 4_000),{power:true,nextRun:now + 4_000 + periodMs(15)});
  assert.deepEqual(evaluate(powerDevice('relay',true),schedule,null,now),{power:undefined,nextRun:now + 4_000});
});

test('humidifier starts below its target and stops at the exact threshold by default',() => {
  const device = powerDevice('humidifier');
  device.state.target_humidity = 55;
  const schedule = {mode:'auto'};
  assert.equal(autoThreshold(device,schedule),55);
  assert.deepEqual(evaluate(device,schedule,{humidity:54.9},now),{...noChange,power:true});
  device.state.power = true;
  assert.deepEqual(evaluate(device,schedule,{humidity:55},now),{...noChange,power:false});
  assert.deepEqual(evaluate(device,{...schedule,hysteresis:2},{humidity:55},now),noChange);
  assert.deepEqual(evaluate(device,{...schedule,hysteresis:2},{humidity:57},now),{...noChange,power:false});
});

test('purifier automates from air quality alone and rejects missing, invalid and stale measurements',() => {
  const purifier = powerDevice('purifier');
  const schedule = {mode:'auto'};
  assert.equal(autoThreshold(purifier,schedule),85);
  assert.deepEqual(evaluate(purifier,schedule,{air_quality:{value:80,updatedAt:now - freshnessMs}},now),{...noChange,power:true});
  for (const air_quality of [null,NaN,Infinity,-1,101,'75',{value:80,updatedAt:now + 1},{value:80,updatedAt:now - freshnessMs - 1}]) {
    assert.deepEqual(evaluate(purifier,schedule,{humidity:40,air_quality},now),noChange);
  }
});

test('legacy daily schedules run only once per minute and explicit mode overrides them',() => {
  const device = powerDevice('relay',true);
  const existing = {off:'12:34',on:'12:34'};
  assert.deepEqual(evaluate(device,existing,null,now,currentMinute - 1),{...noChange,power:false});
  assert.deepEqual(evaluate(device,existing,null,now,currentMinute),noChange);
  assert.deepEqual(evaluate(device,{...existing,mode:'manual'},null,now,currentMinute - 1),noChange);
  assert.deepEqual(evaluate(device,{...existing,mode:'auto'},null,now,currentMinute - 1),noChange);
});

test('offline devices and non-finite clocks do not execute automation',() => {
  const schedule = {mode:'interval',nextRun:now};
  assert.deepEqual(evaluate({...powerDevice('light'),online:false},schedule,null,now),noChange);
  assert.deepEqual(evaluate(powerDevice('light'),schedule,null,Infinity),noChange);
});
