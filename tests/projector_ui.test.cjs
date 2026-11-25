'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const model = require('../public/projector.js');

class Element {
  constructor(tag,className = '',text = '') {
    Object.assign(this,{tag,text,children:[],events:{},attributes:{},disabled:false,hidden:false});
    const classes = new Set(className.split(/\s+/).filter(Boolean));
    this.classList = {add:(...values) => values.forEach(value => classes.add(value)),contains:value => classes.has(value)};
    this.style = {setProperty() {}};
  }
  append(...children) { this.children.push(...children); }
  insertBefore(child,anchor) { this.children.splice(this.children.indexOf(anchor),0,child); }
  setAttribute(key,value) { this.attributes[key] = String(value); }
  addEventListener(type,handler) { this.events[type] = handler; }
  querySelector(selector) {
    if (selector === '[type=submit]') return descendants(this).find(child => child.type === 'submit');
    throw new Error('Unsupported test selector: ' + selector);
  }
  focus() {}
}

function descendants(root) {
  return [root,...root.children.flatMap(child => child instanceof Element ? descendants(child) : [])];
}

const flush = () => new Promise(resolve => setImmediate(resolve));

function harness(send = async () => true,{setTimeout: timer = callback => { queueMicrotask(callback); return 1; },nativePower = false} = {}) {
  const device = {id:'projector',online:true,capabilities:[
    'red_toggle','green_toggle','blue_toggle','white_toggle','laser_toggle','laser_steady','laser_pulse',
    'color_jump','color_fade','purple_from_white'
  ].map(id => ({id,type:'switch',momentary:true}))};
  if (nativePower) device.capabilities.push({id:'light_toggle',type:'switch',momentary:true});
  const node = (tag,className,text) => new Element(tag,className,text);
  const button = (text,className,handler) => {
    const element = node('button',className,text);
    element.events.click = handler;
    return element;
  };
  let tree,dialog;
  let externalBusy = false;
  const sent = [];
  const context = {
    node,button,t:text => text,isBusy:() => externalBusy,
    send:async id => { sent.push(id); return send(id); },
    redraw() { tree = ui.render(device,context); },
    formModal(title,submitText) {
      const form = node('form');
      const error = node('p');
      const submit = button(submitText,'',() => {});
      submit.type = 'submit';
      form.append(error,submit);
      dialog = {title,form,error,submit};
      return {form,error};
    },
    closeDialog() { dialog = null; }
  };
  const sandbox = vm.createContext({
    konturProjector:model,
    setTimeout:timer,
    document:{
      createTextNode:text => node('#text','',text),
      getElementById:id => tree && descendants(tree).find(element => element.id === id)
    }
  });
  vm.runInContext(fs.readFileSync(require.resolve('../public/projector-ui.js'),'utf8'),sandbox);
  const ui = sandbox.konturProjectorUi;
  context.redraw();
  const byId = id => descendants(tree).find(element => element.id === id);
  const byText = (text,root = tree) => descendants(root).find(element => element.tag === 'button' && element.text === text);
  function click(element) {
    assert.ok(element,'button should exist');
    if (!element.disabled) element.events.click();
  }
  function submit() {
    assert.ok(dialog,'a calibration dialog should be open');
    dialog.form.events.submit({preventDefault() {}});
  }
  async function calibrateLaser(value) {
    click(byText('Сверить лазер'));
    click(byText(value,dialog.form));
    submit();
    await flush();
  }
  async function calibrateColor(value) {
    click(byText('Сверить цвет'));
    click(byText(value,dialog.form));
    submit();
    await flush();
  }
  return {
    device,sent,byId,byText,click,submit,calibrateLaser,calibrateColor,
    dialog:() => dialog,tree:() => tree,
    redraw:() => context.redraw(),setBusy:value => { externalBusy = value; },reset:() => ui.reset(device.id)
  };
}

function manualClock() {
  let now = 0;
  const timers = [];
  const delays = [];
  return {
    delays,
    setTimeout(callback,delay) {
      delays.push(delay);
      timers.push({at:now + delay,callback});
      return timers.length;
    },
    async advance(duration) {
      now += duration;
      const due = timers.filter(timer => timer.at <= now);
      for (const timer of due) timers.splice(timers.indexOf(timer),1);
      for (const timer of due) timer.callback();
      await flush();
    }
  };
}

