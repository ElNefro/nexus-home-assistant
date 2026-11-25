'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const projector = require('../public/projector.js');

const colorActions = {red_toggle:'r',green_toggle:'g',blue_toggle:'b',white_toggle:'w'};

function projectorColorAfter(current,action) {
  const channel = colorActions[action];
  assert.ok(channel,'only declared color commands may be sent');
  const next = {...current};
  const count = Object.values(current).filter(Boolean).length;
  if (current[channel]) {
    // Observed on the real unit: pure white does not turn off with the W key.
    if (count > 1) next[channel] = false;
  } else {
    assert.ok(count < 2,'the projector cannot add a third simultaneous color');
    next[channel] = true;
  }
  return next;
}

test('projector profile only applies to one-way devices declaring all required momentary actions',() => {
  const device = {commandFeedback:'transmitted',capabilities:[...Object.keys(colorActions),'laser_toggle'].map(id =>
    ({id,type:'switch',momentary:true}))};
  assert.equal(projector.supports(device),true);
  assert.equal(projector.supports({...device,commandFeedback:undefined}),false);
  assert.equal(projector.supports({...device,capabilities:device.capabilities.slice(0,-1)}),false);
  assert.equal(projector.supports({...device,capabilities:device.capabilities.map(cap =>
    cap.id === 'red_toggle' ? {...cap,momentary:false} : cap)}),false);
  assert.equal(projector.supports(null),false);
});

test('all 49 palette transitions keep one or two colors lit despite the last-color-off guard',() => {
  for (const from of projector.palettes) {
    for (const to of projector.palettes) {
      const current = {...from.channels};
      const plan = projector.colorPlan(current,to.id);
      let simulated = {...current};
      for (const action of plan.actions) {
        simulated = projectorColorAfter(simulated,action);
        assert.ok(Object.values(simulated).filter(Boolean).length >= 1,from.id + ' → ' + to.id);
        assert.ok(Object.values(simulated).filter(Boolean).length <= 2,from.id + ' → ' + to.id);
      }
      assert.deepEqual(simulated,to.channels,from.id + ' → ' + to.id);
      assert.deepEqual(current,from.channels,'planning must not mutate the starting color');
      assert.deepEqual(plan.next,to.channels);
      assert.equal(new Set(plan.actions).size,plan.actions.length);
      assert.equal(plan.actions.length,Object.keys(current).filter(key => current[key] !== to.channels[key]).length);
    }
  }
});

test('single-color replacement lights the new channel before removing the old one',() => {
  const color = id => projector.palettes.find(item => item.id === id).channels;
  assert.deepEqual(projector.colorPlan(color('white'),'purple').actions,
    ['red_toggle','white_toggle','blue_toggle']);
  assert.deepEqual(projector.colorPlan(color('red'),'blue').actions,
    ['blue_toggle','red_toggle']);
  assert.deepEqual(projector.colorPlan(color('blue'),'white').actions,
    ['white_toggle','blue_toggle']);
  assert.deepEqual(projector.colorPlan(color('purple'),'cyan').actions,
    ['red_toggle','green_toggle']);
  assert.deepEqual(projector.colorPlan(color('yellow'),'purple').actions,
    ['green_toggle','blue_toggle']);
  assert.deepEqual(projector.colorPlan(color('purple'),'purple').actions,[]);
});

