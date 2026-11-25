const {contextBridge,ipcRenderer}=require('electron');
contextBridge.exposeInMainWorld('lab',{request:req=>ipcRenderer.invoke('lab:request',req),cancel:()=>ipcRenderer.invoke('lab:cancel')});
