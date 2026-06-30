<?php
/*
 * raspiwol: Slack 勤怠「終了」投稿 -> PC スリープ（不可視・Bot をチャンネルに入れない方式）
 *
 * Bot ではなく「自分の User トークン」で conversations.history を読むだけなので、
 * アプリはチャンネルに参加せず、他のメンバーには一切見えない。新規の自分の
 * 「終了」投稿を見つけたら Beebotte raspi3b/pcsleep へ "sleep" を publish する。
 *
 * cron で1分ごとに実行（VPS）:
 *   * * * * * /usr/bin/php /var/www/.../slack_sleep_poll.php >/dev/null 2>&1
 *
 * 定数は VPS 上のコピーで埋める。★本物のシークレットをリポジトリに commit しないこと。
 * STATE_FILE は cron 実行ユーザーが書き込めるパスにする。
 *
 * 注意: autopilot スイッチとは独立（明示的な退勤操作なので OFF でも常に寝かせる＝案A）。
 */

const SLACK_USER_TOKEN = "xoxp-FILL_ME";  // User OAuth Token（User Token Scope: channels:history）
const BEEBOTTE_TOKEN   = "token_FILL_ME"; // Beebotte チャンネルトークン
const TARGET_CHANNEL   = "C0FILL_ME";     // 勤怠チャンネルの ID（非公開なら groups:history が必要）
const TARGET_USER      = "U0FILL_ME";     // 自分の Slack member ID
const TRIGGER          = "終了";           // 本文に含まれていれば発火（部分一致）
const STATE_FILE       = "/var/lib/raspiwol/slack_last_ts";  // 最後に見た ts（要・書込み権限）

const HIST_URL = "https://slack.com/api/conversations.history";
const PUB_URL  = "https://api.beebotte.com/v1/data/publish/raspi3b/pcsleep";

function http_get($url, $headers) {
    $ch = curl_init($url);
    curl_setopt_array($ch, array(
        CURLOPT_HTTPHEADER => $headers,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => 8,
    ));
    $r = curl_exec($ch);
    curl_close($ch);
    return $r;
}

// 監視の起点(watermark)。初回は「今」にして過去の投稿で誤発火しないようにする。
$last = @file_get_contents(STATE_FILE);
$last = ($last !== false) ? trim($last) : "";
if ($last === "") {
    $last = sprintf("%.6f", time());
    @file_put_contents(STATE_FILE, $last);
}

$url = HIST_URL . "?channel=" . urlencode(TARGET_CHANNEL)
     . "&oldest=" . urlencode($last) . "&limit=100";
$res = http_get($url, array("Authorization: Bearer " . SLACK_USER_TOKEN));
$d = json_decode($res, true);
if (!is_array($d) || empty($d["ok"])) {
    fwrite(STDERR, "slack history error: " . $res . "\n");
    exit(1);
}

$maxTs = (float)$last;
$hit = false;
$messages = isset($d["messages"]) ? $d["messages"] : array();
foreach ($messages as $m) {
    $ts = isset($m["ts"]) ? (float)$m["ts"] : 0.0;
    if ($ts <= (float)$last) continue;             // oldest は境界含むので == は除外
    if ($ts > $maxTs) $maxTs = $ts;
    $text = isset($m["text"]) ? $m["text"] : "";
    if ((isset($m["user"]) && $m["user"] === TARGET_USER)   // ★自分の投稿だけ
        && !isset($m["subtype"])                            // 編集/システム/bot を除外
        && mb_strpos($text, TRIGGER) !== false) {
        $hit = true;
    }
}

if ($hit) {
    $ch = curl_init(PUB_URL);
    curl_setopt_array($ch, array(
        CURLOPT_POST => true,
        CURLOPT_HTTPHEADER => array("X-Auth-Token: " . BEEBOTTE_TOKEN, "Content-Type: application/json"),
        CURLOPT_POSTFIELDS => json_encode(array("data" => "sleep")),
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => 5,
    ));
    curl_exec($ch);
    curl_close($ch);
}

// watermark を前進（同じ投稿で二度寝かせない）
@file_put_contents(STATE_FILE, sprintf("%.6f", $maxTs));
