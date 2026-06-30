<?php
/*
 * raspiwol Slack スリープ連携の認証情報・サイト固有設定（テンプレート）。
 *
 * 使い方（VPS 上）:
 *   cp slack_sleep_config.example.php slack_sleep_config.php
 *   # slack_sleep_config.php を編集して下の値を埋める
 *
 * ★ 実体 slack_sleep_config.php はリポジトリに commit しない（.gitignore 済み）。
 *   .php なので Apache 上では実行され、ソース（＝トークン）は配信されない。
 *   それでも権限は限定し、ディレクトリ一覧に出さないこと。
 */

const SLACK_USER_TOKEN = "xoxp-FILL_ME";  // User OAuth Token（User Token Scope: channels:history）
const BEEBOTTE_TOKEN   = "token_FILL_ME"; // Beebotte チャンネルトークン（ダッシュボードと同じ値）
const TARGET_CHANNEL   = "C0FILL_ME";     // 勤怠チャンネルの ID（非公開なら groups:history が必要）
const TARGET_USER      = "U0FILL_ME";     // 自分の Slack member ID
const TRIGGER          = "終了";           // 本文に含まれていれば発火（部分一致）
const STATE_FILE       = "/var/lib/raspiwol/slack_last_ts";  // 最後に見た ts（cron 実行ユーザーが書込み可なパス）
