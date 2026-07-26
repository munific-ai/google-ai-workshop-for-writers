#!/usr/bin/env python3
"""Google Chat スペースへ会話ログを投入する。

data/chat/spaces/*.json の messages[] を順に投稿し、replies[] があれば
同一スレッドへの返信として投稿する。

【注意】Chat API では投稿日時を過去に設定できません。
JSON の timestamp は再現されず、実行時刻が投稿日時になります。
横断検索は本文の内容で拾うため実害はありませんが、
当日「◯月◯日のチャットで」という言い方は避けてください。

事前準備（詳細は SETUP.md §4.2 を参照）
--------------------------------------
1. Google Cloud プロジェクトで Chat API を【有効化】する

2. Chat API を【構成】する ← ここが最も詰まります
   https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat
   「構成 / Configuration」タブで、アプリ名 / アバターURL / 説明 の 3 つを入力し、
   【インタラクティブ機能のトグルをオフ】にして保存。
     アプリ名     例) TECHSCOPE Loader
     アバターURL  https://developers.google.com/chat/images/quickstart-app-avatar.png
     説明         例) Workshop data loader
   ※ 有効化だけでは足りません。飛ばすと 404 "Google Chat app not found" になります
   ※ トグルをオフにすれば、接続設定（HTTPエンドポイント等）は不要です

3. OAuth 同意画面のユーザーの種類を【内部（Internal）】にする
   ※ 外部（External）にすると Google の審査が必要になります
   ※ スコープの事前登録は不要。内部ならスクリプトが実行時に要求し、
     ブラウザの承認画面でそのまま許可できます

4. OAuth クライアント ID を【デスクトップ アプリ】で作成し、
   JSON を credentials.json という名前で data/ 直下に置く
   ※ ウェブ アプリケーションだと redirect_uri_mismatch になります

5. uv sync（--dry-run なら依存なしで動きます）

認証するアカウントは【投稿先スペースのメンバー】である必要があります。
初回実行時にブラウザが開き、承認すると token.json が生成されます。

⚠️ credentials.json / token.json はコミットしないこと（.gitignore 登録済み）。
   token.json にはリフレッシュトークンが含まれます。

スペース ID は Google Chat の URL から取得します。
    https://mail.google.com/chat/u/0/#chat/space/AAQAL6gjTSw
                                                 ~~~~~~~~~~~ これ
    → --space spaces/AAQAL6gjTSw

使い方
------
    python post_messages.py --space spaces/AAAA1234 \\
        --json ../spaces/koetsu-teisei.json --prefix-author

    # 全スペースをまとめて投入する場合は --space を都度変えて 3 回実行
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

SCOPES = ["https://www.googleapis.com/auth/chat.messages.create"]


def _import_google():
    """Google API 依存を遅延 import する（--dry-run は依存なしで動かせる）。"""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError:
        sys.exit(
            "依存パッケージが不足しています:\n"
            "  uv add google-auth-oauthlib google-api-python-client\n"
            "（--dry-run なら依存なしで内容だけ確認できます）"
        )
    return Request, Credentials, InstalledAppFlow, build, HttpError


def get_service(credentials_path: pathlib.Path, token_path: pathlib.Path):
    Request, Credentials, InstalledAppFlow, build, _ = _import_google()
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                sys.exit(f"OAuth クライアント情報が見つかりません: {credentials_path}")
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("chat", "v1", credentials=creds)


SETUP_HINTS: list[tuple[str, str]] = [
    (
        "Chat app not found",
        """
────────────────────────────────────────────────────────────────
 Chat API の「構成」が未完了です（有効化だけでは足りません）
────────────────────────────────────────────────────────────────
  1. 次を開く（対象プロジェクトを選択）
     https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat

  2. 「構成 / Configuration」タブで入力（3つとも必須）
       アプリ名     : TECHSCOPE Loader          （25文字以内の英数字）
       アバター URL : https://developers.google.com/chat/images/quickstart-app-avatar.png
       説明         : Workshop data loader      （40文字以内の英数字）

  3. 「インタラクティブ機能」のトグルを【オフ】にする
       → ボットを作るわけではないので、接続設定（HTTP エンドポイント等）は不要です

  4. 保存し、数分待ってから再実行

  詳細: SETUP.md §4.2
""",
    ),
    (
        "PERMISSION_DENIED",
        """
────────────────────────────────────────────────────────────────
 権限エラーです
