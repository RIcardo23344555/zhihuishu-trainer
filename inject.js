/* 智慧树加速器 —— 页面注入脚本（由修改器通过 CDP 注入，无需扩展）v2 */
(function () {
  if (window.__ZS && window.__ZS.__v === 22 && window.__ZS.inited) { try { window.__ZS.boot(); } catch (e) { } return; }
  if (window.__ZS && window.__ZS.__stop) { try { window.__ZS.__stop(); } catch (e) { } }

  const DEF = { speed: 1, on: true, mute: true, next: true, quiz: true, hud: false, chunk: 200,
                tail: 45, tailSpeed: 2, waitNext: 40, onlyFull: false, rate: 0 };
  const cfg = Object.assign({}, DEF);
  try {
    if (window.parent !== window && window.parent.__ZS && window.parent.__ZS.cfg) {
      Object.assign(cfg, window.parent.__ZS.cfg);
    }
  } catch (e) { }

  const BANK = [
    { k: /库兹韦尔|2027年电脑/, a: '对' },
    { k: /盒马/, a: '上海' },
    { k: /人际交往的必要性|心理学家.*实验/, a: '早期隔离实验' },
    { k: /心理健康的标准/, a: null }
  ];

  const st = { jumps: 0, quizzes: 0, nexts: 0, lastNext: 0, lastQuiz: 0, quizOpen: false, lastUserPause: 0,
               bootAt: Date.now(), lastD: 0, lessonAt: 0, rewound: false, rewinds: 0,
               lastRewind: 0, pct: -1, lastRate: 0, quizSig: '', quizPicked: false, quizPickAt: 0, quizWait: 0 };

  /* 当前课时在目录里的百分比。
     优先用站点自己标的 .current_play（唯一可靠）；没有再用“时长最接近”兜底
     （时长会撞车：0.2 是 09:48、5.2 是 09:49，按时长可能选错到别的章节）。 */
  function curPct() {
    try {
      const list = [...document.querySelectorAll('.clearfix.video')];
      if (!list.length) return -1;
      const el = v();
      const d = el && isFinite(el.duration) ? el.duration : 0;
      let best = list.findIndex(x => /current_play/i.test(x.className || ''));
      if (best < 0) {
        if (!d) return -1;
        let bd = 99;
        for (let i = 0; i < list.length; i++) {
          const m = (list[i].textContent || '').match(/(\d{2}):(\d{2}):(\d{2})/);
          if (!m) continue;
          const s = (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3]);
          const dif = Math.abs(s - d);
          if (dif < bd) { bd = dif; best = i; }
        }
        if (best < 0 || bd > 3) return -1;
      }
      const item = list[best];
      if (item.querySelector('.time_icofinish')) return 100;
      const pb = item.querySelector('[role="progressbar"]');
      if (pb) { const n = parseInt(pb.getAttribute('aria-valuenow'), 10); return isNaN(n) ? 0 : n; }
      const m2 = (item.textContent || '').match(/(\d{1,3})%/);
      return m2 ? (+m2[1]) : 0;
    } catch (e) { return -1; }
  }

  function v() {
    const list = [...document.querySelectorAll('video')];
    if (!list.length) return null;
    let best = null, bestA = -1;
    for (const el of list) {
      const a = (el.duration ? el.duration : 0) * 1000 + (el.offsetParent ? 1 : 0) + (el.currentTime || 0);
      if (a > bestA) { bestA = a; best = el; }
    }
    return best;
  }

  function applyMute() {
    const el = v(); if (!el) return;
    try { el.muted = !!cfg.mute; el.volume = cfg.mute ? 0 : 1; } catch (e) { }
  }

  /* 用播放器自带的倍速菜单（页面右下「X 1.0」→ 1.0/1.25/1.5）。
     平台自己支持 1.5 倍，走它自己的 UI 等于“合法加速”，比我们改 playbackRate 稳。 */
  const RATE_MAP = { 1: '.speedTab05', 1.25: '.speedTab10', 1.5: '.speedTab15' };
  /* 屏幕物理像素坐标（给 OS 连点器用）。
     站点会检测播放器控件上的合成点击（isTrusted=false）并立刻把页面踢回
     www.zhihuishu.com —— 所以这里只算坐标，真正的点击交给 Windows 输入队列。 */
  function scr(el) {
    const r = el.getBoundingClientRect();
    if (!r || r.width < 1 || r.height < 1) return null;
    const bx = Math.max(0, (window.outerWidth - window.innerWidth) / 2);
    const by = Math.max(0, window.outerHeight - window.innerHeight - bx);
    const dpr = window.devicePixelRatio || 1;
    return {
      x: Math.round((window.screenX + bx + r.left + r.width / 2) * dpr),
      y: Math.round((window.screenY + by + r.top + r.height / 2) * dpr)
    };
  }
  function speedSpan() {
    return [...document.querySelectorAll('span')]
      .find(e => /^X\s*[\d.]+$/.test((e.textContent || '').trim()) &&
                 !/speedTab/.test(e.className || ''));
  }
  /* 返回 {span:{x,y}, tab:{x,y}|null, key:1.5} —— 由 trainer.py 用真实鼠标点 */
  /* 弹题下一步该点哪儿（只算坐标，真点由 trainer.py 用真实鼠标做） */
  function cxy(el, act, tx) {
    const a = scr(el);
    if (!a) return 'null';
    return JSON.stringify({ act: act, x: a.x, y: a.y, txt: textOf(tx || el).slice(0, 20) });
  }
  function radioOf(row) {
    for (const el of [row.querySelector('input[type=radio]'),
                      row.querySelector('.topic-option'),
                      row.querySelector('svg.icon')]) {
      if (!el) continue;
      const r = el.getBoundingClientRect();
      if (r.width > 4 && r.height > 4) return el;   // 隐藏的 input 是 0x0，点不到
    }
    return row;
  }
  /* 选项图标：单选 #iconxuan（未选）/#iconxuanzhong（选中）
     多选 #iconfuxuan（未选）/#iconfuxuanzhong（选中）—— 判「选中」只能认 zhong，
     否则多选项全被当成已选，会立刻点关闭 → 平台弹「未做答」→ 死循环 */
  function iconHref(row) {
    const use = row.querySelector('svg use');
    return use ? (use.getAttribute('xlink:href') || use.getAttribute('href') || '') : '';
  }
  function rowSelected(r) {
    const i = r.querySelector('input');
    if (i && i.checked) return true;
    const href = iconHref(r);
    if (href) return /zhong/.test(href);
    const cls = (typeof r.className === 'string' ? r.className : '');
    return /active|selected|checked|cur/i.test(cls);
  }
  function selectedRow(rows) {
    return rows.find(rowSelected);
  }
  /* 弹题下一步该点哪儿：只有 DOM 里真的出现「选中态」才允许点关闭，
     否则平台会弹「未做答的弹题不能关闭」把流程卡住。 */
  function quizStep() {
    const now = Date.now();
    const m = msgBox();
    if (m) {
      st.quizPicked = false; st.quizPickAt = 0; st.quizWait = now + 900;
      const ok = [...m.querySelectorAll('button,span,div')].find(el => {
        const x = textOf(el).trim();
        return visible(el) && el.children.length === 0 && /^(确定|知道了|好|关闭|OK)$/.test(x);
      });
      return ok ? cxy(ok, 'msgbox') : 'null';
    }
    if (now < (st.quizWait || 0)) return 'null';
    const d = quizDialog();
    if (!d) return 'null';
    const rows = [...d.querySelectorAll('li.topic-item')];
    if (!rows.length) return 'null';
    const sig = rows.map(r => textOf(r)).join('|').slice(0, 120);
    if (st.quizSig !== sig) { st.quizSig = sig; st.quizPicked = false; st.quizPickAt = 0; }
    const sel = selectedRow(rows);
    if (!sel) {
      if (st.quizPicked && now - st.quizPickAt < 1600) return 'null';  // 刚点过，等选中态出来
      const ans = pickAnswer(textOf(d));
      /* 只点「还没选中」的行：多选点已选中的行会把它取消掉 */
      const cands = rows.filter(r => !rowSelected(r));
      if (!cands.length) return 'null';
      let row = ans ? cands.find(r => textOf(r).includes(ans)) : null;
      if (!row) row = cands[(st.quizPickIdx || 0) % cands.length];   // 点多没反应就换一个
      st.quizPickIdx = (st.quizPickIdx || 0) + 1;
      st.quizPicked = true; st.quizPickAt = now;
      return cxy(radioOf(row), 'pick', row);
    }
    if (now - st.quizPickAt < 700) return 'null';   // 选中动画没走完先别关
    const btn = [...d.querySelectorAll('.btn,button,div,span')].find(el =>
      textOf(el).replace(/\s+/g, '') === '关闭' && el.children.length === 0 && visible(el));
    return btn ? cxy(btn, 'close') : 'null';
  }

  /* 播放器区域中心（先把鼠标移到这儿，控制条才会显示出来） */
  function hoverCoords() {
    const el = v(); if (!el) return 'null';
    const a = scr(el);
    return a ? JSON.stringify(a) : 'null';
  }
  function rateCoords(r) {    const key = r >= 1.5 ? 1.5 : (r >= 1.25 ? 1.25 : 1);
    const sp = speedSpan();
    if (!sp) return 'null';
    const a = scr(sp);
    if (!a) return 'null';
    const tb = document.querySelector(RATE_MAP[key] || '');
    const b = (tb && tb.getBoundingClientRect().width > 2) ? scr(tb) : null;
    return JSON.stringify({ key: key, span: a, tab: b });
  }
  function setRate(r) {
    // 不再做 DOM 点击（会触发站点反作弊），保留接口兼容；真正改倍速走 trainer
    const el = v(); if (!el) return 'no-video';
    const key = r >= 1.5 ? 1.5 : (r >= 1.25 ? 1.25 : 1);
    if (Math.abs((el.playbackRate || 1) - key) < 0.02) return 'same';
    return 'wait-os-click';
  }

  function resume(reason) {
    const el = v(); if (!el) return;
    if (!cfg.on) return;
    if (el.paused) { try { el.play(); } catch (e) { } }
  }

  /* ---------- 自动下一节 ---------- */
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (!r || r.width < 4 || r.height < 4) return false;
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';
  }

  function clickNext() {
    const now = Date.now();
    if (now - st.lastNext < 3500) return 'cooldown';
    const re = /^(下一(节|章|讲|集|课)|继续学习|播放下一(节|集)?|下一段|下一个)$/;
    const all = [...document.querySelectorAll('button,a,div,span,li,i')];
    for (const el of all) {
      const t = (el.textContent || '').replace(/\s+/g, '').trim();
      if (!t || t.length > 12) continue;
      if (!re.test(t)) continue;
      if (!visible(el)) continue;
      el.click(); st.lastNext = now; st.nexts++;
      return 'btn:' + t;
    }
    for (const el of all) {
      const t = (el.textContent || '').replace(/\s+/g, '').trim();
      if (!visible(el) || t.length > 20) continue;
      if (!/下一(节|章|讲|集)/.test(t)) continue;
      el.click(); st.lastNext = now; st.nexts++;
      return 'btn2:' + t;
    }
    // 说明：智慧树目录项必须用「受信任点击」(CDP Input.dispatchMouseEvent)，
    // 由 trainer.py 负责；这里用 DOM click 会被站点判定非法并跳到 www.zhihuishu.com。
    return 'none';
  }

  /* ---------- 弹题处理 ---------- */
  const QUIZ_KEY = /AI助教小智给你出题啦|课堂测验|随堂练习|弹题|小测验|做个小测|出题/;
  const CLOSE_TXT = /^(关闭|✕|×|x|X|知道了|跳过|提交|确定|完成|关闭提示|我再想想|残忍离开|下次再说|取消)$/;
  const SUBMIT_TXT = /^(提交|确定|完成|提交答案)$/;

  function textOf(el) { return (el.innerText || el.textContent || '').trim(); }

  /* 智慧树弹题的真实结构（实测）：
     div.el-dialog__wrapper.dialog-test
       li.topic-item > span.topic-option-item(A./B./…) + div.item-topic(选项文字)
       div.btn  “关闭”
     未作答就点“关闭” → el-message-box__wrapper 提示「未做答的弹题不能关闭」，视频一直卡住。
     所以必须先选一个选项，再点关闭。 */
  const DLG_SEL = '.dialog-test, .el-dialog__wrapper.dialog-test';

  function quizDialog() {
    const d = document.querySelector(DLG_SEL);
    return d && visible(d) ? d : null;
  }

  function msgBox() {
    const m = document.querySelector('.el-message-box__wrapper');
    return m && visible(m) ? m : null;
  }

  function pickAnswer(t) {
    for (const b of BANK) { if (b.k.test(t) && b.a) return b.a; }
    if (/判断题/.test(t)) return '对';
    return null;
  }

  function closeDialog(d) {
    const btn = [...d.querySelectorAll('.btn,button,div,span')].find(el => {
      const x = textOf(el).replace(/\s+/g, '');
      return visible(el) && x === '关闭' && el.children.length === 0;
    });
    if (btn) { btn.click(); return true; }
    return false;
  }

  function handleQuiz() {
    if (!cfg.quiz) return;

    // quizOS：全流程交给 Windows 真实鼠标（DOM click 是合成事件，会被站点判作弊踢回首页）
    if (cfg.quizOS) {
      const d0 = quizDialog();
      st.quizOpen = !!d0;
      return;
    }

    // 1) 先清掉「未做答的弹题不能关闭」提示框
    const m = msgBox();
    if (m) {
      const ok = [...m.querySelectorAll('button,span,div')].find(el => {
        const x = textOf(el).trim();
        return visible(el) && el.children.length === 0 && /^(确定|知道了|好|关闭|OK)$/.test(x);
      });
      if (ok) ok.click();
    }

    const d = quizDialog();
    if (!d) {
      if (st.quizOpen) { st.quizOpen = false; resume('quiz-closed'); }
      return;
    }
    st.quizOpen = true;
    st.lastQuiz = Date.now();

    const rows = [...d.querySelectorAll('li.topic-item')];
    if (!rows.length) { closeDialog(d); return; }

    // 已经选过的选项（图标从 #iconxuan 换成勾选态）
    const chosen = rows.find(r => {
      const cls = (typeof r.className === 'string' ? r.className : '');
      if (/active|selected|checked|cur/i.test(cls)) return true;
      const use = r.querySelector('svg use');
      const href = use && (use.getAttribute('xlink:href') || use.getAttribute('href') || '');
      return href && href !== '#iconxuan';
    });

    if (!chosen) {
      const ans = pickAnswer(textOf(d));
      let row = ans ? rows.find(r => textOf(r).includes(ans)) : null;
      if (!row) row = rows[0];                      // 不确定就选 A，反正不计分
      row.click(); st.quizzes++;
      setTimeout(() => {
        const d2 = quizDialog();
        if (d2) closeDialog(d2);
        setTimeout(() => { if (!quizDialog()) resume('answered'); }, 900);
      }, 550);
      return;
    }

    // 已作答 → 关闭
    if (closeDialog(d)) { st.lastQuiz = Date.now(); }
  }

  let quizTimer = null;
  function watchQuiz() {
    if (quizTimer) return;
    quizTimer = setInterval(() => { try { handleQuiz(); } catch (e) { } }, 900);
  }

  /* ---------- 跳格主循环 ---------- */
  let timer = null;
  function tick() {
    const el = v(); if (!el) return;

    // 平台原生倍速（1/1.25/1.5）：放在最前面——暂停/片尾/换节时也要补回来，
    // 否则一旦进过“暂停”分支就永远停在 1×
    if (cfg.rate && Math.abs((el.playbackRate || 1) - cfg.rate) > 0.02 &&
        Date.now() - st.lastRate > 3000) {
      st.lastRate = Date.now();
      setRate(cfg.rate);
    }

    // 换课时（时长变了）重置本节的计数
    if (el.duration && Math.abs(el.duration - st.lastD) > 0.5) {
      st.lastD = el.duration; st.lessonAt = Date.now(); st.jumps = 0; st.rewound = false;
    }

    // 已经重新播起来了：清掉重播请求，别反复点
    if (st.needRepeat && el.currentTime > 5 && el.currentTime < el.duration - 5) st.needRepeat = false;
    const atEnd = el.duration > 5 && (el.ended || el.currentTime >= el.duration - 1.2);
    if (atEnd) {
      const pct = curPct(); st.pct = pct;
      // 挂机刷课模式：本节没满 100% 就回绕重播，绝不往下走
      // （服务端按真实观看时长记账，只有播够时长才会满）
      if (cfg.onlyFull && pct >= 0 && pct < 100) {
        // 不再用 currentTime = 0 回绕：合成 seek 会被站点判异常并踢回首页。
        // 改成置 needRepeat，由 trainer.py 用「真实鼠标点当前课时」重播（站点自己的动作）。
        st.needRepeat = true;
        return;
      }
      // 结尾检测要放在“暂停”判断之前：播完时 video 是 paused 的
      // 注入后前 6 秒不判片尾：刚换页时可能先读到上一节的结尾状态，会误点下一节
      if (cfg.next && Date.now() - st.bootAt > 6000) {
        const r = clickNext();
        if (r !== 'none' && r !== 'cooldown') return;
        if (r === 'none' && Date.now() - st.lastNext > 15000) { st.lastNext = Date.now(); }
      }
      return;                      // 片尾不再跳格
    }
    if (!st.rewound && st.lessonAt && Date.now() - st.lessonAt < 8000 && st.jumps === 0 &&
        cfg.onlyFull && el.duration > 60 && st.pct >= 0 && st.pct < 100 && el.currentTime > el.duration - 3) {
      // 上一轮遗留的“停在片尾但没满”：同样交给 trainer 真实点击重播
      st.rewound = true; st.needRepeat = true;
      return;
    }

    if (!cfg.on) return;
    if (el.paused) {
      // 弹题层挡住导致暂停 → 关掉后恢复
      if (st.quizOpen && Date.now() - st.lastQuiz > 2000) { try { el.play(); } catch (e) { } return; }
      // 正常卡在中间（换节后不自动播 / 加载完停住）→ 自动续播；到片尾的不动
      if (el.duration > 5 && el.currentTime < el.duration - 2 && Date.now() - st.lastUserPause > 5000) {
        try { el.play(); } catch (e) { }
      }
      return;
    }

    const chunk = cfg.chunk || 200;
    // 片尾降速：最后 tail 秒用 tailSpeed 跑，让服务端心跳把尾巴的时长真正记上
    // （心跳约 30s 一次，按“位置增量”上报；如果我们 15s 冲到片尾，尾巴那段就白跑了）
    const left = (isFinite(el.duration) ? el.duration : 0) - el.currentTime;
    const effSpeed = (left > 0 && left < (cfg.tail || 45)) ? Math.min(cfg.tailSpeed || 2, cfg.speed) : cfg.speed;
    const jump = (effSpeed - 1) * chunk / 1000 * (0.85 + Math.random() * 0.3);
    if (jump > 0.02 && isFinite(el.duration)) {
      try { el.currentTime = Math.min(el.currentTime + jump, Math.max(el.duration - 0.3, 0)); st.jumps++; } catch (e) { }
    }
    if (cfg.mute && !el.muted) applyMute();
  }

  function boot() {
    if (timer) clearInterval(timer);
    st.bootAt = Date.now();
    timer = setInterval(() => { try { tick(); } catch (e) { } }, cfg.chunk || 200);
    watchQuiz();
    applyMute();
  }

  /* ---------- 对外接口 ---------- */
  window.__ZS = {
    inited: true,
    __v: 22,
    __stop() {
      if (timer) { clearInterval(timer); timer = null; }
      if (quizTimer) { clearInterval(quizTimer); quizTimer = null; }
      return 'stopped';
    },
    cfg,
    set(o) { Object.assign(cfg, o || {}); applyMute(); boot(); return cfg; },
    toggle() { cfg.on = !cfg.on; return cfg.on; },
    next() { st.lastNext = 0; return clickNext(); },
    reboot() { boot(); return 'ok'; },
    survey() {
      // 诊断：把页面上的“可疑弹层”列出来，方便看真实按钮文案
      const out = [];
      for (const el of document.querySelectorAll('div,section')) {
        if (!visible(el)) continue;
        const t = textOf(el);
        if (t.length < 6 || t.length > 800) continue;
        if (!/题|测验|练习/.test(t)) continue;
        out.push({ txt: t.slice(0, 120), btns: [...el.querySelectorAll('button,span,div,a,i')].filter(b => visible(b) && textOf(b).length <= 8).map(b => textOf(b)).slice(0, 12) });
        if (out.length > 3) break;
      }
      return out;
    },
    status() {
      const el = v();
      const dd = el && isFinite(el.duration) ? el.duration : 0;
      const atEnd = !!(el && dd > 5 && (el.ended || el.currentTime >= dd - 1.2));
      if (atEnd) { if (!st.endAt) st.endAt = Date.now(); } else { st.endAt = 0; }
      return {
        has: !!el,
        t: el ? Math.round(el.currentTime * 10) / 10 : 0,
        d: el ? Math.round((el.duration || 0) * 10) / 10 : 0,
        paused: el ? el.paused : null,
        ended: el ? !!el.ended : null,
        atEnd: atEnd,
        endFor: st.endAt ? Math.round((Date.now() - st.endAt) / 1000) : 0,
        speed: cfg.speed, on: cfg.on, mute: cfg.mute, next: cfg.next, quiz: cfg.quiz,
        jumps: st.jumps, quizzes: st.quizzes, nexts: st.nexts, quizOpen: st.quizOpen, rewinds: st.rewinds,
        pct: curPct(), onlyFull: !!cfg.onlyFull, rate: el ? (el.playbackRate || 1) : 0,
        wantRate: cfg.rate || 0,
        needRepeat: !!st.needRepeat,
        url: location.href
      };
    },
    rateCoords,
    hoverCoords,
    quizStep,
    boot
  };

  // 用户手动按暂停时记录一下，避免和自动恢复打架
  document.addEventListener('pause', (e) => { if (e.target && e.target.tagName === 'VIDEO') st.lastUserPause = Date.now(); }, true);

  boot();
})();
