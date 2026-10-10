// Download with a "save as" dialog where the browser allows it.
// showSaveFilePicker only exists in secure contexts (HTTPS or localhost) on
// Chromium browsers; elsewhere this falls back to a normal download, which
// asks for a location if the browser's "ask where to save" setting is on.
async function rlmDownload(url, suggestedName, el) {
  if (window.showSaveFilePicker) {
    let handle;
    try {
      handle = await window.showSaveFilePicker({ suggestedName });
    } catch (e) {
      if (e.name === 'AbortError') return;  // operator cancelled the dialog
      handle = null;
    }
    if (handle) {
      const label = el ? el.textContent : '';
      try {
        if (el) el.textContent = 'saving…';
        const resp = await fetch(url, { credentials: 'same-origin' });
        if (!resp.ok) throw new Error(resp.status + ' ' + (await resp.text()));
        await resp.body.pipeTo(await handle.createWritable());
        if (el) el.textContent = 'saved ✓';
      } catch (e) {
        if (el) el.textContent = label;
        alert('Download failed: ' + e.message);
      }
      return;
    }
  }
  const a = document.createElement('a');
  a.href = url;
  a.download = suggestedName;
  document.body.appendChild(a);
  a.click();
  a.remove();
}