test('manual dark or excessive mixtures converge without adding a third color or pretending to switch power off',() => {
  const keys = ['r','g','b','w'];
  for (let mask = 0; mask < 16; mask++) {
    const current = Object.fromEntries(keys.map((key,index) => [key,Boolean(mask & (1 << index))]));
    for (const preset of projector.palettes) {
      let simulated = {...current};
      const plan = projector.colorPlan(current,preset.id);
      for (const action of plan.actions) {
        const beforeCount = Object.values(simulated).filter(Boolean).length;
        simulated = projectorColorAfter(simulated,action);
        const afterCount = Object.values(simulated).filter(Boolean).length;
        assert.ok(afterCount >= 1);
        assert.ok(beforeCount > 2 ? afterCount === beforeCount - 1 : afterCount <= 2);
      }
      assert.deepEqual(simulated,preset.channels,'manual mixture ' + mask + ' → ' + preset.id);
      assert.deepEqual(plan.next,preset.channels);
    }
  }
  const white = projector.palettes.find(item => item.id === 'white').channels;
  assert.throws(() => projector.colorPlan(white,{r:false,g:false,b:false,w:false}),
    {message:'unsupported-color-off'});
  assert.throws(() => projector.colorPlan(white,{r:true,g:true,b:true,w:false}),{message:'invalid-color'});
});

test('unknown colors require synchronization and plans do not retain mutable palette references',() => {
  assert.throws(() => projector.colorPlan(null,'red'),{message:'sync-needed'});
  assert.throws(() => projector.colorPlan({},'red'),{message:'sync-needed'});
  assert.throws(() => projector.colorPlan(projector.palettes[0].channels,'orange'),{message:'invalid-color'});
  const requested = {...projector.palettes[0].channels};
  const plan = projector.colorPlan(projector.palettes[6].channels,requested);
  plan.next.g = true;
  assert.equal(requested.g,false);
  assert.equal(projector.palettes[0].channels.g,false);
  assert.equal(projector.colorId(requested),'red');
  assert.equal(projector.colorId({r:false,g:false,b:false,w:false}),null);
  assert.equal(projector.colorId(null),null);
});

test('laser power is idempotent and every unknown state needs synchronization',() => {
  for (const target of ['off','on','steady','pulse']) {
    assert.throws(() => projector.laserPlan('unknown',target),{message:'sync-needed'});
  }
  assert.deepEqual(projector.laserPlan('off','off'),{actions:[],next:'off'});
  for (const active of ['on','steady','pulse']) {
    assert.deepEqual(projector.laserPlan(active,'off'),{actions:['laser_toggle'],next:'off'});
    assert.deepEqual(projector.laserPlan(active,'on'),{actions:[],next:active});
  }
  assert.deepEqual(projector.laserPlan('off','on'),{actions:['laser_steady'],next:'steady'});
  assert.throws(() => projector.laserPlan('unknown','automatic'),{message:'invalid-laser'});
});

test('pulsation always starts from the verified steady state',() => {
  assert.deepEqual(projector.laserPlan('steady','pulse'),{actions:['laser_pulse'],next:'pulse'});
  assert.deepEqual(projector.laserPlan('pulse','pulse'),{actions:[],next:'pulse'});
  assert.deepEqual(projector.laserPlan('off','pulse'),{actions:['laser_steady','laser_pulse'],next:'pulse'});
  assert.deepEqual(projector.laserPlan('on','pulse'),{actions:['laser_toggle','laser_steady','laser_pulse'],next:'pulse'});
  assert.deepEqual(projector.laserPlan('pulse','steady'),{actions:['laser_toggle','laser_steady'],next:'steady'});
  assert.deepEqual(projector.laserPlan('on','steady'),{actions:['laser_toggle','laser_steady'],next:'steady'});
  assert.deepEqual(projector.laserPlan('steady','steady'),{actions:[],next:'steady'});
});