────────────────────────────────────────────────────────────────
  ・認証したアカウントが、投稿先スペースの【メンバー】か確認してください
  ・スコープ chat.messages.create が承認されているか確認してください
    （スコープを変更した場合は token.json を削除して再認証）

  詳細: SETUP.md §4.2
""",
    ),
    (
        "invalid_grant",
        """
────────────────────────────────────────────────────────────────
 認証情報が古くなっています
────────────────────────────────────────────────────────────────
  token.json を削除して、もう一度実行してください。
""",
    ),
]


def diagnose(exc: Exception) -> str | None:
    """既知のセットアップ不備なら、対処法を返す。"""
    text = str(exc)
    for needle, hint in SETUP_HINTS:
        if needle in text:
            return hint
    return None


def format_text(message: dict, prefix_author: bool) -> str:
    text = message["text"]
    if prefix_author:
        text = f"[{message['author']}] {text}"
    return text


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--space", required=True, help="投稿先スペース名（例: spaces/AAAA1234）")
    parser.add_argument("--json", required=True, type=pathlib.Path, help="会話ログ JSON")
    parser.add_argument("--credentials", type=pathlib.Path, default=pathlib.Path("credentials.json"))
    parser.add_argument("--token", type=pathlib.Path, default=pathlib.Path("token.json"))
    parser.add_argument(
        "--prefix-author",
        action="store_true",
        help="本文の先頭に [発言者名] を付ける。複数アカウントを用意できない場合に使う",
    )
    parser.add_argument("--delay", type=float, default=0.8, help="投稿間隔（秒）")
    parser.add_argument("--dry-run", action="store_true", help="投稿せず内容だけ表示する")
    args = parser.parse_args()

    if not args.json.exists():
        sys.exit(f"ファイルが見つかりません: {args.json}")

    data = json.loads(args.json.read_text(encoding="utf-8"))
    messages = data["messages"]

    print(f"スペース : {data['space']}")
    print(f"件数     : {len(messages)}")
    print()

    service = None
    HttpError = Exception
    if not args.dry_run:
        *_, HttpError = _import_google()
        service = get_service(args.credentials, args.token)
    posted = failed = 0

    for msg in messages:
        body = {"text": format_text(msg, args.prefix_author)}
        preview = msg["text"][:40].replace("\n", " ")

        if args.dry_run:
            print(f"  [DRY] {msg['author']}: {preview}...")
            posted += 1
        else:
            try:
                parent = (
                    service.spaces()
                    .messages()
                    .create(parent=args.space, body=body, messageReplyOption="MESSAGE_REPLY_OPTION_UNSPECIFIED")
                    .execute()
                )
                posted += 1
                print(f"  [OK] {msg['author']}: {preview}...")
                time.sleep(args.delay)
            except HttpError as exc:
                failed += 1
                print(f"  [NG] {msg['author']}: {exc}", file=sys.stderr)
                hint = diagnose(exc)
                if hint:
                    print(hint, file=sys.stderr)
                    print(
                        "設定を直すまで同じエラーが続くため、中断しました。\n"
                        f"（このスペースへの投稿は {posted} 件で止まっています）",
                        file=sys.stderr,
                    )
                    return 2
                continue

        thread_name = None if args.dry_run else parent.get("thread", {}).get("name")

        for reply in msg.get("replies", []):
            reply_body = {"text": format_text(reply, args.prefix_author)}
            reply_preview = reply["text"][:40].replace("\n", " ")

            if args.dry_run:
                print(f"    └ [DRY] {reply['author']}: {reply_preview}...")
                posted += 1
                continue

            reply_body["thread"] = {"name": thread_name}
            try:
                (
                    service.spaces()
                    .messages()
                    .create(
                        parent=args.space,
                        body=reply_body,
                        messageReplyOption="REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD",
                    )
                    .execute()
                )
                posted += 1
                print(f"    └ [OK] {reply['author']}")
                time.sleep(args.delay)
            except HttpError as exc:
                failed += 1
                print(f"    └ [NG] {reply['author']}: {exc}", file=sys.stderr)
                hint = diagnose(exc)
                if hint:
                    print(hint, file=sys.stderr)
                    print(
                        "設定を直すまで同じエラーが続くため、中断しました。\n"
                        f"（このスペースへの投稿は {posted} 件で止まっています）",
                        file=sys.stderr,
                    )
                    return 2

    print()
    print(f"投稿完了: {posted} 件 / 失敗: {failed} 件")

    if failed:
        print(
            "失敗があります。SETUP.md §4.2 のトラブルシュート表を確認してください。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
