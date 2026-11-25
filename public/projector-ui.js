'use strict';

(function expose(root) {
  const model = root.konturProjector;
  const commandCooldownMs = 1000;
  const sessions = new Map();
  const handled = new Set(['red_toggle','green_toggle','blue_toggle','white_toggle',
    'laser_toggle','laser_steady','laser_pulse','color_jump','color_fade','purple_from_white','light_toggle']);

  function reset(id) {
    if (id === undefined) sessions.clear();
    else sessions.delete(id);
  }

  function sessionFor(device) {
    if (!device.online) reset(device.id);
    if (!sessions.has(device.id)) sessions.set(device.id,{...model.initial(),busy:false,notice:''});
    return sessions.get(device.id);
  }

  function render(device,context) {
    const {node,button,t} = context;
    const session = sessionFor(device);
    const available = id => device.capabilities.some(cap => cap.id === id && cap.momentary === true);
    const unavailable = () => !device.online || session.busy || context.isBusy() || sessions.get(device.id) !== session;
    const disabled = unavailable();
    const content = node('div','projector-controls');
    content.setAttribute('aria-label',t('Управление проектором'));
    const redraw = () => { context.redraw(); };

    async function execute(plan,domain,focusId) {
      if (unavailable()) return;
      if (plan.actions.some(id => !available(id))) {
        session.notice = t('Для этих кнопок обнови прошивку и найди прибор в сети заново.');
        redraw();
        return;
      }
      if (!plan.actions.length) {
        session.notice = t('Этот вариант уже выбран в приложении. Если свет отличается, сверь настройки.');
        redraw();
        return;
      }
      session.busy = true;
      session.notice = t('Отправляем настройки…');
      redraw();
      try {
        for (let index = 0; index < plan.actions.length; index++) {
          if (sessions.get(device.id) !== session) return;
          if (await context.send(plan.actions[index]) !== true) {
            throw new Error('not-confirmed');
          }
          // Keep the whole panel locked through the final cooldown as well, so
          // another palette selection cannot bypass the projector's IR pause.
          await new Promise(resolve => setTimeout(resolve,commandCooldownMs));
          if (sessions.get(device.id) !== session) return;
        }
        if (sessions.get(device.id) !== session) return;
        if (domain === 'color') {
          session.color = plan.next;
          session.lighting = 'on';
        }
        else if (domain === 'laser') session.laser = plan.next;
        else if (domain === 'power') Object.assign(session,plan.next);
        else {
          Object.assign(session,model.invalidate(session));
          session.lighting = 'unknown';
          delete session.resume;
        }
        session.notice = t('Настройки отправлены. Сверь результат с проектором.');
      } catch {
        if (domain === 'color') {
          session.color = null;
          session.lighting = 'unknown';
          delete session.resume;
        }
        else if (domain === 'laser') session.laser = 'unknown';
        else {
          Object.assign(session,model.initial(),{lighting:'unknown'});
          delete session.resume;
        }
        session.notice = t('Отправка не завершена. Перед повтором сверь настройки с проектором.');
      } finally {
        session.busy = false;
        redraw();
        if (focusId && sessions.get(device.id) === session) document.getElementById(focusId)?.focus({preventScroll:true});
      }
    }

    function chooseColor(id) {
      if (unavailable()) return;
      const lighting = model.lightingState(session);
      if (lighting === 'off' && (!model.isColor(session.color) || !Object.values(session.color).some(Boolean))) {
        session.notice = t('Сначала включи светильник и сверь цвет подсветки.');
        redraw();
        return;
      }
      if (!model.isColor(session.color)) return calibrateColor(() => chooseColor(id));
      const plan = model.colorPlan(session.color,id);
      if (lighting === 'off') plan.actions.unshift('light_toggle');
      void execute(plan,'color','projector-color-' + id);
    }

    function choosePower(target) {
      if (unavailable() || !available('light_toggle')) return;
      if (model.lightingState(session) === 'unknown') return calibrateColor(() => choosePower(target));
      if (session.laser === 'unknown') return calibrateLaser(() => choosePower(target));
      void execute(model.powerPlan(session,target),'power','projector-power-' + target);
    }

    function chooseLaser(target) {
      if (unavailable()) return;
      if (session.laser === 'unknown') return calibrateLaser(() => chooseLaser(target));
      void execute(model.laserPlan(session.laser,target),'laser','projector-laser-' + target);
    }

    function calibrateColor(after) {
      if (unavailable()) return;
      const {form,error} = context.formModal(t('Сверь цвет с проектором'),t(after ? 'Применить цвет' : 'Сохранить'));
      const submit = form.querySelector('[type=submit]');
      submit.disabled = true;
      form.insertBefore(node('p','projector-caption',t('Выбери текущий неподвижный цвет подсветки. Это исходная точка для палитры.')),error);
      const choices = node('div','projector-calibration-swatches');
      choices.setAttribute('role','group');
      choices.setAttribute('aria-label',t('Текущий цвет подсветки'));
      let selected = null;
      const presetButtons = [];
      const inputs = new Map();
      function select(channels) {
        selected = {...channels};
        submit.disabled = false;
        for (const [channel,input] of inputs) input.checked = selected[channel];
        for (const [channels,choice] of presetButtons) {
          choice.setAttribute('aria-pressed',String(Object.keys(channels).every(key => channels[key] === selected[key])));
        }
      }
      for (const preset of [...model.palettes,{id:'dark',label:'Нет подсветки',hex:'#332b43',channels:{r:false,g:false,b:false,w:false}}]) {
        const choice = button(t(preset.label),'projector-choice',() => select(preset.channels));
        choice.style.setProperty('--swatch',preset.hex);
        choice.classList.add('projector-current-chip');
        choice.setAttribute('aria-pressed','false');
        presetButtons.push([preset.channels,choice]);
        choices.append(choice);
      }
      form.insertBefore(choices,error);
      const custom = node('details');
      custom.append(node('summary','projector-caption',t('Другая смесь цветов')));
      const channels = node('div','projector-calibration-grid');
      for (const [channel,label] of [['r','Красный'],['g','Зелёный'],['b','Синий'],['w','Белый']]) {
        const input = node('input');
        input.type = 'checkbox';
        input.addEventListener('change',() => select(Object.fromEntries([...inputs].map(([key,field]) => [key,field.checked]))));
        const text = node('label');
        text.append(input,document.createTextNode(t(label)));
        inputs.set(channel,input);
        channels.append(text);
      }
      custom.append(channels);
      form.insertBefore(custom,error);
      form.addEventListener('submit',event => {
        event.preventDefault();
        if (!selected || unavailable()) return;
        const illuminated = Object.values(selected).some(Boolean);
        session.color = illuminated ? {...selected} : null;
        session.lighting = illuminated ? 'on' : 'off';
        delete session.resume;
        session.notice = t('Исходный цвет указан вручную.');
        context.closeDialog();
        redraw();
        if (after) after();
      });
    }

    function calibrateLaser(after) {
      if (unavailable()) return;
      const {form,error} = context.formModal(t('Сверь лазер с проектором'),t(after ? 'Применить настройку' : 'Сохранить'));
      const submit = form.querySelector('[type=submit]');
      submit.disabled = true;
      form.insertBefore(node('p','projector-caption',t('Как сейчас работает лазер на ночнике?')),error);
      const choices = node('div','projector-tools');
      choices.setAttribute('role','group');
      choices.setAttribute('aria-label',t('Текущее состояние лазера'));
      let selected;
      const buttons = [];
      for (const [value,label] of [['off','Выключен'],['pulse','Пульсация'],['steady','Постоянное свечение']]) {
        const choice = button(t(label),'projector-choice',() => {
          selected = value;
          submit.disabled = false;
          for (const [mode,element] of buttons) element.setAttribute('aria-pressed',String(mode === value));
        });
        choice.setAttribute('aria-pressed','false');
        buttons.push([value,choice]);
        choices.append(choice);
      }
      form.insertBefore(choices,error);
      form.addEventListener('submit',event => {
        event.preventDefault();
        if (!selected || unavailable()) return;
        session.laser = selected;
        delete session.resume;
        session.notice = t('Исходное состояние лазера указано вручную.');
        context.closeDialog();
        redraw();
        if (after) after();
      });
    }

    function card(title,eyebrow) {
      const element = node('section','projector-card');
      const header = node('div','projector-card-header');
      const titleBlock = node('div');
      titleBlock.append(node('p','projector-eyebrow',t(eyebrow)),node('h2','projector-card-title',t(title)));
      header.append(titleBlock);
      element.append(header);
      return element;
    }

    if (available('light_toggle')) {
      const powerCard = card('Светильник','Общее управление');
      powerCard.classList.add('projector-power');
      powerCard.append(node('p','projector-caption',t('Включает или гасит подсветку и лазер.')));
      const row = node('div','projector-segments');
      row.setAttribute('role','group');
      row.setAttribute('aria-label',t('Питание светильника'));
      const lighting = model.lightingState(session);
      const laserActive = !['unknown','off'].includes(session.laser);
      for (const [value,label] of [['on','Включить'],['off','Выключить']]) {
        const choice = button(t(label),'projector-choice',() => choosePower(value));
        choice.id = 'projector-power-' + value;
        choice.setAttribute('aria-pressed',String(value === 'on' ? lighting === 'on' || laserActive : lighting === 'off' && session.laser === 'off'));
        choice.disabled = disabled;
        row.append(choice);
      }
      powerCard.append(row);
      content.append(powerCard);
    }

    const color = card('Цвет подсветки','Палитра');
    color.append(node('p','projector-caption',t('Выбери оттенок на круге. Белый — в центре.')));
    const palette = node('div','projector-wheel');
    palette.setAttribute('role','group');
    palette.setAttribute('aria-label',t('Круговая палитра'));
    const selectedId = model.colorId(session.color);
    const positions = [[50,10],[84.6,30],[84.6,70],[50,90],[15.4,70],[15.4,30]];
    model.palettes.forEach((preset,index) => {
      const choice = button(preset.id === 'white' ? t('Белый') : '',preset.id === 'white' ? 'projector-white' : 'projector-swatch',() => chooseColor(preset.id));
      choice.id = 'projector-color-' + preset.id;
      choice.setAttribute('aria-label',t(preset.label));
      choice.title = t(preset.label);
      choice.setAttribute('aria-pressed',String(preset.id === selectedId));
      choice.style.setProperty('--swatch',preset.hex);
      if (preset.id !== 'white') {
        choice.style.setProperty('--x',positions[index][0] + '%');
        choice.style.setProperty('--y',positions[index][1] + '%');
      }
      choice.disabled = disabled;
      palette.append(choice);
    });
    color.append(palette);
    const selected = node('div','projector-selection');
    const preset = model.palettes.find(item => item.id === selectedId);
    const colorName = preset?.label ?? (session.color ? 'Смешанный цвет' : 'Выбери исходный цвет');
    const chip = node('span','projector-current-chip',t(colorName));
    chip.style.setProperty('--swatch',preset?.hex ?? '#8e7f9f');
    selected.append(chip);
    color.append(selected);
    const colorTools = node('div','projector-tools');
    const syncColor = button(t('Сверить цвет'),'projector-sync',() => calibrateColor());
    syncColor.disabled = disabled;
    colorTools.append(syncColor);
    color.append(colorTools);
    if (available('color_jump') || available('color_fade')) {
      const effects = node('div','projector-effects');
      effects.append(node('h3','projector-mode-title',t('Смена цветов')));
      const modes = node('div','projector-segments');
      for (const [id,label] of [['color_jump','Резкая'],['color_fade','Плавная']]) {
        if (!available(id)) continue;
        const choice = button(t(label),'projector-choice',() => void execute({actions:[id]},'effect',id));
        choice.id = id;
        choice.disabled = disabled;
        modes.append(choice);
      }
      effects.append(modes);
      color.append(effects);
    }

    const laser = card('Лазер','Звёздное небо');
    const power = node('div','projector-segments');
    power.setAttribute('role','group');
    power.setAttribute('aria-label',t('Питание лазера'));
    for (const [value,label] of [['off','Выключен'],['on','Включен']]) {
      const choice = button(t(label),'projector-choice',() => chooseLaser(value));
      choice.id = 'projector-laser-' + value;
      choice.setAttribute('aria-pressed',String(value === 'off' ? session.laser === 'off' : !['unknown','off'].includes(session.laser)));
      choice.disabled = disabled;
      power.append(choice);
    }
    laser.append(power);
    const laserMode = node('div','projector-effects');
    laserMode.hidden = ['unknown','off'].includes(session.laser);
    laserMode.append(node('h3','projector-mode-title',t('Режим свечения')));
    const modes = node('div','projector-segments');
    modes.setAttribute('role','group');
    modes.setAttribute('aria-label',t('Эффект лазера'));
    for (const [value,label] of [['pulse','Пульсация'],['steady','Постоянное свечение']]) {
      const choice = button(t(label),'projector-choice',() => chooseLaser(value));
      choice.id = 'projector-laser-' + value;
      choice.setAttribute('aria-pressed',String(session.laser === value));
      choice.disabled = disabled || !available(value === 'pulse' ? 'laser_pulse' : 'laser_steady');
      modes.append(choice);
    }
    laserMode.append(modes);
    laser.append(laserMode);
    const laserTools = node('div','projector-tools');
    const syncLaser = button(t('Сверить лазер'),'projector-sync',() => calibrateLaser());
    syncLaser.disabled = disabled;
    laserTools.append(syncLaser);
    laser.append(laserTools);
    laser.append(node('p','projector-note',t('Показан выбор в приложении. Если менял свет другим способом, сверь его заново.')));
    if (session.laser === 'unknown') laser.append(node('p','projector-caption',t('Перед первым переключением укажи, как сейчас работает лазер.')));
    content.append(color,laser);
    const notice = node('p','projector-status',session.notice);
    notice.setAttribute('role','status');
    notice.setAttribute('aria-live','polite');
    content.append(notice);
    return content;
  }

  root.konturProjectorUi = {render,reset,handled};
})(globalThis);
