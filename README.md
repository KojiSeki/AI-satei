# AI Satei

CSV / Excel / PDF のアップロード、または Excel からコピーした表の貼り付けで、バックエンド API と Postgres に取込履歴を保存する最小構成です。

## 起動

```bash
docker compose up -d --build
```

既存の開発用ポートと衝突しないよう、ホスト側は以下で公開します。

- Frontend: `http://192.168.0.14:18014`
- Backend API: `http://192.168.0.14:18015`

コンテナ内部のポートは `frontend:80`、`backend:8000` のままです。

## 確認

```bash
docker compose ps
curl -sS http://192.168.0.14:18015/health
```

期待するヘルスチェック結果:

```json
{"status":"ok"}
```

## 初期化と学習

DB テーブルはバックエンド起動時に自動作成されます。明示的に実行する場合:

```bash
docker compose exec -T backend python -c "from backend.main import init_db; init_db(); print('DB tables created')"
```

ML モデル学習:

```bash
docker compose exec -T backend python -m backend.ml.train
```

## アップロード API

```bash
curl -F "file=@path/to/file.xlsx" http://192.168.0.14:18014/upload
```

テキスト貼り付け API:

```bash
curl -F "text=査定メモ" -F "filename=memo.txt" http://192.168.0.14:18014/upload-text
```

貼り付けテキストがタブ区切り表で `No.` 列を含む場合、明細件数、価格入力済件数、推定卸価格合計、カテゴリ別件数を集計します。表として崩れている場合でも、管理番号らしき文字列、メーカー候補、価格表現を可能な範囲で拾います。

停止:

```bash
docker compose down
```
