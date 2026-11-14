'use strict';

(function expose(root) {
  let installPrompt;
  let workerRegistration;
  window.addEventListener('beforeinstallprompt',event => {
    event.preventDefault();
    installPrompt = event;
  });

  function register() {
    if (!('serviceWorker' in navigator) || !window.isSecureContext || location.protocol === 'file:') return;
    workerRegistration = navigator.serviceWorker.register('./sw.js').catch(error => {
      console.warn('Kontur offline cache could not start',error);
      return null;
    });
  }

  async function install() {
    if (window.matchMedia('(display-mode: standalone)').matches || navigator.standalone) return 'installed';
    if (!installPrompt) return 'instructions';
    const prompt = installPrompt;
    installPrompt = null;
    await prompt.prompt();
    return (await prompt.userChoice).outcome;
  }

  async function notify(title,options) {
    const registration = await workerRegistration;
    if (registration?.active) {
      await registration.showNotification(title,{icon:'./icons/kontur-192.png',...options});
    } else {
      new Notification(title,options);
    }
  }

  function download(json) {
    const url = URL.createObjectURL(new Blob([json],{type:'application/json'}));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'kontur-home-' + new Date().toISOString().slice(0,10) + '.json';
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url),10000);
  }

  root.konturPlatform = {register,install,notify,download};
})(globalThis);
