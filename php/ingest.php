<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    header('Allow: POST');
    json_response(405, ['ok' => false, 'error' => 'POST only']);
}

$config = app_config();
$authorization = request_authorization();
$expected = 'Bearer ' . (string)($config['api_token'] ?? '');
if ($expected === 'Bearer ' || !hash_equals($expected, $authorization)) {
    json_response(401, ['ok' => false, 'error' => 'Unauthorized']);
}

$contentLength = (int)($_SERVER['CONTENT_LENGTH'] ?? 0);
if ($contentLength > 65536) {
    json_response(413, ['ok' => false, 'error' => 'Payload too large']);
}
$raw = file_get_contents('php://input');
try {
    $payload = json_decode($raw === false ? '' : $raw, true, 32, JSON_THROW_ON_ERROR);
} catch (JsonException) {
    json_response(400, ['ok' => false, 'error' => 'Invalid JSON']);
}
if (!is_array($payload)) {
    json_response(400, ['ok' => false, 'error' => 'JSON object required']);
}

$action = (string)($payload['action'] ?? '');
$eventId = (string)($payload['event_id'] ?? '');
$callId = (string)($payload['call_id'] ?? '');
if (!in_array($action, ['call_started', 'transcript', 'call_ended'], true)
    || !valid_uuid($eventId)
    || !valid_uuid($callId)) {
    json_response(422, ['ok' => false, 'error' => 'Invalid event']);
}
try {
    $occurredAt = mysql_datetime((string)($payload['occurred_at'] ?? ''));
} catch (Throwable) {
    json_response(422, ['ok' => false, 'error' => 'Invalid occurred_at']);
}

$pdo = db();
try {
    $pdo->beginTransaction();
    $event = $pdo->prepare('INSERT IGNORE INTO ingest_events (event_id) VALUES (?)');
    $event->execute([$eventId]);
    if ($event->rowCount() === 0) {
        $pdo->commit();
        json_response(200, ['ok' => true, 'duplicate' => true]);
    }

    if ($action === 'call_started') {
        $deviceName = trim((string)($payload['device_name'] ?? ''));
        if ($deviceName === '' || mb_strlen($deviceName) > 100) {
            throw new InvalidArgumentException('Invalid device_name');
        }
        $statement = $pdo->prepare(
            'INSERT INTO calls (call_id, device_name, status, started_at) VALUES (?, ?, ?, ?)'
        );
        $statement->execute([$callId, $deviceName, 'active', $occurredAt]);
    } elseif ($action === 'transcript') {
        $speaker = (string)($payload['speaker'] ?? '');
        $text = trim((string)($payload['text'] ?? ''));
        if (!in_array($speaker, ['user', 'assistant'], true)
            || $text === ''
            || mb_strlen($text) > 10000) {
            throw new InvalidArgumentException('Invalid transcript');
        }
        $statement = $pdo->prepare(
            'INSERT INTO transcripts (event_id, call_id, speaker, transcript_text, occurred_at) '
            . 'VALUES (?, ?, ?, ?, ?)'
        );
        $statement->execute([$eventId, $callId, $speaker, $text, $occurredAt]);
    } else {
        $status = (string)($payload['status'] ?? 'completed');
        if (!in_array($status, ['completed', 'cancelled', 'error'], true)) {
            throw new InvalidArgumentException('Invalid status');
        }
        $statement = $pdo->prepare(
            'UPDATE calls SET status = ?, ended_at = ? WHERE call_id = ?'
        );
        $statement->execute([$status, $occurredAt, $callId]);
        if ($statement->rowCount() === 0) {
            throw new RuntimeException('Call not found');
        }
    }

    $pdo->commit();
    json_response(200, ['ok' => true]);
} catch (InvalidArgumentException $error) {
    if ($pdo->inTransaction()) {
        $pdo->rollBack();
    }
    json_response(422, ['ok' => false, 'error' => $error->getMessage()]);
} catch (Throwable $error) {
    if ($pdo->inTransaction()) {
        $pdo->rollBack();
    }
    error_log('Conversation ingest error: ' . $error->getMessage());
    json_response(500, ['ok' => false, 'error' => 'Server error']);
}

