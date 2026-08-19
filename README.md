# nippou-app（日報アプリ）

社内向けのシンプルな日報アプリです。利用者は毎日の日報を書いて提出し、
管理者は全員の日報を検索・閲覧・CSV 出力できます。
Flask + SQLAlchemy で実装しています。

## 機能

### 認証・ユーザー管理

- ユーザー登録 / ログイン / ログアウト（セッションベース認証）
- パスワードはハッシュ化して保存
- ロールによるアクセス制御（`admin` / `user`）
- 管理者と利用者でログインページを分離
  - 利用者ログイン: `/login`
  - 管理者ログイン: `/admin/login`
- ログインID・パスワードの変更（利用者: `/settings` / 管理者: `/admin/mypage`）
- 管理者向けユーザー管理（一覧・作成・ロール変更・削除、最後の管理者は保護）
- 管理者マイページ（`/admin/mypage`）
  - アカウント情報（ログインID・登録日・登録ユーザー数・管理者の人数・自分の日報数/コメント数）
  - ログインIDの変更、パスワードの変更（8 文字以上）。どちらも現在のパスワードによる本人確認が必須
  - 旧 `/admin/settings` はこのページへリダイレクト

### 日報（利用者）

- 日報の作成・編集・削除（`/reports/new`, `/reports/<id>/edit`）
  - 入力項目: 日付 / 作業時間（0〜24h・任意）/ 業務内容（必須）/ 明日の予定 / 課題・所感
  - 1 ユーザー・1 日につき 1 件（同じ日付の二重登録は防止）
  - 日付は日本時間基準。翌日分まで登録可能
- 下書き保存と提出（提出後も編集可、下書きに戻すことも可能）
- 自分の日報一覧（`/reports`）: 月・ステータス・キーワードで絞り込み、20 件ごとのページング
- 日報へのコメント（本人と管理者が投稿・削除できる）
- ダッシュボード（`/dashboard`）: 本日の日報の状態、今月の提出数、下書き数、直近の日報

### Google カレンダー連携（OAuth 2.0 + Calendar API）

- **アプリの登録（管理者・1 回だけ）**: `/admin/google` でクライアント ID とシークレットを登録する。
  Google Cloud に登録するコールバック URL もこの画面に表示される。サーバーの環境変数でも設定でき、
  アプリ内の設定が優先される（環境変数はフォールバック）。シークレットは暗号化して保存
- **利用者の連携（各自）**: `/calendar` から自分の Google アカウントを接続し、権限を選ぶ
  - **読み取りのみ**（`calendar.readonly`）: 予定を読み取って日報に取り込むだけ。カレンダーは変更しない
  - **読み取りと書き込み**（`calendar.events`）: 上に加えて、提出した日報をその日の終日予定として書き出す
- 同意画面で実際に許可された範囲を保存するため、書き込みを許可しなければ読み取りのみとして扱う
- 日報の作成・編集画面から「Google カレンダーの予定を取り込む」で、その日の予定を業務内容へ挿入
  （辞退した予定と削除済みの予定は除外）
- 書き込み権限があるとき、日報の提出でカレンダーに登録、再提出で同じ予定を更新、
  下書きに戻す・日報を削除すると予定も削除する
- カレンダー側の失敗で日報の保存が妨げられることはない（警告を出すだけ）
- リフレッシュトークンは暗号化して保存（`TOKEN_ENCRYPTION_KEY`、未設定なら `SECRET_KEY` から導出）
- `/calendar` から連携解除（Google 側の許可も取り消す）

### 日報（管理者）

- 全員の日報一覧（`/admin/reports`）: ユーザー・期間・ステータス・キーワードで絞り込み
- 絞り込み結果の CSV ダウンロード（`/admin/reports.csv`、Excel 向け BOM 付き UTF-8）
- ダッシュボードに本日の提出状況と未提出者、最近提出された日報を表示
- 他人の日報は閲覧・コメントはできるが編集はできない（編集は本人のみ）

## 画面

| パス | 説明 |
| --- | --- |
| `/dashboard` | ダッシュボード |
| `/reports` | 自分の日報一覧 |
| `/reports/new` | 日報の作成 |
| `/reports/<id>` | 日報の詳細・コメント |
| `/reports/<id>/edit` | 日報の編集 |
| `/admin/reports` | 全員の日報一覧（管理者） |
| `/admin/reports.csv` | 日報の CSV 出力（管理者） |
| `/admin/users` | ユーザー管理（管理者） |
| `/admin/mypage` | 管理者マイページ（ID・パスワード変更） |
| `/admin/google` | Google 連携設定（管理者。クライアント ID / シークレット） |
| `/setup` | 初期管理者の作成（管理者が 1 人も居ない間だけ有効） |
| `/calendar` | Google カレンダー連携の設定（権限の選択・解除） |

