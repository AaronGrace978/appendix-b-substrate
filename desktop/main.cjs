const {app, BrowserWindow, ipcMain} = require('electron');
const {spawn} = require('node:child_process');
const path = require('node:path');
let win, active;
function bridge(req) {
  if (active) throw Error('A request is already running');
  if (!req || !['models','run'].includes(req.action)) throw Error('Invalid action');
  return new Promise((resolve,reject) => {
    const exe = process.env.APPENDIX_B_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
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
app.whenReady().then(()=>{
  win=new BrowserWindow({width:1250,height:850,webPreferences:{preload:path.join(__dirname,'preload.cjs'),contextIsolation:true,nodeIntegration:false,sandbox:true}});
  win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
  win.webContents.on('will-navigate',e=>e.preventDefault());
  ipcMain.handle('lab:request',(e,req)=>{if(e.sender!==win.webContents || e.senderFrame!==win.webContents.mainFrame)throw Error('Invalid sender');return bridge(req)});
  ipcMain.handle('lab:cancel',e=>{if(e.sender===win.webContents && active)active.kill()});
  win.loadFile('index.html');
});
app.on('window-all-closed',()=>app.quit());
app.on('before-quit',()=>{if(active)active.kill()});
