'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const cloudSource = fs.readFileSync(require.resolve('../public/cloud.js'),'utf8');

test('cloud sign-in times out cleanly when AbortSignal.timeout is unavailable',async () => {
  const signalTimers = new Map();
  let calls = 0;
  const browser = vm.createContext({
    AbortController,
    location:{origin:'https://nexus-home-assistant.onrender.com'},
    setTimeout(callback,delay) {
      assert.equal(delay,75_000);
      const timer = Symbol('timeout');
      signalTimers.set(timer,callback);
      return timer;
    },
    clearTimeout(timer) { signalTimers.delete(timer); },
    async fetch(url,{signal,credentials}) {
      assert.equal(url,'https://nexus-home-assistant-api.onrender.com/api/cloud/devices');
      assert.equal(credentials,'omit');
      assert.ok(signal instanceof AbortSignal);
      if (++calls === 1) {
        return new Promise((resolve,reject) => {
          signal.addEventListener('abort',() => reject(signal.reason),{once:true});
          signalTimers.values().next().value();
        });
      }
      return {ok:true,json:async () => ({devices:[]})};
    }
  });
  vm.runInContext(cloudSource,browser);
  const cloud = browser.konturCloud;
  await assert.rejects(cloud.connect('test-owner-code'),error => error.name === 'AbortError');
  assert.equal(cloud.connected(),false);
  assert.equal(signalTimers.size,0);
  assert.equal((await cloud.connect('test-owner-code')).length,0);
  assert.equal(cloud.connected(),true);
  assert.equal(signalTimers.size,0);
});
