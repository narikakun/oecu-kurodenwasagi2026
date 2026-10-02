# 黒詐欺電話

600-A2型黒電話のフックスイッチとロータリーダイヤルをRaspberry Pi 5で読み取り、USB-C受話器を通してGPT-Liveと音声会話する大学制作向けプロジェクトです。

設計の詳細は[設計書](docs/design.md)を参照してください。

## 必要な環境

- Raspberry Pi 5
- Raspberry Pi OS（64bit推奨）
- Python 3.11以上
- USB-C受話器（マイク・スピーカーとして認識されるもの）
- 600-A2型黒電話のフックスイッチとダイヤル接点
- OpenAI APIプロジェクトキーとGPT-Liveへのアクセス

## セットアップ

PortAudioなど、`sounddevice`が使うOSパッケージを入れます。

```bash
sudo apt update
sudo apt install -y python3-venv libportaudio2
```

仮想環境を作り、アプリをインストールします。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

`.env.example`をコピーし、APIキーなどを設定します。プロジェクト直下で起動すると`.env`が自動で読み込まれます。

```bash
cp .env.example .env
# エディタで.envを編集する
kuro-sagi-denwa
```

シェルですでに設定されている環境変数は`.env`より優先されます。systemd運用時は`EnvironmentFile`で指定したファイルが使われます。

モックGPIOでは、`u`で受話器を上げ、`d`で置き、`0`～`9`でダイヤル入力を再現できます。音声とGPT-Liveは実際に接続されるため、APIキーとUSB音声デバイスが必要です。

## 実機接続

BCM GPIO番号で次のように接続します。接続前に、対象が外部電圧のない機械接点であることをテスターで確認してください。

```text
GPIO17 ── フックスイッチ ── GND
GPIO27 ── ダイヤル接点 ─── GND
```

初期設定では内部プルアップを使います。フックの論理が逆なら`HOOK_LIFTED_WHEN_LOW`を変更してください。

USBデバイスは次のコマンドで確認できます。

```bash
arecord -l
aplay -l
python -m sounddevice
```

## テスト

```bash
python -m unittest discover -s tests -v
```

このテストはGPIO、USB受話器、OpenAI APIを使わずに実行できます。

## 注意

- 黒電話を電話回線へ接続しないでください。
- Raspberry PiのGPIOへ5Vを入力しないでください。
- APIキーをソースコードやGitへ保存しないでください。
- 初期版のダイヤル入力はログに記録するところまでです。会話中の機能割り当ては今後追加します。
