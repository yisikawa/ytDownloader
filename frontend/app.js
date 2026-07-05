const form = document.getElementById('download-form');
const urlInput = document.getElementById('video-url');
const status = document.getElementById('status');
const result = document.getElementById('result');
const probeArea = document.getElementById('probe-area');
const thumbnail = document.getElementById('thumbnail');
const videoTitle = document.getElementById('video-title');
const formatSelect = document.getElementById('format-select');
const downloadDirInput = document.getElementById('download-dir');
const downloadBtn = document.getElementById('download-btn');
const progressDiv = document.getElementById('progress');
const progressBar = document.getElementById('progress-bar');
const progressText = document.getElementById('progress-text');
const cancelBtn = document.getElementById('cancel-btn');
const historyList = document.getElementById('history-list');

let currentTask = null;

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const url = urlInput.value.trim();
  status.textContent = 'フォーマット取得中...';
  result.innerHTML = '';

  try {
    const res = await fetch('/api/probe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'プローブに失敗しました');

    probeArea.style.display = 'block';
    videoTitle.textContent = data.title || '';
    if (data.thumbnail) {
      thumbnail.src = data.thumbnail;
      thumbnail.style.display = 'block';
    } else {
      thumbnail.style.display = 'none';
    }

    // populate formats
    formatSelect.innerHTML = '';
    const seen = new Set();
    data.formats.forEach(f => {
      // YouTube's higher-quality formats ship video and audio as separate
      // streams; a video-only format must be merged with an audio stream or
      // the resulting file has no sound.
      const needsAudio = f.has_video && !f.has_audio;
      const label = `${f.format_id} — ${f.ext} — ${f.resolution || ''} — ${f.format_note || ''}` +
        (needsAudio ? ' (音声を自動合成)' : '');
      if (!seen.has(f.format_id)) {
        const opt = document.createElement('option');
        opt.value = needsAudio ? `${f.format_id}+bestaudio` : f.format_id;
        opt.textContent = label;
        // Forces the merged output into the same container shown in the label
        // (e.g. "mp4"), instead of yt-dlp's own choice of best-fitting container
        // for the codec combination, which can differ (e.g. webm/mkv).
        opt.dataset.ext = f.ext;
        formatSelect.appendChild(opt);
        seen.add(f.format_id);
      }
    });

    status.textContent = 'フォーマットを選択してください';
  } catch (err) {
    status.textContent = 'エラー';
    result.textContent = err.message || String(err);
  }
});

downloadBtn.addEventListener('click', async () => {
  const url = urlInput.value.trim();
  const format_id = formatSelect.value || null;
  const download_dir = downloadDirInput.value.trim() || null;
  const selectedOption = formatSelect.selectedOptions[0];
  const merge_output_format = (selectedOption && selectedOption.dataset.ext) || null;
  status.textContent = 'ダウンロードを開始します...';
  result.innerHTML = '';

  try {
    const res = await fetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, format_id, download_dir, merge_output_format }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '開始に失敗しました');

    currentTask = data.task_id;
    progressDiv.style.display = 'block';
    cancelBtn.disabled = false;
    pollStatus(currentTask);
  } catch (err) {
    status.textContent = 'エラー';
    result.textContent = err.message || String(err);
  }
});

cancelBtn.addEventListener('click', async () => {
  if (!currentTask) return;
  try {
    await fetch(`/api/cancel/${currentTask}`, { method: 'POST' });
    status.textContent = 'キャンセルしています...';
  } catch (err) {
    status.textContent = 'キャンセルに失敗しました';
  }
});

async function pollStatus(taskId) {
  try {
    const res = await fetch(`/api/status/${taskId}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'ステータス取得エラー');

    if (data.status === 'queued') {
      status.textContent = 'キューに登録されました';
    } else if (data.status === 'downloading') {
      status.textContent = 'ダウンロード中...';
      const downloaded = data.downloaded_bytes || 0;
      const total = data.total_bytes || 0;
      if (total) {
        const pct = Math.floor((downloaded / total) * 100);
        progressBar.value = pct;
        progressText.textContent = `${pct}% (${formatBytes(downloaded)} / ${formatBytes(total)})`;
      } else {
        progressText.textContent = `${formatBytes(downloaded)} downloaded`;
      }
    } else if (data.status === 'finished' || data.status === 'completed') {
      status.textContent = 'ダウンロード完了';
      progressBar.value = 100;
      progressText.textContent = '100%';
      cancelBtn.disabled = true;
      result.innerHTML = `<p><strong>${data.title || ''}</strong></p>` +
        `<p>保存先: ${data.download_dir || ''}</p>` +
        `<a href="/api/files/task/${taskId}" target="_blank" rel="noopener noreferrer">ファイルを開く</a>`;
      loadHistory();
      return;
    } else if (data.status === 'canceled') {
      status.textContent = 'キャンセルしました';
      cancelBtn.disabled = true;
      loadHistory();
      return;
    } else if (data.status === 'error') {
      status.textContent = 'エラー';
      result.textContent = data.error || '不明なエラー';
      cancelBtn.disabled = true;
      loadHistory();
      return;
    }

    setTimeout(() => pollStatus(taskId), 1000);
  } catch (err) {
    status.textContent = 'ステータス取得エラー';
    result.textContent = err.message || String(err);
  }
}

async function loadHistory() {
  try {
    const res = await fetch('/api/history');
    const data = await res.json();
    if (!res.ok) return;

    historyList.innerHTML = '';
    data.forEach(entry => {
      const li = document.createElement('li');
      if (entry.status === 'completed' && entry.file_path) {
        const dir = entry.download_dir ? ` (${entry.download_dir})` : '';
        li.innerHTML = `<a href="/api/files/task/${entry.task_id}" target="_blank" rel="noopener noreferrer">${entry.title || entry.filename}</a>${dir}`;
      } else if (entry.status === 'error') {
        li.textContent = `${entry.url} — エラー: ${entry.error || '不明'}`;
      } else {
        li.textContent = `${entry.url} — ${entry.status}`;
      }
      historyList.appendChild(li);
    });
  } catch (err) {
    // history is best-effort; ignore failures
  }
}

loadHistory();

function formatBytes(bytes) {
  if (!bytes) return '0 B';
  const sizes = ['B','KB','MB','GB','TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return (bytes / Math.pow(1024, i)).toFixed(2) + ' ' + sizes[i];
}
