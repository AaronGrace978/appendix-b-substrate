const {app, BrowserWindow, ipcMain} = require('electron');
const {spawn, spawnSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
let win, active;
function pythonExecutable() {
  const bundled = path.join(process.resourcesPath, 'python', 'python.exe');
  return process.env.APPENDIX_B_PYTHON || (app.isPackaged && process.platform === 'win32' && fs.existsSync(bundled) ? bundled : (process.platform === 'win32' ? 'python' : 'python3'));
}
function bridge(req) {
  if (active) throw Error('A request is already running');
  if (!req || !['models','run'].includes(req.action)) throw Error('Invalid action');
  return new Promise((resolve,reject) => {
    const exe = pythonExecutable();
    const child = spawn(exe,[path.join(__dirname,'..','desktop_bridge.py'),path.join(app.getPath('userData'),'research')],{windowsHide:true,stdio:['pipe','pipe','pipe']});
    active=child;
    let out='', done=false;
    const finish=(error,value)=>{if(done)return;done=true;clearTimeout(timer);active=null;error?reject(error):resolve(value)};
    const timer=setTimeout(()=>{child.kill();finish(Error('Request timed out'))}, 25*60*1000);
    child.stdout.setEncoding('utf8');
    child.stdout.on('data',s=>{out+=s;if(out.length>16*1024*1024){child.kill();finish(Error('Output limit exceeded'))}});
    child.stderr.resume();
    child.on('error',e=>finish(e));
    child.stdin.on('error',()=>{});
    child.on('close',()=>{try {const result=JSON.parse(out); if(!result.ok)throw Error(result.error);finish(null,result.value)}catch(e){finish(e)}});
    child.stdin.end(JSON.stringify(req)+'\n');
  });
}
app.whenReady().then(async()=>{
  win=new BrowserWindow({width:1250,height:850,webPreferences:{preload:path.join(__dirname,'preload.cjs'),contextIsolation:true,nodeIntegration:false,sandbox:true}});
  win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
  win.webContents.on('will-navigate',e=>e.preventDefault());
  ipcMain.handle('lab:request',(e,req)=>{if(e.sender!==win.webContents || e.senderFrame!==win.webContents.mainFrame)throw Error('Invalid sender');return bridge(req)});
  ipcMain.handle('lab:cancel',e=>{if(e.sender===win.webContents && active)active.kill()});
  await win.loadFile(path.join(__dirname,'index.html'));
  if(process.argv.includes('--smoke-test')) {
    const ui = await win.webContents.executeJavaScript("document.title.includes('Appendix B') && !!window.lab && !!document.getElementById('run')");
    const py = spawnSync(pythonExecutable(), ['-c', 'import sqlite3, ssl; print(\"ready\")'], {windowsHide:true, timeout:10000, encoding:'utf8'});
    app.exit(ui && py.status===0 && py.stdout.trim()==='ready' ? 0 : 1);
  }
}).catch(err=>{console.error(err);app.exit(1)});
app.on('window-all-closed',()=>app.quit());
app.on('before-quit',()=>{if(active)active.kill()});