test('palette commands are spaced by a full second and the final cooldown blocks another selection',async () => {
  const clock = manualClock();
  const ui = harness(async () => true,clock);
  await ui.calibrateColor('Белый');
  const staleBlue = ui.byId('projector-color-blue');
  ui.click(ui.byId('projector-color-purple'));
  await flush();
  assert.deepEqual(ui.sent,['red_toggle']);
  assert.equal(ui.byId('projector-color-purple').disabled,true);
  await clock.advance(999);
  ui.click(staleBlue);
  assert.deepEqual(ui.sent,['red_toggle']);
  await clock.advance(1);
  assert.deepEqual(ui.sent,['red_toggle','white_toggle']);
  await clock.advance(1000);
  assert.deepEqual(ui.sent,['red_toggle','white_toggle','blue_toggle']);
  assert.equal(ui.byId('projector-color-purple').disabled,true,'a successful final ACK must not unlock immediately');
  assert.equal(ui.byId('projector-color-purple').attributes['aria-pressed'],'false','the plan commits only after its final cooldown');
  ui.click(staleBlue);
  await clock.advance(999);
  assert.deepEqual(ui.sent,['red_toggle','white_toggle','blue_toggle']);
  assert.equal(ui.byId('projector-color-purple').disabled,true);
  await clock.advance(1);
  assert.equal(ui.byId('projector-color-purple').disabled,false);
  assert.equal(ui.byId('projector-color-purple').attributes['aria-pressed'],'true');
  assert.deepEqual(clock.delays,[1000,1000,1000]);
  ui.click(ui.byId('projector-color-blue'));
  await flush();
  assert.deepEqual(ui.sent,['red_toggle','white_toggle','blue_toggle','red_toggle']);
});

test('reset during the cooldown cancels remaining frames and does not commit an old color',async () => {
  const clock = manualClock();
  const ui = harness(async () => true,clock);
  await ui.calibrateColor('Белый');
  ui.click(ui.byId('projector-color-purple'));
  await flush();
  assert.deepEqual(ui.sent,['red_toggle']);
  ui.reset();
  ui.redraw();
  await clock.advance(1000);
  assert.deepEqual(ui.sent,['red_toggle']);
  for (const color of model.palettes) assert.equal(ui.byId('projector-color-' + color.id).attributes['aria-pressed'],'false');
  ui.click(ui.byId('projector-color-red'));
  assert.ok(ui.dialog(),'a new session needs fresh synchronization');
});

test('a failed ACK after a paced command aborts without retrying a toggle',async () => {
  const clock = manualClock();
  const ui = harness(async id => id !== 'white_toggle',clock);
  await ui.calibrateColor('Белый');
  ui.click(ui.byId('projector-color-purple'));
  await flush();
  await clock.advance(1000);
  assert.deepEqual(ui.sent,['red_toggle','white_toggle']);
  assert.deepEqual(clock.delays,[1000],'a failed ACK must stop the remaining sequence');
  for (const color of model.palettes) assert.equal(ui.byId('projector-color-' + color.id).attributes['aria-pressed'],'false');
  ui.click(ui.byId('projector-color-purple'));
  assert.ok(ui.dialog());
  await clock.advance(1000);
  assert.deepEqual(ui.sent,['red_toggle','white_toggle']);
});

test('fresh projector does not claim a laser state and off hides effect choices',async () => {
  const ui = harness();
  assert.equal(ui.byId('projector-laser-off').attributes['aria-pressed'],'false');
  assert.equal(ui.byId('projector-laser-on').attributes['aria-pressed'],'false');
  assert.equal(descendants(ui.tree()).find(element => element.classList.contains('projector-effects') &&
    descendants(element).some(child => child.id === 'projector-laser-pulse')).hidden,true);
  await ui.calibrateLaser('Выключен');
  assert.equal(ui.byId('projector-laser-off').attributes['aria-pressed'],'true');
  assert.equal(ui.byId('projector-laser-on').attributes['aria-pressed'],'false');
  assert.equal(descendants(ui.tree()).find(element => element.classList.contains('projector-effects') &&
    descendants(element).some(child => child.id === 'projector-laser-pulse')).hidden,true);
  assert.deepEqual(ui.sent,[],'reading and manually synchronizing state must not send IR');
});

