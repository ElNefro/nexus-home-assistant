const {t,number:localeNumber,deviceCount,roomName,deviceName,profileName,capabilityLabel} = globalThis.konturI18n;
const byId = id => document.getElementById(id);
const node = (tag, className, content) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (content !== undefined) element.textContent = content;
  return element;
};
function button(text, className, action) {
  const element = node('button',className,text);
  element.type = 'button';
  element.addEventListener('click',action);
  return element;
}
const power = {id:'power',type:'switch',label:'Питание'};
const brightness = {id:'brightness',type:'slider',label:'Яркость',min:0,max:100,step:1,unit:'%'};
const kinds = {
  humidifier:{label:'Увлажнитель',tone:'#65c9ff',icon:'≋'},
  purifier:{label:'Очиститель',tone:'#75f4b2',icon:'✧'},
  climate:{label:'Датчик климата',tone:'#ffb36f',icon:'◔'},
  light:{label:'Освещение',tone:'#fdd584',icon:'☼'},
  relay:{label:'Реле',tone:'#b6a3ff',icon:'⏻'},
  feeder:{label:'Кормушка',tone:'#ffb36f',icon:'◒'},
  other:{label:'Другой прибор',tone:'#b6a3ff',icon:'◉'}
};
const metrics = [
  {id:'temperature',label:'Температура',unit:'°C',tone:'var(--amber)',min:0,max:40},
  {id:'humidity',label:'Влажность',unit:'%',tone:'var(--blue)',min:0,max:100},
  {id:'air_quality',label:'Чистота воздуха',unit:'%',tone:'var(--green)',min:0,max:100}
];
const climate = metrics.map(metric => ({id:metric.id,type:'sensor',label:metric.label,unit:metric.unit}));
const legacySampleRooms = [
  {id:'living',name:'Гостиная'},
  {id:'bedroom',name:'Спальня'},
  {id:'kitchen',name:'Кухня'}
];
const legacySampleDevices = [
  {id:'humidifier',name:'Увлажнитель',kind:'humidifier',roomId:'living',online:true,capabilities:[power,{id:'target_humidity',type:'slider',label:'Целевая влажность',min:30,max:70,step:1,unit:'%'}],state:{power:true,target_humidity:50}},
  {id:'purifier',name:'Очиститель воздуха',kind:'purifier',roomId:'living',online:true,capabilities:[power,{id:'speed',type:'slider',label:'Скорость вентилятора',min:1,max:5,step:1}],state:{power:true,speed:2}},
  {id:'climate',name:'Датчик климата',kind:'climate',roomId:'living',online:true,capabilities:climate,state:{temperature:23.4,humidity:46,air_quality:94}},
  {id:'bedroom-climate',name:'Датчик климата',kind:'climate',roomId:'bedroom',online:true,capabilities:climate,state:{temperature:21.8,humidity:52,air_quality:91}},
  {id:'lamp',name:'Ночник',kind:'light',roomId:'bedroom',online:false,capabilities:[power,brightness],state:{power:false,brightness:30}},
  {id:'relay',name:'Реле вытяжки',kind:'relay',roomId:'kitchen',online:true,capabilities:[power],state:{power:false}}
];
const storageKey = 'kontur-dashboard-v2';
function isSampleDevice(device) {
  // Earlier versions reserved these ids for examples. LAN identifiers use "lan-".
  return !device.transport && (device.id === 'light' ||
    legacySampleDevices.some(example => example.id === device.id));
}

function cleanLegacyHome(home) {
  const devices = home.devices.filter(device => !isSampleDevice(device) && !(
    ['ble','ble-test'].includes(device.transport) && /^ble-[0-9a-f]{12}$/.test(device.id)
  )).map(device => device.transport === 'lan' ? device : {...device,online:false});
  const rooms = home.rooms.filter(room => room.id === 'living' ||
    !legacySampleRooms.some(example => example.id === room.id && example.name === room.name) ||
    devices.some(device => device.roomId === room.id));
  const usableRooms = rooms.length ? rooms : [{...legacySampleRooms[0]}];
  return {...home,rooms:usableRooms,devices,
    roomId:usableRooms.some(room => room.id === home.roomId) ? home.roomId : usableRooms[0].id};
}

function migrateLegacyArray(previous,profile) {
  const rooms = legacySampleRooms.map(room => ({...room}));
  const devices = [];
  for (const device of previous) {
    if (!device || typeof device.id !== 'string' || devices.some(item => item.id === device.id)) continue;
    const sample = legacySampleDevices.find(item => item.id === device.id);
    let room = typeof device.room === 'string' && device.room.trim()
      ? rooms.find(item => item.name === device.room)
      : rooms.find(item => item.id === (device.roomId ?? sample?.roomId));
    if (!room && typeof device.room === 'string' && device.room.trim() && device.room.length <= 60) {
      room = {id:'room-' + rooms.length,name:device.room};
      rooms.push(room);
    }
    const migrated = {...sample,...device,roomId:room?.id ?? 'living',kind:device.kind ?? sample?.kind ?? (device.id === 'light' ? 'light' : 'other')};
    try {
      devices.push(konturStorage.normalize({rooms,devices:[migrated],profile}).devices[0]);
    } catch {
      // A malformed legacy entry does not discard the rest of the home.
    }
  }
  return cleanLegacyHome({rooms,devices,profile,roomId:'living'});
}

