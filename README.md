# リーク情報 日本語通知ボット

Reddit の r/GamingLeaksAndRumours の新着を30分おきにチェックし、
Google Gemini（無料枠）で日本語に要約して Discord に通知します（X への自動投稿は任意）。
GitHub Actions 上で動くので、PC やサーバーを起動しておく必要はありません。

## ファイル構成

```
leak_bot.py                     … 本体
requirements.txt                … 必要なライブラリ
.github/workflows/leak-bot.yml  … 定期実行の設定
seen.json                       … 既読管理（初回実行時に自動作成）
```

---

## セットアップ手順

### 1. Discord の Webhook URL を取得する
1. 通知したいチャンネルの「⚙ チャンネルの編集」→「連携サービス」→「ウェブフック」
2. 「新しいウェブフック」→ 名前を付けて「ウェブフックURLをコピー」

### 2. Gemini API キーを取得する（無料）
1. https://aistudio.google.com に Google アカウントでログイン
2. 「Get API key」→「Create API key」でキーをコピー
3. **支払い情報（Billing）は登録しない**でください。登録しなければ課金されません

※ 無料枠では、送った内容が Google のモデル改善に使われることがあります。
　Reddit の公開投稿なので問題はありませんが、個人情報などは流さないでください。

### 3. GitHub にリポジトリを作る
1. GitHub で「New repository」→ 名前は例えば `leak-bot`、**Private** を選択して作成
2. 「Add file」→「Upload files」で `leak_bot.py` `requirements.txt` `README.md` をアップロード
3. 「Add file」→「Create new file」で、ファイル名欄に
   `.github/workflows/leak-bot.yml` と入力し、同名ファイルの中身を貼り付けてコミット
   （`.github` フォルダは Mac/Windows で隠しフォルダ扱いになり、ドラッグで上げ漏れしやすいため）

### 4. キーを GitHub に登録する
リポジトリの「Settings」→「Secrets and variables」→「Actions」

**Secrets** タブ →「New repository secret」

| Name | 値 |
|---|---|
| `DISCORD_WEBHOOK_URL` | 手順1のURL |
| `GEMINI_API_KEY` | 手順2のキー |

### 5. 動作確認
1. 「Actions」タブ →「leak-bot」→「Run workflow」
2. 1回目は既存の投稿を既読登録するだけ（過去分が一気に流れないように）
3. もう一度「Run workflow」→ 新着があれば Discord に日本語で届きます
4. 以降は30分おきに自動実行されます

動いたら、MonitoRSS 側のフィードは停止して二重通知を防いでください。

---

## X への自動投稿（任意・有料）

X API に無料枠はありません。**1投稿 $0.015（約2円）**、URL付きだと $0.20 かかります。
このボットは URL を付けず「出典: r/GamingLeaksAndRumours」と文字で書くので、安い方の料金です。

目安：1日20件で月 約$9、1日50件で月 約$23

### 有効にする手順
1. https://developer.x.com でボット用アカウントにて開発者登録し、クレジットを購入
2. アプリを作成 →「User authentication settings」で権限を **Read and write** にする
3. 「Keys and tokens」で API Key / Secret と Access Token / Secret を発行
   （権限を変更した後に発行し直すこと。先に発行したトークンは読み取り専用のまま）
4. GitHub の Secrets に追加：`X_API_KEY` `X_API_SECRET` `X_ACCESS_TOKEN` `X_ACCESS_TOKEN_SECRET`
5. 同じ画面の **Variables** タブ →「New repository variable」で `ENABLE_X` = `true`
6. X のアカウント設定で「自動化」ラベルを付ける（ボットアカウントの規約上の要件）

止めたいときは `ENABLE_X` を `false` にするだけです。

---

## 費用の目安

| 項目 | 料金 |
|---|---|
| GitHub Actions | 無料（非公開リポジトリは月2,000分まで。30分おきで約1,440分） |
| Gemini API | 無料（1日の回数制限内。このボットの使い方なら十分収まる） |
| X API | 上記のとおり（使う場合のみ） |

---

## よくあるトラブル

### Reddit に弾かれた場合（HTTP 403 / 429）
Reddit は GitHub Actions のようなクラウドサーバーからのアクセスを拒否することがあります。
その場合は、同じスクリプトを自宅PCで動かす方法に切り替えてください。

```
pip install -r requirements.txt
# 環境変数 DISCORD_WEBHOOK_URL と GEMINI_API_KEY を設定して
python leak_bot.py
```
これを Windows の「タスク スケジューラ」や Mac の cron で30分おきに実行します。

### 実行間隔を変えたい
`leak-bot.yml` の `cron: "*/30 * * * *"` を変更（`*/15` で15分おき）。
非公開リポジトリで15分おきにすると無料枠を超えるため、その場合はリポジトリを Public にしてください
（Secrets は公開されません。seen.json（既読の投稿ID）は見えるようになります）。
GitHub の定期実行は混雑時に数分〜十数分遅れることがあります。

### Gemini の回数制限に当たる / モデルが見つからないと言われる
Variables に `GEMINI_MODEL` を追加して、別のモデル名（例：`gemini-3.7-flash`、回数制限に当たる場合は軽量版の `gemini-3.5-flash-lite`）を指定してください。
Google はモデル名をよく更新するので、使えるモデルは https://ai.google.dev/gemini-api/docs/models で確認できます。

### Claude で翻訳したい（有料・品質重視）
Secrets に `ANTHROPIC_API_KEY`、Variables に `TRANSLATOR` = `claude` を追加します。
Claude API はプリペイド式（最低 $5 チャージ、有効期限1年）で、1件 約0.5円です。

### 翻訳の質を上げたい
`leak_bot.py` の `PROMPT` に用語集を追記します。例：
```
- 「Switch 2」は「Nintendo Switch 2」と書く
- 「dataminer」は「データマイナー」と書く
```