test('palette calibration requires a deliberate choice and submit before any IR is sent',async () => {
  const ui = harness();
  ui.click(ui.byId('projector-color-red'));
  assert.equal(ui.dialog().submit.disabled,true);
  ui.submit();
  await flush();
  assert.deepEqual(ui.sent,[]);
  ui.click(ui.byText('Белый',ui.dialog().form));
  assert.equal(ui.dialog().submit.disabled,false);
  assert.deepEqual(ui.sent,[],'selecting the observed color only records a choice');
  ui.submit();
  await flush();
  assert.deepEqual(ui.sent,['red_toggle','white_toggle']);
  assert.equal(ui.dialog(),null);
  assert.equal(ui.byId('projector-color-red').attributes['aria-pressed'],'true');
});

test('a partially failed color change stops the plan and requires synchronization before retry',async () => {
  const ui = harness(async id => id !== 'white_toggle');
  await ui.calibrateColor('Белый');
  ui.click(ui.byId('projector-color-purple'));
  await flush();
  assert.deepEqual(ui.sent,['red_toggle','white_toggle'],'blue must not be sent after a failed white command');
  for (const color of model.palettes) assert.equal(ui.byId('projector-color-' + color.id).attributes['aria-pressed'],'false');
  ui.click(ui.byId('projector-color-purple'));
  assert.ok(ui.dialog(),'the next attempt must ask for the actual color again');
  assert.equal(ui.dialog().submit.disabled,true);
  assert.deepEqual(ui.sent,['red_toggle','white_toggle']);
});

test('laser on is idempotent and mode controls only appear once a known active selection exists',async () => {
  const ui = harness();
  await ui.calibrateLaser('Выключен');
  ui.click(ui.byId('projector-laser-on'));
  await flush();
  assert.deepEqual(ui.sent,['laser_steady']);
  assert.equal(ui.byId('projector-laser-on').attributes['aria-pressed'],'true');
  assert.equal(ui.byId('projector-laser-steady').attributes['aria-pressed'],'true');
  assert.equal(descendants(ui.tree()).find(element => element.classList.contains('projector-effects') &&
    descendants(element).some(child => child.id === 'projector-laser-pulse')).hidden,false);
  ui.click(ui.byId('projector-laser-on'));
  await flush();
  assert.deepEqual(ui.sent,['laser_steady'],'a repeated on choice must not toggle an active laser');
  ui.click(ui.byId('projector-laser-off'));
  await flush();
  assert.deepEqual(ui.sent,['laser_steady','laser_toggle']);
  assert.equal(ui.byId('projector-laser-off').attributes['aria-pressed'],'true');
});

test('an in-flight projector command blocks stale controls and external busy state blocks new plans',async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const ui = harness(() => pending);
  await ui.calibrateLaser('Выключен');
  const staleOn = ui.byId('projector-laser-on');
  const staleColor = ui.byId('projector-color-red');
  ui.click(staleOn);
  assert.deepEqual(ui.sent,['laser_steady']);
  ui.click(staleOn);
  ui.click(staleColor);
  assert.deepEqual(ui.sent,['laser_steady']);
  assert.equal(ui.dialog() === null,true,'busy palette clicks must not open a second flow');
  release(true);
  await flush();
  ui.setBusy(true);
  ui.redraw();
  ui.click(ui.byId('projector-laser-off'));
  assert.deepEqual(ui.sent,['laser_steady']);
  assert.equal(ui.byId('projector-laser-off').disabled,true);
});

test('whole-light power controls require a native light capability and do not invent a power state',() => {
  const legacy = harness();
  assert.equal(legacy.byId('projector-power-on'),undefined);
  assert.equal(legacy.byId('projector-power-off'),undefined);
  const native = harness(async () => true,{nativePower:true});
  assert.equal(native.byId('projector-power-on').attributes['aria-pressed'],'false');
  assert.equal(native.byId('projector-power-off').attributes['aria-pressed'],'false');
});

