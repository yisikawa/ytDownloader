# ytDownloader 修正計画書

作成日: 2026-07-08
対象コミット: 0129460(master)+ 未コミット変更(web_embedded player_client 追加)

コードレビューで検出した問題の修正計画。上から順(重要度順)に実装すること。
各項目は独立して実装・コミット可能。**1項目 = 1コミット**を推奨。

実装後は `backend` ディレクトリで既存テストを実行して回帰がないことを確認する:

```
cd backend
.venv\Scripts\python -m unittest tests.test_main -v
```

---

## 修正1: XSS脆弱性 — 動画タイトル等を innerHTML に未エスケープで挿入【重要度: 高】

**対象:** `frontend/app.js`

**問題:**
動画タイトル(`data.title` / `entry.title`)は動画投稿者が自由に決められる第三者由来の値だが、
これをテンプレートリテラルで `innerHTML` に埋め込んでいる。タイトルに
`<img src=x onerror=alert(1)>` のようなHTMLが含まれると任意スクリプトが実行される。

該当箇所:
- `app.js` 221〜223行付近(`pollStatus` の完了時): `result.innerHTML = ...` に
  `data.title` と `data.download_dir` を未エスケープで埋め込んでいる
- `app.js` 257行付近(`loadHistory`): `li.innerHTML = ...` に `entry.title` /
  `entry.filename` / `entry.download_dir` を未エスケープで埋め込んでいる

**修正方針:**
`innerHTML` へのテンプレートリテラル埋め込みをやめ、`document.createElement` +
`textContent` でDOMを組み立てる。例(完了時の結果表示):

```js
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
```

`loadHistory` 内も同様にDOM組み立てに変更する。`taskId` / `entry.task_id` はサーバーが
生成するUUIDなのでURL部分への埋め込みは安全だが、表示テキストはすべて `textContent` を使う。

**検証:** タイトルに `<img src=x onerror=alert(1)>` を含むダウンロード完了タスクを
`downloads.DOWNLOADS` に手動投入した状態で履歴表示し、alertが発火しないこと。
(手動確認が難しければ、コードレビューで innerHTML への変数埋め込みが残っていないことを確認)

---

## 修正2: キャンセル時の KeyError 競合【重要度: 高】

**対象:** `backend/app/downloads.py` の `cancel_download`(208〜214行付近)

**問題:**
`cancel_download` はロック内でステータスを確認した後、**ロック外**で
`_cancel_events[task_id].set()` を辞書アクセスで呼ぶ。一方ワーカー
`_download_worker` の `finally`(163行付近)は `_cancel_events.pop(task_id, None)` を実行する。
ステータス確認と `.set()` の間にダウンロードが終了するとイベントがpop済みで
`KeyError` になり、`/api/cancel/{task_id}` が500を返す。

**修正方針:**
ロック内でイベントを取得し、None チェックしてから set する:

```python
def cancel_download(task_id: str) -> bool:
    with _lock:
        status = DOWNLOADS.get(task_id)
        if status is None or status.get("status") in TERMINAL_STATUSES:
            return False
        event = _cancel_events.get(task_id)
    if event is None:
        return False
    event.set()
    return True
```

**検証:** 既存テスト `test_cancel_already_finished_task_returns_409` が通ること。
追加テスト: `DOWNLOADS` にアクティブなタスクを登録し `_cancel_events` には登録しない状態で
`cancel_download` を呼び、例外にならず `False` が返ること。

---

## 修正3: 同時ダウンロード数制限のチェックと登録が非アトミック【重要度: 中】

**対象:** `backend/app/downloads.py` の `start_download`(166〜199行付近)

**問題:**
`_active_download_count()` のチェック(176行)とタスク登録(184〜191行)が別々のロック区間で
行われるため、同時リクエストが両方ともチェックを通過し `MAX_CONCURRENT_DOWNLOADS` を超えられる。

**修正方針:**
カウント・チェック・登録を単一の `with _lock:` ブロックにまとめる。
`_resolve_download_dir`(ディスクI/Oあり)はロックの外で先に実行してよい:

