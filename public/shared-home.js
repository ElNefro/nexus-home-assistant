'use strict';

const sharedHome = (() => {
  const api = konturHome.request;
  let user;
  let homes = [];
  let accountRevision = 0;
  let home;
  let guestData;
  let available = false;
  let requestQueue = Promise.resolve();
  let pending = 0;
  let reading = false;
  let currentInvite = '';
  const invitationCode = /^[A-Za-z0-9_-]{32}$/;
  let pendingInvite = '';

  function message(error) {
    const errors = {
      'invalid-login':'Проверь адрес и пароль.',
      'email-in-use':'Этот адрес уже зарегистрирован. Выбери «Войти» или «Восстановить доступ».',
      'invalid-email':'Введи электронную почту полностью, включая часть после @.',
      'invalid-name':'Введи имя длиной до 60 символов.',
      'invalid-password':'Новый пароль должен содержать от 10 до 128 символов.',
      'homes-unavailable':'Сервер аккаунтов сейчас недоступен. Попробуй ещё раз через минуту.',
      'try-again-later':'Сервер занят. Попробуй ещё раз через несколько секунд.',
      'request-timeout':'Сервер долго отвечает. Попробуй ещё раз.',
      'network-unavailable':'Нет связи с сервером. Проверь интернет и попробуй ещё раз.',
      'invalid-recovery':'Адрес или код восстановления не подошёл.',
      'invalid-invitation':'Приглашение недействительно или уже использовано.',
      'revision-conflict':'Дом изменился в другой вкладке. Загружаем свежие данные.',
      'home-access-denied':'Доступ к этому дому отозван или действие доступно только владельцу.',
      'sign-in-required':'Войди снова, чтобы продолжить.',
      'device-offline':'Прибор сейчас не в сети.',
      'device-token':'Код прибора не подошёл.',
      'device-owned':'Прибор уже привязан к другому дому. Попроси владельца удалить его.',
      'device-unconfirmed':'Прибор не подтвердил команду.',
      'push-unavailable':'На локальном сервере недоступна отправка push.',
      'home-index-in-use':'Этот код дома уже используется.',
      'invalid-home-index':'Код дома должен содержать от 3 до 32 латинских букв, цифр или дефисов.',
      'invalid-member-prefix':'Введи семейный префикс длиной до 24 символов.'
    };
    return t(errors[error.message] ?? 'Не удалось выполнить действие. Проверь подключение и введённые данные.');
  }

  function connected() { return Boolean(home); }
  function canEdit() { return !home || home.role === 'owner'; }
  function dataCopy() { return JSON.parse(JSON.stringify({...data,roomId})); }

  function route(route) {
    if (pendingInvite) route.set('invite',pendingInvite);
    return route.toString();
  }

  function captureInvite(params) {
    const code = params.get('invite') ?? '';
    if (!invitationCode.test(code) || code === pendingInvite) return;
    pendingInvite = code;
    if (user) joinWithCode(code).catch(error => status(message(error)));
  }

  function clearInvite(code) {
    if (pendingInvite !== code) return;
    pendingInvite = '';
    const url = new URL(location.href);
    const params = new URLSearchParams(url.hash.slice(1));
    params.delete('invite');
    url.hash = params.toString();
    history.replaceState(history.state,'',url);
  }

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

  function useAccount(result) {
    accountRevision++;
    user = result.user;
    homes = result.homes ?? [];
    available = true;
  }

  async function session() {
    const revision = accountRevision;
    const result = await api('account/session');
    if (accountRevision === revision) useAccount(result);
  }

  function openSnapshot(snapshot) {
    if (!home) {
      guestData = dataCopy();
      lanSessions.clear();
    }
    currentInvite = '';
    try { localStorage.setItem('kontur-preview-home',snapshot.id); } catch {}
    display(snapshot);
  }

  async function open(homeId) {
    openSnapshot(await api('home/get',{homeId}));
  }

  async function setup() {
    if (!konturHome.local() && !konturHome.cloud()) return;
    if (konturHome.cloud()) {
      available = true;
      render();
    }
    setInterval(refresh,2500);
    try {
      await session();
      let last;
      try { last = localStorage.getItem('kontur-preview-home'); } catch {}
      const linkedHome = new URLSearchParams(location.hash.slice(1)).get('homeId');
      if (user && pendingInvite) await joinWithCode(pendingInvite);
      else if (user && homes.some(item => item.id === linkedHome)) await open(linkedHome);
      else if (user && homes.some(item => item.id === last)) await open(last);
      else if (user && homes.length === 1) await open(homes[0].id);
      else render();
    } catch (error) {
      if (pendingInvite) status(message(error));
    }
  }

  async function refresh() {
    if (!home || pending || reading || dialog.open) return;
    if (document.activeElement?.matches?.('input, select, textarea')) return;
    const id = home.id;
    reading = true;
    try {
      const snapshot = await api('home/get',{homeId:id});
      const onlineChanged = konturHome.cloud() && snapshot.state.devices.some(device =>
        device.online !== home?.state.devices.find(item => item.id === device.id)?.online);
      if (home?.id === id && !pending && (snapshot.revision !== home.revision || onlineChanged) && !dialog.open) display(snapshot);
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

  async function claim(room,deviceId,setupKey) {
    const homeId = home?.id;
    if (!homeId || !canEdit()) throw Error('home-access-denied');
    const snapshot = await api('home/cloud/claim',{homeId,roomId:room.id,deviceId,setupKey});
    if (home?.id !== homeId) return;
    dialog.close();
    display(snapshot);
    status(t('Прибор подключён к комнате «{name}».',{name:roomName(room)}));
  }

  function fieldInput(form,error,caption,id,type='text',options={}) {
    const input = node('input');
    input.type = type;
    input.required = true;
    input.maxLength = type === 'email' ? 254 : 128;
    if (type === 'password') input.autocomplete = 'off';
    const {hint,...attributes} = options;
    Object.assign(input,attributes);
    field(form,error,t(caption),id,input);
    if (hint) {
      const description = node('p','field-note',t(hint));
      description.id = id + '-hint';
      input.setAttribute('aria-describedby',description.id);
      form.insertBefore(description,error);
    }
    return input;
  }

  function openForm(title,action,fields,handler,waiting) {
    const {form,error} = formModal(t(title),t(action));
    const inputs = fields.map(([caption,key,type,options]) => fieldInput(form,error,caption,'shared-' + key,type,options));
    const feedback = waiting ? node('p','field-note',t(waiting)) : null;
    if (feedback) {
      feedback.setAttribute('role','status');
      feedback.hidden = true;
      form.insertBefore(feedback,error);
    }
    form.addEventListener('submit',async event => {
      event.preventDefault();
      const submit = form.querySelector('[type=submit]');
      if (submit.disabled) return;
      error.hidden = true;
      submit.disabled = true;
      if (feedback) feedback.hidden = false;
      try {
        const values = Object.fromEntries(fields.map(([,key,type],index) => [
          key,type === 'password' ? inputs[index].value : inputs[index].value.trim()
        ]));
        await handler(values);
      } catch (errorValue) {
        showError(error,message(errorValue));
      } finally {
        submit.disabled = false;
        if (feedback) feedback.hidden = true;
      }
    });
    inputs[0]?.focus();
  }

  function auth(mode) {
    const email = ['Электронная почта','email','email',{autocomplete:'email'}];
    const password = ['Пароль','password','password',{autocomplete:'current-password'}];
    const newPassword = {autocomplete:'new-password',minLength:10,hint:'От 10 до 128 символов.'};
    const config = {
      register:['Создать аккаунт','Зарегистрироваться',[
        ['Имя','name','text',{maxLength:60,autocomplete:'name'}],email,['Пароль','password','password',newPassword]
      ]],
      login:['Войти в аккаунт','Войти',[email,password]],
      recover:['Восстановить доступ','Изменить пароль',[
        email,['Код восстановления','recoveryCode'],['Новый пароль','password','password',newPassword]
      ]]
    };
    const [title,action,fields] = config[mode];
    openForm(title,action,fields,async values => {
      const result = await api('account/' + mode,values);
      useAccount(result);
      dialog.close();
      if (pendingInvite) {
        try { await joinWithCode(pendingInvite); }
        catch (error) { status(message(error)); }
      } else if (homes.length === 1) {
        try { await open(homes[0].id); }
        catch (error) { status(message(error)); }
      }
      render();
      if (result.recoveryCode) {
        const content = modal(t('Код восстановления'));
        content.append(node('p','sub',t('Сохрани этот код. Он понадобится, если забудешь пароль.')),
          node('code','recovery-code',result.recoveryCode),button(t('Закрыть'),'primary',() => dialog.close()));
      }
    },'Ждём ответа сервера…');
  }

  function create() {
    const {form,error} = formModal(t('Создать дом'),t('Создать'));
    const name = nameField('');
    field(form,error,t('Название дома'),'new-home-name',name);
    const index = konturHome.cloud()
      ? fieldInput(form,error,'Код дома','new-home-index') : undefined;
    if (index) {
      index.minLength = 3;
      index.maxLength = 32;
      index.autocomplete = 'off';
      index.autocapitalize = 'characters';
      index.spellcheck = false;
      const hint = node('p','field-note',t('Код дома должен содержать от 3 до 32 латинских букв, цифр или дефисов.'));
      form.insertBefore(hint,error);
    }
    const label = node('label','shared-check');
    const importCurrent = node('input');
    importCurrent.type = 'checkbox';
    label.append(importCurrent,document.createTextNode(t('Перенести мои комнаты и приборы')));
    form.insertBefore(label,error);
    form.addEventListener('submit',async event => {
      event.preventDefault();
      const submit = form.querySelector('[type=submit]');
      if (submit.disabled) return;
      submit.disabled = true;
      try {
        const code = index?.value.trim();
        if (index && !/^[A-Za-z0-9-]{3,32}$/.test(code)) throw Error('invalid-home-index');
        const snapshot = await api('home/create',{
          name:name.value.trim(),index:code,state:importCurrent.checked ? guestData ?? dataCopy() : undefined
        });
        homes.push({id:snapshot.id,name:snapshot.name,role:snapshot.role,index:snapshot.index});
        dialog.close();
        openSnapshot(snapshot);
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
      await joinWithCode(values.code);
    });
    byId('shared-code').value = pendingInvite;
  }

  let joining = false;
  async function joinWithCode(code) {
    if (joining) return;
    joining = true;
    try {
      const snapshot = await api('home/join',{code});
      clearInvite(pendingInvite);
      if (dialog.open) dialog.close();
      homes = homes.filter(item => item.id !== snapshot.id);
      homes.push({id:snapshot.id,name:snapshot.name,role:snapshot.role,index:snapshot.index});
      openSnapshot(snapshot);
    } finally {
      joining = false;
    }
  }

  function memberRow(member) {
    const row = node('div','line member-row');
    row.append(node('span','',member.name || member.email),node('small','muted',t(member.role === 'owner' ? 'Владелец' : 'Участник')));
    if (member.prefix) row.append(node('small','tag',t(member.prefix)));
    if (konturHome.cloud() && canEdit()) {
      row.append(button(t('Префикс'),'ghost',() => {
        openForm('Семейный префикс','Сохранить',[['Префикс','prefix']],async ({prefix}) => {
          const snapshot = await api('home/member-prefix',{homeId:home.id,userId:member.id ?? member.userId,prefix});
          dialog.close();
          display(snapshot);
        });
        const input = byId('shared-prefix');
        input.maxLength = 24;
        input.required = false;
        input.value = member.prefix ?? '';
      }));
    }
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
    if (pendingInvite) panel.append(node('p','sub',t('Открыто приглашение в дом. Войди, чтобы принять его.')));
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
    if (home.index) panel.append(node('p','sub',t('Код дома: {code}',{code:home.index})));
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
      if (konturHome.cloud()) {
        const url = new URL('/',location.origin);
        url.hash = new URLSearchParams({page:'profile',invite:currentInvite}).toString();
        const link = node('input');
        link.type = 'text';
        link.readOnly = true;
        link.value = url.href;
        link.setAttribute('aria-label',t('Ссылка приглашения'));
        panel.append(link,button(t('Копировать ссылку'),'ghost',async () => {
          try {
            await navigator.clipboard.writeText(link.value);
            status(t('Ссылка скопирована.'));
          } catch {
            link.select();
          }
        }));
      } else panel.append(node('p','sub',t('Передай код человеку, которого хочешь пригласить.')),
        node('code','recovery-code',currentInvite));
    }
    if (konturHome.local()) panel.append(pushPanel());
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

  return {setup,profile,pair,claim,route,captureInvite,message,connected,canEdit,save,command,schedule,
    get home() { return home; },get user() { return user; }};
})();