## 初期管理者（ブートストラップ）

管理者アカウントは **管理者が 1 人も居ないときだけ** 作られます。
既に管理者が居るときは何もしないので、デフォルトパスワードのアカウントが
あとから復活することはありません。作成方法は 2 通りです。

### 1. 環境変数で自動作成（自動デプロイ向け）

`ADMIN_USERNAME` と `ADMIN_PASSWORD` の **両方** を設定して起動すると、
管理者が居ない場合にその内容で初期管理者を作成します。

```bash
ADMIN_USERNAME=boss ADMIN_PASSWORD='十分に長いパスワード' python app.py
```

### 2. `/setup` 画面で作成（環境変数なし）

環境変数が未設定のまま起動すると管理者は作られず、`/setup` が初期セットアップ
画面になります（`/` にアクセスすると自動で誘導されます）。ユーザー名と
パスワード（8 文字以上）を入力すると管理者が作成され、そのままログインします。

管理者が 1 人でも作られると `/setup` は閉じられ、以降はアクセスしても
管理者ログインへリダイレクトされます。

公開サーバーで初回セットアップを保護したい場合は `SETUP_TOKEN` を設定してください。
設定するとセットアップ画面で合言葉の入力が必須になります。

## 起動方法

### Docker Compose（Flask + PostgreSQL）

```bash
docker compose up --build
```

http://localhost:8000 にアクセスします。

### ローカル単体実行（SQLite にフォールバック）

`DATABASE_URL` が未設定の場合は SQLite (`app.db`) を使います。

```bash
pip install -r requirements.txt
python app.py
```

## テスト

一時 SQLite を使ったスモークテストが入っています（標準ライブラリの unittest のみ）。

```bash
python -m unittest discover -s tests
```

## 環境変数

| 変数 | 説明 |
| --- | --- |
| `DATABASE_URL` | DB 接続先。未設定なら SQLite を使用 |
| `SECRET_KEY` | セッション署名鍵。本番では必ず変更 |
| `ADMIN_USERNAME` | 初期管理者のユーザー名（`ADMIN_PASSWORD` と両方揃ったときのみ有効） |
| `ADMIN_PASSWORD` | 初期管理者のパスワード。未設定なら `/setup` で作成する |
| `SETUP_TOKEN` | 設定すると `/setup` で合言葉の入力を必須にする |
| `GOOGLE_CLIENT_ID` | Google OAuth クライアント ID（`/admin/google` の設定が優先） |
| `GOOGLE_CLIENT_SECRET` | Google OAuth クライアントシークレット |
| `GOOGLE_REDIRECT_URI` | コールバック URL を明示したい場合に設定（リバースプロキシ配下向け） |
| `TOKEN_ENCRYPTION_KEY` | トークン暗号化鍵（Fernet 形式）。未設定なら `SECRET_KEY` から導出 |

## 構成

| ファイル | 役割 |
| --- | --- |
| `app.py` | アプリ生成、認証・ユーザー管理のルーティング |
| `auth.py` | ログイン状態の取得と `login_required` / `admin_required` |
| `reports.py` | 日報のルーティング、検索・集計・CSV 出力 |
| `google_calendar.py` | Google OAuth とカレンダー API、トークンの暗号化、日報の書き出し |
| `calendar_routes.py` | 連携画面・認可・コールバック・解除 |
| `models.py` | `User` / `DailyReport` / `ReportComment` / `GoogleCalendarLink` / `ReportCalendarEvent` / `GoogleOAuthSetting` |
| `templates/` | 画面（Jinja2） |
| `tests/` | スモークテスト |

テーブルは起動時に `db.create_all()` で作成されます（既存テーブルはそのまま）。

## Google カレンダー連携のセットアップ

1. Google Cloud コンソールでプロジェクトを作り、**Google Calendar API** を有効化する
2. 「OAuth 同意画面」を設定する（社内利用なら内部）。スコープは
   `calendar.readonly` と `calendar.events`、`userinfo.email` を登録する
3. 「認証情報」→ OAuth 2.0 クライアント ID（ウェブアプリケーション）を作成し、
   **承認済みのリダイレクト URI** に `/admin/google` に表示される URL をそのまま追加する
   （通常は `https://<アプリのURL>/calendar/callback`）
4. 発行された client ID / client secret を、管理者としてログインして `/admin/google` に登録する
   （サーバーの環境変数 `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` でも設定可能）
5. トークン暗号化鍵を作って `TOKEN_ENCRYPTION_KEY` に設定する（推奨）

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

6. アプリを再起動し、各利用者が `/calendar` から権限を選んで連携する

`TOKEN_ENCRYPTION_KEY` を未設定にすると `SECRET_KEY` から鍵を導出するため、
`SECRET_KEY` を変更すると保存済みトークンが復号できなくなり、再連携が必要になります。
