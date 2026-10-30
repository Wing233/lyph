const invoke = window.__TAURI__.core.invoke;
let current = {running: false, status: {local_paused: false}};
const message = text => { document.getElementById('message').textContent = text; };
async function call(command, args) { try { const value = await invoke(command,args); await refresh(); return value; } catch(error) { message(String(error)); throw error; } }
async function refresh() {
  try {
    current = await invoke('status'); const s=current.status;
    document.getElementById('pairing').hidden=current.paired;
    document.getElementById('state').textContent=s.revoked?'已吊销':current.running?'运行中':'未启动';
    document.getElementById('pending').textContent=s.pending_count;
    document.getElementById('confirmed').textContent=s.confirmed_count;
    document.getElementById('cache').textContent=(s.cache_bytes/1048576).toFixed(1)+' MiB';
    const labels={capture_unavailable:'当前无法截屏，请检查桌面与系统权限；速记仍可用。',cache_full:'缓存已满，新增截图暂停；原队列保留。',device_revoked:'设备凭证已吊销，采集与上传停止。',capturing:'主屏采集正常',waiting:'等待下一次采集'};
    document.getElementById('capture').textContent=s.local_paused?'本机截图已暂停':s.remote_paused?'后台暂停截图':labels[s.capture_status]||s.capture_status;
    document.getElementById('last').textContent=s.last_capture_at?'最近采集：'+new Date(s.last_capture_at).toLocaleString('zh-CN'):'暂无采集';
    document.getElementById('start').textContent=current.running?'停止采集':'启动采集';
    document.getElementById('pause').textContent=s.local_paused?'恢复截图':'暂停截图';
    if(s.last_error)message(s.last_error);
  } catch(error) {message(String(error));}
}
document.getElementById('pairing').addEventListener('submit',async e=>{e.preventDefault();const data=new FormData(e.target);try{await call('pair',{baseUrl:data.get('base_url'),deviceToken:data.get('device_token')});e.target.reset();message('配对已保存。点击启动采集后开始记录。');}catch{}});
document.getElementById('start').onclick=async()=>{try{await call(current.running?'stop':'start');message(current.running?'采集已启动。':'已停止，离线队列保留。');}catch{}};
document.getElementById('pause').onclick=()=>call('pause',{paused:!current.status.local_paused}).catch(()=>{});
document.getElementById('capture-now').onclick=()=>call('action',{command:'capture'}).catch(()=>{});
document.getElementById('sync').onclick=()=>call('action',{command:'sync'}).catch(()=>{});
document.getElementById('note').addEventListener('submit',async e=>{e.preventDefault();const input=e.target.elements.text;try{await call('note',{text:input.value});input.value='';message('速记已持久保存，等待上传确认。');}catch{}});
refresh(); setInterval(refresh,1500);
