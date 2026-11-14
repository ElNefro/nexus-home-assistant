'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const storage = require('../public/storage.js');
const ble = require('../public/ble.js');

function home() {
  return {
    rooms:[{id:'living',name:'Гостиная'}],
    devices:[{
      id:'ble-0123456789ab',name:'Увлажнитель (ESP32)',nameIsDefault:true,kind:'humidifier',
      online:true,transport:'ble',roomId:'living',state:{power:true},
      capabilities:[{id:'power',type:'switch',label:'Питание'}]
    }],
    profile:{name:'Тестовый профиль',customName:true},
    roomId:'living'
  };
}

test('restore preserves pairing identity but never restores a live Bluetooth session',() => {
  const result = storage.decodeBackup(JSON.stringify({version:1,format:'kontur-home',home:home()}));
  assert.equal(result.devices[0].transport,'ble');
  assert.equal(result.devices[0].online,false);
  assert.equal(result.devices[0].nameIsDefault,true);
  assert.equal(result.profile.customName,true);
});

test('invalid device references and duplicate capability ids reject an entire backup',() => {
  const wrongRoom = home();
  wrongRoom.devices[0].roomId = 'missing';
  assert.throws(() => storage.normalize(wrongRoom),/invalid-device/);
  const duplicate = home();
  duplicate.devices[0].capabilities.push(duplicate.devices[0].capabilities[0]);
  assert.throws(() => storage.normalize(duplicate),/invalid-device/);
});

test('BLE reads validate protocol version and sensor range',() => {
  assert.deepEqual(ble.parseTelemetry(Uint8Array.of(1,1,45,42,0)),{
    power:true,target_humidity:45,humidity:42,water_level:0
  });
  assert.throws(() => ble.parseTelemetry(Uint8Array.of(1,0,45,101,2)),/invalid-telemetry/);
  assert.throws(() => ble.parseTelemetry(Uint8Array.of(2,0,45,10,2)),/invalid-telemetry/);
  assert.throws(() => ble.encodeCommand('target_humidity',80),/invalid-command/);
});

test('BLE sends commands serially and waits for matching telemetry after a write',async () => {
  const writes = [];
  let power = 0;
  let nextPower = null;
  let target = 45;
  const telemetry = {
    async startNotifications() {},
    async readValue() {
      const value = Uint8Array.of(1,power,target,42,65);
      if (nextPower !== null) {
        power = nextPower;
        nextPower = null;
      }
      return value;
    }
  };
  const command = {async writeValueWithResponse(bytes) {
    writes.push(Array.from(bytes));
    if (bytes[0] === 1) nextPower = bytes[1];
    if (bytes[0] === 2) target = bytes[1];
  }};
  const identity = {async readValue() {return new TextEncoder().encode(JSON.stringify({
    v:1,kind:'humidifier',id:'0123456789ab',telemetry:1
  }));}};
  const peripheral = {gatt:{
    connected:false,
    async connect() {
      this.connected = true;
      return {async getPrimaryService() {
        return {async getCharacteristic(id) {
          return {'933f3721':identity,'933f3722':telemetry,'933f3723':command}[id.slice(0,8)];
        }};
      }};
    },
    disconnect() { this.connected = false; }
  }};
  const bluetooth = {async requestDevice() {return peripheral;}};
  const connection = await ble.choose(bluetooth,'0123456789ab');
  const results = await Promise.all([connection.send('power',true),connection.send('target_humidity',55)]);
  assert.deepEqual(writes,[[1,1],[2,55]]);
  assert.equal(results[0].power,true);
  assert.equal(results[1].target_humidity,55);
  connection.disconnect();
  await assert.rejects(ble.choose(bluetooth,'0123456789ac'),/wrong-device/);
  assert.equal(peripheral.gatt.connected,false);
});

test('all scripts parse and the PWA shell references real files',() => {
  for (const path of fs.readdirSync('public').filter(name => name.endsWith('.js'))) {
    new vm.Script(fs.readFileSync('public/' + path,'utf8'),{filename:path});
  }
  const urls = fs.readFileSync('public/sw.js','utf8').match(/const shell = \[([\s\S]*?)\];/)[1];
  const assets = [...urls.matchAll(/'\.\/([^']+)'/g)].map(match => match[1].split('?')[0]);
  for (const asset of assets) assert.ok(fs.existsSync('public/' + asset),asset);
  const manifest = JSON.parse(fs.readFileSync('public/manifest.webmanifest','utf8'));
  for (const icon of manifest.icons) {
    const image = fs.readFileSync('public/' + icon.src);
    assert.equal(image.readUInt32BE(16) + 'x' + image.readUInt32BE(20),icon.sizes);
  }
});
test('Wi-Fi discovery uses the device manifest and pairing keeps the chosen identity',async () => {
  const manifest = {
    schemaVersion:1,id:'light-0123456789ab',name:'Workshop light',kind:'light',
    capabilities:[{id:'dimmer',label:'Dimmer',type:'slider',min:0,max:100,step:1}]
  };
  const replies = [
    {devices:[manifest,{...manifest,id:'broken light'}]},
    {session:'opaque',manifest,state:{dimmer:50}},
    {state:{dimmer:70}}
  ];
  const calls = [];
  const browser = vm.createContext({
    AbortSignal,konturStorage:storage,location:{origin:'http://127.0.0.1:8080'},
    async fetch(path,options) {
      calls.push([path,options.body && JSON.parse(options.body)]);
      return {ok:true,json:async () => replies.shift()};
    }
  });
  vm.runInContext(fs.readFileSync('public/lan.js','utf8'),browser);
  const devices = await browser.konturLan.discover();
  assert.equal(devices.length,1);
  assert.equal(devices[0].capabilities[0].id,'dimmer');
  const session = await browser.konturLan.pair(devices[0].id,'test-code');
  assert.deepEqual(await session.send('dimmer',70),{dimmer:70});
  assert.deepEqual(calls,[
    ['./api/discovery',undefined],
    ['./api/pair',{id:manifest.id,token:'test-code'}],
    ['./api/command',{session:'opaque',capability:'dimmer',value:70}]
  ]);
  browser.location.origin = 'https://nexus-home-assistant.onrender.com';
  await assert.rejects(browser.konturLan.discover(),/local-service/);
});
