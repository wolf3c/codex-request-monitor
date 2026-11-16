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
        reject(new Error('Local plugin timed out'));
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
    if (data.error) call.reject(new Error('Local plugin read failed'));
    else call.resolve(data.result);
  });

  async function initialize() {
    if (closed || window.parent === window) throw new Error('Local plugin view is disconnected');
    connected ??= request('ui/initialize', {
      protocolVersion: '2026-01-26',
      appInfo: {name: 'codex-request-monitor', version: '1.0.0'},
      appCapabilities: {availableDisplayModes: ['fullscreen']},
    }).then(() => send({method: 'ui/notifications/initialized', params: {}}))
      .catch(error => { connected = undefined; throw error; });
    await connected;
  }

  window.readLocalMonitorStatus = async () => {
    await initialize();
    const result = await request('tools/call', {name: 'read_request_monitor', arguments: {}});
    const status = result?._meta?.status;
    if (result?.isError || !status) throw new Error('Unable to read local Codex logs');
    return status;
  };

  window.readLocalMonitorNetwork = async () => {
    await initialize();
    const result = await request('tools/call', {name: 'read_network_diagnostics', arguments: {}});
    const network = result?._meta?.network;
    if (result?.isError || !network) throw new Error('Unable to inspect local network configuration');
    return network;
  };

  window.openTemporaryProxyPage = async () => {
    await initialize();
    const result = await request('tools/call', {name: 'open_temporary_proxy_page', arguments: {}});
    if (result?.isError || !result?._meta?.external_opened) throw new Error('Unable to open external browser');
  };

  window.openLocalMonitorLink = async url => {
    if (closed || !connected) throw new Error('Local plugin view is disconnected');
    if (!/^codex:\/\/threads\/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(url)) {
      throw new Error('Invalid chat link');
    }
    await connected;
    const result = await request('ui/open-link', {url});
    if (result?.isError) throw new Error('Unable to open chat');
  };
})();