```python
def start_download(...) -> str:
    _cleanup_stale_tasks()
    output_dir = _resolve_download_dir(download_dir)

    task_id = str(uuid.uuid4())
    with _lock:
        active = sum(1 for s in DOWNLOADS.values() if s.get("status") in ACTIVE_STATUSES)
        if active >= MAX_CONCURRENT_DOWNLOADS:
            raise TooManyDownloadsError(...)
        DOWNLOADS[task_id] = {...}
        _cancel_events[task_id] = threading.Event()

    thread = threading.Thread(...)
    thread.start()
    return task_id
```

`_active_download_count()` が他から使われなくなる場合は削除してよい。

**検証:** 既存テスト `test_start_download_rejects_when_over_concurrency_limit` が通ること。

---

## 修正4: ステータスポーリングが一度の通信エラーで恒久停止【重要度: 中】

**対象:** `frontend/app.js` の `pollStatus`(197〜244行付近)

**問題:**
ポーリング中に一時的なネットワークエラー(fetch失敗)が起きると catch に落ちて
`setTimeout` による次回ポーリングが予約されず、サーバー側ではダウンロード継続中なのに
UI更新が二度と再開しない。

**修正方針:**
連続失敗回数をカウントし、上限(例: 5回)までは再スケジュールする。
成功したらカウンタをリセット。ただしHTTP 404(タスク不明)は即時終了でよい:

```js
async function pollStatus(taskId, failCount = 0) {
  try {
    const res = await fetch(`/api/status/${taskId}`);
    if (res.status === 404) { /* タスク不明 → エラー表示して終了 */ }
    ...
    setTimeout(() => pollStatus(taskId), 1000);   // 成功時は failCount リセット
  } catch (err) {
    if (failCount < 5) {
      setTimeout(() => pollStatus(taskId, failCount + 1), 2000);
      return;
    }
    status.textContent = 'ステータス取得エラー';
    ...
  }
}
```

**検証:** 目視でロジック確認(フロントに自動テストなし)。ダウンロード中に一時的に
サーバーを止めて再起動し、ポーリングが復帰することを確認できればなお良い。

---

## 修正5: 2回目のダウンロード開始時にプログレスバーが未リセット【重要度: 低】

**対象:** `frontend/app.js` の `downloadBtn` クリックハンドラ(177〜180行付近)

**問題:**
2回目のダウンロード開始時、最初のステータス取得までの間、前回の「100%」表示が残る。

**修正方針:**
`currentTask = data.task_id;` の直後(progressDiv 表示前)に追加:

```js
progressBar.value = 0;
progressText.textContent = '';
```

---

## 修正6: 音声のみフォーマット + 字幕選択の組み合わせでタスク全体がエラー【重要度: 低】

**対象:** `frontend/app.js`(必須)+ `backend/app/downloads.py`(任意の防御)

**問題:**
フォーマット一覧には音声のみ(m4a等)の項目も含まれる。音声のみを選んだ状態で字幕を選ぶと、
バックエンドで `FFmpegEmbedSubtitle` が音声コンテナへの埋め込みに失敗し、ダウンロード
自体は成功しているのにタスクがエラーになる。

**修正方針(フロント側):**
- probe時に各フォーマットの `has_video` を `opt.dataset.hasVideo` として保持する
- `formatSelect` の change 時(既存の `updateAudioTrackVisibility` を拡張、または関数追加)に、
  選択フォーマットが `has_video === false` なら `subtitleTrackArea` を非表示にし、
  `subtitleSelect.value = ''` にリセットする
- 表示条件: 「字幕が1件以上存在」かつ「選択フォーマットに映像がある」

**修正方針(バックエンド側・任意):**
`_download_worker` で字幕埋め込みを行う前提条件として、映像なしのダウンロードでは
字幕オプションを無視する実装は情報が取れないため難しい。フロント側修正を主とし、
バックエンドは変更不要でよい。

---

