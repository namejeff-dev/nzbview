(() => {
  const button = document.getElementById('setup-collect');
  const status = document.getElementById('setup-run-status');
  let timer;
  async function poll() {
    try {
      const response = await fetch('/api/collector/status');
      if (!response.ok) throw new Error('Could not read collector status. Sign in again if needed.');
      const data = await response.json();
      button.disabled = data.running || !data.configured;
      document.getElementById('setup-config-status').textContent = data.config_message;
      if (data.running) {
        status.textContent = `Collector running · ${Math.round(data.duration_seconds || 0)}s`;
        timer = setTimeout(poll, 1000);
      } else if (data.status === 'success' || data.status === 'partial') {
        status.textContent = data.message;
        window.location.assign('/');
      } else if (data.status === 'error') {
        status.textContent = data.message + (data.output_tail ? '\n' + data.output_tail : '');
      }
    } catch (err) { status.textContent = err.message; button.disabled = false; }
  }
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const response = await fetch('/api/collector/run', {method: 'POST', headers: {'X-Nzbview-Request': '1'}});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Could not start collector.');
      clearTimeout(timer);
      poll();
    } catch (err) { status.textContent = err.message; button.disabled = false; }
  });
  poll();
})();
