# -*- coding: utf-8 -*-
"""章测 AI 作答器
流程：拦 doHomework 接口拿明文题面 -> DeepSeek 逐题作答 -> 真实鼠标点选 -> 下一题 / 提交
用法：
    python exam.py            # 抓题 + AI 作答 + 填到页面（不提交）
    python exam.py --submit   # 填完并点「提交作业」
    python exam.py --dry      # 只抓题 + AI 答案，不动页面
"""
import io, os, sys, json, time, base64, urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(APP_DIR, 'api_key.txt')
sys.path.insert(0, APP_DIR)
import trainer as T

# ---------- DeepSeek ----------

def _key():
    try:
        with io.open(KEY_FILE, 'r', encoding='ascii') as f:
            k = f.read().strip()
    except Exception:
        k = ''
    if not k:
        print('未配置 DeepSeek API Key：请在修改器面板的「AI 章测用的 DeepSeek Key」里粘贴后点保存，'
              '或把 key 写进 %s' % KEY_FILE)
        sys.exit(2)
    return k


def ask(qname, qtype, options, timeout=90):
    """options: [(oid, text)] -> (oid, reason)"""
    opt_txt = '\n'.join('%s. %s' % (chr(65 + i), o[1]) for i, o in enumerate(options))
    hint = ''
    if '判断' in qtype:
        hint = ('判断题提示：这类共享课的题干若出现「我校…荣获/被评为…」等校方自我宣传，'
                '大概率是「对」；不要因为「无法核实」就答错。')
    prompt = (
        '你是中国大学通识课考试助手，正在回答智慧树平台的题目。\n'
        '题型：%s\n题目：%s\n选项：\n%s\n\n%s\n'
        '请判断正确答案。只输出一行 JSON：{"answer":"C","reason":"不超过25字"}\n'
        'answer 只能是 %s 之一。答错会扣分，请认真判断。'
        % (qtype, qname, opt_txt, hint, '/'.join(chr(65 + i) for i in range(len(options))))
    )
    body = json.dumps({
        'model': 'deepseek-chat',
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0,
        'max_tokens': 300,
        'response_format': {'type': 'json_object'},
    }).encode('utf-8')
    req = urllib.request.Request(
        'https://api.deepseek.com/chat/completions', data=body,
        headers={'Authorization': 'Bearer ' + _key(), 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode('utf-8'))
    txt = data['choices'][0]['message']['content'].strip()
    try:
        j = json.loads(txt)
        letter = str(j.get('answer', '')).strip().upper()[:1]
    except Exception:
        letter = ''
        for ch in txt:
            if 'A' <= ch.upper() <= 'Z':
                letter = ch.upper()
                break
    idx = ord(letter) - 65 if letter else 0
    if idx < 0 or idx >= len(options):
        idx = 0
    return options[idx][0], (j.get('reason', '') if 'j' in dir() else ''), letter


# ---------- 抓题 ----------

def capture_paper(c, wait=40):
    c.call('Fetch.enable', timeout=10,
           patterns=[{'urlPattern': '*doHomework*', 'requestStage': 'Response'}])
    c.events[:] = []
    c.call('Page.reload', timeout=20)
    t0 = time.time()
    while time.time() - t0 < wait:
        for e in list(c.events):
            if e.get('method') != 'Fetch.requestPaused':
                continue
            p = e.get('params', {})
            rid = p.get('requestId')
            if not rid or p.get('responseStatusCode') is None:
                try: c.call('Fetch.continueRequest', timeout=8, requestId=rid)
                except Exception: pass
                c.events.remove(e)
                continue
            try:
                r = c.call('Fetch.getResponseBody', timeout=15, requestId=rid)
                b = r.get('body', '')
                if r.get('base64Encoded'):
                    b = base64.b64decode(b).decode('utf-8', 'ignore')
                try: c.call('Fetch.continueRequest', timeout=8, requestId=rid)
                except Exception: pass
                j = json.loads(b)
                if j.get('rt', {}).get('examBase'):
                    try: c.call('Fetch.disable', timeout=8)
                    except Exception: pass
                    return j['rt']
            except Exception as ex:
                print('  capture err', str(ex)[:120])
            c.events.remove(e)
            break
        time.sleep(0.3)
    try: c.call('Fetch.disable', timeout=8)
    except Exception: pass
    return None


def flatten(paper):
    qs = []
    for part in paper.get('examBase', {}).get('workExamParts', []) or []:
        for q in part.get('questionDtos', []) or []:
            opts = [(str(o['id']), (o.get('content') or '').replace('<br>', '').strip())
                    for o in (q.get('questionOptions') or [])]
            qs.append({'name': q.get('name', ''), 'type': (q.get('questionType') or {}).get('name', ''),
                       'score': q.get('questionScore'), 'opts': opts,
                       'ids': sorted(o[0] for o in opts)})
    return qs


# ---------- 页面操作 ----------

CUR_JS = r'''
(() => {
  const clean = s => (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const boxes = [...document.querySelectorAll('.examPaper_subject')]
      .filter(b => b.getBoundingClientRect().height > 40);
  if (!boxes.length) return JSON.stringify({err:'no-question'});
  const b = boxes[0];
  const dpr = devicePixelRatio || 1;
  const bx = (outerWidth - innerWidth) / 2, by = outerHeight - innerHeight - bx;
  const S = (x, y) => ({sx: Math.round((screenX + bx + x) * dpr), sy: Math.round((screenY + by + y) * dpr)});
  const rows = [...b.querySelectorAll('.nodeLab')].map(l => {
    const inp = l.querySelector('input');
    const r = l.getBoundingClientRect();
    const c = S(r.left + 40, r.top + r.height / 2);
    return { id: inp ? String(inp.value) : '', checked: inp ? inp.checked : false,
             t: clean(l.textContent).slice(0, 60), sx: c.sx, sy: c.sy };
  });
  const btns = {};
  for (const el of document.querySelectorAll('button, div, span')) {
    const t = clean(el.textContent);
    if (!/^(下一题|上一题|提交作业|暂存作业|确定|取消)$/.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 20 || r.height < 10) continue;
    const c = S(r.left + r.width / 2, r.top + r.height / 2);
    const dis = /is-disabled/.test(typeof el.className === 'string' ? el.className : '');
    if (!btns[t]) btns[t] = { sx: c.sx, sy: c.sy, disabled: dis };
  }
  return JSON.stringify({ rows, btns, url: location.href });
})()
'''


def click(c, sx, sy, settle=0.45):
    T.os_focus_edge()
    time.sleep(0.15)
    T.os_click(sx, sy)
    time.sleep(settle)


def do_exam(c, submit=False):
    print('[1] 抓取题面…')
    paper = capture_paper(c)
    if not paper:
        print('   失败：没抓到题面'); return False
    qs = flatten(paper)
    print('   试卷：%s  %d 题  总分 %s' % (paper['examBase'].get('name'), len(qs), paper['examBase'].get('totalScore')))

    print('[2] DeepSeek 作答…')
    plan = {}
    for i, q in enumerate(qs, 1):
        oid, reason, letter = ask(q['name'], q['type'], q['opts'])
        plan[tuple(q['ids'])] = oid
        picked = [t for (o, t) in q['opts'] if o == oid][0]
        print('   %d. [%s] %s' % (i, q['type'], q['name'][:40]))
        print('      -> %s  %s   (%s)' % (letter, picked, reason))

    print('[3] 填答案…')
    for i in range(len(qs)):
        st = json.loads(c.js(CUR_JS, timeout=10))
        if st.get('err'):
            print('   页面没有题目块，停止'); return False
        ids = tuple(sorted(r['id'] for r in st['rows']))
        target = plan.get(ids)
        if target is None:
            # 用选项文本兜底匹配
            for q in qs:
                if sorted(o[0] for o in q['opts']) == list(ids):
                    target = plan[tuple(q['ids'])]
                    break
        row = [r for r in st['rows'] if r['id'] == target]
        if row:
            click(c, row[0]['sx'], row[0]['sy'], 0.5)
            print('   第%d题 点选 %s' % (i + 1, row[0]['t'][:30]))
        else:
            print('   第%d题 没找到选项 id=%s，跳过' % (i + 1, target))
        if i < len(qs) - 1:
            nxt = st['btns'].get('下一题')
            if nxt:
                click(c, nxt['sx'], nxt['sy'], 1.0)
        time.sleep(0.3)

    print('[4] 收尾')
    if submit:
        st = json.loads(c.js(CUR_JS, timeout=10))
        sub = st['btns'].get('提交作业')
        if sub:
            click(c, sub['sx'], sub['sy'], 1.2)
            conf = json.loads(c.js(CUR_JS, timeout=10))
            ok = conf['btns'].get('确定')
            if ok:
                click(c, ok['sx'], ok['sy'], 1.2)
                print('   已点「确定」提交')
            print('   已点「提交作业」')
    else:
        print('   未提交（加 --submit 才会点提交作业）')
    return True


if __name__ == '__main__':
    submit = '--submit' in sys.argv
    dry = '--dry' in sys.argv
    targets = T.http_json('/json/list')
    cand = [x for x in targets if x.get('type') == 'page' and 'onlineexam' in x.get('url', '')]
    if not cand:
        print('没有打开「作业考试」页面，请先在浏览器里点开某章的平时测试')
        sys.exit(1)
    c = T.CDP(cand[0]['webSocketDebuggerUrl'])
    try:
        if dry:
            paper = capture_paper(c)
            for q in flatten(paper or {}):
                oid, reason, letter = ask(q['name'], q['type'], q['opts'])
                print('[%s] %s' % (q['type'], q['name']))
                print('   -> %s %s (%s)' % (letter, [t for o, t in q['opts'] if o == oid][0], reason))
        else:
            do_exam(c, submit=submit)
    finally:
        c.alive = False
