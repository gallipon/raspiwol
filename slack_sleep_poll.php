<?php
/*
 * raspiwol: Slack 勤怠「終了」投稿 -> PC スリープ（不可視・Bot をチャンネルに入れない方式）
 *
 * Bot ではなく「自分の User トークン」で conversations.history を読むだけなので、
 * アプリはチャンネルに参加せず、他のメンバーには一切見えない。新規の自分の
 * 「終了」投稿を見つけたら Beebotte raspi3b/pcsleep_req へ SLEEP_CMD を write する。
 *
 * 即時の "sleep" ではなく退勤予約 "sleep_in 1" を送る（2026-09-17 変更）。"sleep" は
 * エージェントが無条件に寝かせるため、Claude Code の作業中でも寝てしまっていた。
 * 予約はエージェント側で「1分経過＋無操作5分＋Claude Code 非稼働（最長180分で打ち切り）」
 * を満たしたときに発火する。ダッシュボードの Sleep ボタンは従来どおり即時。
 *
 * cron で10分ごとに実行（VPS。投稿から寝るまで最大10分遅れる）:
 *   0,10,20,30,40,50 * * * * /usr/bin/php /var/www/.../slack_sleep_poll.php >/dev/null 2>&1
 *
 * 認証情報・サイト固有設定は別ファイル slack_sleep_config.php に分離する（同じ
 * ディレクトリに置く）。その実体はリポジトリに commit しない（.gitignore 済み）。
 * テンプレートは slack_sleep_config.example.php をコピーして使う。
 * STATE_FILE は cron 実行ユーザーが書き込めるパスにする。
 *
 * autopilot スイッチ（raspi3b/autopilot）が "off" のときは「終了」を検出しても寝かせない
 * （2026-09-17 変更。休暇・残業などで OFF にした日に Slack 経由で寝るのを防ぐ）。
 * 未作成(404)は従来どおり on 扱い。読み取り失敗時は watermark を進めず次回再判定する。
 *
 * 自前ブローカー（2026-11-21）: 設定に MQTT_SLACK_PASS があれば、VPS 内の Mosquitto
 * （127.0.0.1:1883、ユーザー slack）で autopilot を読み、指示を送る。Beebotte の証明書が
 * 切れたまま更新されないための移行。送信に失敗したときだけ Beebotte へ送り直す（エージェントは
 * ブローカーごとに一度だけ実行するので、仮に両方へ届いても sleep_in の予約がかけ直されるだけ）。
 * パスワードをコマンドの引数に出さないよう、mosquitto_pub は使わず MQTT 3.1.1 を直接話す。
 */

// 認証情報・サイト固有設定を読み込む（SLACK_USER_TOKEN / BEEBOTTE_TOKEN /
// TARGET_CHANNEL / TARGET_USER / TRIGGER / STATE_FILE）。無ければ fatal で気づける。
require __DIR__ . "/slack_sleep_config.php";

const HIST_URL = "https://slack.com/api/conversations.history";
// publish ではなく write（永続）。エージェントはこのリソースを REST でポーリング
// するので、Beebotte の MQTT が落ちていても届く（2026-09-22 の障害対策）。
const PUB_URL  = "https://api.beebotte.com/v1/data/write/raspi3b/pcsleep_req";
const AUTO_URL = "https://api.beebotte.com/v1/data/read/raspi3b/autopilot?limit=1";
const SLEEP_CMD = "sleep_in 1";   // pcsleep_agent の退勤予約（分）。1〜240 の範囲で指定

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

// autopilot スイッチを読む。"on" / "off" / null（読み取り失敗）を返す。
// リソース未作成(404)は Pi/エージェントと同じく "on" 扱い。
function read_autopilot() {
    $ch = curl_init(AUTO_URL);
    curl_setopt_array($ch, array(
        CURLOPT_HTTPHEADER => array("X-Auth-Token: " . BEEBOTTE_TOKEN),
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => 5,
        // publish と同じく api.beebotte.com の不完全チェーン回避（下の PUB 参照）
        CURLOPT_SSL_VERIFYPEER => false,
        CURLOPT_SSL_VERIFYHOST => 0,
    ));
    $r = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    if ($code === 404) return "on";
    if ($code !== 200) return null;
    $arr = json_decode($r, true);
    if (!is_array($arr)) return null;
    if (!isset($arr[0]["data"])) return "on";   // 値が一度も書かれていない
    return strtolower(trim((string)$arr[0]["data"])) === "off" ? "off" : "on";
}

// ── 自前ブローカー（最小限の MQTT 3.1.1 クライアント）────────────────────────────
const MQ_ADDR = "tcp://127.0.0.1:1883";
const MQ_USER = "slack";

function mq_str($s) { return pack("n", strlen($s)) . $s; }

function mq_packet($type, $body) {
    $len = strlen($body);
    $rl = "";
    do {                                   // 残りの長さ（可変長）
        $b = $len % 128;
        $len = intdiv($len, 128);
        if ($len > 0) $b |= 0x80;
        $rl .= chr($b);
    } while ($len > 0);
    return chr($type) . $rl . $body;
}

