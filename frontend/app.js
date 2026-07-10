const form = document.getElementById('download-form');
const urlInput = document.getElementById('video-url');
const status = document.getElementById('status');
const result = document.getElementById('result');
const probeArea = document.getElementById('probe-area');
const thumbnail = document.getElementById('thumbnail');
const videoTitle = document.getElementById('video-title');
const formatSelect = document.getElementById('format-select');
const audioTrackArea = document.getElementById('audio-track-area');
const audioSelect = document.getElementById('audio-select');
const subtitleTrackArea = document.getElementById('subtitle-track-area');
const subtitleSelect = document.getElementById('subtitle-select');
const downloadDirInput = document.getElementById('download-dir');
const downloadBtn = document.getElementById('download-btn');
const progressDiv = document.getElementById('progress');
const progressBar = document.getElementById('progress-bar');
const progressText = document.getElementById('progress-text');
const cancelBtn = document.getElementById('cancel-btn');
const historyList = document.getElementById('history-list');

let currentTask = null;

const LANGUAGE_NAMES = (() => {
  try {
    return new Intl.DisplayNames(['ja'], { type: 'language' });
  } catch (err) {
    return null;
  }
})();

function describeAudioTrack(f, lang) {
  if (lang !== 'und' && LANGUAGE_NAMES) {
    try {
      const name = LANGUAGE_NAMES.of(lang);
      if (name) return `${name} (${lang})`;
    } catch (err) {
      // unknown/invalid language tag; fall through to other labels
    }
  }
  return f.format_note || (lang !== 'und' ? lang : '既定の音声');
}

function updateAudioTrackVisibility() {
  const selected = formatSelect.selectedOptions[0];
  const needsAudio = selected && selected.dataset.needsAudio === 'true';
  const hasAudioOptions = audioSelect.options.length > 0;
  audioTrackArea.style.display = needsAudio && hasAudioOptions ? 'block' : 'none';
}

formatSelect.addEventListener('change', updateAudioTrackVisibility);

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
        (needsAudio ? ' (音声を別途選択/自動合成)' : '');
      if (!seen.has(f.format_id)) {
        const opt = document.createElement('option');
        opt.value = f.format_id;
        opt.textContent = label;
        opt.dataset.needsAudio = needsAudio ? 'true' : 'false';
        // Forces the merged output into the same container shown in the label
        // (e.g. "mp4"), instead of yt-dlp's own choice of best-fitting container
        // for the codec combination, which can differ (e.g. webm/mkv).
        opt.dataset.ext = f.ext;
        formatSelect.appendChild(opt);
        seen.add(f.format_id);
      }
    });

    // populate audio tracks: a dubbed video ships one audio-only stream per
    // language, so users can pick which language to merge with the video.
    audioSelect.innerHTML = '';
    const bestPerLanguage = new Map();
    data.formats
      .filter(f => f.has_audio && !f.has_video)
      .forEach(f => {
        const key = f.language || 'und';
        const current = bestPerLanguage.get(key);
        if (!current || (f.tbr || 0) > (current.tbr || 0)) {
          bestPerLanguage.set(key, f);
        }
      });
    bestPerLanguage.forEach((f, lang) => {
      const opt = document.createElement('option');
      opt.value = f.format_id;
      opt.textContent = describeAudioTrack(f, lang);
      audioSelect.appendChild(opt);
    });

    updateAudioTrackVisibility();

    // populate subtitles: embedded as a selectable track in the output file,
    // not burned into the video image.
    subtitleSelect.innerHTML = '';
    const noneOpt = document.createElement('option');
    noneOpt.value = '';
    noneOpt.textContent = 'なし';
    subtitleSelect.appendChild(noneOpt);
    (data.subtitles || []).forEach(sub => {
      const opt = document.createElement('option');
      opt.value = `${sub.auto ? 'auto' : 'manual'}:${sub.lang}`;
      opt.textContent = sub.name + (sub.auto ? ' (自動生成)' : '');
      subtitleSelect.appendChild(opt);
    });
    subtitleTrackArea.style.display = (data.subtitles || []).length > 0 ? 'block' : 'none';

    status.textContent = 'フォーマットを選択してください';
  } catch (err) {
    status.textContent = 'エラー';
    result.textContent = err.message || String(err);
  }
});

downloadBtn.addEventListener('click', async () => {
  const url = urlInput.value.trim();
  const download_dir = downloadDirInput.value.trim() || null;
  const selectedOption = formatSelect.selectedOptions[0];
  const merge_output_format = (selectedOption && selectedOption.dataset.ext) || null;

  let format_id = formatSelect.value || null;
  const needsAudio = selectedOption && selectedOption.dataset.needsAudio === 'true';
  if (needsAudio) {
    const audioFormatId = audioTrackArea.style.display !== 'none' && audioSelect.value;
    format_id = `${format_id}+${audioFormatId || 'bestaudio'}`;
  }

  let subtitle_lang = null;
  let subtitle_auto = false;
  if (subtitleSelect.value) {
    const [kind, lang] = subtitleSelect.value.split(':');
    subtitle_lang = lang;
    subtitle_auto = kind === 'auto';
  }

  status.textContent = 'ダウンロードを開始します...';
  result.innerHTML = '';

  try {
    const res = await fetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, format_id, download_dir, merge_output_format, subtitle_lang, subtitle_auto }),
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

async function pollStatus(taskId, failCount = 0) {
  try {
    const res = await fetch(`/api/status/${taskId}`);

    // Handle 404 immediately - task not found, don't retry
    if (res.status === 404) {
      status.textContent = 'エラー';
      result.textContent = 'タスクが見つかりません';
      cancelBtn.disabled = true;
      return;
    }

    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'ステータス取得エラー');

    // Success - process status update and reschedule with failCount reset
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
      result.innerHTML = '';
      const titleP = document.createElement('p');
      const strong = document.createElement('strong');
      strong.textContent = data.title || '';
      titleP.appendChild(strong);
      const dirP = document.createElement('p');
      dirP.textContent = `保存先: ${data.download_dir || ''}`;
      const link = document.createElement('a');
      link.href = `/api/files/task/${taskId}`;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = 'ファイルを開く';
      result.append(titleP, dirP, link);
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
    // Network error or HTTP error (not 404)
    if (failCount < 5) {
      // Retry with exponential backoff (2 seconds)
      setTimeout(() => pollStatus(taskId, failCount + 1), 2000);
      return;
    }
    // Max retries exceeded - show error and stop polling
    status.textContent = 'ステータス取得エラー';
    result.textContent = 'ネットワークエラー: 最大再試行回数に達しました';
    cancelBtn.disabled = true;
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
        const link = document.createElement('a');
        link.href = `/api/files/task/${entry.task_id}`;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = entry.title || entry.filename || '';
        li.appendChild(link);
        if (entry.download_dir) {
          li.appendChild(document.createTextNode(` (${entry.download_dir})`));
        }
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
