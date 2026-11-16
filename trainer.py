# -*- coding: utf-8 -*-
"""
智慧树加速器 · 修改器版
- 独立浏览器配置（复制你现有的 Edge 登录状态，只需复制一次）
- 通过 DevTools 协议把加速脚本注入页面：跳格加速 / 自动连播 / 自动处理弹题 / 静音
- 不装任何扩展，不需要开发者模式
"""
import os
import sys
import json
import time
import queue
import base64
import socket
import struct
import threading
import subprocess
import urllib.parse
import urllib.request

import tkinter as tk
from tkinter import ttk

# ---------------------------------------------------------------- 连点器
# 站点用 event.isTrusted / 反作弊判定 CDP 合成的点击，点完就把页面踢回
# www.zhihuishu.com。这里改成真正走 Windows 输入队列的鼠标点击
# （SetCursorPos + mouse_event），浏览器看到的是和真人一模一样的点击。
import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 物理像素坐标，别被 DPI 缩放坑
except Exception:
    pass
_u32 = ctypes.windll.user32


def _proc_name(pid):
    """按 PID 取进程名（区分 msedge.exe，比窗口标题可靠）。"""
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ''
    try:
        buf = ctypes.create_unicode_buffer(300)
        size = ctypes.c_ulong(300)
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.split('\\')[-1].lower()
    finally:
        k.CloseHandle(h)
    return ''


def os_focus_edge():
    """把 Edge 主窗口提到前台，否则真实点击会打到别的窗口上。"""
    found = []
    pid = ctypes.c_ulong()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(h, l):
        if not _u32.IsWindowVisible(h):
            return True
        n = ctypes.c_ulong()
        _u32.GetWindowThreadProcessId(h, ctypes.byref(n))
        try:
            if _proc_name(n.value) != 'msedge.exe':
                return True
        except Exception:
            return True
        buf = ctypes.create_unicode_buffer(300)
        _u32.GetWindowTextW(h, buf, 300)
        if 'Edge' not in buf.value:      # 标题里带零宽字符，用包含判断
            return True
        r = ctypes.wintypes.RECT() if hasattr(ctypes, 'wintypes') else None
        found.append(h)
        return True

    try:
        _u32.EnumWindows(_cb, 0)
    except Exception:
        return False
    if not found:
        return False
    h = found[0]
    try:
        if _u32.IsIconic(h):
            _u32.ShowWindow(h, 9)      # SW_RESTORE
        _u32.SetForegroundWindow(h)
        return True
    except Exception:
        return False


def os_click(x, y):
    """真实鼠标点击：移动 → 按下 → 松开。"""
    x, y = int(x), int(y)
    _u32.SetCursorPos(x, y)
    time.sleep(0.15)
    _u32.mouse_event(0x0002, 0, 0, 0, 0)   # MOUSEEVENTF_LEFTDOWN
    time.sleep(0.08)
    _u32.mouse_event(0x0004, 0, 0, 0, 0)   # MOUSEEVENTF_LEFTUP

APP_DIR = os.path.dirname(os.path.abspath(__file__))
INJECT_PATH = os.path.join(APP_DIR, 'inject.js')

EDGE_CANDIDATES = [
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
]
PORT = 9223
WAIT_REPORT = 40          # 片尾后等多久再切下一节（等一次心跳把尾巴时长报上去）
BASE = os.path.join(os.environ.get('LOCALAPPDATA', APP_DIR), 'zs-trainer')
PROFILE = os.path.join(BASE, 'profile')
EDGE_SRC = os.path.join(os.environ.get('LOCALAPPDATA', ''), r'Microsoft\Edge\User Data')
START_URL = 'https://onlineweb.zhihuishu.com/'

EXCLUDE_DIRS = ('Cache', 'Code Cache', 'GPUCache', 'GrShaderCache', 'ShaderCache',
                'DawnCache', 'DawnGraphiteCache', 'DawnWebGPUCache', 'CacheStorage',
                'ScriptCache', 'Service Worker', 'Crashpad', 'BrowserMetrics',
                'ProvenanceData', 'ProvenanceDataTensors', 'components', 'extensions_crx_cache',
                'component_crx_cache', 'WidevineCdm', 'GraphiteDawnCache')


def find_edge():
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


