<?php
/*
 * raspiwol: Slack 勤怠「終了」投稿 -> PC スリープ
 *
 * Slack Events API のエンドポイント。勤怠チャンネルで「自分」が「終了」を含む
 * メッセージを投稿したら、Beebotte raspi3b/pcsleep へ "sleep" を publish する
 * （PC 常駐エージェント pcsleep_agent.py が SetSuspendState）。
 *
 * 配置: VPS の docroot 配下の推測しにくいパスに置く（HTTPS, Apache + PHP）。
 * 下の定数は「VPS 上のコピーで」埋める。★本物のシークレットをリポジトリに commit しないこと。
 * ファイル権限は限定し、ディレクトリ一覧に出さないこと（.php はソースが配信されないが念のため）。
 *
 * 注意: autopilot スイッチとは独立（明示的な退勤操作なので OFF でも常に寝かせる＝案A）。
 */

const SLACK_SIGNING_SECRET = "FILL_ME";       // Slack App -> Basic Information -> Signing Secret
const BEEBOTTE_TOKEN       = "token_FILL_ME"; // Beebotte チャンネルトークン
const TARGET_CHANNEL       = "C0FILL_ME";     // 勤怠チャンネルの ID（Cxxxx / 非公開なら Gxxxx）
const TARGET_USER          = "U0FILL_ME";     // 自分の Slack member ID（Uxxxx）
const TRIGGER              = "終了";           // 本文に含まれていれば発火（部分一致）

const BEEBOTTE_URL = "https://api.beebotte.com/v1/data/publish/raspi3b/pcsleep";

function respond($code, $body = "") { http_response_code($code); echo $body; exit; }

// --- 生ボディと Slack 署名ヘッダ ---
$raw = file_get_contents("php://input");
$ts  = isset($_SERVER["HTTP_X_SLACK_REQUEST_TIMESTAMP"]) ? $_SERVER["HTTP_X_SLACK_REQUEST_TIMESTAMP"] : "";
$sig = isset($_SERVER["HTTP_X_SLACK_SIGNATURE"]) ? $_SERVER["HTTP_X_SLACK_SIGNATURE"] : "";

// リプレイ防止: 5分より古いリクエストは拒否
if ($ts === "" || abs(time() - (int)$ts) > 300) respond(400, "stale");

// 署名検証（HMAC-SHA256）
$expected = "v0=" . hash_hmac("sha256", "v0:" . $ts . ":" . $raw, SLACK_SIGNING_SECRET);
if (!hash_equals($expected, $sig)) respond(401, "bad signature");

$p = json_decode($raw, true);
if (!is_array($p)) respond(400, "bad json");

// Slack の URL 検証ハンドシェイク（Event Subscriptions 設定時）
if (isset($p["type"]) && $p["type"] === "url_verification") {
    header("Content-Type: text/plain");
    respond(200, isset($p["challenge"]) ? $p["challenge"] : "");
}

// イベント本体
if (isset($p["type"]) && $p["type"] === "event_callback") {
    $e = isset($p["event"]) ? $p["event"] : array();
    $text = isset($e["text"]) ? $e["text"] : "";
    if ((isset($e["type"]) && $e["type"] === "message")
        && !isset($e["subtype"])                                   // 編集/システム/bot を除外
        && !isset($e["bot_id"])
        && (isset($e["channel"]) && $e["channel"] === TARGET_CHANNEL)
        && (isset($e["user"]) && $e["user"] === TARGET_USER)       // ★自分の投稿だけ
        && mb_strpos($text, TRIGGER) !== false) {
        // sleep を publish（best-effort）
        $ch = curl_init(BEEBOTTE_URL);
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
    respond(200, "ok");   // 常に 200（Slack の再送を防ぐ）
}

respond(200, "ignored");
