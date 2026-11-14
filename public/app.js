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
  other:{label:'Другой прибор',tone:'#b6a3ff',icon:'◉'}
};
const metrics = [
  {id:'temperature',label:'Температура',unit:'°C',tone:'var(--amber)',min:0,max:40},
  {id:'humidity',label:'Влажность',unit:'%',tone:'var(--blue)',min:0,max:100},
  {id:'air_quality',label:'Чистота воздуха',unit:'%',tone:'var(--green)',min:0,max:100}
];
const climate = metrics.map(metric => ({id:metric.id,type:'sensor',label:metric.label,unit:metric.unit}));
const seedRooms = [
  {id:'living',name:'Гостиная'},
  {id:'bedroom',name:'Спальня'},
  {id:'kitchen',name:'Кухня'}
];
const seedDevices = [
  {id:'humidifier',name:'Увлажнитель',kind:'humidifier',roomId:'living',online:true,capabilities:[power,{id:'target_humidity',type:'slider',label:'Целевая влажность',min:30,max:70,step:1,unit:'%'}],state:{power:true,target_humidity:50}},
  {id:'purifier',name:'Очиститель воздуха',kind:'purifier',roomId:'living',online:true,capabilities:[power,{id:'speed',type:'slider',label:'Скорость вентилятора',min:1,max:5,step:1}],state:{power:true,speed:2}},
  {id:'climate',name:'Датчик климата',kind:'climate',roomId:'living',online:true,capabilities:climate,state:{temperature:23.4,humidity:46,air_quality:94}},
  {id:'bedroom-climate',name:'Датчик климата',kind:'climate',roomId:'bedroom',online:true,capabilities:climate,state:{temperature:21.8,humidity:52,air_quality:91}},
  {id:'lamp',name:'Ночник',kind:'light',roomId:'bedroom',online:false,capabilities:[power,brightness],state:{power:false,brightness:30}},
  {id:'relay',name:'Реле вытяжки',kind:'relay',roomId:'kitchen',online:true,capabilities:[power],state:{power:false}}
];
const storageKey = 'kontur-dashboard-v2';
function loadData() {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    if (saved !== null) return konturStorage.normalize(saved);
    const previous = JSON.parse(localStorage.getItem('kontur-smart-home-v1'));
    if (Array.isArray(previous)) {
      for (const device of previous) {
        const match = seedDevices.find(item => item.id === device.id);
        if (match) Object.assign(match.state,device.state);
        else if (device.id === 'light') {
          seedDevices.push({...device,roomId:'living',kind:'light'});
        } else {
          let room = seedRooms.find(item => item.name === device.room);
          if (!room) {
            room = {id:'room-' + seedRooms.length,name:device.room};
            seedRooms.push(room);
          }
          seedDevices.push({...device,roomId:room.id,kind:'other'});
        }
      }
    }
  } catch {}
  return {rooms:seedRooms,devices:seedDevices,profile:{name:'Владелец дома',role:'Админ'},roomId:'living'};
}
const data = loadData();
const bleSessions = new Map();
const busyBle = new Set();
let page = 'home';
let detail;
const water = {id:'water_level',type:'sensor',label:'Уровень воды',unit:'%',min:0,max:100,description:'Показывает остаток воды в баке увлажнителя.'};
function migrateWater() {
  const humidifier = data.devices.find(device => device.id === 'humidifier');
  if (humidifier && !humidifier.capabilities.some(item => item.id === water.id)) {
    humidifier.capabilities.push(water);
    humidifier.state.water_level = 0;
  }
}
migrateWater();
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
  if (device.transport === 'ble') return device.online ? t('Подключён по Bluetooth') : t('Отключён');
  if (device.transport === 'ble-test') return t('BLE проверен');
  return t('Демо-режим') + ' · ' + (device.online ? t('В сети') : t('Не в сети'));
}
function deviceTile(device,back) {
  const kind = kindFor(device);
  const connection = deviceStatus(device);
  return tile(deviceName(device),t(kind.label) + ' · ' + connection,kind.icon,kind.tone,() => showDevice(device,back));
}
function gauge(metric,devices) {
  const source = devices.find(device => device.online && device.capabilities.some(item => item.id === metric.id && item.type === 'sensor') && Number.isFinite(device.state[metric.id]));
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
  return [panel,warnings(devices)];
}
function warning(device) {
  return device.online && device.kind === 'humidifier' && device.state.water_level === 0;
}
const pendingNotifications = new Set();
async function notifyWarnings() {
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
  const enable = button(t('Включить уведомления'),'ghost',async () => {
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
  section.append(sectionHead(t('Предупреждения'),globalThis.Notification?.permission === 'granted' ? null : enable));
  section.append(node('p','field-note',t('Уведомления приходят на этом устройстве, пока панель открыта.')));
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
    sectionHead(t('Список приборов'),button(t('＋ Добавить прибор'),'primary',() => addDevice(room))),
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
    sectionHead(t('Твои комнаты'),button(t('＋ Создать комнату'),'primary',() => editRoom())),
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
  actions.append(button(t('Переименовать'),'ghost',() => editRoom(room,reopen)),button(t('Добавить прибор'),'primary',() => addDevice(room,reopen)));
  const devices = node('div','device-links');
  devices.append(list(roomDevices(room.id).map(device => deviceTile(device,reopen)),t('В этой комнате пока нет приборов.')));
  settings.append(actions,devices);
  content.append(settings);
  return [content];
}
function renderProfile() {
  const profile = node('div','panel');
  const top = node('div','profile');
  const initials = profileName(data.profile).split(/\s+/).slice(0,2).map(part => Array.from(part)[0]).join('').toUpperCase();
  const copy = node('div');
  copy.append(node('h2','',profileName(data.profile)),node('p','muted',t('Локальный профиль')));
  top.append(node('span','square',initials),copy,node('span','tag',t(data.profile.role)));
  profile.append(top,button(t('Изменить имя'),'ghost',editProfile));
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


  return [heading(t('Настройки аккаунта'),t('Профиль'),t('Имя и роль в доме')),profile,preferences,backup];
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
      const restored = konturStorage.decodeBackup(await selected.text());
      if (!dialog.open || !form.isConnected) return;
      for (const session of bleSessions.values()) session.disconnect();
      bleSessions.clear();
      Object.assign(data,restored);
      migrateWater();
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
function control(device,capability,updateGauge) {
  const result = node('div','control');
  const row = node('div','line');
  const label = node('span','',capabilityLabel(device,capability));
  const value = Object.hasOwn(device.state,capability.id) ? device.state[capability.id] : undefined;
  row.append(label);
  result.append(row);
  if (capability.type === 'switch') {
    const toggle = button('','switch',() => {
      const next = toggle.getAttribute('aria-checked') !== 'true';
      if (device.transport) {
        transmit(device,capability.id,next);
        return;
      }
      toggle.setAttribute('aria-checked',String(next));
      device.state[capability.id] = next;
      updateGauge?.(Number(next));
      save();
    });
    toggle.setAttribute('role','switch');
    toggle.setAttribute('aria-label',capabilityLabel(device,capability));
    toggle.setAttribute('aria-checked',String(Boolean(value)));
    toggle.disabled = !device.online || busyBle.has(device.id);
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
      input.disabled = !device.online || busyBle.has(device.id);
      input.setAttribute('aria-label',capabilityLabel(device,capability));
      input.addEventListener('input',() => {
        reading.textContent = valueText(input.valueAsNumber,capability.unit);
        updateGauge?.(input.valueAsNumber);
      });
      input.addEventListener('change',() => {
        if (device.transport) {
          transmit(device,capability.id,input.valueAsNumber);
          return;
        }
        device.state[capability.id] = input.valueAsNumber;
        save();
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
    if (capability.type === 'switch') {
      const metric = {...capability,label:capabilityLabel(device,capability),tone:kind.tone,min:0,max:1,format:value => value ? t('Вкл.') : t('Выкл.')};
      const indicator = makeGauge(metric,Number(Boolean(device.state[capability.id])));
      gauges.append(indicator.card);
      controls.append(control(device,capability,indicator.update));
      continue;
    }
    const metric = {...metrics.find(item => item.id === capability.id),...capability,label:capabilityLabel(device,capability),tone:kind.tone};
    const value = device.state[capability.id];
    const indicator = makeGauge(metric,value);
    gauges.append(indicator.card);
    if (capability.type === 'slider') controls.append(control(device,capability,indicator.update));
  }
  content.append(heading(t(kind.label),deviceName(device),deviceStatus(device)));
  const actions = node('div','row-buttons');
  actions.append(button(t('Удалить прибор'),'ghost',() => confirmRemoveDevice(device)));
  content.append(actions);
  if (device.transport) {
    const connection = node('section','panel');
    const action = device.online
      ? button(t('Отключить Bluetooth'),'ghost',() => bleSessions.get(device.id)?.disconnect())
      : button(t('Подключить по Bluetooth'),'primary',() => connectHumidifier(
        data.rooms.find(room => room.id === device.roomId),null,connectionStatus,action,device.id.slice(4)
      ));
    const connectionStatus = node('p','sub');
    connectionStatus.setAttribute('role','status');
    connection.append(node('h2','',t('Тестовая плата')),node('p','sub',t('Тестовая прошивка передаёт имитацию показаний.')),action,connectionStatus);
    content.append(connection);
  }
  if (gauges.childElementCount) {
    panel.prepend(node('h2','',t('Показатели работы')));
    content.append(panel);
  }
  if (controls.childElementCount) {
    const settings = node('section','panel');
    settings.append(node('h2','',t('Настройки прибора')),controls);
    if (!device.transport && device.capabilities.some(item => item.id === 'power' && item.type === 'switch')) settings.append(scheduleControls(device));
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
    const session = bleSessions.get(device.id);
    bleSessions.delete(device.id);
    session?.disconnect();
    data.devices.splice(index,1);
    dialog.close();
    const parentRoom = detail?.room;
    if (parentRoom) openDetail('room',parentRoom);
    else navigate(page);
    save();
  });
}

const periods = [[15,'15 минут'],[30,'30 минут'],[60,'1 час'],[120,'2 часа'],[240,'4 часа'],[480,'8 часов'],[1440,'24 часа']];
function startInterval(schedule, now) {
  schedule.nextRun = now + (schedule.period ?? 60) * 60000;
}
function scheduleControls(device) {
  if (device.kind !== 'humidifier') return dailyScheduleControls(device);
  const section = node('section','control');
  section.append(node('h3','',t('Расписание')));
  const modes = node('div','schedule-modes');
  const mode = device.schedule?.mode;
  for (const [id,label] of [['interval',t('Включать каждые…')],['auto',t('Авто')]]) {
    const choice = button(label,mode === id ? 'primary' : 'ghost',() => {
      const settings = device.schedule ??= {};
      settings.mode = mode === id ? 'manual' : id;
      if (settings.mode === 'interval') startInterval(settings,Date.now());
      save();
      if (!runHumidifierSchedules(Date.now())) render();
      byId('schedule-mode-' + id)?.focus();
    });
    choice.id = 'schedule-mode-' + id;
    choice.setAttribute('aria-pressed',String(mode === id));
    modes.append(choice);
  }
  section.append(modes);
  if (mode === 'interval' || mode === 'auto') {
    const settings = device.schedule;
    const interval = mode === 'interval';
    const input = node(interval ? 'select' : 'input');
    input.id = 'schedule-value';
    const label = node('label','',interval ? t('Период') : t('Целевая влажность, %'));
    label.htmlFor = input.id;
    if (interval) {
      for (const [minutes,text] of periods) {
        const option = node('option','',t(text));
        option.value = String(minutes);
        input.append(option);
      }
      input.value = String(settings.period ?? 60);
    } else {
      input.type = 'number';
      input.min = 1;
      input.max = 100;
      input.step = 1;
      input.required = true;
      input.value = String(settings.threshold ?? device.state.target_humidity ?? 50);
    }
    input.addEventListener('change',() => {
      if (!input.reportValidity()) return;
      if (interval) {
        settings.period = Number(input.value);
        startInterval(settings,Date.now());
      } else settings.threshold = Number(input.value);
      save(t('Расписание сохранено.'));
      runHumidifierSchedules(Date.now());
    });
    const row = node('div','schedule-row');
    row.append(label,input);
    section.append(row);
  }
  if (mode === 'interval') section.append(node('p','field-note',t('Прибор включается через заданный период. Выключение вручную.')));
  if (mode === 'auto') section.append(node('p','field-note',roomHumidity(device) === undefined
    ? t('Для авторежима нужны показания датчика влажности в этой комнате.')
    : t('Включается ниже порога и выключается при его достижении.')));
  section.append(node('p','field-note',t('Повторное нажатие отключает режим. Автоматика работает, пока панель открыта.')));
  return section;
}
function roomHumidity(device) {
  const source = roomDevices(device.roomId).find(item => !item.transport && item.online &&
    item.capabilities.some(capability => capability.id === 'humidity' && capability.type === 'sensor') &&
    Number.isFinite(item.state.humidity));
  return source?.state.humidity;
}
function runHumidifierSchedules(now) {
  let powerChanged = false;
  let scheduleChanged = false;
  for (const device of data.devices) {
    const schedule = device.schedule;
    if (device.transport || !device.online || device.kind !== 'humidifier' || !schedule) continue;
    let power;
    if (schedule.mode === 'auto') {
      const humidity = roomHumidity(device);
      if (humidity !== undefined) power = humidity < (schedule.threshold ?? device.state.target_humidity ?? 50);
    } else if (schedule.mode === 'interval' && now >= schedule.nextRun) {
      const period = (schedule.period ?? 60)*60000;
      schedule.nextRun += (Math.floor((now - schedule.nextRun)/period) + 1)*period;
      scheduleChanged = true;
      power = true;
    }
    if (power !== undefined && power !== device.state.power) {
      device.state.power = power;
      powerChanged = true;
    }
  }
  if (powerChanged || scheduleChanged) save();
  if (powerChanged && !dialog.open) render();
  return powerChanged && !dialog.open;
}
function dailyScheduleControls(device) {
  const section = node('section','control');
  section.append(node('h3','',t('Расписание')));
  for (const [key,label] of [['on',t('Включать в')],['off',t('Выключать в')]]) {
    const row = node('div','schedule-row');
    const input = node('input');
    input.type = 'time';
    input.id = 'schedule-' + key;
    input.value = device.schedule?.[key] ?? '';
    const caption = node('label','',label);
    caption.htmlFor = input.id;
    function update() {
      device.schedule ??= {};
      device.schedule[key] = input.value;
      save(t('Расписание сохранено.'));
    }
    input.addEventListener('change',update);
    row.append(caption,input,button(t('Очистить'),'ghost',() => {
      input.value = '';
      update();
    }));
    section.append(row);
  }
  section.append(node('p','field-note',t('Срабатывает ежедневно по местному времени, пока панель открыта.')));
  return section;
}
let lastMinute;
function runSchedules() {
  const now = new Date();
  runHumidifierSchedules(now.getTime());
  const minute = Math.floor(now.getTime()/60000);
  if (minute === lastMinute) return;
  lastMinute = minute;
  const time = [now.getHours(),now.getMinutes()].map(value => String(value).padStart(2,'0')).join(':');
  let changed = false;
  for (const device of data.devices) {
    if (device.transport || !device.online || !device.schedule || device.schedule.mode) continue;
    const power = device.schedule.off === time ? false : device.schedule.on === time ? true : undefined;
    if (power !== undefined && device.state.power !== power) {
      device.state.power = power;
      changed = true;
    }
  }
  if (changed) {
    save();
    if (!dialog.open) render();
  }
}
function parseManifest(raw) {
  let manifest;
  try { manifest = JSON.parse(raw); }
  catch { throw new Error(t('Проверь синтаксис JSON.')); }
  if (manifest?.schemaVersion !== 1 || !Array.isArray(manifest.capabilities) || manifest.capabilities.length < 1 || manifest.capabilities.length > 20) throw new Error(t('Укажи schemaVersion: 1 и от 1 до 20 функций в capabilities.'));
  const ids = new Set();
  for (const item of manifest.capabilities) {
    if (!item || typeof item.id !== 'string' || !/^[a-z][a-z0-9_-]{0,39}$/.test(item.id) || ids.has(item.id)) throw new Error(t('У каждой функции должен быть уникальный id. Начни со строчной латинской буквы и используй буквы, цифры, дефисы или подчёркивания.'));
    ids.add(item.id);
    if (!['switch','slider','sensor'].includes(item.type) || typeof item.label !== 'string' || !item.label.trim() || item.label.length > 60 || (item.unit !== undefined && (typeof item.unit !== 'string' || item.unit.length > 12))) throw new Error(t('Укажи label и тип switch, slider или sensor. Единица измерения unit — короткая строка.'));
    if (item.type === 'slider' && (![item.min,item.max,item.step,item.max-item.min].every(Number.isFinite) || item.max <= item.min || item.step <= 0 || item.step > item.max - item.min)) throw new Error(t('Для slider укажи числа min, max и положительный step в пределах диапазона.'));
  }
  if (!manifest.capabilities.every(konturStorage.capability)) {
    throw new Error(t('Описание датчика должно содержать не более 500 символов.'));
  }
  return manifest.capabilities;
}
function applyTelemetry(device,session,state) {
  if (bleSessions.get(device.id) !== session) return;
  const changed = Object.entries(state).some(([key,value]) => device.state[key] !== value);
  if (!changed) return;
  Object.assign(device.state,state);
  save();
  if (!dialog.open && !busyBle.has(device.id) && byId('schedule-value') !== document.activeElement) render();
  notifyWarnings();
}
async function transmit(device,capability,value) {
  const session = bleSessions.get(device.id);
  if (!session || busyBle.has(device.id)) return;
  busyBle.add(device.id);
  render();
  try {
    const state = await session.send(capability,value);
    applyTelemetry(device,session,state);
    status(t('Плата подтвердила команду.'));
  } catch {
    status(t('Плата не подтвердила команду.'));
  } finally {
    busyBle.delete(device.id);
    if (!dialog.open) render();
  }
}
async function connectHumidifier(room,back,feedback,search,expectedId) {
  if (!navigator.bluetooth) {
    feedback.textContent = t('Открой панель в Chrome на компьютере с Bluetooth.');
    return;
  }
  let connection;
  search.disabled = true;
  try {
    feedback.textContent = t('Выбери «Увлажнитель» в списке браузера.');
    connection = await konturBle.choose(navigator.bluetooth,expectedId);
    if (!search.isConnected || (!expectedId && !dialog.open)) {
      connection.disconnect?.();
      return;
    }
    const id = 'ble-' + connection.identity.id;
    let device = data.devices.find(item => item.id === id);
    if (device && device.roomId !== room.id) {
      feedback.textContent = t('Этот прибор уже добавлен в комнату «{name}».',{name:roomName(data.rooms.find(item => item.id === device.roomId))});
      connection.disconnect?.();
      return;
    }
    if (!device) {
      device = {id,name:'Увлажнитель (ESP32)',nameIsDefault:true,kind:'humidifier',roomId:room.id,capabilities:[],state:{}};
      data.devices.push(device);
    }
    device.transport = connection.state ? 'ble' : 'ble-test';
    device.online = Boolean(connection.state);
    device.state = connection.state ?? {};
    device.capabilities = connection.state ? [
      power,{id:'target_humidity',type:'slider',label:t('Целевая влажность'),min:30,max:70,step:1,unit:'%'},
      {id:'humidity',type:'sensor',label:'Влажность',unit:'%',min:0,max:100},water
    ] : [];
    if (connection.state) {
      const oldSession = bleSessions.get(id);
      bleSessions.set(id,connection);
      oldSession?.disconnect();
      connection.subscribe(state => applyTelemetry(device,connection,state),() => {
        if (bleSessions.get(id) !== connection) return;
        bleSessions.delete(id);
        device.online = false;
        save();
        if (!dialog.open) render();
      });
    }
    const message = connection.state ? t('Подключён по Bluetooth') : t('BLE проверен');
    if (expectedId) {
      save(message);
      render();
    } else complete(message,back);
    notifyWarnings();
  } catch (error) {
    connection?.disconnect?.();
    feedback.textContent = error.name === 'NotFoundError'
      ? t('Прибор не выбран. Проверь питание ESP32 и повтори поиск.')
      : error.message === 'wrong-device' ? t('Выбран другой прибор.') : t('Не удалось подключиться по Bluetooth.');
  } finally {
    search.disabled = false;
  }
}
const pairing = {
  humidifier:'Удерживай кнопку питания 10 секунд, пока индикатор не начнёт мигать.',
  purifier:'Удерживай кнопку Wi-Fi 5 секунд, пока индикатор сети не начнёт мигать.',
  climate:'Удерживай кнопку сопряжения 3 секунды.',
  light:'Удерживай кнопку питания 5 секунд, пока лампа не мигнёт.',
  relay:'Удерживай кнопку SET 5 секунд, пока светодиод не начнёт мигать.',
  other:'Переведи прибор в режим подключения по его инструкции.'
};
function addDevice(room,back) {
  const content = modal(t('Подключение прибора'));
  const models = node('select');
  models.setAttribute('aria-label',t('Тип прибора'));
  for (const [id,kind] of Object.entries(kinds)) {
    const option = node('option','',t(kind.label));
    option.value = id;
    models.append(option);
  }
  models.value = 'humidifier';
  const hint = node('p','sub');
  const feedback = node('p','sub');
  feedback.setAttribute('role','status');
  const search = button(t('Найти ESP32 по Bluetooth'),'primary',() => connectHumidifier(room,back,feedback,search));
  const found = node('div','device-links');
  function waitForSignal() {
    hint.textContent = t(pairing[models.value]);
    search.hidden = models.value !== 'humidifier';
    feedback.textContent = search.hidden ? '' : t('Тестовая прошивка подаёт сигнал сразу после включения платы.');
    found.replaceChildren(node('p','muted',t('Ожидание сигнала')));
  }
  models.addEventListener('change',waitForSignal);
  waitForSignal();
  const actions = node('div','actions');
  actions.append(button(t('Вручную'),'ghost',() => addDeviceManually(room,back)),button(t('Сымитировать сигнал'),'primary',() => {
    const type = models.value;
    const kind = kinds[type];
    const template = data.devices.find(item => item.kind === type);
    found.replaceChildren(tile(t(kind.label),t('Демоустройство обнаружено'),kind.icon,kind.tone,() => {
      const capabilities = template?.capabilities ?? [power];
      const state = {...(template?.state ?? {power:false})};
      if (type === 'humidifier') state.water_level = 100;
      data.devices.push({id:idFor(),name:kind.label,nameIsDefault:true,kind:type,roomId:room.id,online:true,capabilities,state});
      complete(t('Прибор подключён к комнате «{name}».',{name:roomName(room)}),back);
    }));
  }),button(t('Закрыть'),'ghost',() => dialog.close()));
  content.append(models,hint,search,feedback,found,actions);
  models.focus();
}
function addDeviceManually(room,back) {
  const {form,error} = formModal(t('Добавить прибор'),t('Добавить'),back);
  const mode = node('select');
  const create = node('option','',t('Новое демоустройство'));
  create.value = '';
  mode.append(create);
  for (const device of data.devices.filter(item => item.roomId !== room.id)) {
    const source = data.rooms.find(item => item.id === device.roomId);
    const option = node('option','',t('{device} · из комнаты «{room}»',{device:deviceName(device),room:source ? roomName(source) : t('Без комнаты')}));
    option.value = device.id;
    mode.append(option);
  }
  const name = nameField('');
  const kind = node('select');
  for (const [id,item] of Object.entries(kinds)) {
    const option = node('option','',t(item.label));
    option.value = id;
    kind.append(option);
  }
  kind.value = 'other';
  const manifest = node('textarea');
  manifest.required = true;
  manifest.spellcheck = false;
  manifest.value = JSON.stringify({schemaVersion:1,capabilities:[power,brightness]},null,2);
  field(form,error,t('Прибор'),'device-mode',mode);
  const group = node('div');
  const stop = node('span');
  group.append(stop);
  field(group,stop,t('Название'),'device-name',name);
  field(group,stop,t('Тип и цвет акцента'),'device-kind',kind);
  field(group,stop,t('Функции (JSON)'),'device-manifest',manifest);
  stop.remove();
  form.insertBefore(group,error);
  mode.addEventListener('change',() => {
    group.hidden = Boolean(mode.value);
    name.disabled = group.hidden;
    manifest.disabled = group.hidden;
    error.hidden = true;
  });
  mode.focus();
  form.addEventListener('submit',event => {
    event.preventDefault();
    if (mode.value) {
      const device = data.devices.find(item => item.id === mode.value);
      if (!device) return showError(error,t('Выбери прибор из списка.'));
      device.roomId = room.id;
      return complete(t('Прибор перемещён в комнату «{name}».',{name:roomName(room)}),back);
    }
    const title = name.value.trim();
    if (!title) return showError(error,t('Введи название прибора.'));
    let capabilities;
    try { capabilities = parseManifest(manifest.value); }
    catch (failure) { return showError(error,failure.message); }
    const state = Object.fromEntries(capabilities.filter(item => item.type !== 'sensor').map(item => [item.id,item.type === 'switch' ? false : item.min]));
    data.devices.push({id:idFor(),name:title,kind:kind.value,roomId:room.id,online:true,capabilities,state});
    complete(t('Прибор добавлен в комнату «{name}».',{name:roomName(room)}),back);
  });
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
runSchedules();
notifyWarnings();
setInterval(runSchedules,1000);