// 1パケット読む。array(type, body) か、時間切れ・切断なら null。
function mq_read($fp) {
    $h = fread($fp, 1);
    if ($h === false || $h === "") return null;
    $len = 0; $mul = 1;
    for ($i = 0; $i < 4; $i++) {
        $c = fread($fp, 1);
        if ($c === false || $c === "") return null;
        $len += (ord($c) & 0x7f) * $mul;
        if ((ord($c) & 0x80) === 0) break;
        $mul *= 128;
    }
    $body = "";
    while (strlen($body) < $len) {
        $chunk = fread($fp, $len - strlen($body));
        if ($chunk === false || $chunk === "") return null;
        $body .= $chunk;
    }
    return array(ord($h), $body);
}

// 接続して CONNACK を確かめる。成功ならソケット、失敗なら null。
function mq_connect() {
    $fp = @stream_socket_client(MQ_ADDR, $errno, $errstr, 5);
    if (!$fp) { fwrite(STDERR, "mq connect failed: $errstr\n"); return null; }
    stream_set_timeout($fp, 5);
    $vh = mq_str("MQTT") . chr(4) . chr(0xC2) . pack("n", 30);   // v3.1.1・user+pass・clean
    $pl = mq_str("slack-poll-" . getmypid()) . mq_str(MQ_USER) . mq_str(MQTT_SLACK_PASS);
    fwrite($fp, mq_packet(0x10, $vh . $pl));
    $p = mq_read($fp);
    if ($p === null || $p[0] !== 0x20 || strlen($p[1]) < 2 || ord($p[1][1]) !== 0) {
        fwrite(STDERR, "mq connack refused\n");
        fclose($fp);
        return null;
    }
    return $fp;
}

function mq_close($fp) { @fwrite($fp, mq_packet(0xE0, "")); fclose($fp); }

// retained の autopilot を読む。"on" / "off" / null（読み取り失敗）。値が無ければ "on"。
function mq_read_autopilot() {
    $fp = mq_connect();
    if (!$fp) return null;
    fwrite($fp, mq_packet(0x82, pack("n", 1) . mq_str("raspi3b/autopilot") . chr(0)));
    stream_set_timeout($fp, 2);            // SUBACK の後、retained が無ければ何も来ない
    $val = "on";
    $acked = false;
    while (($p = mq_read($fp)) !== null) {
        $type = $p[0] & 0xF0;
        if ($type === 0x90) {              // SUBACK
            if (substr($p[1], 2, 1) === chr(0x80)) { mq_close($fp); return null; }   // 拒否
            $acked = true;
            continue;
        }
        if ($type === 0x30) {              // PUBLISH（QoS0 なので packet id は無い）
            $tlen = unpack("n", substr($p[1], 0, 2))[1];
            $payload = substr($p[1], 2 + $tlen);
            $j = json_decode($payload, true);
            $data = is_array($j) && isset($j["data"]) ? $j["data"] : $payload;
            $val = strtolower(trim((string)$data)) === "off" ? "off" : "on";
            break;
        }
    }
    mq_close($fp);
    return $acked ? $val : null;
}

// pcsleep_req へ retained・QoS1 で送る。PUBACK まで確かめられたら true。
function mq_send_sleep($cmd) {
    $fp = mq_connect();
    if (!$fp) return false;
    $body = json_encode(array("data" => $cmd, "ts" => (int)round(microtime(true) * 1000)));
    fwrite($fp, mq_packet(0x33, mq_str("raspi3b/pcsleep_req") . pack("n", 1) . $body));
    $p = mq_read($fp);
    $ok = ($p !== null && ($p[0] & 0xF0) === 0x40);
    mq_close($fp);
    return $ok;
}

$use_mq = defined("MQTT_SLACK_PASS") && MQTT_SLACK_PASS !== "";

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
    // 並行運用中は自前ブローカーの autopilot が正（ダッシュボード・エージェント・Pi と同じ）
    $auto = $use_mq ? mq_read_autopilot() : read_autopilot();
    if ($auto === null) {
        // 判定できない: watermark を進めずに抜け、次回の cron で再判定する
        fwrite(STDERR, "autopilot read failed; retry next run\n");
        exit(1);
    }
    if ($auto === "off") $hit = false;             // OFF の日は「終了」でも寝かせない
}

if ($hit && $use_mq) {
    if (mq_send_sleep(SLEEP_CMD)) {
        $hit = false;                              // 送れたので Beebotte へは送らない
    } else {
        fwrite(STDERR, "mq send failed; falling back to Beebotte\n");
    }
}

if ($hit) {
    $ch = curl_init(PUB_URL);
    curl_setopt_array($ch, array(
        CURLOPT_POST => true,
        CURLOPT_HTTPHEADER => array("X-Auth-Token: " . BEEBOTTE_TOKEN, "Content-Type: application/json"),
        CURLOPT_POSTFIELDS => json_encode(array("data" => SLEEP_CMD)),
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => 5,
        // api.beebotte.com への TLS 検証が失敗する（HTTP 0／chain 検証不可。Slack 等の
        // 他ホストは通るのにここだけ失敗＝Pi の bbt_write と同じ事象）。コマンドを投げる
        // だけの内部用途なので curl -k 相当で回避。正攻法は中間証明書/CAバンドルの整備。
        CURLOPT_SSL_VERIFYPEER => false,
        CURLOPT_SSL_VERIFYHOST => 0,
    ));
    curl_exec($ch);
    curl_close($ch);
}

// watermark を前進（同じ投稿で二度寝かせない）
@file_put_contents(STATE_FILE, sprintf("%.6f", $maxTs));
