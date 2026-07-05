# ytDownloader

yt-dlp を使って YouTube 動画をダウンロードできる、バックエンド/フロントエンド分離のサンプルアプリです。

## 構成

- Backend: FastAPI
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

4. 入力欄に YouTube の動画 URL を入力し、ダウンロードボタンを押します。

## テスト

```powershell
python -m unittest discover -s backend\tests -p "test_*.py"
```

## 備考

- ダウンロードされたファイルは backend/downloads 配下に保存されます。
- YouTube 側の制限やネットワーク状況により、ダウンロードが失敗する場合があります。
