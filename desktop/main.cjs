const {app, BrowserWindow, ipcMain} = require('electron');
const {spawn, spawnSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

let win;
function pythonExecutable() {
  const bundled = path.join(process.resourcesPath, 'python', 'python.exe');
  return process.env.APPENDIX_B_PYTHON || (app.isPackaged && process.platform === 'win32' && fs.existsSync(bundled) ? bundled : (process.platform === 'win32' ? 'python' : 'python3'));
}

/* One long-lived Python substrate process, JSON lines with request ids.
   Requests run concurrently; chat replies stream as {id, delta} lines. */
const ACTIONS = ['models', 'run', 'chat', 'state', 'ping'];
const pending = new Map();
let child = null, buffer = '', nextId = 1;

function startBridge() {
  if (child) return child;
  const proc = spawn(pythonExecutable(), ['-u', path.join(__dirname, '..', 'desktop_bridge.py'), path.join(app.getPath('userData'), 'research'), '--serve'],
    {windowsHide: true, stdio: ['pipe', 'pipe', 'pipe']});
  child = proc; buffer = '';
  proc.stdout.setEncoding('utf8');
  proc.stdout.on('data', chunk => {
    buffer += chunk;
    let nl;
    while ((nl = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, nl); buffer = buffer.slice(nl + 1);
      let msg; try { msg = JSON.parse(line); } catch { continue; }
      const p = pending.get(msg.id);
      if (!p) continue;
      if ('delta' in msg || 'thinking' in msg) { if (win && !win.isDestroyed()) win.webContents.send('lab:delta', {id: msg.id, delta: msg.delta, thinking: msg.thinking}); continue; }
      pending.delete(msg.id); clearTimeout(p.timer);
      msg.ok ? p.resolve(msg.value) : p.reject(Error(msg.error));
    }
    if (buffer.length > 16 * 1024 * 1024) proc.kill();
  });
  proc.stderr.resume();
  proc.stdin.on('error', () => {});
  const died = err => {
    if (child !== proc) return;
    child = null;
    for (const [id, p] of pending) { clearTimeout(p.timer); p.reject(err || Error(p.cancelled ? 'Cancelled' : 'Substrate process exited')); pending.delete(id); }
  };
  proc.on('error', died);
  proc.on('close', () => died());
  return proc;
}

function bridge(req, id = nextId++) {
  if (!req || !ACTIONS.includes(req.action)) return Promise.reject(Error('Invalid action'));
  return new Promise((resolve, reject) => {
    const proc = startBridge();
    const timer = setTimeout(() => cancel(id, true), 25 * 60 * 1000);
    pending.set(id, {resolve, reject, timer, action: req.action});
    proc.stdin.write(JSON.stringify({...req, id}) + '\n');
  });
}

function cancel(id, hard = false) {
  const p = pending.get(id);
  if (!p) return;
  p.cancelled = true;
  if (p.action === 'chat' && !hard) {
    // Streaming chat stops cooperatively; the process keeps serving other requests.
    if (child) child.stdin.write(JSON.stringify({action: 'cancel', id: 'c' + id, target: id}) + '\n');
  } else if (child) {
    // Experiments are synchronous: restart the process (other pending requests fail).
    child.kill();
  }
}

app.whenReady().then(async () => {
  win = new BrowserWindow({width: 1320, height: 880, minWidth: 960, minHeight: 640, backgroundColor: '#080b10', title: 'Appendix B', autoHideMenuBar: true,
    icon: path.join(__dirname, 'icon.png'),
    webPreferences: {preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true}});
  win.webContents.setWindowOpenHandler(() => ({action: 'deny'}));
  win.webContents.on('will-navigate', e => e.preventDefault());
  const trusted = e => e.sender === win.webContents && e.senderFrame === win.webContents.mainFrame;
  ipcMain.handle('lab:request', (e, req, id) => { if (!trusted(e)) throw Error('Invalid sender'); return bridge(req, Number.isInteger(id) ? id : undefined); });
  ipcMain.handle('lab:newId', e => { if (trusted(e)) return nextId++; });
  ipcMain.handle('lab:cancel', (e, id) => { if (!trusted(e)) return; if (Number.isInteger(id)) cancel(id); else for (const k of [...pending.keys()]) cancel(k); });
  await win.loadFile(path.join(__dirname, 'index.html'));
  if (process.argv.includes('--smoke-test')) {
    const ui = await win.webContents.executeJavaScript("document.title.includes('Appendix B') && !!window.lab && !!document.getElementById('run')");
    const py = spawnSync(pythonExecutable(), ['-c', 'import sqlite3, ssl; print("ready")'], {windowsHide: true, timeout: 10000, encoding: 'utf8'});
    let served = false;
    try { served = await Promise.race([bridge({action: 'ping'}).then(v => v === 'pong'), new Promise(r => setTimeout(() => r(false), 20000))]); } catch { served = false; }
    app.exit(ui && py.status === 0 && py.stdout.trim() === 'ready' && served ? 0 : 1);
  }
}).catch(err => { console.error(err); app.exit(1); });
app.on('window-all-closed', () => app.quit());
app.on('before-quit', () => { if (child) child.kill(); });