function loadData() {
  const profile = {name:'Владелец дома',role:'Админ'};
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    if (saved !== null) return cleanLegacyHome(konturStorage.normalize(saved));
    const previous = JSON.parse(localStorage.getItem('kontur-smart-home-v1'));
    if (Array.isArray(previous)) return migrateLegacyArray(previous,profile);
  } catch {}
  return {rooms:[{...legacySampleRooms[0]}],devices:[],profile,roomId:'living'};
}
const data = loadData();
const lanSessions = new Map();
const measuredAt = new Map();
const automationRetryAt = new Map();
const busyDevices = new Set();
let page = 'home';
let detail;
const sensorDescriptions = {
  temperature:'Измеряет температуру воздуха в комнате.',
  humidity:'Измеряет относительную влажность воздуха.',
  air_quality:'Оценивает чистоту воздуха по шкале от 0 до 100%.'
};
function openDetail(type,id,room='') {
  location.hash = new URLSearchParams({page,type,id,room}).toString();
}
function syncRoute() {
  const route = new URLSearchParams(location.hash.slice(1));
  page = Object.hasOwn(pages,route.get('page')) ? route.get('page') : 'home';
  detail = {type:route.get('type'),id:route.get('id'),room:route.get('room')};
  if (dialog.open) dialog.close();
  status('');
  render();
  window.scrollTo(0,0);
  if (document.activeElement !== document.body) byId('screen').focus({preventScroll:true});
}
let roomId = data.rooms.some(room => room.id === data.roomId) ? data.roomId : data.rooms[0].id;
const dialog = byId('dialog');
const status = message => { byId('status').textContent = t(message); };
function save(message = '') {
  if (sharedHome.connected()) {
    status(message);
    sharedHome.save();
    return;
  }
  try {
    localStorage.setItem(storageKey,JSON.stringify({...data,roomId}));
    status(message);
  } catch {
    status((message ? message + ' ' : '') + t('Браузер не сохранил изменения для следующего сеанса.'));
  }
}
const currentRoom = () => data.rooms.find(room => room.id === roomId);
const roomDevices = id => data.devices.filter(device => device.roomId === id);
const kindFor = device => kinds[device.kind] ?? kinds.other;
const idFor = () => globalThis.crypto?.randomUUID?.() ?? Date.now() + '-' + Math.random().toString(36).slice(2);
let number = localeNumber({maximumFractionDigits:8});
const valueText = (value,unit) => Number.isFinite(value) ? number.format(value) + (unit ? ' ' + unit : '') : '—';
function heading(kicker,title,description) {
  const result = node('div','heading');
  if (kicker) result.append(node('p','kicker',kicker));
  result.append(node('h1','',title));
  if (description) result.append(node('p','sub',description));
  return result;
}
function sectionHead(title,action) {
  const result = node('div','section-head');
  result.append(node('h2','',title));
  if (action) result.append(action);
  return result;
}
function list(items,emptyText) {
  if (!items.length) return node('p','empty',emptyText);
  const result = node('div','stack');
  result.append(...items);
  return result;
}
function tile(title,detail,icon,tone,action) {
  const result = button('','tile',action);
  result.style.setProperty('--tone',tone);
  const graphic = node('span','square',icon);
  graphic.setAttribute('aria-hidden','true');
  const copy = node('span');
  copy.append(node('span','title',title),node('small','',detail));
  const arrow = node('span','arrow','›');
  arrow.setAttribute('aria-hidden','true');
  result.append(graphic,copy,arrow);
  return result;
}
function deviceStatus(device) {
  if (device.transport === 'lan') return device.online ? t('Подключён по Wi-Fi') : t('Добавлен по Wi-Fi') + ' · ' + t('Не в сети');
  return t('Не в сети');
}
function deviceTile(device,back) {
  const kind = kindFor(device);
  const connection = deviceStatus(device);
  return tile(deviceName(device),t(kind.label) + ' · ' + connection,kind.icon,kind.tone,() => showDevice(device,back));
}
function gauge(metric,devices) {
  const sources = devices.filter(device => device.online && device.capabilities.some(item => item.id === metric.id && item.type === 'sensor') && Number.isFinite(device.state[metric.id]));
  const source = sources.find(device => device.transport) ?? sources[0];
  const result = makeGauge({...metric,label:t(metric.label)},source?.state[metric.id]);
  const note = node('p',source ? 'sub source' : 'sub missing',source ? deviceName(source) : t('Нет показаний'));
  result.card.append(note);
  return result.card;
}
function makeGauge(metric,value) {
  const available = Number.isFinite(value);
  const result = node('article','metric');
  result.style.setProperty('--tone',metric.tone);
  const dial = node('div','dial');
  const center = node('div','dial-center');
  const reading = node('strong');
  center.append(reading,node('small','',metric.unit ?? ''));
  dial.append(center);
  result.append(node('h2','',metric.label),dial);
  function update(next) {
    const hasValue = Number.isFinite(next);
    const ratio = metric.max > metric.min && hasValue ? (next - metric.min) / (metric.max - metric.min) : 0;
    result.style.setProperty('--fill',Math.max(0,Math.min(1,ratio)) * 280 + 'deg');
    reading.textContent = hasValue ? (metric.format?.(next) ?? number.format(next)) : '—';
  }
  update(available ? value : undefined);
  return {card:result,update};
}
function renderHome() {
  const room = currentRoom();
  const devices = roomDevices(room.id);
  const panel = node('section','home-panel');
  panel.setAttribute('aria-label',t('Климат комнаты «{name}»',{name:roomName(room)}));
  const gauges = node('div','gauges');
  const order = ['humidity','air_quality','temperature'];
  gauges.append(...order.map(id => gauge(metrics.find(metric => metric.id === id),devices)));
  panel.append(gauges);
  const hero = node('section','room-hero');
  const copy = node('div','room-hero-copy');
  copy.append(node('p','kicker',t('Умный дом')),node('h1','',roomName(room)),
    node('p','sub',deviceCount(devices.length)));
  const illustration = node('img','room-art');
  illustration.src = './images/room-dome.png';
  illustration.alt = '';
  illustration.width = 1536;
  illustration.height = 1024;
  hero.append(copy,illustration);
  konturRoomScene.attach(hero,devices,room.id);
  return [hero,panel,warnings(devices)];
}
function warning(device) {
  return device.online && device.kind === 'humidifier' && device.state.water_level === 0;
}
const pendingNotifications = new Set();
async function notifyWarnings() {
  if (sharedHome.connected()) return;
  let changed = false;
  for (const device of data.devices) {
    if (!warning(device)) {
      if (device.state.water_level > 0 && device.waterNotificationSent) {
        device.waterNotificationSent = false;
        changed = true;
      }
      continue;
    }
    if (!device.transport || globalThis.Notification?.permission !== 'granted' ||
        device.waterNotificationSent || pendingNotifications.has(device.id)) continue;
    pendingNotifications.add(device.id);
    try {
      await konturPlatform.notify(deviceName(device),{
        body:t('В баке закончилась вода'),tag:'kontur-water-' + device.id,data:{deviceId:device.id}
      });
      if (warning(device)) {
        device.waterNotificationSent = true;
        changed = true;
      }
    } catch {
      status(t('Этот браузер не поддерживает уведомления с открытой страницы.'));
    } finally {
      pendingNotifications.delete(device.id);
    }
  }
  if (changed) save();
}
function warnings(devices) {
  const section = node('section','warnings');
  const canNotify = sharedHome.connected() || devices.some(device => device.transport === 'lan' && device.kind === 'humidifier');
  const enable = button(t('Включить уведомления'),'ghost',async () => {
    if (sharedHome.connected()) return navigate('profile');
    try {
      if (await Notification.requestPermission() !== 'granted') {
        status(t('Разреши уведомления в настройках браузера.'));
        return;
      }
      render();
      notifyWarnings();
    } catch {
      status(t('Уведомления недоступны в этом браузере.'));
    }
  });
  section.append(sectionHead(t('Предупреждения'),canNotify && globalThis.Notification?.permission !== 'granted' ? enable : null));
  if (canNotify) section.append(node('p','field-note',t(sharedHome.connected() ? 'Push настраивается в профиле.' : 'Уведомления приходят на этом устройстве, пока панель открыта.')));
  section.append(list(devices.filter(warning).map(device => {
    const kind = kindFor(device);
    const card = tile(deviceName(device),t('В баке закончилась вода'),'',kind.tone,() => showDevice(device));
    card.firstElementChild.classList.add('humidifier-picture');
    return card;
  }),t('В этой комнате нет предупреждений.')));
  return section;
}
function renderDevices() {
  const room = currentRoom();
  const devices = roomDevices(room.id);
  return [
    heading(roomName(room),t('Приборы'),''),
    sectionHead(t('Список приборов'),sharedHome.canEdit() ? button(t('＋ Добавить прибор'),'primary',() => addDevice(room)) : null),
    list(devices.map(device => deviceTile(device)),t('В этой комнате пока нет приборов.'))
  ];
}
function renderRooms() {
  const rooms = data.rooms.map(room => {
    const count = roomDevices(room.id).length;
    return tile(roomName(room),deviceCount(count),'▦','var(--active)',() => showRoom(room));
  });
  return [
    heading(t('Настройки дома'),t('Комнаты'),''),
    sectionHead(t('Твои комнаты'),sharedHome.canEdit() ? button(t('＋ Создать комнату'),'primary',() => editRoom()) : null),
    list(rooms,t('Комнат пока нет.'))
  ];
}
function showRoom(room) {
  openDetail('room',room.id);
}
function renderRoom(room) {
  const content = node('section','detail-page');
  content.append(button(t('‹ Комнаты'),'ghost back',() => navigate('rooms')),heading(t('Настройки комнаты'),roomName(room),''));
  const settings = node('div','panel');
  const reopen = render;
  const actions = node('div','row-buttons');
  actions.append(button(t('Переименовать'),'ghost',() => editRoom(room,reopen)),button(t('Добавить прибор'),'primary',() => addDevice(room)));
  const devices = node('div','device-links');
  devices.append(list(roomDevices(room.id).map(device => deviceTile(device,reopen)),t('В этой комнате пока нет приборов.')));
  if (sharedHome.canEdit()) settings.append(actions);
  settings.append(devices);
  content.append(settings);
  return [content];
}
function renderProfile() {
  const profile = node('div','panel');
  const top = node('div','profile');
  const initials = profileName(data.profile).split(/\s+/).slice(0,2).map(part => Array.from(part)[0]).join('').toUpperCase();
  const copy = node('div');
  copy.append(node('h2','',profileName(data.profile)),node('p','muted',t('Локальный профиль')));
  const role = sharedHome.home?.role === 'member' ? 'Участник' : sharedHome.home?.role === 'owner' ? 'Владелец' : data.profile.role;
  top.append(node('span','square',initials),copy,node('span','tag',t(role)));
  profile.append(top);
  if (!sharedHome.connected()) profile.append(button(t('Изменить имя'),'ghost',editProfile));
  const preferences = node('div','panel profile-section');
  const language = node('select');
  language.id = 'app-language';
  const languageLabel = node('label','',t('Язык приложения'));
  languageLabel.htmlFor = language.id;
  for (const [code,name] of [['ru','Русский'],['en','English']]) {
    const option = node('option','',name);
    option.value = code;
    language.append(option);
  }
  language.value = konturI18n.getLocale();
  language.addEventListener('change',() => {
    konturI18n.setLocale(language.value);
    number = localeNumber({maximumFractionDigits:8});
    status('');
    render();
  });
  preferences.append(languageLabel,language,button(t('Установить приложение'),'ghost',installApp));
  const backup = node('div','panel profile-section');
  const backupActions = node('div','row-buttons');
  backupActions.append(button(t('Скачать копию'),'ghost',() => {
    konturPlatform.download(JSON.stringify({version:1,format:'kontur-home',home:{...data,roomId}},null,2));
  }),button(t('Восстановить из файла'),'ghost',restoreBackup));
  backup.append(node('h2','',t('Резервная копия')),node('p','sub',t('Данные хранятся в этом браузере.')),backupActions);


  return [heading(t('Настройки аккаунта'),t('Профиль'),t('Имя и роль в доме')),
    sharedHome.profile(),profile,preferences,sharedHome.canEdit() ? backup : null].filter(Boolean);
}
async function installApp() {
  try {
    const result = await konturPlatform.install();
    if (result === 'installed') status(t('Приложение уже установлено.'));
    if (result === 'instructions') status(t('В меню браузера выбери «Установить приложение» или «На экран Домой».'));
  } catch {
    status(t('В меню браузера выбери «Установить приложение» или «На экран Домой».'));
  }
}
function restoreBackup() {
  const {form,error} = formModal(t('Резервная копия'),t('Восстановить'));
  const file = node('input');
  file.type = 'file';
  file.accept = '.json,application/json';
  file.required = true;
  form.prepend(node('p','sub',t('Восстановление заменит комнаты, приборы и настройки этого браузера.')));
  field(form,error,t('Резервная копия'),'backup-file',file);
  const submit = form.querySelector('[type=submit]');
  form.addEventListener('submit', async event => {
    event.preventDefault();
    submit.disabled = true;
    try {
      const selected = file.files[0];
      if (!selected || selected.size > 2_000_000) throw Error('invalid-backup');
      const restored = cleanLegacyHome(konturStorage.decodeBackup(await selected.text()));
      if (!dialog.open || !form.isConnected) return;
      lanSessions.clear();
      measuredAt.clear();
      automationRetryAt.clear();
      Object.assign(data,restored);
      roomId = restored.roomId;
      complete(t('Копия восстановлена.'));
    } catch {
      showError(error,t('Не удалось прочитать копию.'));
    } finally {
      submit.disabled = false;
    }
  });
}
const pages = {home:renderHome,devices:renderDevices,rooms:renderRooms,profile:renderProfile};
function render() {
  konturI18n.translateChrome();
  const focus = document.activeElement;
  const screen = byId('screen');
  const restoreId = screen.contains(focus) ? focus.id : '';
  const picker = byId('room-picker');
  picker.replaceChildren(...data.rooms.map(room => {
    const option = node('option','',roomName(room));
    option.value = room.id;
    return option;
  }));
  picker.value = roomId;
  for (const tab of byId('nav').children) {
    if (tab.dataset.page === page) tab.setAttribute('aria-current','page');
    else tab.removeAttribute('aria-current');
  }
  const room = detail?.type === 'room' && data.rooms.find(item => item.id === detail.id);
  const device = detail?.type === 'device' && data.devices.find(item => item.id === detail.id);
  screen.replaceChildren(...(room ? renderRoom(room) : device ? renderDevice(device) : pages[page]()));
  if (restoreId && restoreId !== 'screen') byId(restoreId)?.focus({preventScroll:true});
}
function navigate(next) {
  const route = 'page=' + next;
  if (location.hash.slice(1) === route) {
    window.scrollTo(0,0);
    return;
  }
  location.hash = route;
}
function selectRoom(next) {
  if (!data.rooms.some(room => room.id === next)) return;
  roomId = next;
  save();
  if (detail?.type) navigate(page);
  else render();
}
function modal(title) {
  const content = byId('dialog-body');
  const heading = node('h2','',title);
  heading.id = 'dialog-title';
  heading.tabIndex = -1;
  heading.setAttribute('autofocus','');
  content.replaceChildren(heading);
  if (!dialog.open) dialog.showModal();
  else heading.focus({preventScroll:true});
  dialog.scrollTop = 0;
  return content;
}
function formModal(title,submitText,back) {
  const content = modal(title);
  const form = node('form');
  const error = node('p','error');
  error.setAttribute('role','alert');
  error.hidden = true;
  const actions = node('div','actions');
  const submit = node('button','primary',submitText);
  submit.type = 'submit';
  actions.append(button(t('Отмена'),'ghost',() => dialog.close()),submit);
  form.append(error,actions);
  content.append(form);
  return {form,error,actions};
}
function field(form,error,label,id,element) {
  const text = node('label','',label);
  text.htmlFor = id;
  element.id = id;
  form.insertBefore(text,error);
  form.insertBefore(element,error);
}
function nameField(value) {
  const input = node('input');
  input.type = 'text';
  input.value = value;
  input.required = true;
  input.maxLength = 60;
  return input;
}
function showError(error,message) {
  error.textContent = t(message);
  error.hidden = false;
}
function complete(message) {
  dialog.close();
  render();
  save(message);
}
function editRoom(room,back) {
  const {form,error} = formModal(room ? t('Изменить комнату') : t('Создать комнату'),t('Сохранить'),back);
  const input = nameField(room?.name ?? '');
  field(form,error,t('Название комнаты'),'edit-room-name',input);
  input.focus();
  form.addEventListener('submit',event => {
    event.preventDefault();
    const name = input.value.trim();
    if (!name) return showError(error,t('Введи название комнаты.'));
    if (data.rooms.some(item => item.id !== room?.id && item.name.toLocaleLowerCase('ru') === name.toLocaleLowerCase('ru'))) return showError(error,t('Комната с таким названием уже есть.'));
    if (room) room.name = name;
    else {
      const created = {id:idFor(),name};
      data.rooms.push(created);
      roomId = created.id;
    }
    complete(room ? t('Название комнаты обновлено.') : t('Комната создана.'),back);
  });
}
function editProfile() {
  const {form,error} = formModal(t('Изменить имя'),t('Сохранить'));
  const input = nameField(data.profile.name);
  field(form,error,t('Имя'),'profile-name',input);
  input.focus();
  form.addEventListener('submit',event => {
    event.preventDefault();
    const name = input.value.trim();
    if (!name) return showError(error,t('Введи имя.'));
    data.profile.name = name;
    data.profile.customName = true;
    complete(t('Имя профиля обновлено.'));
  });
}
function control(device,capability) {
  const result = node('div','control');
  const row = node('div','line');
  const label = node('span','',capabilityLabel(device,capability));
  const value = Object.hasOwn(device.state,capability.id) ? device.state[capability.id] : undefined;
  row.append(label);
  result.append(row);
  if (capability.momentary === true) {
    const action = button(t('Включить'),'primary',async () => {
      if (!device.online || busyDevices.has(device.id)) return;
      if (!sharedHome.connected()) {
        await transmit(device,capability.id,true);
        return;
      }
      busyDevices.add(device.id);
      render();
      try {
        await sharedHome.command(device,capability.id,true);
      } finally {
        busyDevices.delete(device.id);
        if (!dialog.open) render();
      }
    });
    action.id = 'device-action-' + capability.id;
    action.disabled = !device.online;
    if (busyDevices.has(device.id)) action.setAttribute('aria-disabled','true');
    row.append(action);
  } else if (capability.type === 'switch') {
    const toggle = button('','switch',() => {
      const next = toggle.getAttribute('aria-checked') !== 'true';
      if (sharedHome.connected()) sharedHome.command(device,capability.id,next);
      else transmit(device,capability.id,next);
    });
    toggle.setAttribute('role','switch');
    toggle.setAttribute('aria-label',capabilityLabel(device,capability));
    toggle.setAttribute('aria-checked',String(Boolean(value)));
    toggle.disabled = !device.online || busyDevices.has(device.id);
    row.append(toggle);
  } else {
    const reading = node('output','reading',valueText(value,capability.unit));
    row.append(reading);
    if (capability.type === 'slider') {
      const input = node('input');
      input.type = 'range';
      input.min = capability.min;
      input.max = capability.max;
      input.step = capability.step;
      input.value = value ?? capability.min;
      input.disabled = !device.online || busyDevices.has(device.id);
      input.setAttribute('aria-label',capabilityLabel(device,capability));
      input.addEventListener('change',() => {
        if (sharedHome.connected()) sharedHome.command(device,capability.id,input.valueAsNumber);
        else transmit(device,capability.id,input.valueAsNumber);
      });
      result.append(input);
    }
  }
  return result;
}
function showDevice(device,back) {
  openDetail('device',device.id,back ? device.roomId : '');
}
function renderDevice(device) {
  const content = node('section','detail-page');
  content.append(button(t('‹ Назад'),'ghost back',() => {
    if (detail.room) openDetail('room',detail.room);
    else navigate(page);
  }));
  const kind = kindFor(device);
  const controls = node('div');
  controls.style.setProperty('--tone',kind.tone);
  const panel = node('section','home-panel detail-panel');
  panel.setAttribute('aria-label',t('Показания прибора'));
  const gauges = node('div','gauges');
  panel.append(gauges);
  for (const capability of device.capabilities) {
    if (capability.momentary === true) {
      controls.append(control(device,capability));
      continue;
    }
    if (capability.type === 'switch') {
      const metric = {...capability,label:capabilityLabel(device,capability),tone:kind.tone,min:0,max:1,format:value => value ? t('Вкл.') : t('Выкл.')};
      const indicator = makeGauge(metric,typeof device.state[capability.id] === 'boolean' ? Number(device.state[capability.id]) : undefined);
      gauges.append(indicator.card);
      controls.append(control(device,capability));
      continue;
    }
    const metric = {...metrics.find(item => item.id === capability.id),...capability,label:capabilityLabel(device,capability),tone:kind.tone};
    const value = Object.hasOwn(device.state,capability.id) ? device.state[capability.id] : undefined;
    const indicator = makeGauge(metric,value);
    gauges.append(indicator.card);
    if (capability.type === 'slider') controls.append(control(device,capability));
  }
  content.append(heading(t(kind.label),deviceName(device),deviceStatus(device)));
  const actions = node('div','row-buttons');
  if (sharedHome.canEdit()) {
    if (data.rooms.length > 1) actions.append(button(t('Переместить прибор'),'ghost',() => moveDevice(device)));
    actions.append(button(t('Удалить прибор'),'ghost',() => confirmRemoveDevice(device)));
    content.append(actions);
  }
  if (device.transport === 'lan' && !device.online && device.lastSeen) {
    content.append(node('p','sub',t('Последнее состояние: {time}',{
      time:new Intl.DateTimeFormat(konturI18n.getLocale(),{dateStyle:'short',timeStyle:'short'}).format(device.lastSeen)
    })));
  }
  if (device.transport === 'lan' && !device.online && sharedHome.canEdit()) {
    const connection = node('section','panel');
    connection.append(button(t('Найти в сети'),'primary',() => addDevice(data.rooms.find(room => room.id === device.roomId))));
    content.append(connection);
  }
  if (gauges.childElementCount) {
    panel.prepend(node('h2','',t('Показатели работы')));
    content.append(panel);
  }
  if (controls.childElementCount) {
    const settings = node('section','panel');
    settings.append(node('h2','',t('Настройки прибора')),controls);
    if (konturAutomation.supportsMode(device,'interval')) settings.append(scheduleControls(device));
    content.append(settings);
  }
  const sensors = device.capabilities.filter(item => item.type === 'sensor');
  if (sensors.length) {
    const section = node('section','panel');
    section.append(node('h2','',t('Датчики')));
    for (const sensor of sensors) {
      const entry = node('div','control');
      const description = sensor.description || sensorDescriptions[sensor.id] || 'Текущее показание датчика.';
      entry.append(node('strong','',capabilityLabel(device,sensor)),node('div','muted',t(description)));
      section.append(entry);
    }
    content.append(section);
  }
  return [content];
}

