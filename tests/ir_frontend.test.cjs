'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const storage = require('../public/storage.js');
const automation = require('../public/automation.js');

const metadata = {
  commandFeedback:'transmitted',
  description:'ИК-пульт проектора'
};
const capabilities = ['red_toggle','white_toggle','blue_toggle','green_toggle',
  'laser_toggle','color_jump','color_fade','purple_from_white'].map(id => ({
  id,type:'switch',momentary:true,label:id,description:'Команда ' + id
}));

test('IR manifest metadata survives restore without inventing state or automation',() => {
  const input = {
    rooms:[{id:'living',name:'Living'}],profile:{name:'Owner'},
    devices:[{id:'lan-ir-projector-123',name:'Projector',kind:'light',roomId:'living',
      transport:'lan',online:true,state:{red_toggle:true},capabilities,...metadata}]
  };
  const restored = storage.decodeBackup(JSON.stringify({version:1,format:'kontur-home',home:input})).devices[0];
  assert.equal(restored.commandFeedback,'transmitted');
  assert.equal(restored.description,metadata.description);
  assert.equal(restored.capabilities[0].description,'Команда red_toggle');
  assert.deepEqual(restored.state,{});
  assert.equal(automation.supportsMode(restored,'interval'),false);
  assert.equal(storage.deviceMetadata({commandFeedback:'state-confirmed'}),false);
  assert.equal(storage.deviceMetadata({description:'x'.repeat(501)}),false);
});

test('LAN pairing sends a device password separately and keeps legacy token devices compatible',async () => {
  const manifest = {schemaVersion:1,id:'ir-projector-test',name:'Projector',kind:'light',
    requiresPassword:true,requiresToken:false,capabilities,...metadata};
  const requests = [];
  let activeManifest = manifest;
  const context = vm.createContext({
    location:{origin:'http://localhost:8080'},konturStorage:storage,AbortSignal,
    fetch:async (url,options) => {
      requests.push({url,body:JSON.parse(options.body)});
      return {ok:true,status:200,json:async()=>({session:'local-session',manifest:activeManifest,state:{}})};
    }
  });
  vm.runInContext(fs.readFileSync(require.resolve('../public/lan.js'),'utf8'),context);
  const lan = context.konturLan;
  assert.equal(lan.acceptManifest(manifest),true);
  assert.equal(lan.acceptManifest({...manifest,requiresToken:true}),false);
  assert.equal(lan.acceptManifest({...manifest,requiresPassword:'true'}),false);
  await lan.pair(manifest.id,'password-for-test',true);
  assert.deepEqual(requests[0],{url:'./api/pair',body:{id:manifest.id,password:'password-for-test'}});
  activeManifest = {...manifest,requiresPassword:false,requiresToken:true};
  assert.equal(lan.acceptManifest(activeManifest),true);
  await lan.pair(manifest.id,'legacy-device-code');
  assert.deepEqual(requests[1],{url:'./api/pair',body:{id:manifest.id,token:'legacy-device-code'}});
});

test('IR actions send true repeatedly and purple requires an explicit initial-state confirmation',async () => {
  const source = fs.readFileSync(require.resolve('../public/app.js'),'utf8');
  const start = source.indexOf('function control(device,capability) {');
  const end = source.indexOf('function showDevice(',start);
  class Element {
    constructor(tag,text) { Object.assign(this,{tag,text,children:[],events:{},attributes:{}}); }
    append(...items) { this.children.push(...items); }
    prepend(...items) { this.children.unshift(...items); }
    insertBefore(item,anchor) { this.children.splice(this.children.indexOf(anchor),0,item); }
    setAttribute(key,value) { this.attributes[key] = value; }
    addEventListener(type,handler) { this.events[type] = handler; }
    focus() {}
  }
  const form = new Element('form');
  const error = new Element('p');
  const submit = new Element('button');
  form.append(error,submit);
  form.querySelector = () => submit;
  const calls = [];
  let closed = 0;
  const context = vm.createContext({
    node:(tag,style,text)=>new Element(tag,text),t:text=>text,
    button:(text,style,handler)=>{const result=new Element('button',text);result.events.click=handler;return result;},
    capabilityLabel:(device,capability)=>capability.label,
    busyDevices:new Set(),sharedHome:{connected:()=>false},
    transmit:async (device,id,value)=>calls.push({id,value}),
    formModal:()=>({form,error}),
    document:{createTextNode:text=>text},dialog:{close(){closed++;}},
    render(){},valueText:value=>value
  });
  vm.runInContext(source.slice(start,end),context);
  const device = {id:'ir',online:true,state:{},...metadata};
  context.device = device;
  context.capabilities = capabilities;
  const control = vm.runInContext('control(device,capabilities[0])',context);
  const action = control.children[0].children[1];
  assert.equal(action.text,'Отправить');
  assert.equal(action.attributes['aria-pressed'],undefined);
  await action.events.click();
  await action.events.click();
  assert.deepEqual(calls,[{id:'red_toggle',value:true},{id:'red_toggle',value:true}]);
  const purple = vm.runInContext('control(device,capabilities[7])',context);
  purple.children[0].children[1].events.click();
  const check = form.children.find(item=>item.tag==='label').children[0];
  const trigger = () => form.events.submit({preventDefault(){}});
  trigger();
  assert.equal(calls.length,2);
  assert.equal(submit.disabled,true);
  check.checked = true;
  check.events.change();
  assert.equal(submit.disabled,false);
  trigger();
  assert.equal(closed,1);
  assert.deepEqual(calls[2],{id:'purple_from_white',value:true});
  assert.deepEqual(device.state,{});
});
