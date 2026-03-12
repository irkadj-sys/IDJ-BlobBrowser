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

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

const state = {
  me: null,
  folders: [],
  currentFolder: "",
  galleryLimit: 5,
  files: [],
};

function shortName(fullName, folder) {
  if (!folder) return fullName;
  const prefix = `${folder}/`;
  if (!fullName.startsWith(prefix)) return fullName;
  return fullName.slice(prefix.length);
}

async function loadMe() {
  const res = await fetch('/api/me');
  if (!res.ok) {
    document.getElementById('user').textContent = 'Not authenticated or not allowed.';
    return;
  }
  state.me = await res.json();
  const roleText = state.me.is_admin ? "Admin" : "User";
  document.getElementById('user').textContent = `Signed in: ${state.me.name} (${state.me.email}) | ${roleText}`;
}

function renderFolderList() {
  const list = document.getElementById('folderList');
  const empty = document.getElementById('folderEmpty');
  list.innerHTML = '';

  if (!state.folders.length) {
    empty.style.display = 'block';
    return;
  }

  empty.style.display = 'none';
  for (const folder of state.folders) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'folder-btn';
    if (folder.name === state.currentFolder) {
      btn.classList.add('active');
    }
    btn.dataset.folder = folder.name;
    btn.textContent = folder.name;
    list.appendChild(btn);
  }
}

function renderUploadFolders() {
  const select = document.getElementById('uploadFolderSelect');
  select.innerHTML = '';

  const uploadable = state.folders.filter((folder) => folder.can_upload);
  for (const folder of uploadable) {
    const option = document.createElement('option');
    option.value = folder.name;
    option.textContent = folder.name;
    if (folder.name === state.currentFolder) {
      option.selected = true;
    }
    select.appendChild(option);
  }

  if (!select.options.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = 'No writable folder';
    select.appendChild(option);
  }
}

async function loadFolders() {
  const res = await fetch('/api/folders');
  if (!res.ok) {
    document.getElementById('count').textContent = 'Failed to load folders.';
    return;
  }

  const data = await res.json();
  state.folders = data.items || [];
  const currentExists = state.folders.some((folder) => folder.name === state.currentFolder);
  if (!currentExists) {
    state.currentFolder = "";
  }
  if (!state.currentFolder && state.folders.length) {
    state.currentFolder = state.folders[0].name;
  }
  renderFolderList();
  renderUploadFolders();
}

function renderGallery() {
  const grid = document.getElementById('imageGrid');
  grid.innerHTML = '';

  const images = state.files
    .filter((item) => item.is_image)
    .sort((a, b) => new Date(b.last_modified || 0) - new Date(a.last_modified || 0))
    .slice(0, state.galleryLimit);

  for (const item of images) {
    const card = document.createElement('div');
    card.className = 'thumb';
    const openHref = `/api/files/${encodePath(item.name)}`;
    const imgSrc = `/api/images/${encodePath(item.name)}`;
    const fileName = escapeHtml(shortName(item.name, state.currentFolder));
    card.innerHTML = `<a href="${openHref}" target="_blank" rel="noopener noreferrer"><img src="${imgSrc}" alt="${fileName}" /></a><div class="name">${fileName}</div>`;
    grid.appendChild(card);
  }
}

async function loadFiles() {
  const prefix = state.currentFolder;
  if (!prefix) {
    document.querySelector('#fileTable tbody').innerHTML = '';
    document.getElementById('imageGrid').innerHTML = '';
    document.getElementById('count').textContent = 'No folder available.';
    return;
  }

  const res = await fetch(`/api/files?prefix=${encodeURIComponent(prefix)}`);
  const tbody = document.querySelector('#fileTable tbody');
  const grid = document.getElementById('imageGrid');
  tbody.innerHTML = '';
  grid.innerHTML = '';

  if (!res.ok) {
    document.getElementById('count').textContent = 'Failed to load files.';
    return;
  }

  const data = await res.json();
  state.files = data.items || [];
  document.getElementById('count').textContent = `${state.currentFolder}: ${data.count} file(s)`;

  for (const item of state.files) {
    const tr = document.createElement('tr');
    const openHref = `/api/files/${encodePath(item.name)}`;
    const fileName = escapeHtml(shortName(item.name, state.currentFolder));
    const contentType = escapeHtml(item.content_type || "");
    const modified = escapeHtml(item.last_modified || "");
    tr.innerHTML = `
      <td>${fileName}</td>
      <td>${formatSize(item.size)}</td>
      <td>${contentType}</td>
      <td>${modified}</td>
      <td><a href="${openHref}" target="_blank" rel="noopener noreferrer">Open</a></td>
    `;
    tbody.appendChild(tr);
  }

  renderGallery();
}

async function uploadFile(event) {
  event.preventDefault();
  const status = document.getElementById('uploadStatus');
  status.textContent = '';

  const fileInput = document.getElementById('fileInput');
  const folderSelect = document.getElementById('uploadFolderSelect');
  if (!fileInput.files.length) {
    status.textContent = 'Choose one or more files first.';
    return;
  }
  if (!folderSelect.value) {
    status.textContent = 'Select a folder first.';
    return;
  }

  const form = new FormData();
  for (const file of fileInput.files) {
    form.append('files', file);
  }
  form.append('folder', folderSelect.value);

  const btn = event.target.querySelector('button[type="submit"]');
  btn.disabled = true;
  status.textContent = 'Uploading...';

  try {
    const res = await fetch('/api/upload', { method: 'POST', body: form });
    const data = await res.json();
    if (!res.ok) {
      status.textContent = data.detail || 'Upload failed';
    } else {
      status.textContent = `Uploaded ${data.count} file(s) to ${folderSelect.value}`;
      fileInput.value = '';
      await loadFiles();
    }
  } catch (err) {
    status.textContent = 'Upload failed due to network error.';
  } finally {
    btn.disabled = false;
  }
}

async function onFolderClick(event) {
  const button = event.target.closest('.folder-btn');
  if (!button) return;

  const folder = button.dataset.folder || '';
  if (!folder || folder === state.currentFolder) return;

  state.currentFolder = folder;
  renderFolderList();
  renderUploadFolders();
  await loadFiles();
}

window.addEventListener('DOMContentLoaded', async () => {
  document.getElementById('uploadForm').addEventListener('submit', uploadFile);
  document.getElementById('folderList').addEventListener('click', onFolderClick);
  document.getElementById('refreshBtn').addEventListener('click', async () => {
    await loadFolders();
    await loadFiles();
  });
  document.getElementById('galleryLimit').addEventListener('change', (event) => {
    state.galleryLimit = Number(event.target.value || 5);
    renderGallery();
  });
  await loadMe();
  await loadFolders();
  await loadFiles();
});