function confirmRemoveDevice(device) {
  const {form,error} = formModal(t('Удалить прибор'),t('Удалить'));
  form.prepend(node('p','sub',t('Прибор «{device}» будет удалён из этой комнаты.',{device:deviceName(device)})));
  form.addEventListener('submit',event => {
    event.preventDefault();
    const index = data.devices.indexOf(device);
    if (index === -1) return showError(error,t('Прибор уже удалён.'));
    lanSessions.delete(device.id);
    measuredAt.delete(device.id);
    automationRetryAt.delete(device.id);
    data.devices.splice(index,1);
    dialog.close();
    const parentRoom = detail?.room;
    if (parentRoom) openDetail('room',parentRoom);
    else navigate(page);
    save();
  });
}

function moveDevice(device) {
  const {form,error} = formModal(t('Переместить прибор'),t('Переместить прибор'));
  const choices = node('select');
  for (const room of data.rooms) {
    if (room.id === device.roomId) continue;
    const choice = node('option','',roomName(room));
    choice.value = room.id;
    choices.append(choice);
  }
  field(form,error,t('Выбери комнату'),'destination-room',choices);
  choices.focus();
  form.addEventListener('submit',event => {
    event.preventDefault();
    const room = data.rooms.find(item => item.id === choices.value);
    if (!room) return;
    device.roomId = room.id;
    complete(t('Прибор перемещён в комнату «{name}».',{name:roomName(room)}));
    if (detail?.type === 'device' && detail.id === device.id) openDetail('device',device.id,room.id);
  });
}