test('native LED power turns lighting and laser off and restores a remembered pulse without mutating state',() => {
  const original = {color:{...projector.palettes.find(item => item.id === 'purple').channels},laser:'pulse'};
  const saved = JSON.parse(JSON.stringify(original));
  const off = projector.powerPlan(original,'off');
  assert.deepEqual(off.actions,['light_toggle','laser_toggle']);
  assert.equal(off.next.lighting,'off');
  assert.equal(off.next.laser,'off');
  assert.deepEqual(off.next.color,original.color);
  assert.deepEqual(off.next.resume,{color:original.color,laser:'pulse'});
  assert.notEqual(off.next.color,original.color);
  assert.notEqual(off.next.resume.color,original.color);
  assert.notEqual(off.next.resume.color,off.next.color);
  const on = projector.powerPlan(off.next,'on');
  assert.deepEqual(on.actions,['light_toggle','laser_steady','laser_pulse']);
  assert.equal(on.next.lighting,'on');
  assert.equal(on.next.laser,'pulse');
  assert.deepEqual(on.next.color,original.color);
  assert.deepEqual(original,saved);
  assert.equal(off.next.lighting,'off');
  on.next.color.r = false;
  on.next.resume.color.b = false;
  assert.equal(off.next.color.r,true);
  assert.equal(off.next.resume.color.b,true);
});

test('repeated whole-light power selections are idempotent and do not overwrite the remembered settings',() => {
  const original = {color:{...projector.palettes[0].channels},laser:'steady'};
  const off = projector.powerPlan(original,'off');
  const again = projector.powerPlan(off.next,'off');
  assert.deepEqual(again.actions,[]);
  assert.deepEqual(again.next.resume,off.next.resume);
  const on = projector.powerPlan(off.next,'on');
  assert.deepEqual(on.actions,['light_toggle','laser_steady']);
  assert.deepEqual(projector.powerPlan(on.next,'on').actions,[]);
  // Turning LED lighting on must not toggle or replace a laser already active.
  const activeLaser = {...off.next,laser:'pulse'};
  const restored = projector.powerPlan(activeLaser,'on');
  assert.deepEqual(restored.actions,['light_toggle']);
  assert.equal(restored.next.laser,'pulse');
});

test('whole-light power needs synchronized lighting and laser and does not invent a color from a dark observation',() => {
  const dark = {r:false,g:false,b:false,w:false};
  const red = {...projector.palettes[0].channels};
  assert.equal(projector.lightingState({color:red}),'on');
  assert.equal(projector.lightingState({color:dark}),'off');
  assert.equal(projector.lightingState({color:red,lighting:'off'}),'off');
  assert.equal(projector.lightingState({color:red,lighting:'unknown'}),'unknown');
  assert.equal(projector.lightingState({color:red,lighting:'invalid'}),'unknown');
  assert.equal(projector.lightingState({color:null}),'unknown');
  for (const state of [null,{color:null,laser:'off'},{color:red,laser:'unknown'},
    {color:red,lighting:'unknown',laser:'off'}]) {
    for (const target of ['on','off']) {
      assert.throws(() => projector.powerPlan(state,target),{message:'sync-needed'});
    }
  }
  assert.throws(() => projector.powerPlan({color:red,laser:'off'},'toggle'),{message:'invalid-power'});
  assert.deepEqual(projector.powerPlan({color:dark,laser:'off'},'off').actions,[]);
  const lit = projector.powerPlan({color:dark,laser:'off'},'on');
  assert.deepEqual(lit.actions,['light_toggle']);
  assert.equal(lit.next.color,null,'the native switch may restore any old color after a dark observation');
  assert.equal(lit.next.laser,'off');
});

test('manual actions and uncertain effects invalidate only the affected assumptions',() => {
  const known = {color:{...projector.palettes[0].channels},laser:'pulse'};
  assert.deepEqual(projector.invalidate(known,'red_toggle'),{color:null,laser:'pulse'});
  assert.deepEqual(projector.invalidate(known,'purple_from_white'),{color:null,laser:'pulse'});
  assert.deepEqual(projector.invalidate(known,'laser_toggle'),{color:known.color,laser:'unknown'});
  for (const action of ['color_jump','color_fade',undefined,'unrecognized']) {
    assert.deepEqual(projector.invalidate(known,action),{color:null,laser:'unknown'});
  }
  assert.deepEqual(projector.initial(),{color:null,laser:'unknown'});
  assert.deepEqual(projector.initial(),projector.invalidate());
  assert.equal(known.laser,'pulse');
  assert.equal(known.color.r,true);
});
