'use strict';

const cacheName = 'kontur-shell-1.6.1';
const shell = [
  './','./index.html','./offline.html','./styles.css?v=1.6.1','./app.js?v=1.6.1','./i18n.js?v=1.6.1','./home-api.js?v=1.6.1','./room-scene.js?v=1.6.1','./shared-home.js?v=1.6.1',
  './storage.js?v=1.6.1','./platform.js?v=1.6.1','./manifest.webmanifest',
  './icons/kontur-192.png','./icons/kontur-512.png','./automation.js?v=1.6.1','./lan.js?v=1.6.1','./provisioning.js?v=1.6.1','./images/room-dome.png'
];
const shellUrls = new Set(shell.map(path => new URL(path,self.registration.scope).href));
const navigationPaths = new Set(['./','./index.html'].map(path => new URL(path,self.registration.scope).pathname));

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
  const url = new URL(request.url);
  if (request.method === 'GET' && request.mode === 'navigate' && url.origin === self.location.origin && navigationPaths.has(url.pathname)) {
    event.respondWith(fetch(request,{cache:'no-cache'}).catch(async () => {
      const cache = await caches.open(cacheName);
      return await cache.match(self.registration.scope) ?? Response.error();
    }));
    return;
  }
  if (request.method !== 'GET' || !shellUrls.has(request.url)) return;
  event.respondWith((async () => {
    const cache = await caches.open(cacheName);
    return await cache.match(request) ?? await fetch(request);
  })());
});

self.addEventListener('push',event => {
  let payload;
  try { payload = event.data?.json(); } catch { return; }
  if (!payload || typeof payload.title !== 'string') return;
  event.waitUntil(self.registration.showNotification(payload.title,{
    body:payload.body,tag:payload.tag,icon:'./icons/kontur-192.png',
    data:{deviceId:payload.deviceId,homeId:payload.homeId}
  }));
});

self.addEventListener('notificationclick',event => {
  event.notification.close();
  event.waitUntil((async () => {
    const destination = new URL('./',self.registration.scope);
    const deviceId = event.notification.data?.deviceId;
    const route = new URLSearchParams({page:typeof deviceId === 'string' ? 'devices' : 'home'});
    if (typeof deviceId === 'string') {
      route.set('type','device');
      route.set('id',deviceId);
    }
    if (typeof event.notification.data?.homeId === 'string') route.set('homeId',event.notification.data.homeId);
    destination.hash = route.toString();
    for (const client of await self.clients.matchAll({type:'window',includeUncontrolled:true})) {
      if (client.url.split('#')[0] !== destination.href.split('#')[0]) continue;
      await client.navigate(destination.href);
      await client.focus();
      return;
    }
    await self.clients.openWindow(destination.href);
  })());
});
