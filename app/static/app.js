// Upload one file with a PUT, reporting progress. Resolves with the JSON reply.
function rlmPut(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', url);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total); };
    xhr.onload = () => xhr.status === 200 ? resolve(JSON.parse(xhr.responseText))
      : reject(new Error(xhr.status + ': ' + xhr.responseText));
    xhr.onerror = () => reject(new Error('network error'));
    xhr.send(file);
  });
}

// Upload an archive (to _manager/uploads) or a whole folder (to _manager/uploads/folder-<id>),
// put the resulting server path into the input #targetId, then submit #formId.
async function rlmUploadAndScan(kind, inputId, targetId, formId, barId, msgId) {
  const files = Array.from(document.getElementById(inputId).files);
  const msg = document.getElementById(msgId), bar = document.getElementById(barId);
  if (!files.length) { msg.textContent = 'Choose ' + (kind === 'folder' ? 'a folder' : 'a file') + ' first.'; return; }
  const total = files.reduce((n, f) => n + f.size, 0) || 1;
  let done = 0, path = null;
  bar.style.display = 'block'; bar.value = 0;
  try {
    if (kind === 'archive') {
      msg.textContent = 'Uploading ' + files[0].name + '…';
      path = (await rlmPut('/import/upload?name=' + encodeURIComponent(files[0].name), files[0],
        (l) => { bar.value = 100 * l / total; })).path;
    } else {
      const batch = new Date().toISOString().replace(/[^0-9]/g, '').slice(0, 14) + '-' + Math.random().toString(36).slice(2, 6);
      for (const [i, f] of files.entries()) {
        const rel = f.webkitRelativePath || f.name;
        msg.textContent = 'Uploading ' + (i + 1) + ' of ' + files.length + ': ' + rel;
        path = (await rlmPut('/identify/upload-file?batch=' + batch + '&rel=' + encodeURIComponent(rel), f,
          (l) => { bar.value = 100 * (done + l) / total; })).path;
        done += f.size;
      }
    }
  } catch (e) { msg.textContent = 'Upload failed: ' + e.message; return; }
  bar.value = 100;
  document.getElementById(targetId).value = path;
  msg.textContent = 'Uploaded to ' + path + ' — identifying…';
  htmx.trigger('#' + formId, 'submit');
}

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