const periods = ['15 минут','30 минут','1 час','2 часа','4 часа','8 часов','24 часа'];
function startInterval(schedule) {
  schedule.nextRun = Date.now() + konturAutomation.periodMs(schedule.period);
}
function scheduleControls(device) {
  const section = node('section','control');
  section.append(node('h3','',t('Расписание')));
  const modes = node('div','schedule-modes');
  const mode = device.schedule?.mode;
  const available = [['interval','Включать каждые…'],['auto','Авто']]
    .filter(([id]) => konturAutomation.supportsMode(device,id));
  for (const [id,label] of available) {
    const choice = button(t(label),mode === id ? 'primary' : 'ghost',() => {
      const settings = device.schedule ??= {};
      settings.mode = mode === id ? 'manual' : id;
      if (settings.mode === 'interval') startInterval(settings);
      if (sharedHome.connected()) sharedHome.schedule(device);
      else {
        save();
        runSchedules();
      }
      render();
      byId('schedule-mode-' + id)?.focus();
    });
    choice.id = 'schedule-mode-' + id;
    choice.setAttribute('aria-pressed',String(mode === id));
    modes.append(choice);
  }
  section.append(modes);
  if (mode === 'interval' || mode === 'auto' && konturAutomation.supportsMode(device,mode)) {
    const settings = device.schedule;
    const interval = mode === 'interval';
    const input = node(interval ? 'select' : 'input');
    input.id = 'schedule-value';
    const metric = konturAutomation.autoMetric(device);
    const labelText = metric === 'air_quality' ? 'Порог чистоты воздуха, %' : 'Целевая влажность, %';
    const label = node('label','',t(interval ? 'Период' : labelText));
    label.htmlFor = input.id;
    if (interval) {
      konturAutomation.periods.forEach((minutes,index) => {
        const option = node('option','',t(periods[index]));
        option.value = String(minutes);
        input.append(option);
      });
      input.value = String(settings.period ?? 60);
    } else {
      input.type = 'number';
      input.min = 1;
      input.max = 100;
      input.step = 1;
      input.required = true;
      input.value = String(konturAutomation.autoThreshold(device,settings));
    }
    input.addEventListener('change',() => {
      if (!input.reportValidity()) return;
      if (interval) {
        settings.period = Number(input.value);
        startInterval(settings);
      } else settings.threshold = Number(input.value);
      if (sharedHome.connected()) sharedHome.schedule(device);
      else {
        save(t('Расписание сохранено.'));
        runSchedules();
      }
    });
    const row = node('div','schedule-row');
    row.append(label,input);
    section.append(row);
    if (mode === 'auto') {
      section.append(node('p','field-note',t(roomMeasurement(device,metric) === undefined
        ? 'Авто ожидает показания датчика в этой комнате.'
        : 'Включается ниже порога и выключается при его достижении.')));
    } else section.append(node('p','field-note',t('Прибор включается через заданный период. Выключение вручную.')));
  }
  if (!mode && (device.schedule?.on || device.schedule?.off)) {
    section.append(node('p','field-note',t('Прежнее расписание действует до выбора нового режима.')));
    section.append(button(t('Отключить прежнее расписание'),'ghost',() => {
      device.schedule.mode = 'manual';
      if (sharedHome.connected()) sharedHome.schedule(device);
      else save(t('Расписание сохранено.'));
      render();
    }));
  }
  section.append(node('p','field-note',t(sharedHome.connected() ? 'Автоматика выполняется на локальном сервере.' : 'Повторное нажатие отключает режим. Автоматика работает, пока панель открыта.')));
  return section;
}
function roomMeasurement(device,metric) {
  const now = Date.now();
  const source = roomDevices(device.roomId).find(item => item.online &&
    Boolean(item.transport) === Boolean(device.transport) &&
    item.capabilities.some(capability => capability.id === metric && capability.type === 'sensor') &&
    item.state[metric] >= 0 && item.state[metric] <= 100 && Number.isFinite(item.state[metric]) &&
    (!item.transport || now - (measuredAt.get(item.id) ?? 0) <= konturAutomation.freshnessMs));
  if (!source) return undefined;
  return source.transport ? {value:source.state[metric],updatedAt:measuredAt.get(source.id)} : source.state[metric];
}
let lastMinute;
function runSchedules() {
  if (sharedHome.connected()) return;
  const now = Date.now();
  let changed = false;
  for (const device of data.devices) {
    if (busyDevices.has(device.id) || !deviceSession(device)) continue;
    const metric = konturAutomation.autoMetric(device);
    const measurements = metric ? {[metric]:roomMeasurement(device,metric)} : {};
    const {power,nextRun} = konturAutomation.evaluate(device,device.schedule,measurements,now,lastMinute);
    if (power !== undefined) {
      if (now < (automationRetryAt.get(device.id) ?? 0)) continue;
      automationRetryAt.set(device.id,now + 5000);
      const schedule = device.schedule;
      const deadline = schedule.nextRun;
      transmit(device,'power',power,true).then(confirmed => {
        if (confirmed && nextRun !== undefined && device.schedule === schedule && schedule.mode === 'interval' && schedule.nextRun === deadline) {
          schedule.nextRun = nextRun;
          save();
        }
      });
      continue;
    }
    if (nextRun !== undefined) {
      device.schedule.nextRun = nextRun;
      changed = true;
    }
  }
  lastMinute = Math.floor(now/60000);
  if (changed) save();
}
function deviceSession(device) {
  return lanSessions.get(device.id);
}
function applyTelemetry(device,session,state) {
  if (deviceSession(device) !== session || !state || typeof state !== 'object') return;
  measuredAt.set(device.id,Date.now());
  const changed = Object.entries(state).some(([key,value]) => device.state[key] !== value);
  if (!changed) return;
  Object.assign(device.state,state);
  save();
  if (!dialog.open && !busyDevices.has(device.id) && byId('schedule-value') !== document.activeElement) render();
  notifyWarnings();
}
async function transmit(device,capability,value,automatic=false) {
  const session = deviceSession(device);
  if (!session || busyDevices.has(device.id)) return;
  busyDevices.add(device.id);
  render();
  try {
    const state = await session.send(capability,value);
    applyTelemetry(device,session,state);
    if (!automatic && deviceSession(device) === session) status(t('Плата подтвердила команду.'));
    return deviceSession(device) === session;
  } catch {
    if (!automatic) status(t('Плата не подтвердила команду.'));
    return false;
  } finally {
    busyDevices.delete(device.id);
    if (!dialog.open && byId('schedule-value') !== document.activeElement) render();
  }
}
function addDevice(room) {
  const content = modal(t('Подключение прибора'));
  const lanResults = node('div','device-links');
  lanResults.setAttribute('aria-live','polite');
  const rescan = button(t('Обновить список'),'ghost',scanLan);
  async function scanLan() {
    rescan.disabled = true;
    lanResults.replaceChildren(node('p','muted',t('Поиск приборов в сети…')));
    try {
      const items = await konturLan.discover();
      const ids = new Set(data.devices.filter(device => device.online).map(device => device.id));
      const candidates = items.filter(item => !ids.has('lan-' + item.id) &&
        !data.devices.some(device => device.id === 'lan-' + item.id && (item.legacy || device.roomId !== room.id)));
      lanResults.replaceChildren(...candidates.map(item => {
        const kind = kinds[Object.hasOwn(kinds,item.kind) ? item.kind : 'other'];
        const title = item.legacy ? t(kind.label) : item.name;
        return tile(title,item.legacy ? t('Только обнаружение') : t(kind.label),kind.icon,kind.tone,() => pairLan(room,item));
      }));
      if (!candidates.length) lanResults.append(node('p','muted',t('Новых приборов в сети пока нет.')));
    } catch {
      if (!konturLan.available()) {
        const link = node('a','ghost',t('Открыть локальную панель'));
        link.href = 'http://localhost:8080/#page=devices';
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        lanResults.replaceChildren(node('p','sub',t('Запусти локальный сервис на компьютере в той же сети.')),link);
      } else lanResults.replaceChildren(node('p','muted',t('Поиск по Wi-Fi недоступен. Повтори попытку.')));

    } finally {
      rescan.disabled = false;
    }
  }
  const actions = node('div','actions');
  actions.append(button(t('Закрыть'),'ghost',() => dialog.close()));
  content.append(node('p','sub',t('Подключи ESP32 к домашней сети Wi-Fi.')),
    sectionHead(t('Устройства в сети'),rescan),lanResults,actions);
  rescan.focus();
  scanLan();
}
function pairLan(room,item) {
  const id = 'lan-' + item.id;
  if (item.legacy) {
    if (data.devices.some(device => device.id === id)) return;
    const kind = Object.hasOwn(kinds,item.kind) ? item.kind : 'other';
    data.devices.push({id,name:kinds[kind].label,nameIsDefault:true,kind,roomId:room.id,
      transport:'lan',online:false,capabilities:[],state:{}});
    complete(t('Прибор добавлен к комнате без управления.'));
    return;
  }
  const {form,error} = formModal(t('Подключить по Wi-Fi'),t('Подключить'));
  const token = node('input');
  token.type = 'password';
  token.autocomplete = 'off';
  token.maxLength = 128;
  token.required = item.requiresToken !== false;
  form.prepend(node('p','sub',item.name));
  if (token.required) field(form,error,t('Код прибора'),'lan-code',token);
  const submit = form.querySelector('[type=submit]');
  form.addEventListener('submit',async event => {
    event.preventDefault();
    submit.disabled = true;
    error.hidden = true;
    try {
      if (sharedHome.connected()) {
        await sharedHome.pair(room,item,token.value.trim());
        return;
      }
      const session = await konturLan.pair(item.id,token.value.trim());
      if (!dialog.open || !form.isConnected) return;
      const manifest = session.manifest;
      let device = data.devices.find(entry => entry.id === id);
      if (device && device.roomId !== room.id) throw Error('device-unavailable');
      if (!device) {
        device = {id,roomId:room.id,transport:'lan',nameIsDefault:false};
        data.devices.push(device);
      }
      Object.assign(device,{
        name:manifest.name,kind:Object.hasOwn(kinds,manifest.kind) ? manifest.kind : 'other',
        capabilities:manifest.capabilities,state:session.state,online:true
      });
      lanSessions.set(id,session);
      measuredAt.set(id,Date.now());
      complete(t('Прибор подключён к комнате «{name}».',{name:roomName(room)}));
      notifyWarnings();
    } catch (failure) {
      showError(error,sharedHome.connected() ? sharedHome.message(failure) : t(failure.message === 'device-token' ? 'Код прибора не подошёл.' : 'Не удалось подключиться по Wi-Fi.'));
    } finally {
      submit.disabled = false;
    }
  });
  (token.required ? token : submit).focus();
}
let refreshingLan = false;
async function pollLan() {
  if (refreshingLan || !lanSessions.size) return;
  refreshingLan = true;
  try {
    await Promise.all(data.devices.filter(device => lanSessions.has(device.id) && !busyDevices.has(device.id)).map(async device => {
      const session = lanSessions.get(device.id);
      try {
        const state = await session.read();
        if (!busyDevices.has(device.id) && lanSessions.get(device.id) === session) {
          const reconnected = !device.online;
          device.online = true;
          applyTelemetry(device,session,state);
          if (reconnected && !dialog.open) render();
        }
      } catch {
        if (lanSessions.get(device.id) !== session) return;
        if (device.online) {
          device.online = false;
          measuredAt.delete(device.id);
          save();
          if (!dialog.open && byId('schedule-value') !== document.activeElement) render();
        }
      }
    }));
  } finally {
    refreshingLan = false;
  }
}
for (const tab of byId('nav').children) tab.addEventListener('click',() => navigate(tab.dataset.page));
byId('room-picker').addEventListener('change',event => selectRoom(event.target.value));
document.querySelector('.skip-link').addEventListener('click',event => {
  event.preventDefault();
  byId('screen').focus();
});
window.addEventListener('hashchange',syncRoute);
konturPlatform.register();
syncRoute();
sharedHome.setup();
runSchedules();
notifyWarnings();
setInterval(runSchedules,1000);
setInterval(pollLan,5000);
