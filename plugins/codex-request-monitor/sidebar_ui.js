(() => {
  const pending = new Map();
  let nextId = 0;
  let targetOrigin = '*';
  let connected;
  let closed = false;

  function send(message) {
    window.parent.postMessage({jsonrpc: '2.0', ...message}, targetOrigin);
  }

  function request(method, params) {
    return new Promise((resolve, reject) => {
      const id = ++nextId;
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error('本机插件响应超时'));
      }, 8000);
      pending.set(id, {resolve, reject, timer});
      send({id, method, params});
    });
  }

  window.addEventListener('message', ({source, origin, data}) => {
    if (source !== window.parent || (targetOrigin !== '*' && origin !== targetOrigin) ||
        data?.jsonrpc !== '2.0') return;
    if (data.method === 'ui/resource-teardown' || data.method === 'ping') {
      if (data.method === 'ui/resource-teardown') closed = true;
      send({id: data.id, result: {}});
      return;
    }
    const call = pending.get(data.id);
    if (!call) return;
    pending.delete(data.id);
    clearTimeout(call.timer);
    if (origin !== 'null') targetOrigin = origin;
    if (data.error) call.reject(new Error('本机插件读取失败'));
    else call.resolve(data.result);
  });

  window.readLocalMonitorStatus = async () => {
    if (closed || window.parent === window) throw new Error('本机插件界面未连接');
    connected ??= request('ui/initialize', {
      protocolVersion: '2026-01-26',
      appInfo: {name: 'codex-request-monitor', version: '1.0.0'},
      appCapabilities: {availableDisplayModes: ['fullscreen']},
    }).then(() => send({method: 'ui/notifications/initialized', params: {}}))
      .catch(error => { connected = undefined; throw error; });
    await connected;
    const result = await request('tools/call', {name: 'read_request_monitor', arguments: {}});
    const status = result?._meta?.status;
    if (result?.isError || !status) throw new Error('无法读取本机 Codex 日志');
    return status;
  };
})();
