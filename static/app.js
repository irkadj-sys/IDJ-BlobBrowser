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

function normalizePath(path) {
  return String(path || "").replaceAll("\\", "/").replace(/^\/+|\/+$/g, "");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

const DEFAULT_CHUNK_SIZE_BYTES = 8 * 1024 * 1024;

const state = {
  me: null,
  folders: [],
  currentFolder: "",
  galleryLimit: 5,
  files: [],
  dragBlobPath: "",
  maxUploadMb: 2048,
  uploadChunkBytes: DEFAULT_CHUNK_SIZE_BYTES,
  uploadConcurrency: 4,
};

function shortName(fullName, folder) {
  if (!folder) return fullName;
  const prefix = `${folder}/`;
  if (!fullName.startsWith(prefix)) return fullName;
  return fullName.slice(prefix.length);
}

function folderLabel(folder) {
  return folder.label || folder.name;
}

function folderDepth(folder) {
  return Number(folder.depth || 0);
}

function folderByName(name) {
  return state.folders.find((folder) => folder.name === name);
}

function clearDropTargets() {
  for (const btn of document.querySelectorAll('.folder-btn.drop-target')) {
    btn.classList.remove('drop-target');
  }
}

function setMoveStatus(message, isError = false) {
  const el = document.getElementById('moveStatus');
  if (!el) return;
  el.textContent = message;
  el.style.color = isError ? '#b91c1c' : '';
}

function setCreateStatus(message, isError = false) {
  const el = document.getElementById('createFolderStatus');
  if (!el) return;
  el.textContent = message;
  el.style.color = isError ? '#b91c1c' : '';
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

async function loadConfig() {
  const res = await fetch('/api/config');
  if (!res.ok) return;
  const cfg = await res.json();
  const maxUploadMb = Number(cfg.max_upload_mb);
  const uploadChunkMb = Number(cfg.upload_chunk_mb);
  const uploadConcurrency = Number(cfg.upload_concurrency);
  if (Number.isFinite(maxUploadMb)) {
    state.maxUploadMb = maxUploadMb;
  }
  if (Number.isFinite(uploadChunkMb) && uploadChunkMb > 0) {
    state.uploadChunkBytes = uploadChunkMb * 1024 * 1024;
  }
  if (Number.isFinite(uploadConcurrency) && uploadConcurrency > 0) {
    state.uploadConcurrency = uploadConcurrency;
  }
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
    btn.style.paddingLeft = `${10 + folderDepth(folder) * 18}px`;
    btn.dataset.folder = folder.name;
    btn.title = folder.name;
    btn.textContent = folderLabel(folder);
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
    option.textContent = folderLabel(folder);
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

function refreshCreateFolderControls() {
  const input = document.getElementById('newFolderName');
  const btn = document.getElementById('createFolderBtn');
  const hint = document.getElementById('createFolderHint');
  const selected = folderByName(state.currentFolder);
  const canCreate = Boolean(selected && selected.can_create_subfolder);

  input.disabled = !canCreate;
  btn.disabled = !canCreate;
  if (!selected) {
    hint.textContent = 'Select a folder to create a subfolder.';
    return;
  }
  if (canCreate) {
    hint.textContent = `Create a subfolder under: ${selected.name}`;
  } else if (state.me?.is_admin) {
    hint.textContent = `Folder selected: ${selected.name}`;
  } else {
    hint.textContent = 'You can create folders only inside your own account folder.';
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
  refreshCreateFolderControls();
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

  const url = `/api/files?prefix=${encodeURIComponent(prefix)}&direct_only=true`;
  const res = await fetch(url);
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
  const currentFolderLabel = state.folders.find((f) => f.name === state.currentFolder)?.label || state.currentFolder;
  document.getElementById('count').textContent = `${currentFolderLabel}: ${data.count} file(s)`;

  for (const item of state.files) {
    const tr = document.createElement('tr');
    tr.className = 'file-row';
    tr.draggable = true;
    tr.dataset.blobPath = item.name;
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

  const btn = event.target.querySelector('button[type="submit"]');
  btn.disabled = true;
  status.textContent = 'Uploading...';

  try {
    const files = Array.from(fileInput.files);
    const maxBytes = state.maxUploadMb > 0 ? state.maxUploadMb * 1024 * 1024 : 0;
    for (const file of files) {
      if (maxBytes > 0 && file.size > maxBytes) {
        throw new Error(`${file.name}: File too large. Max is ${state.maxUploadMb} MB`);
      }
    }

    let completed = 0;
    for (const file of files) {
      status.textContent = `Uploading ${file.name} (${completed + 1}/${files.length})...`;
      await uploadSingleFileHighPerformance(file, folderSelect.value, status);
      completed += 1;
    }
    status.textContent = `Uploaded ${completed} file(s) to ${folderSelect.value}`;
    fileInput.value = '';
    await loadFiles();
  } catch (err) {
    status.textContent = err?.message || 'Upload failed due to network error.';
  } finally {
    btn.disabled = false;
  }
}

async function requestUploadSas(file, folder) {
  const form = new FormData();
  form.append('folder', folder);
  form.append('original_name', file.name);
  form.append('file_size', String(file.size));

  const res = await fetch('/api/upload/sas', { method: 'POST', body: form });
  let data = null;
  try {
    data = await res.json();
  } catch (e) {
    data = null;
  }
  if (!res.ok) {
    throw new Error(data?.detail || `Failed to create upload URL for ${file.name}`);
  }
  return data;
}

async function uploadSingleFileHighPerformance(file, folder, statusEl) {
  const sasInfo = await requestUploadSas(file, folder);
  if (window.azblob?.BlockBlobClient) {
    const blockBlobClient = new window.azblob.BlockBlobClient(sasInfo.upload_url);
    await blockBlobClient.uploadBrowserData(file, {
      blockSize: state.uploadChunkBytes,
      maxSingleShotSize: state.uploadChunkBytes,
      concurrency: state.uploadConcurrency,
      blobHTTPHeaders: { blobContentType: file.type || 'application/octet-stream' },
      onProgress: (ev) => {
        const loaded = Number(ev.loadedBytes || 0);
        const pct = file.size > 0 ? Math.floor((loaded / file.size) * 100) : 100;
        statusEl.textContent = `Uploading ${file.name}: ${pct}%`;
      },
    });
    return;
  }

  // Fallback path if azblob SDK fails to load in browser.
  await uploadSingleFileInChunks(file, folder, statusEl);
}

async function uploadSingleFileInChunks(file, folder, statusEl) {
  const chunkSize = state.uploadChunkBytes || DEFAULT_CHUNK_SIZE_BYTES;
  const totalChunks = Math.max(1, Math.ceil(file.size / chunkSize));
  const contentType = file.type || 'application/octet-stream';

  for (let chunkIndex = 0; chunkIndex < totalChunks; chunkIndex += 1) {
    const start = chunkIndex * chunkSize;
    const end = Math.min(start + chunkSize, file.size);
    const chunkBlob = file.slice(start, end);

    const form = new FormData();
    form.append('file', chunkBlob, file.name);
    form.append('folder', folder);
    form.append('original_name', file.name);
    form.append('chunk_index', String(chunkIndex));
    form.append('total_chunks', String(totalChunks));
    form.append('file_size', String(file.size));

    const res = await fetch('/api/upload/chunked', { method: 'POST', body: form });
    let data = null;
    try {
      data = await res.json();
    } catch (e) {
      data = null;
    }
    if (!res.ok) {
      throw new Error(data?.detail || `Upload failed for ${file.name}`);
    }
    statusEl.textContent = `Uploading ${file.name}: chunk ${chunkIndex + 1}/${totalChunks}`;
  }

  const completeForm = new FormData();
  completeForm.append('folder', folder);
  completeForm.append('original_name', file.name);
  completeForm.append('total_chunks', String(totalChunks));
  completeForm.append('file_size', String(file.size));
  completeForm.append('content_type', contentType);

  const completeRes = await fetch('/api/upload/chunked/complete', { method: 'POST', body: completeForm });
  let completeData = null;
  try {
    completeData = await completeRes.json();
  } catch (e) {
    completeData = null;
  }
  if (!completeRes.ok) {
    throw new Error(completeData?.detail || `Finalize failed for ${file.name}`);
  }
}

async function createFolder(event) {
  event.preventDefault();
  const input = document.getElementById('newFolderName');
  const folderName = input.value.trim();
  const parentFolder = state.currentFolder;

  setCreateStatus('');
  if (!parentFolder) {
    setCreateStatus('Select a parent folder first.', true);
    return;
  }
  if (!folderName) {
    setCreateStatus('Enter a folder name.', true);
    return;
  }

  const form = new FormData();
  form.append('parent_folder', parentFolder);
  form.append('folder_name', folderName);

  try {
    const res = await fetch('/api/folders/create', { method: 'POST', body: form });
    const data = await res.json();
    if (!res.ok) {
      setCreateStatus(data.detail || 'Folder creation failed.', true);
      return;
    }
    input.value = '';
    state.currentFolder = data.folder_path;
    await loadFolders();
    await loadFiles();
    setCreateStatus(`Created folder: ${data.folder_path}`);
  } catch (err) {
    setCreateStatus('Folder creation failed due to network error.', true);
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
  refreshCreateFolderControls();
  setCreateStatus('');
  await loadFiles();
}

function onFileDragStart(event) {
  const row = event.target.closest('tr[data-blob-path]');
  if (!row) return;
  state.dragBlobPath = row.dataset.blobPath || "";
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('text/plain', state.dragBlobPath);
  row.classList.add('dragging');
  setMoveStatus('');
}

function onFileDragEnd(event) {
  const row = event.target.closest('tr[data-blob-path]');
  if (row) {
    row.classList.remove('dragging');
  }
  clearDropTargets();
}

function onFolderDragOver(event) {
  const button = event.target.closest('.folder-btn');
  if (!button) return;
  const folder = button.dataset.folder || "";
  const folderNode = folderByName(folder);
  if (!folder || !state.dragBlobPath || !folderNode || !folderNode.can_upload) return;

  event.preventDefault();
  event.dataTransfer.dropEffect = 'move';
  clearDropTargets();
  button.classList.add('drop-target');
}

function onFolderDragLeave(event) {
  const button = event.target.closest('.folder-btn');
  if (!button) return;
  button.classList.remove('drop-target');
}

async function moveBlob(sourcePath, targetFolder) {
  const normalizedSource = normalizePath(sourcePath);
  const sourceParent = normalizedSource.includes('/') ? normalizedSource.substring(0, normalizedSource.lastIndexOf('/')) : "";
  if (!normalizedSource || !targetFolder) return;
  if (normalizePath(sourceParent) === normalizePath(targetFolder)) {
    setMoveStatus(`File is already in ${targetFolder}.`);
    return;
  }

  setMoveStatus(`Moving file to ${targetFolder}...`);
  const form = new FormData();
  form.append('source_path', normalizedSource);
  form.append('target_folder', targetFolder);

  try {
    const res = await fetch('/api/move', { method: 'POST', body: form });
    const data = await res.json();
    if (!res.ok) {
      setMoveStatus(data.detail || 'Move failed.', true);
      return;
    }
    setMoveStatus(`Moved to ${targetFolder}.`);
    await loadFolders();
    await loadFiles();
  } catch (err) {
    setMoveStatus('Move failed due to network error.', true);
  }
}

async function onFolderDrop(event) {
  const button = event.target.closest('.folder-btn');
  if (!button) return;
  const targetFolder = button.dataset.folder || "";
  const folderNode = folderByName(targetFolder);
  if (!targetFolder || !folderNode || !folderNode.can_upload) return;

  event.preventDefault();
  clearDropTargets();
  const sourcePath = event.dataTransfer.getData('text/plain') || state.dragBlobPath;
  await moveBlob(sourcePath, targetFolder);
}

window.addEventListener('DOMContentLoaded', async () => {
  document.getElementById('uploadForm').addEventListener('submit', uploadFile);
  document.getElementById('createFolderForm').addEventListener('submit', createFolder);
  document.getElementById('folderList').addEventListener('click', onFolderClick);
  document.querySelector('#fileTable tbody').addEventListener('dragstart', onFileDragStart);
  document.querySelector('#fileTable tbody').addEventListener('dragend', onFileDragEnd);
  document.getElementById('folderList').addEventListener('dragover', onFolderDragOver);
  document.getElementById('folderList').addEventListener('dragleave', onFolderDragLeave);
  document.getElementById('folderList').addEventListener('drop', onFolderDrop);
  document.getElementById('refreshBtn').addEventListener('click', async () => {
    await loadFolders();
    await loadFiles();
  });
  document.getElementById('galleryLimit').addEventListener('change', (event) => {
    state.galleryLimit = Number(event.target.value || 5);
    renderGallery();
  });
  await loadMe();
  await loadConfig();
  await loadFolders();
  await loadFiles();
});
