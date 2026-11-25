const {contextBridge, ipcRenderer} = require('electron');
contextBridge.exposeInMainWorld('lab', {
  request: (req, id) => ipcRenderer.invoke('lab:request', req, id),
  newId: () => ipcRenderer.invoke('lab:newId'),
  cancel: id => ipcRenderer.invoke('lab:cancel', id),
  onDelta: fn => { const h = (_e, m) => fn(m); ipcRenderer.on('lab:delta', h); return () => ipcRenderer.removeListener('lab:delta', h); }
});
