const form = document.getElementById('finderForm');
const results = document.getElementById('results');
const submitBtn = document.getElementById('submitBtn');
const timer = document.getElementById('timer');

const esc = (s = '') => s.replace(/[&<>"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));

let intervalId = null;
function startTimer() {
  const start = Date.now();
  timer.textContent = 'Searching... 00:00 / 03:00';
  intervalId = setInterval(() => {
    const sec = Math.floor((Date.now() - start) / 1000);
    const mm = String(Math.floor(sec / 60)).padStart(2, '0');
    const ss = String(sec % 60).padStart(2, '0');
    timer.textContent = `Searching... ${mm}:${ss} / 03:00`;
    if (sec >= 180) clearInterval(intervalId);
  }, 1000);
}
function stopTimer(msg = 'Ready') {
  if (intervalId) clearInterval(intervalId);
  timer.textContent = msg;
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  submitBtn.disabled = true;
  startTimer();
  results.innerHTML = '<h2>Results</h2><p class="muted">Looking for logo candidates...</p>';

  const payload = {
    fi_name: document.getElementById('fiName').value.trim(),
    home_url: document.getElementById('homeUrl').value.trim(),
    login_url: document.getElementById('loginUrl').value.trim(),
  };

  try {
    const res = await fetch('/api/find-logo', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();

    if (!res.ok) {
      results.innerHTML = `<h2>Results</h2><p>${esc(data.error || data.message || 'Unknown error')}</p>`;
      stopTimer('Failed');
      return;
    }

    if (!data.results?.length) {
      results.innerHTML = `<h2>Results</h2><p>${esc(data.message || 'No results')}</p>`;
      stopTimer('Need more details');
      return;
    }

    const cards = data.results.map((item) => `
      <article class="logo-card">
        <img src="data:image/png;base64,${item.data_b64}" alt="logo candidate" />
        <span class="badge">Confidence ${(item.confidence * 100).toFixed(0)}%</span>
        <p class="meta"><strong>Source:</strong> ${esc(item.source)}</p>
        <p class="meta"><strong>Reason:</strong> ${esc(item.reason)}</p>
        <p class="meta"><strong>Size:</strong> ${item.size_bytes} bytes (${item.width}x${item.height})</p>
        <a href="/api/download/${item.id}"><button type="button">Download PNG</button></a>
      </article>
    `).join('');

    results.innerHTML = `<h2>Results</h2><p>${esc(data.message)}</p><div class="result-grid">${cards}</div>`;
    stopTimer('Done');
  } catch (err) {
    results.innerHTML = '<h2>Results</h2><p>Request failed. Please try again.</p>';
    stopTimer('Failed');
  } finally {
    submitBtn.disabled = false;
  }
});
