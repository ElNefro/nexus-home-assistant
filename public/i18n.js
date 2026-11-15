(() => {
  'use strict';

  const english = new Map(Object.entries({
    'Контур · Умный дом': 'Kontur · Smart Home',
    'Контур — управление климатом, приборами и комнатами умного дома.': 'Kontur lets you manage your smart home’s climate, devices and rooms.',
    'К содержимому': 'Skip to content',
    'Текущая комната': 'Current room',
    'Рабочая область': 'Main content',
    'Основная навигация': 'Main navigation',
    'Главная': 'Home',
    'Приборы': 'Devices',
    'Комнаты': 'Rooms',
    'Профиль': 'Profile',
    'Язык приложения': 'App language',
    'Гостиная': 'Living room',
    'Спальня': 'Bedroom',
    'Кухня': 'Kitchen',
    'Настройки аккаунта': 'Account settings',
    'Настройки дома': 'Home settings',
    'Имя и роль в доме': 'Your name and role in the home',
    'Владелец дома': 'Home owner',
    'Локальный профиль': 'Local profile',
    'Админ': 'Admin',
    'Изменить имя': 'Edit name',
    'Имя': 'Name',
    'Имя профиля обновлено.': 'Profile name updated.',
    'Введи имя.': 'Enter a name.',
    'Увлажнитель': 'Humidifier',
    'Увлажнитель (ESP32)': 'Humidifier (ESP32)',
    'Очиститель': 'Air purifier',
    'Очиститель воздуха': 'Air purifier',
    'Датчик климата': 'Climate sensor',
    'Освещение': 'Lighting',
    'Реле': 'Relay',
    'Другой прибор': 'Other device',
    'Ночник': 'Night light',
    'Реле вытяжки': 'Extractor fan relay',
    'Яркость': 'Brightness',
    'Температура': 'Temperature',
    'Влажность': 'Humidity',
    'Чистота воздуха': 'Air quality',
    'Целевая влажность': 'Target humidity',
    'Скорость вентилятора': 'Fan speed',
    'Уровень воды': 'Water level',
    'Питание': 'Power',
    'Вкл.': 'On',
    'Выкл.': 'Off',
    'В сети': 'Online',
    'Не в сети': 'Offline',
    'BLE проверен': 'BLE verified',
    'BLE проверен. Плата передаёт идентификатор.': 'BLE verified. The board shares its identifier.',
    'Нет показаний': 'No reading',
    'Климат комнаты «{name}»': 'Climate in {name}',
    'В баке закончилась вода': 'The water tank is empty',
    'Этот браузер не поддерживает уведомления с открытой страницы.': 'This browser cannot send notifications from an open page.',
    'Включить уведомления': 'Enable notifications',
    'Разреши уведомления в настройках браузера.': 'Allow notifications in your browser settings.',
    'Уведомления недоступны в этом браузере.': 'Notifications are unavailable in this browser.',
    'Предупреждения': 'Alerts',
    'В этой комнате нет предупреждений.': 'No alerts in this room.',
    'Список приборов': 'Device list',
    '＋ Добавить прибор': '＋ Add device',
    'В этой комнате пока нет приборов.': 'No devices in this room yet.',
    'Твои комнаты': 'Your rooms',
    '＋ Создать комнату': '＋ Create room',
    '‹ Комнаты': '‹ Rooms',
    '‹ Назад': '‹ Back',
    'Комнат пока нет.': 'No rooms yet.',
    'Настройки комнаты': 'Room settings',
    'Переименовать': 'Rename',
    'Добавить прибор': 'Add device',
    'Создать комнату': 'Create room',
    'Изменить комнату': 'Edit room',
    'Название комнаты': 'Room name',
    'Введи название комнаты.': 'Enter a room name.',
    'Комната с таким названием уже есть.': 'A room with this name already exists.',
    'Название комнаты обновлено.': 'Room name updated.',
    'Комната создана.': 'Room created.',
    'Сохранить': 'Save',
    'Отмена': 'Cancel',
    'Показания прибора': 'Device readings',
    'Показатели работы': 'Device status',
    'Настройки прибора': 'Device settings',
    'Датчики': 'Sensors',
    'Показывает остаток воды в баке увлажнителя.': 'Shows how much water remains in the humidifier tank.',
    'Измеряет температуру воздуха в комнате.': 'Measures room air temperature.',
    'Измеряет относительную влажность воздуха.': 'Measures relative humidity.',
    'Оценивает чистоту воздуха по шкале от 0 до 100%.': 'Estimates air quality on a scale from 0% to 100%.',
    'Текущее показание датчика.': 'Shows the current sensor reading.',
    'Расписание': 'Schedule',
    '15 минут': '15 minutes',
    '30 минут': '30 minutes',
    '1 час': '1 hour',
    '2 часа': '2 hours',
    '4 часа': '4 hours',
    '8 часов': '8 hours',
    '24 часа': '24 hours',
    'Включать каждые…': 'Turn on every…',
    'Авто': 'Auto',
    'Период': 'Interval',
    'Целевая влажность, %': 'Target humidity, %',
    'Расписание сохранено.': 'Schedule saved.',
    'Прибор включается через заданный период. Выключение вручную.': 'The device turns on at each interval. Turn it off manually.',
    'Для авторежима нужны показания датчика влажности в этой комнате.': 'Auto mode needs a humidity reading from this room.',
    'Включается ниже порога и выключается при его достижении.': 'Turns on below the target and off when it reaches the target.',
    'Повторное нажатие отключает режим. Автоматика работает, пока панель открыта.': 'Select the active mode again to disable it. Automations run while the dashboard is open.',
    'Включать в': 'Turn on at',
    'Выключать в': 'Turn off at',
    'Очистить': 'Clear',
    'Срабатывает ежедневно по местному времени, пока панель открыта.': 'Runs daily in the device’s local time while the dashboard is open.',
    'Браузер не сохранил изменения для следующего сеанса.': 'The browser could not save changes for your next session.',
    'Проверь синтаксис JSON.': 'Check the JSON syntax.',
    'Укажи schemaVersion: 1 и от 1 до 20 функций в capabilities.': 'Use schemaVersion: 1 and include 1–20 entries in capabilities.',
    'У каждой функции должен быть уникальный id. Начни со строчной латинской буквы и используй буквы, цифры, дефисы или подчёркивания.': 'Each function needs a unique id. Start with a lowercase Latin letter and use letters, digits, hyphens or underscores.',
    'Укажи label и тип switch, slider или sensor. Единица измерения unit — короткая строка.': 'Provide a label and one of the types switch, slider or sensor. Use a short string for unit.',
    'Для slider укажи числа min, max и положительный step в пределах диапазона.': 'For a slider, provide numeric min and max and a positive step within the range.',
    'Открой панель в Chrome на компьютере с Bluetooth.': 'Open the dashboard in Chrome on a computer with Bluetooth.',
    'Выбери «Увлажнитель» в списке браузера.': 'Select the humidifier in the browser’s device list.',
    'Кормушка': 'Pet feeder',
    'Добавлен по Wi-Fi': 'Added over Wi-Fi',
    'Умный дом': 'Smart home',
    'Подключён по Wi-Fi': 'Connected over Wi-Fi',
    'Найти в сети': 'Find on network',
    'Порог чистоты воздуха, %': 'Air quality threshold, %',
    'Авто ожидает показания датчика в этой комнате.': 'Auto is waiting for a sensor reading in this room.',
    'Прежнее расписание действует до выбора нового режима.': 'The previous schedule runs until you choose a new mode.',
    'Отключить прежнее расписание': 'Stop previous schedule',
    'Только обнаружение': 'Discovery only',
    'Новых приборов в сети пока нет.': 'No new devices found on the network.',
    'Открыть локальную панель': 'Open local dashboard',
    'Запусти локальный сервис на компьютере в той же сети.': 'Start the local service on a computer on the same network.',
    'Поиск по Wi-Fi недоступен. Повтори попытку.': 'Wi-Fi discovery is unavailable. Try again.',
    'Прибор добавлен к комнате без управления.': 'Device added to the room in discovery-only mode.',
    'Подключить по Wi-Fi': 'Connect over Wi-Fi',
    'Подключить': 'Connect',
    'Код прибора': 'Device code',
    'Код прибора не подошёл.': 'The device code was rejected.',
    'Не удалось подключиться по Wi-Fi.': 'Could not connect over Wi-Fi.',
    'Подключи ESP32 к домашней сети Wi-Fi.': 'Connect the ESP32 to your home Wi-Fi.',
    'Обновить список': 'Refresh list',
    'Поиск приборов в сети…': 'Searching for devices on the network…',
    'Уже добавлен': 'Already added',
    'В сети пока нет приборов «Контур».': 'No Kontur devices found on this network.',
    'Для поиска по Wi-Fi открой «Контур» через локальный сервис.': 'Open Kontur through the local service to discover Wi-Fi devices.',
    'Устройства в сети': 'Devices on the network',
    'Другие способы подключения': 'Other ways to connect',
    'Плата вернула неизвестный формат данных.': 'The board returned an unsupported data format.',
    'Этот прибор уже добавлен в комнату «{name}».': 'This device is already in {name}.',
    'без названия': 'an unnamed room',
    'BLE проверен. Панель прочитала идентификатор платы {id}.': 'BLE verified. The dashboard read board ID {id}.',
    'Прибор не выбран. Проверь питание ESP32 и повтори поиск.': 'No device selected. Check the ESP32 power and search again.',
    'Не удалось прочитать данные по Bluetooth: {error}': 'Could not read data over Bluetooth: {error}',
    'Не удалось подключиться по Bluetooth.': 'Could not connect via Bluetooth.',
    'Удерживай кнопку питания 10 секунд, пока индикатор не начнёт мигать.': 'Hold the power button for 10 seconds until the indicator flashes.',
    'Удерживай кнопку Wi-Fi 5 секунд, пока индикатор сети не начнёт мигать.': 'Hold the Wi-Fi button for 5 seconds until the network indicator flashes.',
    'Удерживай кнопку сопряжения 3 секунды.': 'Hold the pairing button for 3 seconds.',
    'Удерживай кнопку питания 5 секунд, пока лампа не мигнёт.': 'Hold the power button for 5 seconds until the light flashes.',
    'Удерживай кнопку SET 5 секунд, пока светодиод не начнёт мигать.': 'Hold the SET button for 5 seconds until the LED flashes.',
    'Переведи прибор в режим подключения по его инструкции.': 'Follow the device manual to enter pairing mode.',
    'Подключение прибора': 'Pair device',
    'Тип прибора': 'Device type',
    'Найти ESP32 по Bluetooth': 'Find ESP32 via Bluetooth',
    'Ожидание сигнала': 'Waiting for a signal',
    'Вручную': 'Add manually',
    'Прибор подключён к комнате «{name}».': 'Device paired with {name}.',
    'Прибор перемещён в комнату «{name}».': 'Device moved to {name}.',
    'Прибор добавлен в комнату «{name}».': 'Device added to {name}.',
    '{device} · из комнаты «{room}»': '{device} · from {room}',
    'Без комнаты': 'No room',
    'Закрыть': 'Close',
    'Добавить': 'Add',
    'Прибор': 'Device',
    'Название': 'Name',
    'Тип и цвет акцента': 'Type and accent color',
    'Функции (JSON)': 'Functions (JSON)',
    'Выбери прибор из списка.': 'Select a device from the list.',
    'Введи название прибора.': 'Enter a device name.',
    'Подключён по Bluetooth': 'Connected via Bluetooth',
    'Подключить по Bluetooth': 'Connect via Bluetooth',
    'Отключить Bluetooth': 'Disconnect Bluetooth',
    'Отключён': 'Disconnected',
    'Плата подтвердила команду.': 'The board confirmed the command.',
    'Плата не подтвердила команду.': 'The board did not confirm the command.',
    'Выбран другой прибор.': 'You selected a different device.',
    'Резервная копия': 'Backup',
    'Скачать копию': 'Download backup',
    'Восстановить из файла': 'Restore from file',
    'Восстановление заменит комнаты, приборы и настройки этого браузера.': 'Restoring will replace the rooms, devices and settings in this browser.',
    'Восстановить': 'Restore',
    'Копия восстановлена.': 'Backup restored.',
    'Не удалось прочитать копию.': 'Could not read the backup.',
    'Установить приложение': 'Install app',
    'Приложение уже установлено.': 'The app is already installed.',
    'В меню браузера выбери «Установить приложение» или «На экран Домой».': 'In your browser menu, choose Install app or Add to Home Screen.',
    'Данные хранятся в этом браузере.': 'Data stays in this browser.',
    'Уведомления приходят на этом устройстве, пока панель открыта.': 'Notifications appear on this device while the dashboard is open.',
    'Показания платы тестовые.': 'The board reports test readings.',
    'Доступ без интернета': 'Offline access',
    'Локальные данные': 'Local data',
    'Удалить прибор': 'Remove device',
    'Удалить': 'Remove',
    'Прибор «{device}» будет удалён из этой комнаты.': '{device} will be removed from this room.',
    'Прибор уже удалён.': 'The device has already been removed.',
    'Описание датчика должно содержать не более 500 символов.': 'Use up to 500 characters for the sensor description.',
    "Проверь адрес и пароль.": "Check your email and password.",
    "Аккаунт с таким адресом уже существует.": "An account with this email already exists.",
    "Адрес или код восстановления не подошёл.": "The email or recovery code was rejected.",
    "Приглашение недействительно или уже использовано.": "This invitation is invalid or was already used.",
    "Дом изменился в другой вкладке. Загружаем свежие данные.": "The home changed in another session. Loading current data.",
    "Доступ к этому дому отозван или действие доступно только владельцу.": "Home access was revoked or this action requires the owner.",
    "Войди снова, чтобы продолжить.": "Sign in again to continue.",
    "Прибор сейчас не в сети.": "The device is offline.",
    "Прибор уже привязан к другому дому. Попроси владельца удалить его.": "This device belongs to another home. Ask its owner to remove it.",
    "Прибор не подтвердил команду.": "The device did not confirm the command.",
    "На локальном сервере недоступна отправка push.": "Web Push is unavailable on the local server.",
    "Не удалось выполнить действие. Проверь локальный сервер и введённые данные.": "Could not complete the action. Check the local server and the information entered.",
    "Отправляем команду…": "Sending command\u2026",
    "Состояние подтверждено.": "State confirmed.",
    "Электронная почта": "Email",
    "Пароль": "Password",
    "Создать аккаунт": "Create account",
    "Зарегистрироваться": "Register",
    "Войти в аккаунт": "Sign in to an account",
    "Войти": "Sign in",
    "Восстановить доступ": "Recover account",
    "Изменить пароль": "Change password",
    "Код восстановления": "Recovery code",
    "Новый пароль": "New password",
    "Сохрани этот код. Он понадобится, если забудешь пароль.": "Save this code. You will need it if you forget your password.",
    "Создать дом": "Create home",
    "Создать": "Create",
    "Название дома": "Home name",
    "Перенести мои комнаты и приборы": "Import my rooms and devices",
    "Войти по приглашению": "Join by invitation",
    "Код приглашения": "Invitation code",
    "Владелец": "Owner",
    "Участник": "Member",
    "Отозвать доступ": "Revoke access",
    "Общий дом": "Shared home",
    "Создай аккаунт, чтобы управлять домом вместе.": "Create an account to manage your home together.",
    "Выйти": "Sign out",
    "Участники дома": "Home members",
    "Создать приглашение": "Create invitation",
    "Передай код человеку, которого хочешь пригласить.": "Share this code with the person you want to invite.",
    "Включить push": "Enable push",
    "Отключить push": "Disable push",
    "Уведомления отключены в настройках браузера.": "Notifications are disabled in your browser settings.",
    "Push включён для этого дома.": "Push is enabled for this home.",
    "Push отключён для этого дома.": "Push is disabled for this home.",
    "Для push открой установленное приложение в поддерживаемом браузере.": "For push, open the installed app in a supported browser.",
    "Нет подтверждённой доставки. Включи push на устройстве.": "No delivery was confirmed. Enable push on your device.",
    "Автоматика выполняется на локальном сервере.": "Automation runs on the local server.",
    "Последнее состояние: {time}": "Last seen: {time}",
    "Push настраивается в профиле.": "Manage push notifications in Profile.",
    'Переместить прибор': 'Move device',
    'Выбери комнату': 'Select a room',
  }));

  const roomDefaults = new Map([['living','Гостиная'],['bedroom','Спальня'],['kitchen','Кухня']]);
  const deviceDefaults = new Map([
    ['humidifier','Увлажнитель'],['purifier','Очиститель воздуха'],
    ['climate','Датчик климата'],['bedroom-climate','Датчик климата'],
    ['lamp','Ночник'],['relay','Реле вытяжки']
  ]);
  const capabilityDefaults = new Map([
    ['power','Питание'],['brightness','Яркость'],['target_humidity','Целевая влажность'],
    ['speed','Скорость вентилятора'],['temperature','Температура'],['humidity','Влажность'],
    ['air_quality','Чистота воздуха'],['water_level','Уровень воды']
  ]);
  const languageKey = 'kontur-language';
  let locale = globalThis.navigator?.language?.toLowerCase().startsWith('en') ? 'en' : 'ru';
  try {
    const storedLocale = localStorage.getItem(languageKey);
    if (storedLocale === 'ru' || storedLocale === 'en') locale = storedLocale;
  } catch {
    // The current page can still use either language with storage disabled.
  }
  const getLocale = () => locale;
  function setLocale(value) {
    if (value !== 'ru' && value !== 'en') return false;
    locale = value;
    try { localStorage.setItem(languageKey,locale); }
    catch { return false; }
    return true;
  }
  function t(source, values) {
    const translated = locale === 'en' ? english.get(source) ?? source : source;
    if (!values) return translated;
    return translated.replace(/\{(\w+)\}/g,(match,key) => Object.hasOwn(values,key) ? String(values[key]) : match);
  }
  function number(options) {
    return new Intl.NumberFormat(locale,options);
  }
  function deviceCount(count) {
    const category = new Intl.PluralRules(locale).select(count);
    const words = locale === 'en'
      ? {one:'device',other:'devices'}
      : {one:'прибор',few:'прибора',other:'приборов'};
    return number().format(count) + ' ' + (words[category] ?? words.other);
  }
  function roomName(room) {
    if (!room) return t('Без комнаты');
    return roomDefaults.get(room.id) === room.name ? t(room.name) : room.name;
  }
  function deviceName(device) {
    if (deviceDefaults.get(device.id) === device.name || device.transport === 'ble-test' && device.name === 'Увлажнитель (ESP32)' || device.nameIsDefault) return t(device.name);
    return device.name;
  }
  function profileName(profile) {
    return profile.name === 'Владелец дома' && !profile.customName ? t(profile.name) : profile.name;
  }
  function capabilityLabel(device,capability) {
    if (!device || deviceDefaults.has(device.id) || device.nameIsDefault) {
      if (capabilityDefaults.get(capability.id) === capability.label) return t(capability.label);
    }
    return capability.label;
  }
  function translateChrome() {
    document.documentElement.lang = locale;
    document.title = t('Контур · Умный дом');
    for (const element of document.querySelectorAll('[data-i18n]')) element.textContent = t(element.dataset.i18n);
    for (const element of document.querySelectorAll('[data-i18n-label]')) element.setAttribute('aria-label',t(element.dataset.i18nLabel));
    for (const element of document.querySelectorAll('[data-i18n-content]')) element.setAttribute('content',t(element.dataset.i18nContent));
  }

  globalThis.konturI18n = {t,getLocale,setLocale,number,deviceCount,roomName,deviceName,profileName,capabilityLabel,translateChrome};
})();
