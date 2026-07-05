# ytDownloader

yt-dlp を使って YouTube 動画をダウンロードできる、バックエンド/フロントエンド分離のサンプルアプリです。

## 構成

- Backend: FastAPI
  - `backend/app/main.py`: アプリ生成・静的ファイル配信のみを担当するエントリポイント
  - `backend/app/routes.py`: APIエンドポイント定義
  - `backend/app/downloads.py`: ダウンロード状態管理(同時実行数制限・TTLクリーンアップ・キャンセル処理)
  - `backend/app/schemas.py`: リクエストのPydanticモデル
- Frontend: HTML / CSS / JavaScript
- Download engine: yt-dlp

## 必要環境

- Python 3.11
- Windows PowerShell

## セットアップ

1. プロジェクトルートへ移動します。

```powershell
cd D:\LLMProjects\ytDownloader
```

2. バックエンド用の仮想環境を作成します。

```powershell
py -3.11 -m venv backend\.venv
```

3. 仮想環境を有効化します。

```powershell
.\backend\.venv\Scripts\Activate.ps1
```

4. 依存関係をインストールします。

```powershell
python -m pip install --upgrade pip
python -m pip install -r backend\requirements.txt
```

## 起動方法

1. 仮想環境を有効化します。

```powershell
cd D:\LLMProjects\ytDownloader
.\backend\.venv\Scripts\Activate.ps1
```

2. バックエンドサーバーを起動します。

```powershell
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8001
```

3. ブラウザで次の URL を開きます。

```text
http://127.0.0.1:8001/
```

4. 入力欄に YouTube の動画 URL を入力し、「フォーマット取得」を押します。
5. フォーマットを選択して「ダウンロード開始」を押すと、進捗が表示されます。ダウンロード中は「キャンセル」ボタンで中断できます。
6. 完了したダウンロードは「ダウンロード履歴」に表示され、いつでもファイルを開けます。

## API

| Method | Path | 説明 |
| --- | --- | --- |
| GET | `/api/health` | ヘルスチェック |
| POST | `/api/probe` | URLから動画情報・フォーマット一覧を取得(プレイリスト/ラジオURLでも単一動画のみ対象) |
| POST | `/api/download` | ダウンロードを開始しタスクIDを返す(同時実行数の上限は3件、超過時は429) |
| POST | `/api/cancel/{task_id}` | 実行中のダウンロードをキャンセル |
| GET | `/api/status/{task_id}` | ダウンロードの進捗・状態を取得 |
| GET | `/api/history` | 完了・失敗・キャンセル済みタスクの履歴一覧(直近50件) |
| GET | `/api/files/{filename}` | `backend/downloads` 配下のファイルを取得 |

## テスト

```powershell
python -m unittest discover -s backend\tests -p "test_*.py"
```

## 備考

- ダウンロードされたファイルは backend/downloads 配下に保存されます。
- YouTube 側の制限やネットワーク状況により、ダウンロードが失敗する場合があります。
- 完了・失敗・キャンセル済みのタスク情報は1時間後に自動的に破棄されます(ファイル自体は削除されません)。
- 同時に実行できるダウンロードは最大3件です。上限を超えるとリクエストは429エラーになります。
- サーバーはインメモリでタスク状態を保持するため、再起動すると進行中のダウンロード状況は失われます。
