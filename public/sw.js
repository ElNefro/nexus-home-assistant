'use strict';

const cacheName = 'kontur-shell-1.3.1';
const shell = [
  './','./index.html','./offline.html','./styles.css?v=1.3.1','./app.js?v=1.3.1','./i18n.js',
  './storage.js?v=1.3.1','./ble.js?v=1.3.1','./platform.js?v=1.3.1','./manifest.webmanifest',
  './icons/kontur-192.png','./icons/kontur-512.png','./automation.js?v=1.3.1','./lan.js?v=1.3.1','./images/room-dome.png'
];
const shellUrls = new Set(shell.map(path => new URL(path,self.registration.scope).href));

self.addEventListener('install',event => {
  event.waitUntil(caches.open(cacheName).then(cache =>
    cache.addAll(shell.map(url => new Request(url,{cache:'reload'}))).then(() => self.skipWaiting())
  ));
});

self.addEventListener('activate',event => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter(name => name.startsWith('kontur-shell-') && name !== cacheName)
      .map(name => caches.delete(name)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch',event => {
  const request = event.request;
  if (request.method !== 'GET' || !shellUrls.has(request.url)) return;
  event.respondWith((async () => {
    const cache = await caches.open(cacheName);
    return await cache.match(request) ?? await fetch(request);
  })());
});

self.addEventListener('notificationclick',event => {
  event.notification.close();
  event.waitUntil((async () => {
    const destination = new URL('./',self.registration.scope);
    const deviceId = event.notification.data?.deviceId;
    if (typeof deviceId === 'string') {
      destination.hash = new URLSearchParams({page:'devices',type:'device',id:deviceId}).toString();
    }
    for (const client of await self.clients.matchAll({type:'window',includeUncontrolled:true})) {
      if (client.url.split('#')[0] !== destination.href.split('#')[0]) continue;
      await client.navigate(destination.href);
      await client.focus();
      return;
    }
    await self.clients.openWindow(destination.href);
  })());
});
