'use strict';

const sharedHome = (() => {
  const api = konturHome.request;
  let user;
  let homes = [];
  let home;
  let guestData;
  let available = false;
  let requestQueue = Promise.resolve();
  let pending = 0;
  let reading = false;
  let currentInvite = '';

  function message(error) {
    const errors = {
      'invalid-login':'Проверь адрес и пароль.',
      'email-in-use':'Аккаунт с таким адресом уже существует.',
      'invalid-recovery':'Адрес или код восстановления не подошёл.',
      'invalid-invitation':'Приглашение недействительно или уже использовано.',
      'revision-conflict':'Дом изменился в другой вкладке. Загружаем свежие данные.',
      'home-access-denied':'Доступ к этому дому отозван или действие доступно только владельцу.',
      'sign-in-required':'Войди снова, чтобы продолжить.',
      'device-offline':'Прибор сейчас не в сети.',
      'device-token':'Код прибора не подошёл.',
      'device-owned':'Прибор уже привязан к другому дому. Попроси владельца удалить его.',
      'device-unconfirmed':'Прибор не подтвердил команду.',
      'push-unavailable':'На локальном сервере недоступна отправка push.'
    };
    return t(errors[error.message] ?? 'Не удалось выполнить действие. Проверь локальный сервер и введённые данные.');
  }

  function connected() { return Boolean(home); }
  function canEdit() { return !home || home.role === 'owner'; }
  function dataCopy() { return JSON.parse(JSON.stringify({...data,roomId})); }

  function display(snapshot) {
    home = snapshot;
    const next = cleanLegacyHome(konturStorage.normalize(snapshot.state,{trustedLive:true}));
    const selection = next.rooms.some(room => room.id === roomId) ? roomId : next.roomId;
    Object.assign(data,next);
    roomId = selection;
    if (!dialog.open) render();
  }

  function leave() {
    if (!home) return;
    home = undefined;
    currentInvite = '';
    if (guestData) {
      Object.assign(data,guestData);
      roomId = guestData.roomId;
      guestData = undefined;
    }
    if (!dialog.open) render();
  }

  async function session() {
    const result = await api('account/session');
    user = result.user;
    homes = result.homes ?? [];
    available = true;
  }

  async function open(homeId) {
    const snapshot = await api('home/get',{homeId});
    if (!home) {
      guestData = dataCopy();
      lanSessions.clear();
    }
    currentInvite = '';
    try { localStorage.setItem('kontur-preview-home',homeId); } catch {}
    display(snapshot);
  }

  async function setup() {
    if (!konturHome.local()) return;
    try {
      await session();
      let last;
      try { last = localStorage.getItem('kontur-preview-home'); } catch {}
      const linkedHome = new URLSearchParams(location.hash.slice(1)).get('homeId');
      if (user && homes.some(item => item.id === linkedHome)) await open(linkedHome);
      else if (user && homes.some(item => item.id === last)) await open(last);
      else if (user && homes.length === 1) await open(homes[0].id);
      else render();
      setInterval(refresh,2500);
    } catch {}
  }

  async function refresh() {
    if (!home || pending || reading || dialog.open) return;
    if (document.activeElement?.matches?.('input, select, textarea')) return;
    const id = home.id;
    reading = true;
    try {
      const snapshot = await api('home/get',{homeId:id});
      if (home?.id === id && !pending && snapshot.revision !== home.revision && !dialog.open) display(snapshot);
    } catch (error) {
      if (home?.id === id && ['home-access-denied','sign-in-required'].includes(error.message)) {
        leave();
        await session().catch(() => {});
        render();
        status(message(error));
      }
    } finally {
      reading = false;
    }
  }

  function queue(route,body,success) {
    if (!home) return;
    const id = home.id;
    pending++;
    requestQueue = requestQueue.then(async () => {
      if (home?.id !== id) return;
      try {
        const snapshot = await api(route,{...body,homeId:id,revision:home.revision});
        if (home?.id !== id) return;
        if (pending === 1) display(snapshot);
        else home = snapshot;
        if (success) status(success);
      } catch (error) {
        status(message(error));
        const snapshot = await api('home/get',{homeId:id}).catch(() => null);
        if (home?.id === id && snapshot) display(snapshot);
        if (home?.id === id && !snapshot && ['home-access-denied','sign-in-required'].includes(error.message)) leave();
      }
    }).finally(() => { pending--; });
    return requestQueue;
  }

  function save() {
    if (!canEdit()) return;
    const state = dataCopy();
    const baseline = {...cleanLegacyHome(konturStorage.normalize(home.state,{trustedLive:true})),roomId:state.roomId};
    if (JSON.stringify(state) !== JSON.stringify(baseline)) queue('home/save',{state});
  }

  function command(device, capability, value) {
    status('Отправляем команду…');
    const momentary = device.capabilities.some(item => item.id === capability && item.momentary === true);
    const result = momentary ? 'Плата подтвердила команду.' : 'Состояние подтверждено.';
    return queue('home/command',{id:device.id,capability,value},t(result));
  }

  function schedule(device) {
    return queue('home/schedule',{id:device.id,schedule:device.schedule},t('Расписание сохранено.'));
  }

  async function pair(room, candidate, token) {
    const snapshot = await api('pair',{homeId:home.id,roomId:room.id,id:candidate.id,token});
    dialog.close();
    display(snapshot);
    status(t('Прибор подключён к комнате «{name}».',{name:roomName(room)}));
  }

  function fieldInput(form,error,caption,id,type='text') {
    const input = node('input');
    input.type = type;
    input.required = true;
    input.maxLength = type === 'email' ? 254 : 128;
    if (type === 'password') input.autocomplete = id === 'shared-password' ? 'current-password' : 'off';
    field(form,error,t(caption),id,input);
    return input;
  }

  function openForm(title,action,fields,handler) {
    const {form,error} = formModal(t(title),t(action));
    const inputs = fields.map(([caption,key,type]) => fieldInput(form,error,caption,'shared-' + key,type));
    form.addEventListener('submit',async event => {
      event.preventDefault();
      const submit = form.querySelector('[type=submit]');
      submit.disabled = true;
      try {
        const values = Object.fromEntries(fields.map(([,key,type],index) => [
          key,type === 'password' ? inputs[index].value : inputs[index].value.trim()
        ]));
        await handler(values);
      } catch (errorValue) {
        showError(error,message(errorValue));
      } finally {
        submit.disabled = false;
      }
    });
    inputs[0]?.focus();
  }

  function auth(mode) {
    const email = ['Электронная почта','email','email'];
    const password = ['Пароль','password','password'];
    const config = {
      register:['Создать аккаунт','Зарегистрироваться',[['Имя','name'],email,password]],
      login:['Войти в аккаунт','Войти',[email,password]],
      recover:['Восстановить доступ','Изменить пароль',[email,['Код восстановления','recoveryCode'],['Новый пароль','password','password']]]
    };
    const [title,action,fields] = config[mode];
    openForm(title,action,fields,async values => {
      const result = await api('account/' + mode,values);
      await session();
      dialog.close();
      if (homes.length === 1) await open(homes[0].id);
      render();
      if (result.recoveryCode) {
        const content = modal(t('Код восстановления'));
        content.append(node('p','sub',t('Сохрани этот код. Он понадобится, если забудешь пароль.')),
          node('code','recovery-code',result.recoveryCode),button(t('Закрыть'),'primary',() => dialog.close()));
      }
    });
  }

  function create() {
    const {form,error} = formModal(t('Создать дом'),t('Создать'));
    const name = nameField('');
    field(form,error,t('Название дома'),'new-home-name',name);
    const label = node('label','shared-check');
    const importCurrent = node('input');
    importCurrent.type = 'checkbox';
    label.append(importCurrent,document.createTextNode(t('Перенести мои комнаты и приборы')));
    form.insertBefore(label,error);
    form.addEventListener('submit',async event => {
      event.preventDefault();
      const submit = form.querySelector('[type=submit]');
      submit.disabled = true;
      try {
        const snapshot = await api('home/create',{
          name:name.value.trim(),state:importCurrent.checked ? guestData ?? dataCopy() : undefined
        });
        await session();
        dialog.close();
        await open(snapshot.id);
        navigate('rooms');
      } catch (errorValue) {
        showError(error,message(errorValue));
      } finally {
        submit.disabled = false;
      }
    });
    name.focus();
  }

  function join() {
    openForm('Войти по приглашению','Войти',[['Код приглашения','code']],async values => {
      const snapshot = await api('home/join',values);
      await session();
      const selected = snapshot.id ?? snapshot.homeId ?? homes.find(item => item.role === 'member')?.id;
      dialog.close();
      if (selected) await open(selected);
      render();
    });
  }

  function memberRow(member) {
    const row = node('div','line');
    row.append(node('span','',member.name || member.email),node('small','muted',t(member.role === 'owner' ? 'Владелец' : 'Участник')));
    if (canEdit() && member.role !== 'owner') {
      row.append(button(t('Отозвать доступ'),'ghost',async () => {
        try {
          await api('home/revoke',{homeId:home.id,userId:member.id ?? member.userId});
          await refreshMembership();
        } catch (error) {status(message(error));}
      }));
    }
    return row;
  }

  async function refreshMembership() {
    if (home) display(await api('home/get',{homeId:home.id}));
  }

  function profile() {
    if (!available) return null;
    const panel = node('section','panel profile-section shared-panel');
    panel.append(node('h2','',t(home?.name ?? 'Общий дом')));
    const actions = node('div','row-buttons');
    if (!user) {
      panel.append(node('p','sub',t('Создай аккаунт, чтобы управлять домом вместе.')));
      actions.append(button(t('Создать аккаунт'),'primary',() => auth('register')),
        button(t('Войти'),'ghost',() => auth('login')),
        button(t('Восстановить доступ'),'ghost',() => auth('recover')));
      panel.append(actions);
      return panel;
    }
    panel.append(node('p','sub',user.email));
    actions.append(button(t('Создать дом'),'primary',create),button(t('Войти по приглашению'),'ghost',join),
      button(t('Выйти'),'ghost',async () => {
        try {
          const registration = await navigator.serviceWorker?.getRegistration().catch(() => null);
          const subscription = await registration?.pushManager?.getSubscription().catch(() => null);
          if (subscription && home) await api('push/unsubscribe',{homeId:home.id,endpoint:subscription.endpoint}).catch(() => {});
          await api('account/logout',{});
          leave();
          await session();
          render();
        } catch (error) {status(message(error));}
      }));
    panel.append(actions);
    for (const item of homes) {
      if (item.id === home?.id) continue;
      panel.append(button(item.name,'ghost',async () => {
        try { await open(item.id); } catch (error) { status(message(error)); }
      }));
    }
    if (!home) return panel;
    panel.append(node('h3','',t('Участники дома')));
    for (const member of home.members ?? []) panel.append(memberRow(member));
    if (canEdit()) {
      panel.append(button(t('Создать приглашение'),'ghost',async () => {
        try {
          currentInvite = (await api('home/invite',{homeId:home.id})).code;
          render();
        } catch (error) {status(message(error));}
      }));
    }
    if (currentInvite) {
      panel.append(node('p','sub',t('Передай код человеку, которого хочешь пригласить.')),
        node('code','recovery-code',currentInvite));
    }
    panel.append(pushPanel());
    return panel;
  }

  function pushPanel() {
    const section = node('section','profile-section');
    const actions = node('div','row-buttons');
    async function subscription() {
      if (!('serviceWorker' in navigator) || !('PushManager' in window) || !globalThis.Notification) {
        throw new Error('push-browser');
      }
      const registration = await navigator.serviceWorker.getRegistration();
      if (!registration?.active) throw new Error('push-browser');
      return registration;
    }
    actions.append(button(t('Включить push'),'ghost',async () => {
      const homeId = home.id;
      try {
        if (!globalThis.Notification || !('PushManager' in window)) throw new Error('push-browser');
        if (await Notification.requestPermission() !== 'granted') {
          status(t('Уведомления отключены в настройках браузера.'));
          return;
        }
        const registration = await subscription();
        const {publicKey} = await api('push/key',{homeId});
        const decoded = atob(publicKey.replace(/-/g,'+').replace(/_/g,'/').padEnd(Math.ceil(publicKey.length/4)*4,'='));
        const key = Uint8Array.from(decoded,letter => letter.charCodeAt(0));
        const existing = await registration.pushManager.getSubscription();
        const current = existing ?? await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:key});
        await api('push/subscribe',{homeId,subscription:current.toJSON()});
        status(t('Push включён для этого дома.'));
      } catch (error) {
        status(error.message === 'push-browser' ? t('Для push открой установленное приложение в поддерживаемом браузере.') : message(error));
      }
    }),button(t('Отключить push'),'ghost',async () => {
      try {
        const registration = await subscription();
        const current = await registration.pushManager.getSubscription();
        if (current) await api('push/unsubscribe',{homeId:home.id,endpoint:current.endpoint});
        status(t('Push отключён для этого дома.'));
      } catch (error) {status(message(error));}
    }));
    section.append(node('h3','','Push'),actions);
    return section;
  }

  return {setup,profile,pair,message,connected,canEdit,save,command,schedule,get home() { return home; },get user() { return user; }};
})();