test('power off and on preserve the selected color and restore the laser without color toggles',async () => {
  const ui = harness(async () => true,{nativePower:true});
  await ui.calibrateColor('Фиолетовый');
  await ui.calibrateLaser('Постоянное свечение');
  assert.equal(ui.byId('projector-power-on').attributes['aria-pressed'],'true');
  ui.click(ui.byId('projector-power-off'));
  await flush();
  assert.deepEqual(ui.sent,['light_toggle','laser_toggle']);
  assert.equal(ui.byId('projector-power-off').attributes['aria-pressed'],'true');
  ui.click(ui.byId('projector-power-off'));
  await flush();
  assert.deepEqual(ui.sent,['light_toggle','laser_toggle'],'duplicate off must not wake the light');
  ui.click(ui.byId('projector-power-on'));
  await flush();
  assert.deepEqual(ui.sent,['light_toggle','laser_toggle','light_toggle','laser_steady']);
  assert.equal(ui.byId('projector-power-on').attributes['aria-pressed'],'true');
  assert.equal(ui.byId('projector-color-purple').attributes['aria-pressed'],'true');
  assert.equal(ui.byId('projector-laser-steady').attributes['aria-pressed'],'true');
});

test('choosing a palette color from remembered light-off wakes the native light before changing its mask',async () => {
  const ui = harness(async () => true,{nativePower:true});
  await ui.calibrateColor('Фиолетовый');
  await ui.calibrateLaser('Выключен');
  ui.click(ui.byId('projector-power-off'));
  await flush();
  assert.deepEqual(ui.sent,['light_toggle']);
  ui.click(ui.byId('projector-color-blue'));
  await flush();
  assert.deepEqual(ui.sent,['light_toggle','light_toggle','red_toggle']);
  assert.equal(ui.byId('projector-power-on').attributes['aria-pressed'],'true');
  assert.equal(ui.byId('projector-color-blue').attributes['aria-pressed'],'true');
});

test('a dark observation does not guess the retained mask or trap palette selection in a calibration loop',async () => {
  const ui = harness(async () => true,{nativePower:true});
  await ui.calibrateColor('Нет подсветки');
  await ui.calibrateLaser('Выключен');
  ui.click(ui.byId('projector-color-red'));
  assert.equal(ui.dialog(),null);
  assert.deepEqual(ui.sent,[]);
  assert.ok(descendants(ui.tree()).some(element => element.text === 'Сначала включи светильник и сверь цвет подсветки.'));
  ui.click(ui.byId('projector-power-on'));
  await flush();
  assert.deepEqual(ui.sent,['light_toggle']);
  for (const color of model.palettes) assert.equal(ui.byId('projector-color-' + color.id).attributes['aria-pressed'],'false');
  ui.click(ui.byId('projector-color-red'));
  assert.ok(ui.dialog(),'once the native light is on the user can observe its actual color');
  assert.deepEqual(ui.sent,['light_toggle']);
});

test('an incomplete power-off plan clears both domains and requires synchronization before another power request',async () => {
  const ui = harness(async id => id !== 'laser_toggle',{nativePower:true});
  await ui.calibrateColor('Фиолетовый');
  await ui.calibrateLaser('Постоянное свечение');
  ui.click(ui.byId('projector-power-off'));
  await flush();
  assert.deepEqual(ui.sent,['light_toggle','laser_toggle']);
  assert.equal(ui.byId('projector-power-on').attributes['aria-pressed'],'false');
  assert.equal(ui.byId('projector-power-off').attributes['aria-pressed'],'false');
  assert.equal(ui.byId('projector-color-purple').attributes['aria-pressed'],'false');
  ui.click(ui.byId('projector-power-on'));
  assert.ok(ui.dialog());
  assert.equal(ui.dialog().title,'Сверь цвет с проектором');
  assert.deepEqual(ui.sent,['light_toggle','laser_toggle']);
});