## 修正7: キャンセル後に中間ファイル(.part / .fXXX)が残る【重要度: 低】

**対象:** `backend/app/downloads.py` の `_download_worker`

**問題:**
キャンセルは進捗フック内の例外で行われるため、キャンセル時点でダウンロード済みの
`*.part` や `*.fXXX.*`(映像・音声の個別ストリーム)ファイルが保存先に残る。

**修正方針:**
`except DownloadCancelledByUser:` ブロック内で、この動画IDに対応する中間ファイルを削除する。
確実な対応付けのため、`ydl_opts["outtmpl"]` に含まれるタイトルではなく、
事前に `outtmpl` を `%(title).150B [%(id)s].%(ext)s` のようにID入りへ変更するか、
もしくはシンプルに `output_dir` 内の `*.part` を削除する方法でもよい
(同時ダウンロード中の他タスクの .part を消さないよう注意。ID入りouttmplの方が安全)。

**注意:** outtmpl を変更する場合、`routes.py` の `/api/files/{filename}` やファイル名表示には
影響しない(ファイル名は yt-dlp の戻り値から取得している)が、README のファイル名説明が
あれば更新すること。ファイル名仕様の変更が望ましくない場合はこの修正をスキップしてよい。

---

## 修正8: probe 失敗が一律 500【重要度: 低】

**対象:** `backend/app/routes.py` の `probe`(60〜64行付近)

**問題:**
存在しない動画ID・削除済み動画などユーザー入力起因のエラーもすべて500になる。

**修正方針:**
`yt_dlp.utils.DownloadError`(抽出失敗の代表例外)を捕捉して **422** を返し、
それ以外の予期しない例外は現状どおり500のままとする:

```python
try:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(payload.url, download=False)
except yt_dlp.utils.DownloadError as exc:
    raise HTTPException(status_code=422, detail=f"Probe failed: {exc}") from exc
except Exception as exc:
    raise HTTPException(status_code=500, detail=f"Probe failed: {exc}") from exc
```

**注意:** 既存テスト `test_probe_failure_returns_500` は `RuntimeError` を投げているので
そのまま通るはず。`DownloadError` → 422 のテストを1本追加すること。

---

## 修正9: .gitattributes 追加(改行コード警告の解消)【重要度: 低】

**対象:** リポジトリルートに `.gitattributes` を新規作成

**問題:**
Windows環境で `git diff` のたびに「LF will be replaced by CRLF」警告が出る。

**修正方針:**

```
* text=auto
*.py text eol=lf
*.js text eol=lf
*.css text eol=lf
*.html text eol=lf
*.md text eol=lf
```

追加後、`git add --renormalize .` を実行して正規化する。

---

## 修正対象外(認識済み・対応不要)

- **`download_dir` による任意パス書き込み**: ローカル個人用ツールとして意図した仕様。
  ただし uvicorn は必ず `127.0.0.1` にバインドして起動すること(認証なしのため
  LAN公開は不可)。README に起動コマンドの注意書きがなければ追記してもよい。
- **同名タイトルの上書き**: `downloads.py` のコメントに記載済みの既知の仕様。
- **マージ・後処理中はキャンセル不可**: yt-dlp の構造上、進捗フックが呼ばれない
  後処理フェーズでの中断は困難。現状維持とする。
- **キャンセル例外の伝播方式**: 進捗フックから独自例外 `DownloadCancelledByUser` を
  投げる方式は、yt-dlp の `_handle_extraction_exceptions` が `ignoreerrors` 未設定時に
  素の例外を再送出することを確認済みのため、現状のままで正しく動作する。

## 実装時の共通注意

- コミットは日本語メッセージ(既存コミットの慣例に合わせる)
- `backend/.venv` は触らない(gitignore済み)
- 各修正後に `python -m unittest tests.test_main -v` で全テストが通ることを確認
- フロントエンドの変更(修正1, 4, 5, 6)は自動テストがないため、可能なら実際にサーバーを
  起動して動作確認する: `cd backend && .venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 8000`
