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
 * 認証情報・サイト固有設定は別ファイル slack_sleep_config.php に分離する（同じ
 * ディレクトリに置く）。その実体はリポジトリに commit しない（.gitignore 済み）。
 * テンプレートは slack_sleep_config.example.php をコピーして使う。
 * STATE_FILE は cron 実行ユーザーが書き込めるパスにする。
 *
 * 注意: autopilot スイッチとは独立（明示的な退勤操作なので OFF でも常に寝かせる＝案A）。
 */

// 認証情報・サイト固有設定を読み込む（SLACK_USER_TOKEN / BEEBOTTE_TOKEN /
// TARGET_CHANNEL / TARGET_USER / TRIGGER / STATE_FILE）。無ければ fatal で気づける。
require __DIR__ . "/slack_sleep_config.php";

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
        // api.beebotte.com への TLS 検証が失敗する（HTTP 0／chain 検証不可。Slack 等の
        // 他ホストは通るのにここだけ失敗＝Pi の bbt_write と同じ事象）。"sleep" を投げる
        // だけの内部用途なので curl -k 相当で回避。正攻法は中間証明書/CAバンドルの整備。
        CURLOPT_SSL_VERIFYPEER => false,
        CURLOPT_SSL_VERIFYHOST => 0,
    ));
    curl_exec($ch);
    curl_close($ch);
}

// watermark を前進（同じ投稿で二度寝かせない）
@file_put_contents(STATE_FILE, sprintf("%.6f", $maxTs));