# ---------------------------------------------------------------- WebSocket
class WS:
    def __init__(self, url, timeout=20):
        u = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (('?' + u.query) if u.query else '')
        req = ("GET %s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
               % (path, u.hostname, u.port, key))
        self.sock.sendall(req.encode())
        buf = b''
        while b'\r\n\r\n' not in buf:
            d = self.sock.recv(4096)
            if not d:
                raise IOError('websocket handshake closed')
            buf += d
        first = buf.split(b'\r\n', 1)[0]
        if b' 101 ' not in first:
            raise IOError('websocket handshake failed: %r' % first[:160])
        # 握手后必须转成无限阻塞：留着 20s 超时的话，空闲超过 20s 就会 recv 超时
        # → 读线程抛异常 → 整条 CDP 连接被判定断开（表现为“页面老是重新注入/连不上”）
        self.sock.settimeout(None)

    def send(self, data, op=0x1):
        if isinstance(data, str):
            data = data.encode('utf-8')
        mask = os.urandom(4)
        n = len(data)
        hdr = bytearray([0x80 | op])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack('>H', n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack('>Q', n)
        hdr += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(hdr) + masked)

    def _read(self, n):
        buf = b''
        while len(buf) < n:
            d = self.sock.recv(n - len(buf))
            if not d:
                raise IOError('websocket closed')
            buf += d
        return buf

    def recv(self):
        payload = b''
        op = None
        while True:
            b1, b2 = self._read(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack('>H', self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack('>Q', self._read(8))[0]
            data = self._read(ln) if ln else b''
            if opcode == 0x9:          # ping
                self.send(data, op=0xA)
                continue
            if opcode == 0x8:
                raise IOError('websocket closed by peer')
            if op is None:
                op = opcode
            payload += data
            if fin:
                return op, payload

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class CDP:
    """一条页面级 CDP 连接"""
    def __init__(self, ws_url):
        self.ws = WS(ws_url)
        self.id = 0
        self.lock = threading.Lock()
        self.pending = {}
        self.events = []
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            while self.alive:
                op, data = self.ws.recv()
                if op != 0x1:
                    continue
                try:
                    msg = json.loads(data.decode('utf-8'))
                except Exception:
                    continue
                mid = msg.get('id')
                if mid is not None and mid in self.pending:
                    self.pending[mid] = msg
                elif mid is None:
                    try:
                        self.events.append(msg)
                        if len(self.events) > 4000:
                            del self.events[:2000]
                    except Exception:
                        pass
        except Exception:
            self.alive = False

    def call(self, method, timeout=15, **params):
        if not self.alive:
            raise IOError('cdp disconnected')
        with self.lock:
            self.id += 1
            mid = self.id
            self.pending[mid] = None
            self.ws.send(json.dumps({'id': mid, 'method': method, 'params': params}))
        t0 = time.time()
        while time.time() - t0 < timeout:
            r = self.pending.get(mid)
            if r is not None:
                self.pending.pop(mid, None)
                if 'error' in r:
                    raise RuntimeError(str(r['error']))
                return r.get('result', {})
            time.sleep(0.02)
        self.pending.pop(mid, None)
        raise TimeoutError(method)

    def js(self, expr, timeout=10):
        r = self.call('Runtime.evaluate', timeout=timeout, expression=expr,
                      returnByValue=True, awaitPromise=False, userGesture=True)
        if r.get('exceptionDetails'):
            raise RuntimeError(str(r['exceptionDetails'])[:300])
        return r.get('result', {}).get('value')


# ---------------------------------------------------------------- 浏览器控制
def http_json(path, timeout=3):
    with urllib.request.urlopen('http://127.0.0.1:%d%s' % (PORT, path), timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', 'replace'))


def kill_edge():
    subprocess.run(['taskkill', '/F', '/IM', 'msedge.exe'], capture_output=True)


def ensure_profile(log):
    """独立浏览器配置目录：第一次需要在这个窗口里扫码登录一次，之后长期记住。"""
    os.makedirs(PROFILE, exist_ok=True)
    first = not os.path.isdir(os.path.join(PROFILE, 'Default'))
    if first:
        log('首次使用：请在打开的浏览器里用「知到 APP」扫码登录一次（只需一次）')
    return first


def launch_edge(log, url=START_URL):
    edge = find_edge()
    if not edge:
        raise RuntimeError('没有找到 msedge.exe')
    # 已经在跑（调试端口活着）就复用，别再开新标签
    try:
        http_json('/json/version', timeout=2)
        log('复用已打开的浏览器')
        return True
    except Exception:
        pass
    args = ['--user-data-dir=%s' % PROFILE,
            '--remote-debugging-port=%d' % PORT,
            '--remote-allow-origins=*',
            '--no-first-run', '--no-default-browser-check',
            '--disable-sync', '--hide-crash-restore-bubble',
            '--disable-features=msEdgeIdentityFRE,msEdgeSyncPromo,EdgeSyncPromo',
            url]
    log('启动浏览器…')
    subprocess.Popen([edge] + args, close_fds=True)
    t0 = time.time()
    while time.time() - t0 < 40:
        try:
            http_json('/json/version', timeout=2)
            return True
        except Exception:
            time.sleep(0.6)
    raise RuntimeError('浏览器调试端口未就绪')


# ---------------------------------------------------------------- 主界面
class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.logs = []
        self.cdp = {}            # target_id -> CDP
        self.inject_src = ''
        self.ready = False
        self.last_status = {}
        self._last_next = 0
        self._boot_ts = time.time()
        self.study_url = ''
        try:
            with open(os.path.join(APP_DIR, 'last_study.txt'), 'r', encoding='utf-8') as f:
                self.study_url = f.read().strip()
        except Exception:
            pass

        root.title('智慧树加速器 · 修改器版')
        root.geometry('430x700')
        root.resizable(False, False)
        root.attributes('-topmost', True)

        style = ttk.Style()
        try:
            style.theme_use('clam')
        except Exception:
            pass

        head = tk.Label(root, text='智慧树加速器', font=('Microsoft YaHei UI', 15, 'bold'))
        head.pack(pady=(12, 2))
        self.sub = tk.Label(root, text='正在启动…', font=('Microsoft YaHei UI', 9), fg='#666')
        self.sub.pack()

        box = tk.Frame(root)
        box.pack(fill='x', padx=18, pady=10)

        tk.Label(box, text='加速倍率', font=('Microsoft YaHei UI', 10)).pack(anchor='w')
        row = tk.Frame(box)
        row.pack(fill='x')
        self.speed = tk.IntVar(value=8)
        self.speed_lbl = tk.Label(row, text='1.5×', width=5, font=('Consolas', 11, 'bold'))
        self.slider = ttk.Scale(row, from_=1, to=16, orient='horizontal',
                                command=self._on_slide)
        self.slider.pack(side='left', fill='x', expand=True)
        self.speed_lbl.pack(side='left')
        self.slider.set(1.5)

        btns = tk.Frame(root)
        btns.pack(fill='x', padx=18, pady=4)
        self.b_toggle = tk.Button(btns, text='暂停加速', height=2, command=self.on_toggle)
        self.b_toggle.pack(side='left', fill='x', expand=True, padx=(0, 4))
        self.b_next = tk.Button(btns, text='下一节', height=2, command=self.on_next)
        self.b_next.pack(side='left', fill='x', expand=True, padx=(4, 0))

        btns2 = tk.Frame(root)
        btns2.pack(fill='x', padx=18, pady=(0, 2))
        self.b_farm = tk.Button(btns2, text='挂机刷课（1.5× · 满 100% 自动下一节）', height=1,
                                command=self.on_farm)
        self.b_farm.pack(fill='x')

        btns3 = tk.Frame(root)
        btns3.pack(fill='x', padx=18, pady=(2, 0))
        self.b_exam = tk.Button(btns3, text='AI 做章测（不提交）', height=1,
                                command=lambda: self.on_exam(False))
        self.b_exam.pack(side='left', fill='x', expand=True, padx=(0, 4))
        self.b_exam2 = tk.Button(btns3, text='AI 做章测并提交', height=1,
                                 command=lambda: self.on_exam(True))
        self.b_exam2.pack(side='left', fill='x', expand=True, padx=(4, 0))

        # ---- DeepSeek API Key（别人用这个软件时在这里填）----
        keyrow = tk.LabelFrame(root, text=' AI 章测用的 DeepSeek Key ')
        keyrow.pack(fill='x', padx=18, pady=(6, 0))
        self.v_key = tk.StringVar(value=self._load_key())
        self.e_key = tk.Entry(keyrow, textvariable=self.v_key, show='*',
                              font=('Consolas', 9))
        self.e_key.pack(side='left', fill='x', expand=True, padx=(8, 4), pady=6)
        tk.Button(keyrow, text='保存', width=6, command=self.on_save_key).pack(side='left', padx=(0, 4), pady=6)
        tk.Button(keyrow, text='清除', width=6, command=self.on_clear_key).pack(side='left', padx=(0, 8), pady=6)

        opts = tk.LabelFrame(root, text=' 开关 ')
        opts.pack(fill='x', padx=18, pady=8)
        self.v_mute = tk.BooleanVar(value=True)
        self.v_next = tk.BooleanVar(value=True)
        self.v_quiz = tk.BooleanVar(value=True)
        for var, text, cb in ((self.v_mute, '自动静音（后台放）', 'mute'),
                              (self.v_next, '看完自动下一节', 'next'),
                              (self.v_quiz, '自动处理弹题', 'quiz')):
            tk.Checkbutton(opts, text=text, variable=var, anchor='w',
                           command=lambda c=cb: self.on_opt(c)).pack(fill='x', padx=8)

        self.stat = tk.Label(root, text='状态：等待页面…', justify='left', anchor='w',
                             font=('Microsoft YaHei UI', 9), fg='#1a6')
        self.stat.pack(fill='x', padx=18, pady=(4, 2))

        self.logbox = tk.Text(root, height=6, font=('Consolas', 8), fg='#555', bg='#f7f7f7',
                              relief='flat', wrap='word')
        self.logbox.pack(fill='both', expand=True, padx=18, pady=(0, 12))

        threading.Thread(target=self.bootstrap, daemon=True).start()
        self.root.after(200, self.pump)

    # -------- 日志 --------
    def log(self, msg):
        line = time.strftime('%H:%M:%S ') + msg
        self.logs.append(line)
        self.logs = self.logs[-60:]
        try:
            with open(os.path.join(APP_DIR, 'log.txt'), 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass
        self.root.after(0, lambda: (self.logbox.delete('1.0', 'end'),
                                    self.logbox.insert('end', '\n'.join(self.logs))))
        self.root.after(0, lambda: self.sub.config(text=msg))

    # -------- 启动流程 --------
    def bootstrap(self):
        try:
            with open(INJECT_PATH, 'r', encoding='utf-8') as f:
                self.inject_src = f.read()
        except Exception as e:
            self.log('读取 inject.js 失败：%s' % e)
            return
        try:
            copy_ok = ensure_profile(self.log)
            launch_edge(self.log)
            self.ready = True
            self.log('已启动浏览器，等待进入课程页面…')
        except Exception as e:
            self.log('启动失败：%s' % e)
            return
        self.attach_loop()

    def attach_loop(self):
        """持续把注入脚本挂到所有智慧树页面（新标签/跳转都覆盖）"""
        while True:
            try:
                targets = http_json('/json/list')
            except Exception:
                time.sleep(2)
                continue
            for t in targets:
                if t.get('type') != 'page':
                    continue
                url = t.get('url', '')
                # 只处理播放页：营销首页/学习中心注入没有意义，还会刷屏
                if 'studyvideoh5' not in url:
                    continue
                tid = t.get('id')
                if tid in self.cdp and self.cdp[tid].alive:
                    continue
                try:
                    c = CDP(t['webSocketDebuggerUrl'])
                    c.call('Runtime.enable')
                    try:
                        c.call('Page.enable')
                        c.call('Page.addScriptToEvaluateOnNewDocument', source=self.inject_src)
                    except Exception:
                        pass
                    c.js(self.inject_src)
                    self.cdp[tid] = c
                    # 页面刷新后注入脚本会用默认配置，把面板当前设置补回去
                    try:
                        kw = self._cur_cfg()
                        if kw:
                            c.js('window.__ZS && __ZS.set(%s)' % json.dumps(kw, ensure_ascii=False))
                    except Exception:
                        pass
                    if 'studyvideoh5' in url and 'recruitAndCourseId' in url:
                        if url != self.study_url:
                            self.study_url = url
                            try:
                                with open(os.path.join(APP_DIR, 'last_study.txt'), 'w', encoding='utf-8') as f:
                                    f.write(url)
                            except Exception:
                                pass
                    self._grace_ts = time.time()
                    self.log('已注入：%s' % url[:70])
                except Exception as e:
                    self.log('注入失败：%s' % str(e)[:80])
            # 清理断开的
            for tid in list(self.cdp):
                if not self.cdp[tid].alive:
                    self.cdp.pop(tid, None)
            time.sleep(2.5)

    # -------- 命令 --------
    def _cur_cfg(self):
        """面板当前设置 → __ZS.set() 的参数（页面刷新/重连后回填用）"""
        sp, rate, full = self._split(getattr(self, '_sent_speed', 1.5))
        return {'speed': sp, 'rate': rate, 'quizOS': True,
                'on': self.b_toggle.cget('text') == '暂停加速',
                'mute': bool(self.v_mute.get()),
                'next': bool(self.v_next.get()),
                'quiz': bool(self.v_quiz.get()),
                'onlyFull': full,
                'tail': 0 if full else 45}

    def _apply(self, **kw):
        for tid, c in list(self.cdp.items()):
            if not c.alive:
                continue
            try:
                body = json.dumps(kw, ensure_ascii=False)
                c.js('window.__ZS && __ZS.set(%s)' % body)
            except Exception:
                pass

    @staticmethod
    def _split(v):
        """滑杆值 → (跳格倍率, 平台原生倍速, 是否挂机模式)
        ≤1.5 走播放器自带的 1.0/1.25/1.5（平台认，进度照算）；
        >1.5 才用跳格（跳格只让画面跑得快，进度还是按真实时长）。"""
        if v <= 1.5:
            rate = 1.5 if v >= 1.4 else (1.25 if v >= 1.1 else 1.0)
            return 1, rate, True
        return v, 0, False

    def _on_slide(self, _=None):
        raw = float(self.slider.get())
        val = round(raw * 4) / 4.0
        lbl = getattr(self, 'speed_lbl', None)
        if lbl is not None:
            lbl.config(text=('%g×' % val))
        if self.ready and val != getattr(self, '_sent_speed', None):
            self._sent_speed = val
            sp, rate, full = self._split(val)
            self._apply(speed=sp, rate=rate, onlyFull=full,
                        tail=0 if full else 45)

    def on_farm(self):
        """一键挂机：平台原生 1.5 倍 + 满 100% 自动下一节。"""
        self.slider.set(1.5)
        self.speed_lbl.config(text='1.5×')
        self._sent_speed = 1.5
        self._apply(speed=1, rate=1.5, onlyFull=True, tail=0, on=True,
                    mute=True, next=True, quiz=True)
        self.b_toggle.config(text='暂停加速')
        self.v_mute.set(True); self.v_next.set(True); self.v_quiz.set(True)
        self.log('已切到「挂机刷课」：平台自带 1.5 倍，满 100% 自动下一节')

    # -------- DeepSeek Key --------
    KEY_FILE = os.path.join(APP_DIR, 'api_key.txt')

    def _load_key(self):
        try:
            with open(self.KEY_FILE, 'r', encoding='ascii') as f:
                return f.read().strip()
        except Exception:
            return ''

    def on_save_key(self):
        k = (self.v_key.get() or '').strip()
        if not k.startswith('sk-') or len(k) < 20:
            self.log('Key 格式不对：应该以 sk- 开头，请到 platform.deepseek.com 复制')
            return
        try:
            with open(self.KEY_FILE, 'w', encoding='ascii') as f:
                f.write(k)
            self.log('已保存 DeepSeek Key（%d 位），AI 做章测可以直接用了' % len(k))
        except Exception as e:
            self.log('保存失败：%s' % str(e)[:60])

    def on_clear_key(self):
        try:
            with open(self.KEY_FILE, 'w', encoding='ascii') as f:
                f.write('')
            self.v_key.set('')
            self.log('已清除本机保存的 DeepSeek Key')
        except Exception as e:
            self.log('清除失败：%s' % str(e)[:60])

    def on_exam(self, submit):
        """AI 做章测：抓题 -> DeepSeek 作答 -> 真实鼠标点选（可选提交）。"""
        if getattr(self, '_exam_busy', False):
            self.log('章测任务还在跑，等它结束')
            return
        if not self._load_key():
            self.log('还没填 DeepSeek Key：在上面「AI 章测用的 DeepSeek Key」里粘贴后点「保存」')
            return
        self._exam_busy = True
        self.log('开始 AI 做章测（%s）…先暂停挂机点击' % ('提交' if submit else '不提交'))
        try:
            self._apply(on=False, mute=False)
        except Exception:
            pass

        def run():
            cmd = [sys.executable, os.path.join(APP_DIR, 'exam.py')]
            if submit:
                cmd.append('--submit')
            try:
                env = dict(os.environ)
                env['PYTHONIOENCODING'] = 'utf-8'
                env['PYTHONUTF8'] = '1'
                p = subprocess.Popen(cmd, cwd=APP_DIR, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=env,
                                     creationflags=0x08000000)
                while True:
                    raw = p.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode('utf-8', 'ignore').rstrip()
                    if line:
                        self.log('  ' + line)
                p.wait()
                self.log('章测结束（返回码 %s）' % p.returncode)
            except Exception as e:
                self.log('章测失败：%s' % e)
            finally:
                self._exam_busy = False
                try:
                    self._apply(on=True, mute=True)
                except Exception:
                    pass

        threading.Thread(target=run, daemon=True).start()

    def on_toggle(self):
        on = self.b_toggle.cget('text') == '暂停加速'
        self._apply(on=not on)
        self.b_toggle.config(text='继续加速' if on else '暂停加速')

    def on_next(self):
        for tid, c in list(self.cdp.items()):
            try:
                c.js('window.__ZS && __ZS.next()')
            except Exception:
                pass
        self.log('已点“下一节”')

    def on_opt(self, which):
        m = {'mute': self.v_mute, 'next': self.v_next, 'quiz': self.v_quiz}
        self._apply(**{which: m[which].get()})

    # -------- 看门狗：课程页被踢走就拉回来（带退避，别把风控惹毛） --------
    def ensure_study(self):
        try:
            targets = http_json('/json/list')
        except Exception:
            return
        pages = [t for t in targets if t.get('type') == 'page']
        if any('studyvideoh5' in t.get('url', '') for t in pages):
            return
        url = self.study_url
        if not url:
            return
        # 退避：被踢后 45s 才拉第一次；连续被踢就翻倍（最长 5 分钟）
        # 站点刚把我们弹出去时立刻回跳，会立刻再被弹一次，形成死循环
        now = time.time()
        gap = getattr(self, '_kick_gap', 45)
        self._kick_gap = gap
        if now - getattr(self, '_last_reopen', 0) < gap:
            return
        if now - getattr(self, '_last_reopen', 0) < 60 and getattr(self, '_last_reopen', 0):
            self._kicks = getattr(self, '_kicks', 0) + 1
            if self._kicks >= 2:
                self._kick_gap = min(300, gap * 2)
                self._kicks = 0
        else:
            self._kicks = 0
        self._last_reopen = now
        # 优先把现有的空白页/智慧树页导航过去；实在没有才新建
        cands = [t for t in pages if t.get('url', '').startswith('about:blank')] + \
                [t for t in pages if 'zhihuishu.com' in t.get('url', '')]
        for t in cands[:3]:
            try:
                c = self.cdp.get(t['id'])
                if not (c and c.alive):
                    c = CDP(t['webSocketDebuggerUrl'])
                c.call('Page.enable')
                c.call('Page.navigate', url=url)
                self._grace_ts = time.time()
                self.log('课程页被踢走，已拉回（下次重试间隔 %ds）' % self._kick_gap)
                return
            except Exception as e:
                self.log('拉回失败(%s)：%s' % (t.get('url', '')[:24], str(e)[:40]))
        try:
            req = urllib.request.Request('http://127.0.0.1:%d/json/new?%s' % (PORT, urllib.parse.quote(url, safe=':?=&')),
                                         data=b'', method='PUT')
            urllib.request.urlopen(req, timeout=5).read()
            self.log('重新打开课程页')
        except Exception as e:
            self.log('重开课程页失败：%s' % str(e)[:50])

    # -------- 状态刷新 --------
    def pump(self):
        if self.ready:
            self.ensure_study()
        if self.ready and self.cdp:
            pool = [(tid, c) for tid, c in self.cdp.items() if c.alive]
            # 播放页优先（其它页面没有 video，轮询没意义）
            try:
                urls = {t['id']: t.get('url', '') for t in http_json('/json/list') if t.get('type') == 'page'}
                pool.sort(key=lambda kv: 0 if 'studyvideoh5' in urls.get(kv[0], '') else 1)
            except Exception:
                pass
            if pool:
                threading.Thread(target=self._poll_once, args=(pool,), daemon=True).start()
        self.root.after(1200, self.pump)

    def _poll_once(self, pool):
        tid, c = pool[0]
        try:
            raw = c.js('window.__ZS ? JSON.stringify(__ZS.status()) : "null"')
            s = json.loads(raw) if raw and raw != 'null' else {}
        except Exception:
            return
        self.last_status = s
        q = s.get('quizzes', 0)
        if q != getattr(self, '_last_q', 0):
            self._last_q = q
            self.log('处理弹题 %d 次（quizOpen=%s）' % (q, s.get('quizOpen')))
        if not s:
            self.root.after(0, lambda: self.stat.config(text='状态：页面里没有找到视频（先点开一节课）'))
            return
        t, d = s.get('t', 0), s.get('d', 0)
        pct = (t / d * 100) if d else 0
        real = s.get('pct', -1)
        rate = s.get('rate', 1) or 1
        spd = s.get('speed', 1) or 1
        shown = rate if rate > 1.01 else spd
        txt = ('状态：%s   %s / %s  (%.0f%%)   跳格 %d 次\n弹题处理 %d   自动跳转 %d   倍率 %g×'
               % ('播放中' if not s.get('paused') else '已暂停',
                  fmt_t(t), fmt_t(d), pct,
                  s.get('jumps', 0), s.get('quizzes', 0), s.get('nexts', 0),
                  shown))
        if real >= 0:
            txt += '   进度 %d%%' % real
            if s.get('onlyFull'):
                txt += '（满 100% 自动下一节）'
        self.root.after(0, lambda: self.stat.config(text=txt))

        # ---- 页面刷新后配置会退回默认值：每 10 秒把面板设置重推一次（幂等）----
        if time.time() - getattr(self, '_last_cfg_push', 0) > 10:
            self._last_cfg_push = time.time()
            try:
                c.js('window.__ZS && __ZS.set(%s)'
                     % json.dumps(self._cur_cfg(), ensure_ascii=False))
            except Exception:
                pass

        # ---- AI 章测进行中：不要再抢鼠标 ----
        if getattr(self, '_exam_busy', False):
            return

        # ---- 弹题：用真实鼠标选答案 + 点关闭（DOM click 会被判作弊）----
        if s.get('quizOpen') and time.time() - getattr(self, '_last_quiz_click', 0) > 1.4:
            self._last_quiz_click = time.time()
            self._quiz_step(c)

        # ---- 倍速：只在「真实鼠标点击」下改，DOM 点击会被站点判作弊踢回首页 ----
        want = s.get('wantRate', 0) or 0
        if want > 1.01 and abs((s.get('rate') or 1) - want) > 0.02 \
                and time.time() - getattr(self, '_last_rate', 0) > 6:
            self._last_rate = time.time()
            self._apply_rate(c, want)

        # ---- 本节没满 100% 就播到片尾了：真实点击当前课时重播 ----
        if s.get('needRepeat') and not s.get('quizOpen') \
                and time.time() - getattr(self, '_last_rep', 0) > 25:
            self._last_rep = time.time()
            self._click_current(c)

        # ---- 挂机刷课模式：目录百分比满 100% 就切下一节（不等片尾，省时间）----
        if s.get('onlyFull') and real >= 100 and not s.get('quizOpen'):
            # 启动后 20 秒内不动（刚注入时读到的是上一节的旧状态，连点会被平台踢）；
            # 切节频率压到 25 秒一次，避免“连续跳节”触发风控
            grace = max(getattr(self, '_grace_ts', 0), self._boot_ts)
            if (time.time() - grace > 30) and (time.time() - self._last_next > 30):
                self._last_next = time.time()
                self.log('本节已满 100%，切换下一节')
                self._trusted_next(c)
            return

        # ---- 播完自动换下一节：智慧树必须用“受信任点击”，DOM click 会跳营销首页 ----
        # 片尾先等一次心跳上报（约 30s 一次），否则最后那段时长白跑、目录百分比不涨
        try:
            if s.get('next') and not s.get('quizOpen'):
                done = s.get('ended') or s.get('atEnd') or (s.get('paused') and d > 5 and t >= d - 2)
                if done:
                    if not getattr(self, '_end_since', 0):
                        self._end_since = time.time()
                        self.log('已到片尾，等待上报 %d 秒再切下一节' % WAIT_REPORT)
                    waited = time.time() - self._end_since
                    if waited >= WAIT_REPORT and time.time() - self._last_next > 9:
                        self._last_next = time.time()
                        self._end_since = 0
                        self._trusted_next(c)
                else:
                    self._end_since = 0
        except Exception:
            pass

    NEXT_JS = r'''
(() => {
  const list = [...document.querySelectorAll('.clearfix.video')];
  if (!list.length) return 'null';
  const vd = document.querySelector('video');
  const dur = vd && isFinite(vd.duration) ? vd.duration : 0;
  const secOf = (txt) => {
    const m = (txt || '').match(/(\d{2}):(\d{2}):(\d{2})/) || (txt || '').match(/(\d{1,2}):(\d{2})\s*$/);
    if (!m) return -1;
    if (m.length === 4) return (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3]);
    return (+m[1]) * 60 + (+m[2]);
  };
  // 站点反作弊：跨多个课时跳转会把你踢回 www.zhihuishu.com。
  // 实测「只点紧邻的下一节（current_play 的下一个 li）」连点 3 次都安全，
  // 而按「时长匹配」推断当前位置会误判到 5.2 → 点到 5.3 立刻被踢。所以只认 current_play + 1。
  let cur = list.findIndex(el => /current_play/i.test(el.className || ''));
  if (cur < 0) {
    const vd0 = document.querySelector('video');
    const dur0 = vd0 && isFinite(vd0.duration) ? vd0.duration : 0;
    let best = 99;
    for (let i = 0; i < list.length; i++) {
      const s = secOf(list[i].textContent);
      if (s <= 0) continue;
      const dif = Math.abs(s - dur0);
      if (dif < best) { best = dif; cur = i; }
    }
    if (best > 3) cur = -1;
  }
  if (cur < 0) return 'null';
  const j = cur + 1;
  if (j >= list.length) return 'null';
  const t = (list[j].textContent || '').replace(/\s+/g, ' ').trim();
  try { list[j].scrollIntoView({ block: 'center' }); } catch (e) {}
  const r = list[j].getBoundingClientRect();
  if (!r || r.width < 5 || r.height < 5) return 'null';
  if (r.top < 4 || r.bottom > (window.innerHeight - 4)) return 'null';   // 不在可视区就别点
  // 换算成「屏幕物理像素」坐标，交给 OS 连点器去点
  const bx = Math.max(0, (window.outerWidth - window.innerWidth) / 2);
  const by = Math.max(0, window.outerHeight - window.innerHeight - bx);
  const dpr = window.devicePixelRatio || 1;
  const sx = Math.round((window.screenX + bx + r.left + r.width / 2) * dpr);
  const sy = Math.round((window.screenY + by + r.top + r.height / 2) * dpr);
  return JSON.stringify({x: r.left + r.width / 2, y: r.top + r.height / 2,
                         sx: sx, sy: sy, dpr: dpr,
                         txt: t.slice(0, 24), idx: j, cur: cur});
})()
'''

    REPEAT_JS = r'''
(() => {
  const li = document.querySelector('.clearfix.video.current_play');
  if (!li) return 'null';
  try { li.scrollIntoView({ block: 'center' }); } catch (e) {}
  const r = li.getBoundingClientRect();
  if (!r || r.width < 5 || r.height < 5) return 'null';
  if (r.top < 4 || r.bottom > (window.innerHeight - 4)) return 'null';
  const bx = Math.max(0, (window.outerWidth - window.innerWidth) / 2);
  const by = Math.max(0, window.outerHeight - window.innerHeight - bx);
  const dpr = window.devicePixelRatio || 1;
  return JSON.stringify({
    x: r.left + r.width / 2, y: r.top + r.height / 2,
    sx: Math.round((window.screenX + bx + r.left + r.width / 2) * dpr),
    sy: Math.round((window.screenY + by + r.top + r.height / 2) * dpr),
    txt: (li.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 24)
  });
})()
'''

    def _quiz_step(self, c):
        """弹题处下一步：真实鼠标点选项 / 点关闭。"""
        try:
            self._front(c)
            raw = c.js('__ZS.quizStep ? __ZS.quizStep() : "null"')
            if not raw or raw == 'null':
                self._quiz_null = getattr(self, '_quiz_null', 0) + 1
                if self._quiz_null in (15, 40):
                    self.log('弹题卡住了：连续 %d 次找不到可点的元素（可能是不认识的弹层）'
                             % self._quiz_null)
                return
            self._quiz_null = 0
            r = json.loads(raw)
            if not os_focus_edge():
                return
            time.sleep(0.15)
            os_click(r['x'], r['y'])
            if r.get('act') == 'pick':
                self._quiz_picked = True
                self.log('弹题：真实鼠标选中「%s」' % str(r.get('txt', ''))[:16])
            elif r.get('act') == 'close':
                self._quiz_picked = False
                self.log('弹题：真实鼠标点关闭')
            elif r.get('act') == 'msgbox':
                self.log('弹题：被「未做答不能关闭」拦住，点确定后重选')
        except Exception as e:
            self.log('弹题点击失败：%s' % str(e)[:50])

    def _front(self, c):
        """把目标标签页切到前台——连点器点的是屏幕坐标，标签页在后台就点到别的页面上。"""
        try:
            c.call('Page.bringToFront')
            time.sleep(0.25)
        except Exception:
            pass

    def _click_current(self, c):
        """本节没满 100% 但已经播到片尾：用真实鼠标点一下「当前课时」重播。
        不用 currentTime=0 回绕（合成 seek 会被判异常踢回首页）。"""
        try:
            self._front(c)
            if not os_focus_edge():
                return
            time.sleep(0.25)
            raw = c.js(self.REPEAT_JS)
            if not raw or raw == 'null':
                self.log('当前课时不可见，跳过重播')
                return
            r = json.loads(raw)
            os_click(r['sx'], r['sy'])
            self.log('本节未满 100%%，连点器重播：%s' % r.get('txt', '')[:18])
        except Exception as e:
            self.log('重播失败：%s' % str(e)[:50])

    def _apply_rate(self, c, want):
        """用 OS 连点器点开播放器倍速菜单并选 1.5×。

        两个前提（都实测踩过）：
        1. 播放器控制条只在「真实 mousemove」后显示 3 秒左右，所以先晃一下鼠标；
        2. 坐标只有控件可见时才有尺寸，必须晃完立刻读、立刻点。"""
        try:
            self._front(c)
            if not os_focus_edge():
                self.log('没找到 Edge 窗口，跳过设倍速')
                return
            time.sleep(0.25)
            hv = c.js('__ZS.hoverCoords()')
            if not hv or hv == 'null':
                return
            h = json.loads(hv)
            for dx in (0, 14, -14, 0, 10):          # 晃鼠标 → 控制条浮现
                _u32.SetCursorPos(int(h['x'] + dx), int(h['y']))
                time.sleep(0.12)
            raw = c.js('__ZS.rateCoords(%s)' % json.dumps(want))
            if not raw or raw == 'null':
                self.log('倍速控件没出来，稍后重试')
                return
            r = json.loads(raw)
            sp = r['span']
            os_click(sp['x'], sp['y'])              # 展开倍速菜单
            time.sleep(0.6)
            raw2 = c.js('__ZS.rateCoords(%s)' % json.dumps(want))
            r2 = json.loads(raw2) if raw2 and raw2 != 'null' else {}
            tb = r2.get('tab')
            if not tb:
                self.log('倍速菜单没展开，稍后重试')
                return
            os_click(tb['x'], tb['y'])              # 选 1.5×
            time.sleep(0.6)
            now = c.js('(document.querySelector("video")||{}).playbackRate')
            self.log('连点器设倍速 %s× → 实际 %s' % (r2.get('key'), now))
        except Exception as e:
            self.log('设倍速失败：%s' % str(e)[:60])

    def _trusted_next(self, c):
        """用 CDP 派发真实鼠标事件点目录里的下一节（isTrusted=true 才被站点接受）

        顺序很关键：先把标签页调到前台，等布局稳定，再取 rect，再点。
        反过来（先前台后取坐标）会因为窗口尺寸变化拿到过期的坐标，
        结果点到顶栏链接，整页跳到 www.zhihuishu.com。
        """
        try:
            c.call('Page.bringToFront')
        except Exception:
            pass
        time.sleep(0.45)
        try:
            raw = c.js(self.NEXT_JS)
        except Exception:
            return
        if not raw or raw == 'null':
            self.log('已是最后一节，或目录不可见')
            return
        try:
            r = json.loads(raw)
            x, y = float(r['x']), float(r['y'])
        except Exception:
            return
        key = str(r.get('txt', ''))
        # 点击前复核：该坐标上确实是这一节目录项（目录会随进度刷新重排，坐标可能过期）
        try:
            hit = c.js("""(() => {
              const e = document.elementFromPoint(%f, %f);
              const li = e && e.closest ? e.closest('.clearfix.video') : null;
              return li ? (li.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 24) : '';
            })()""" % (x, y))
        except Exception:
            hit = ''
        if hit and key and hit[:6] != key[:6]:
            self.log('目标漂移(%s≠%s)，本次跳过' % (hit[:10], key[:10]))
            return
        if key and key == getattr(self, '_last_next_key', '') and time.time() - getattr(self, '_last_next_key_t', 0) < 40:
            return                      # 同一个目标刚点过，别连点
        self._last_next_key = key
        self._last_next_key_t = time.time()
        sx, sy = r.get('sx'), r.get('sy')
        if sx is None:
            self.log('拿不到屏幕坐标，跳过')
            return
        try:
            os_focus_edge()
            time.sleep(0.35)
            os_click(sx, sy)
            self.log('连点器点下一节：%s（屏幕 %d,%d）' % (key, sx, sy))
        except Exception as e:
            self.log('切节失败：%s' % str(e)[:60])
            return
        # 点完核验：页面若被带到别处，立刻拉回课程页
        time.sleep(1.6)
        try:
            u = c.js('location.href') or ''
        except Exception:
            u = ''
        if 'studyvideoh5' not in u:
            self.log('切节后页面被带走(%s)，立即拉回' % u[:40])
            try:
                c.call('Page.navigate', url=self.study_url)
            except Exception:
                pass


def fmt_t(x):
    try:
        x = int(x)
        return '%d:%02d' % (x // 60, x % 60)
    except Exception:
        return '--'


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        try:
            with open(os.path.join(APP_DIR, 'crash.log'), 'a', encoding='utf-8') as _f:
                _f.write(time.strftime('%Y-%m-%d %H:%M:%S  ') + traceback.format_exc() + '\n')
        except Exception:
            pass
        raise
