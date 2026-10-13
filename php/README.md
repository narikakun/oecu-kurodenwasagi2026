# 会話記録サーバーの設置

PHP 7.4以上（PDO MySQL・mbstring）、MySQL 5.7以上またはMariaDB 10.3以上を想定しています。公開時は必ずHTTPSを使用してください。

## 1. MySQLテーブルを作成

レンタルサーバーのphpMyAdminなどで、使用するデータベースを選び、`schema.sql`を実行します。

## 2. サーバー設定を作成

`config.example.php`を`config.php`としてコピーし、MySQL接続情報とAPIトークンを設定します。APIトークンには32バイト以上のランダム値を推奨します。

Basic認証用パスワードのハッシュは、PHPを実行できる安全な端末で次のように作成し、`dashboard_password_hash`へ貼り付けます。

```bash
php -r 'echo password_hash("ここを十分に長いパスワードへ変更", PASSWORD_DEFAULT), PHP_EOL;'
```

`config.php`は`.gitignore`の対象です。実際のパスワード、DBパスワード、APIトークンをGitへコミットしないでください。

## 3. レンタルサーバーへ配置

`php`フォルダの中身を、レンタルサーバーの公開ディレクトリへアップロードします。Apacheの`.htaccess`が許可されている環境では、設定ファイルやSQLファイルへの直接アクセスを自動で拒否します。

ブラウザで次のURLを開き、Basic認証後にダッシュボードが表示されることを確認します。

```text
https://あなたのドメイン/配置先/index.php
```

`.htaccess`が使えないサーバーでは、`config.php`、`config.example.php`、`bootstrap.php`、`schema.sql`、`README.md`をWebから取得できないよう、レンタルサーバーのアクセス制限を設定してください。

## 4. Raspberry Piを設定

Pi側の`.env`へ次を追加します。`TRANSCRIPT_API_TOKEN`はサーバーの`config.php`と同じ値です。

```dotenv
TRANSCRIPT_API_URL=https://あなたのドメイン/配置先/ingest.php
TRANSCRIPT_API_TOKEN=十分に長いランダム文字列
TRANSCRIPT_DEVICE_NAME=black-phone-1
```

アプリを再起動し、ログに次が表示されれば送信機能が有効です。

```text
会話記録のリアルタイム送信を有効にしました
```

確定した発話ごとに送信されます。通信失敗時は通話を止めずに最大5回再試行します。ダッシュボードは3秒ごとに更新されます。

## 動作確認

1. 受話器を上げ、AIと短く会話します。
2. ダッシュボードの一覧に「通話中」が追加されることを確認します。
3. AIと参加者の発話が順番に表示されることを確認します。
4. 受話器を置き、状態が「完了」に変わることを確認します。
5. 誤ったBasic認証情報ではダッシュボードを開けないことを確認します。

