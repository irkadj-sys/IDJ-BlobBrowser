function formatSize(bytes) {
  if (bytes == null) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function encodePath(path) {
  return path.split('/').map(encodeURIComponent).join('/');
}

async function loadMe() {
  const res = await fetch('/api/me');
  if (!res.ok) {
    document.getElementById('user').textContent = 'Not authenticated or not allowed.';
    return;
  }
  const me = await res.json();
  document.getElementById('user').textContent = `Signed in: ${me.name} (${me.email})`;
}

async function loadFiles() {
  const prefix = document.getElementById('prefixInput').value.trim();
  const qs = prefix ? `?prefix=${encodeURIComponent(prefix)}` : '';
  const res = await fetch(`/api/files${qs}`);
  const tbody = document.querySelector('#fileTable tbody');
  const grid = document.getElementById('imageGrid');
  tbody.innerHTML = '';
  grid.innerHTML = '';

  if (!res.ok) {
    document.getElementById('count').textContent = 'Failed to load files.';
    return;
  }

  const data = await res.json();
  document.getElementById('count').textContent = `${data.count} file(s)`;

  for (const item of data.items) {
    const tr = document.createElement('tr');
    const openHref = `/api/files/${encodePath(item.name)}`;
    tr.innerHTML = `
      <td>${item.name}</td>
      <td>${formatSize(item.size)}</td>
      <td>${item.content_type || ''}</td>
      <td>${item.last_modified || ''}</td>
      <td><a href="${openHref}" target="_blank" rel="noopener noreferrer">Open</a></td>
    `;
    tbody.appendChild(tr);

    if (item.is_image) {
      const card = document.createElement('div');
      card.className = 'thumb';
      const imgSrc = `/api/images/${encodePath(item.name)}`;
      card.innerHTML = `<a href="${openHref}" target="_blank" rel="noopener noreferrer"><img src="${imgSrc}" alt="${item.name}" /></a><div class="name">${item.name}</div>`;
      grid.appendChild(card);
    }
  }
}

async function uploadFile(event) {
  event.preventDefault();
  const status = document.getElementById('uploadStatus');
  status.textContent = '';

  const fileInput = document.getElementById('fileInput');
  const folderInput = document.getElementById('folderInput');
  if (!fileInput.files.length) {
    status.textContent = 'Choose a file first.';
    return;
  }

  const form = new FormData();
  form.append('file', fileInput.files[0]);
  form.append('folder', folderInput.value || '');

  const btn = event.target.querySelector('button[type="submit"]');
  btn.disabled = true;
  status.textContent = 'Uploading...';

  try {
    const res = await fetch('/api/upload', { method: 'POST', body: form });
    const data = await res.json();
    if (!res.ok) {
      status.textContent = data.detail || 'Upload failed';
    } else {
      status.textContent = `Uploaded: ${data.blob_name}`;
      fileInput.value = '';
      await loadFiles();
    }
  } catch (err) {
    status.textContent = 'Upload failed due to network error.';
  } finally {
    btn.disabled = false;
  }
}

window.addEventListener('DOMContentLoaded', async () => {
  document.getElementById('uploadForm').addEventListener('submit', uploadFile);
  document.getElementById('refreshBtn').addEventListener('click', loadFiles);
  await loadMe();
  await loadFiles();
});
