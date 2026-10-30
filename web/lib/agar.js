/* 单玩家吞噬背景。复用页面的统一帧调度，不占用滚轮或键盘。 */
export function initAgar(canvas, menuItems = []) {
  const abort = new AbortController();
  const signal = abort.signal;
  let animationId = 0;
  const motion = {
    addFrame(fn) {
      const tick = (now) => {
        fn(now);
        animationId = requestAnimationFrame(tick);
      };
      animationId = requestAnimationFrame(tick);
    },
  };
  if (!canvas || !motion) return;
  const ctx = canvas.getContext("2d", { alpha: true });
  if (!ctx) return;

  const TAU = Math.PI * 2;
  const VERSION = "menu-bots-v3";
  const rules = {
    worldWidth: 4200,
    worldHeight: 3400,
    startRadius: 36,
    eatMassRatio: 1.25,
    overlapAllowance: 0.35,
    botCount: 24,
    foodCount: 450,
    foodMassMin: 9,
    foodMassMax: 25,
    playerSpeed: 245,
    respawnProtectionSeconds: 4,
    frameRate: 30,
  };
  const colors = [
    "#c99b49",
    "#658e75",
    "#648b9b",
    "#c67777",
    "#9984af",
    "#ca9473",
  ];
  const menuHref = (value) => {
    const href = typeof value === "string" ? value.trim() : "";
    if (/[\\\n\r\t]/.test(href)) return "";
    if (href.startsWith("#") && href.length > 1) return href;
    if (href.startsWith("/") && !href.startsWith("//")) return href;
    if (/^https?:\/\//i.test(href)) return href;
    return "";
  };
  const menuSize = (value) => {
    const size = Number(value);
    return Number.isFinite(size) ? Math.min(2.5, Math.max(0.4, size)) : 1;
  };
  const menus = Object.freeze(
    (Array.isArray(menuItems) ? menuItems : [])
      .map((item) => {
        const id = typeof item?.id === "string" ? item.id.trim() : "";
        const name =
          typeof item?.name === "string"
            ? item.name.trim()
            : typeof item?.label === "string"
              ? item.label.trim()
              : "";
        const href = menuHref(item?.href);
        if (!/^[A-Za-z0-9_-]{1,64}$/.test(id) || !name || !href) return null;
        return Object.freeze({
          id,
          name,
          href,
          size: menuSize(item?.size ?? item?.bot_size),
        });
      })
      .filter(Boolean),
  );
  // 隐藏球保持在原有的第 7 个初始槽位，避免既有视觉与球大小发生漂移。
  const secretSlot = menus.length ? 6 : 0;
  // 菜单增加时自动扩充球数；没有菜单时仍保留最大的隐藏球。
  const botCount = menus.length
    ? Math.max(rules.botCount, menus.length + 1)
    : 1;
  rules.botCount = botCount;
  const state = {
    player: { id: "player", name: "YOU", x: 2100, y: 1700, mass: 1296, r: 36 },
    bots: [],
    foods: [],
    camera: { x: 2100, y: 1700, zoom: 1 },
    paused: false,
    consumedBots: 0,
    protection: 0,
  };
  const pointer = { x: 0, y: 0, active: false, interactive: false };
  const particles = [];
  const ripples = [];
  const reducedMotion = window.matchMedia(
    "(prefers-reduced-motion: reduce)",
  ).matches;
  let width = innerWidth;
  let height = innerHeight;
  let pixelRatio = 1;
  let botSerial = 0;
  let lastFrame = 0;
  let lastStats = -Infinity;
  let hidden = document.hidden;
  let editing = false;
  let dirty = true;
  let ready =
    !document.getElementById("loader") ||
    document.getElementById("loader").style.display === "none";
  let nearbyCheckIn = 0;

  const random = (min, max) => min + Math.random() * (max - min);
  const clamp = (n, min, max) => Math.min(max, Math.max(min, n));
  const distanceSquared = (a, b) => (a.x - b.x) ** 2 + (a.y - b.y) ** 2;
  const speed = (cell, factor = 1) =>
    rules.playerSpeed * factor * (rules.startRadius / cell.r) ** 0.35;
  const interactiveTarget = (target) =>
    target instanceof Element &&
    !!target.closest(
      'a, button, input, textarea, select, [contenteditable="true"], dialog, [role="dialog"]',
    );

  function canEat(predator, prey) {
    if (predator.mass < prey.mass * rules.eatMassRatio) return false;
    const coverage = predator.r - prey.r * rules.overlapAllowance;
    return (
      coverage > 0 && distanceSquared(predator, prey) <= coverage * coverage
    );
  }

  function grow(cell, gain) {
    cell.mass += gain;
    cell.r = Math.sqrt(cell.mass);
  }

  function keepInWorld(cell) {
    cell.x = clamp(cell.x, cell.r, rules.worldWidth - cell.r);
    cell.y = clamp(cell.y, cell.r, rules.worldHeight - cell.r);
  }

  function emitStats(force = false, now = performance.now()) {
    if (!force && now - lastStats < 250) return;
    lastStats = now;
    window.dispatchEvent(
      new CustomEvent("agar:stats", {
        detail: {
          mass: state.player.mass,
          consumedBots: state.consumedBots,
          paused: state.paused,
        },
      }),
    );
  }

  function makeFood(nearPlayer = false) {
    const mass = random(rules.foodMassMin, rules.foodMassMax);
    let x = random(12, rules.worldWidth - 12);
    let y = random(12, rules.worldHeight - 12);
    if (nearPlayer) {
      const angle = random(0, TAU);
      const distance = random(
        state.player.r + 24,
        Math.max(620, state.player.r * 3),
      );
      x = clamp(
        state.player.x + Math.cos(angle) * distance,
        12,
        rules.worldWidth - 12,
      );
      y = clamp(
        state.player.y + Math.sin(angle) * distance,
        12,
        rules.worldHeight - 12,
      );
    }
    return { x, y, mass, r: Math.sqrt(mass), color: Math.floor(random(0, 3)) };
  }

  function makeBot(index, initial = false) {
    const secret = menus.length ? index === secretSlot : index === 0;
    const menu = secret
      ? null
      : menus[(index - (index > secretSlot ? 1 : 0)) % menus.length];
    const sizes = [0.5, 0.65, 0.78, 1.25, 1.5, 1.9, 2.25, 0.9];
    const menuScale = menu?.size || 1;
    let radius = initial
      ? rules.startRadius * sizes[index % sizes.length] * menuScale
      : clamp(
          state.player.r *
            sizes[index % sizes.length] *
            menuScale *
            random(0.88, 1.12),
          18,
          180,
        );
    if (secret)
      radius = Math.max(
        100,
        ...state.bots
          .filter((bot) => bot.kind !== "secret")
          .map((bot) => bot.r * 1.2),
      );
    const bot = {
      id: `bot-${++botSerial}`,
      name: menu?.name || "",
      kind: secret ? "secret" : "menu",
      menuId: menu?.id || null,
      x: random(radius, rules.worldWidth - radius),
      y: random(radius, rules.worldHeight - radius),
      mass: radius * radius,
      r: radius,
      color: colors[index % colors.length],
      targetX: 0,
      targetY: 0,
      decisionIn: random(0, 0.35),
    };
    if (initial && index <= Math.max(secretSlot, menus.length)) {
      // 按视口排布，窄屏也能看到菜单名称，而不是沿用桌面世界坐标偏移。
      const anchors = [
        [0.24, 0.19],
        [0.86, 0.27],
        [0.2, 0.77],
        [0.84, 0.85],
        [0.5, 0.9],
        [0.63, 0.17],
        [0.2, 0.43],
        [0.6, 0.8],
      ];
      // 保留原有 7 个菜单 + 隐藏球的视觉位置；更多菜单继续沿边缘环形排布。
      let anchor;
      if (index < anchors.length) {
        anchor = anchors[index];
      } else {
        const extra = index - anchors.length;
        const total = Math.max(1, menus.length + 1 - anchors.length);
        const angle = -Math.PI / 2 + (extra / total) * TAU;
        anchor = [0.5 + Math.cos(angle) * 0.4, 0.5 + Math.sin(angle) * 0.4];
      }
      bot.x = state.player.x + (anchor[0] - 0.73) * width;
      bot.y = state.player.y + (anchor[1] - 0.7) * height;
    } else if (
      distanceSquared(bot, state.player) <
      (state.player.r + radius + 220) ** 2
    ) {
      const angle = random(0, TAU);
      const separation = state.player.r + radius + random(300, 700);
      bot.x = state.player.x + Math.cos(angle) * separation;
      bot.y = state.player.y + Math.sin(angle) * separation;
    }
    keepInWorld(bot);
    bot.targetX = clamp(
      bot.x + random(-180, 180),
      radius,
      rules.worldWidth - radius,
    );
    bot.targetY = clamp(
      bot.y + random(-180, 180),
      radius,
      rules.worldHeight - radius,
    );
    return bot;
  }

  function keepNearbyMenus(dt) {
    nearbyCheckIn -= dt;
    if (nearbyCheckIn > 0 || state.bots.length !== rules.botCount) return;
    nearbyCheckIn = 1;
    const { x, y, zoom } = state.camera;
    const left = x - width / (2 * zoom),
      top = y - height / (2 * zoom);
    const right = left + width / zoom,
      bottom = top + height / zoom;
    const inView = (bot) =>
      bot.x > left + 16 / zoom &&
      bot.x < right - 16 / zoom &&
      bot.y > top + 70 / zoom &&
      bot.y < bottom - 60 / zoom;
    const visible = state.bots.filter(
      (bot) => bot.kind === "menu" && inView(bot),
    );
    const ids = new Set(visible.map((bot) => bot.menuId));
    // 保证视野内至少有 4 个不同菜单，而不是同一菜单的 4 个重复球。
    if (ids.size >= Math.min(4, menus.length)) return;
    const eligible = (bot) =>
      bot.kind === "menu" && !inView(bot) && !bot.reenterIn;
    const bot =
      state.bots.find((bot) => eligible(bot) && !ids.has(bot.menuId)) ||
      state.bots.find(eligible);
    if (!bot) return;
    const fromLeft = Math.random() < 0.5;
    bot.x = fromLeft ? left - bot.r - 8 / zoom : right + bot.r + 8 / zoom;
    bot.y = top + (random(0.22, 0.8) * height) / zoom;
    keepInWorld(bot);
    bot.targetX = fromLeft
      ? left + bot.r + 40 / zoom
      : right - bot.r - 40 / zoom;
    bot.targetY = bot.y;
    bot.reenterIn = (2 * bot.r + 64 / zoom) / Math.max(1, speed(bot, 0.64)) + 1;
    bot.decisionIn = bot.reenterIn;
  }

  function keepSecretLargest() {
    const secret = state.bots.find((bot) => bot.kind === "secret");
    if (!secret) return;
    let largest = 0;
    for (const bot of state.bots)
      if (bot !== secret) largest = Math.max(largest, bot.mass);
    const minimum = Math.max(10000, largest * 1.44);
    if (secret.mass < minimum) {
      secret.mass = minimum;
      secret.r = Math.sqrt(minimum);
      keepInWorld(secret);
    }
  }

  function reset() {
    const player = state.player;
    Object.assign(player, {
      x: rules.worldWidth / 2,
      y: rules.worldHeight / 2,
      mass: rules.startRadius ** 2,
      r: rules.startRadius,
    });
    state.bots.length = 0;
    state.foods.length = 0;
    state.consumedBots = 0;
    state.protection = rules.respawnProtectionSeconds;
    nearbyCheckIn = 0;
    particles.length = 0;
    ripples.length = 0;
    for (let i = 0; i < rules.botCount; i++) state.bots.push(makeBot(i, true));
    keepSecretLargest();
    for (let i = 0; i < rules.foodCount; i++)
      state.foods.push(makeFood(i < 160));
    state.camera.zoom = 1;
    state.camera.x = player.x - width * 0.23;
    state.camera.y = player.y - height * 0.2;
    lastFrame = 0;
    dirty = true;
    emitStats(true);
  }

  function setPaused(value) {
    state.paused = !!value;
    lastFrame = 0;
    dirty = true;
    emitStats(true);
    return state.paused;
  }

  function resize() {
    width = innerWidth;
    height = innerHeight;
    // 限制高分屏的填充开销；CSS 像素仍与指针坐标一一对应。
    pixelRatio = Math.min(
      devicePixelRatio || 1,
      1.5,
      Math.sqrt(2100000 / Math.max(1, width * height)),
    );
    canvas.width = Math.round(width * pixelRatio);
    canvas.height = Math.round(height * pixelRatio);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    state.camera.x = state.player.x - (width * 0.23) / state.camera.zoom;
    state.camera.y = state.player.y - (height * 0.2) / state.camera.zoom;
    dirty = true;
  }

  function chooseTarget(bot) {
    let threat = null;
    let prey = null;
    let threatDistance = Infinity;
    let preyDistance = Infinity;
    const perception = (350 + bot.r * 2) ** 2;
    for (let i = -1; i < state.bots.length; i++) {
      const other = i < 0 ? state.player : state.bots[i];
      // 菜单球互不捕食或驱赶，避免开场几秒后入口球全离开镜头。
      if (i >= 0 || (i < 0 && state.protection > 0)) continue;
      const distance = distanceSquared(bot, other);
      if (distance > perception) continue;
      if (
        other.mass >= bot.mass * rules.eatMassRatio &&
        distance < threatDistance
      ) {
        threat = other;
        threatDistance = distance;
      } else if (
        pointer.active &&
        bot.mass >= other.mass * rules.eatMassRatio &&
        distance < preyDistance
      ) {
        prey = other;
        preyDistance = distance;
      }
    }
    if (threat) {
      const dx = bot.x - threat.x;
      const dy = bot.y - threat.y;
      const distance = Math.hypot(dx, dy) || 1;
      bot.targetX = bot.x + (dx / distance) * 420;
      bot.targetY = bot.y + (dy / distance) * 420;
    } else if (prey) {
      bot.targetX = prey.x;
      bot.targetY = prey.y;
    } else {
      let food = null;
      let closest = Infinity;
      for (const candidate of state.foods) {
        const distance = distanceSquared(bot, candidate);
        if (distance < closest) {
          closest = distance;
          food = candidate;
        }
      }
      if (food) {
        bot.targetX = food.x;
        bot.targetY = food.y;
      }
    }
    bot.targetX = clamp(bot.targetX, bot.r, rules.worldWidth - bot.r);
    bot.targetY = clamp(bot.targetY, bot.r, rules.worldHeight - bot.r);
    bot.decisionIn = random(0.3, 0.6);
  }

  function moveTowards(cell, x, y, distance) {
    const dx = x - cell.x;
    const dy = y - cell.y;
    const length = Math.hypot(dx, dy);
    if (length > 1) {
      const step = Math.min(distance, length);
      cell.x += (dx / length) * step;
      cell.y += (dy / length) * step;
    }
    keepInWorld(cell);
  }

  function consumeEffect(bot) {
    if (ripples.length >= 6) ripples.shift();
    ripples.push({ x: bot.x, y: bot.y, r: bot.r, age: 0, duration: 0.65 });
    if (reducedMotion) return;
    for (let i = 0; i < 12; i++) {
      if (particles.length >= 72) particles.shift();
      const angle = random(0, TAU);
      const velocity = random(35, 115);
      particles.push({
        x: bot.x,
        y: bot.y,
        vx: Math.cos(angle) * velocity,
        vy: Math.sin(angle) * velocity,
        age: 0,
        duration: random(0.35, 0.7),
        color: bot.color,
      });
    }
  }

  function respawnPlayer(predator) {
    const player = state.player;
    const radius = rules.startRadius;
    let x = rules.worldWidth / 2;
    let y = rules.worldHeight / 2;
    // 在远离现有大球的位置重生，同时保留本局吞并数与页面解锁进度。
    for (let attempt = 0; attempt < 24; attempt++) {
      x = random(360, rules.worldWidth - 360);
      y = random(360, rules.worldHeight - 360);
      if (
        state.bots.every(
          (bot) =>
            bot.mass < radius * radius * rules.eatMassRatio ||
            (bot.x - x) ** 2 + (bot.y - y) ** 2 > (bot.r + radius + 250) ** 2,
        )
      )
        break;
    }
    Object.assign(player, { x, y, mass: radius * radius, r: radius });
    state.protection = rules.respawnProtectionSeconds;
    state.camera.zoom = 1;
    state.camera.x = x - width * 0.23;
    state.camera.y = y - height * 0.2;
    for (let i = 0; i < 50; i++) state.foods[i] = makeFood(true);
    window.dispatchEvent(
      new CustomEvent("agar:respawn", {
        detail: {
          botId: predator.id,
          botName: predator.name,
          protectionSeconds: state.protection,
        },
      }),
    );
    emitStats(true);
  }

  function update(dt) {
    const player = state.player;
    const camera = state.camera;
    state.protection = Math.max(0, state.protection - dt);
    if (pointer.active && !pointer.interactive && !editing) {
      const targetX = camera.x + (pointer.x - width / 2) / camera.zoom;
      const targetY = camera.y + (pointer.y - height / 2) / camera.zoom;
      moveTowards(player, targetX, targetY, speed(player) * dt);
    }
    for (const bot of state.bots) {
      bot.reenterIn = Math.max(0, (bot.reenterIn || 0) - dt);
      bot.decisionIn -= dt;
      if (bot.decisionIn <= 0) chooseTarget(bot);
      moveTowards(bot, bot.targetX, bot.targetY, speed(bot, 0.64) * dt);
    }

    // 食物使用平方距离检测，原位置直接补给，不在循环中 splice 数组。
    for (let i = 0; i < state.foods.length; i++) {
      const food = state.foods[i];
      if (distanceSquared(player, food) <= (player.r - food.r * 0.2) ** 2) {
        grow(player, food.mass);
        state.foods[i] = makeFood(Math.random() < 0.35);
        continue;
      }
      for (const bot of state.bots) {
        if (distanceSquared(bot, food) <= (bot.r - food.r * 0.2) ** 2) {
          grow(bot, food.mass);
          state.foods[i] = makeFood(Math.random() < 0.2);
          break;
        }
      }
    }

    keepSecretLargest();
    for (let i = 0; i < state.bots.length; i++) {
      const bot = state.bots[i];
      if (canEat(player, bot)) {
        const gain = bot.mass;
        const wasLargest =
          bot.kind === "secret" &&
          state.bots.every((other) => other.mass <= bot.mass);
        grow(player, gain);
        state.consumedBots++;
        consumeEffect(bot);
        state.bots[i] = makeBot(i);
        window.dispatchEvent(
          new CustomEvent("agar:consume", {
            detail: {
              gameVersion: VERSION,
              botId: bot.id,
              botName: bot.name,
              menuId: bot.menuId,
              secret: bot.kind === "secret",
              wasLargest,
              massGain: gain,
              mass: player.mass,
              consumedBots: state.consumedBots,
            },
          }),
        );
      } else if (state.protection <= 0 && canEat(bot, player)) {
        grow(bot, player.mass);
        respawnPlayer(bot);
        break;
      }
    }
    keepSecretLargest();
    const blend = 1 - Math.exp(-5 * dt);
    const targetZoom = clamp((rules.startRadius / player.r) ** 0.6, 0.32, 1);
    camera.zoom += (targetZoom - camera.zoom) * blend;
    camera.x += (player.x - (width * 0.23) / camera.zoom - camera.x) * blend;
    camera.y += (player.y - (height * 0.2) / camera.zoom - camera.y) * blend;
    keepNearbyMenus(dt);
    for (let i = particles.length - 1; i >= 0; i--) {
      const particle = particles[i];
      particle.age += dt;
      particle.x += particle.vx * dt;
      particle.y += particle.vy * dt;
      if (particle.age >= particle.duration) particles.splice(i, 1);
    }
    for (let i = ripples.length - 1; i >= 0; i--) {
      ripples[i].age += dt;
      if (ripples[i].age >= ripples[i].duration) ripples.splice(i, 1);
    }
  }

  function draw() {
    const camera = state.camera;
    const left = camera.x - width / (2 * camera.zoom);
    const right = camera.x + width / (2 * camera.zoom);
    const top = camera.y - height / (2 * camera.zoom);
    const bottom = camera.y + height / (2 * camera.zoom);
    const visible = (cell) =>
      cell.x + cell.r >= left &&
      cell.x - cell.r <= right &&
      cell.y + cell.r >= top &&
      cell.y - cell.r <= bottom;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.setTransform(
      pixelRatio * camera.zoom,
      0,
      0,
      pixelRatio * camera.zoom,
      pixelRatio * (width / 2 - camera.x * camera.zoom),
      pixelRatio * (height / 2 - camera.y * camera.zoom),
    );

    // 只画镜头内的网格、食物和球。三组食物各一次 fill，减少绘制调用。
    const gridStep = 100;
    ctx.strokeStyle = "rgba(22,19,15,0.035)";
    ctx.lineWidth = 1 / camera.zoom;
    ctx.beginPath();
    for (
      let x = Math.ceil(left / gridStep) * gridStep;
      x < right;
      x += gridStep
    ) {
      ctx.moveTo(x, top);
      ctx.lineTo(x, bottom);
    }
    for (
      let y = Math.ceil(top / gridStep) * gridStep;
      y < bottom;
      y += gridStep
    ) {
      ctx.moveTo(left, y);
      ctx.lineTo(right, y);
    }
    ctx.stroke();
    const foodColors = [
      "rgba(255,77,0,0.27)",
      "rgba(126,139,108,0.3)",
      "rgba(22,19,15,0.15)",
    ];
    for (let color = 0; color < foodColors.length; color++) {
      ctx.fillStyle = foodColors[color];
      ctx.beginPath();
      for (const food of state.foods) {
        if (food.color !== color || !visible(food)) continue;
        ctx.moveTo(food.x + food.r, food.y);
        ctx.arc(food.x, food.y, food.r, 0, TAU);
      }
      ctx.fill();
    }
    for (const bot of state.bots) {
      if (!visible(bot)) continue;
      const edible = state.player.mass >= bot.mass * rules.eatMassRatio;
      ctx.globalAlpha = 0.23;
      ctx.fillStyle = bot.color;
      ctx.beginPath();
      ctx.arc(bot.x, bot.y, bot.r, 0, TAU);
      ctx.fill();
      ctx.globalAlpha = 0.48;
      ctx.strokeStyle = edible ? "#da6b35" : bot.color;
      ctx.lineWidth = 1.5 / camera.zoom;
      ctx.setLineDash(edible ? [4 / camera.zoom, 4 / camera.zoom] : []);
      ctx.stroke();
      ctx.setLineDash([]);
      if (bot.name) {
        // 文本仍按镜头裁剪；字号限制在球内，缩放后保持可读。
        const fontSize = Math.min(16 / camera.zoom, bot.r * 0.72);
        ctx.globalAlpha = 1;
        ctx.fillStyle = "#443c31";
        ctx.font = `500 ${fontSize}px "Microsoft YaHei", sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(bot.name, bot.x, bot.y, bot.r * 1.7);
      }
    }
    const player = state.player;
    ctx.globalAlpha = 0.45;
    ctx.fillStyle = "#ff4d00";
    ctx.beginPath();
    ctx.arc(player.x, player.y, player.r, 0, TAU);
    ctx.fill();
    ctx.globalAlpha = 0.7;
    ctx.strokeStyle = "#cf5822";
    ctx.lineWidth = 2 / camera.zoom;
    ctx.stroke();
    if (state.protection > 0) {
      ctx.globalAlpha = 0.42;
      ctx.strokeStyle = "#ff4d00";
      ctx.lineWidth = 1 / camera.zoom;
      ctx.setLineDash([3 / camera.zoom, 5 / camera.zoom]);
      ctx.beginPath();
      ctx.arc(player.x, player.y, player.r + 7 / camera.zoom, 0, TAU);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    for (const ripple of ripples) {
      const progress = ripple.age / ripple.duration;
      ctx.globalAlpha = (1 - progress) * 0.5;
      ctx.strokeStyle = "#ff4d00";
      ctx.lineWidth = 2 / camera.zoom;
      ctx.beginPath();
      ctx.arc(ripple.x, ripple.y, ripple.r + progress * 50, 0, TAU);
      ctx.stroke();
    }
    for (const particle of particles) {
      ctx.globalAlpha = (1 - particle.age / particle.duration) * 0.7;
      ctx.fillStyle = particle.color;
      ctx.beginPath();
      ctx.arc(particle.x, particle.y, 2.5 / camera.zoom, 0, TAU);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  function frame(now) {
    if (hidden) {
      lastFrame = 0;
      return;
    }
    if (state.paused || !ready) {
      lastFrame = now;
      if (dirty) draw();
      dirty = false;
      return;
    }
    // 容忍刷新时钟的小数误差，避免 30fps 背景交替等待两帧和三帧。
    if (lastFrame && now - lastFrame < 1000 / rules.frameRate - 1) return;
    const dt = lastFrame ? Math.min((now - lastFrame) / 1000, 0.05) : 0;
    lastFrame = now;
    update(dt);
    draw();
    dirty = false;
    emitStats(false, now);
  }

  window.addEventListener(
    "pointermove",
    (event) => {
      pointer.x = event.clientX;
      pointer.y = event.clientY;
      pointer.active = true;
      pointer.interactive = interactiveTarget(event.target);
    },
    { passive: true, signal },
  );
  document.addEventListener(
    "pointerleave",
    () => {
      pointer.active = false;
    },
    { passive: true, signal },
  );
  window.addEventListener(
    "blur",
    () => {
      pointer.active = false;
    },
    { signal },
  );
  document.addEventListener(
    "focusin",
    (event) => {
      // 按钮获得焦点后仍能回到背景移动；输入或模态框才持续暂停操控。
      editing =
        event.target instanceof Element &&
        !!event.target.closest(
          'input, textarea, select, [contenteditable="true"], dialog, [role="dialog"]',
        );
    },
    { signal },
  );
  document.addEventListener(
    "focusout",
    () => {
      editing = false;
    },
    { signal },
  );
  document.addEventListener(
    "visibilitychange",
    () => {
      hidden = document.hidden;
      lastFrame = 0;
      if (!hidden) dirty = true;
    },
    { signal },
  );
  window.addEventListener("resize", resize, { passive: true, signal });
  window.addEventListener(
    "prototype:ready",
    () => {
      ready = true;
      lastFrame = 0;
      dirty = true;
    },
    { signal },
  );

  document.documentElement.dataset.prototypeVersion = VERSION;
  window.agarGame = {
    version: VERSION,
    state,
    rules,
    menus,
    canEat,
    reset,
    setPaused,
  };
  resize();
  reset();
  motion.addFrame(frame);
  return {
    state,
    setPaused,
    dispose() {
      abort.abort();
      cancelAnimationFrame(animationId);
      delete window.agarGame;
    },
  };
}
