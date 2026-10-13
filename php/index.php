<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';
require_dashboard_auth();

header('Cache-Control: no-store');
header("Content-Security-Policy: default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'");
header('X-Content-Type-Options: nosniff');
header('Referrer-Policy: no-referrer');

$config = app_config();
$timezone = new DateTimeZone((string)($config['display_timezone'] ?? 'Asia/Tokyo'));
$pdo = db();
$calls = $pdo->query(
    'SELECT c.call_id, c.device_name, c.status, c.started_at, c.ended_at, '
    . 'COUNT(t.id) AS message_count '
    . 'FROM calls c LEFT JOIN transcripts t ON t.call_id = c.call_id '
    . 'GROUP BY c.call_id, c.device_name, c.status, c.started_at, c.ended_at '
    . 'ORDER BY c.started_at DESC LIMIT 100'
)->fetchAll();

$selectedId = (string)($_GET['call'] ?? ($calls[0]['call_id'] ?? ''));
$selectedCall = null;
$messages = [];
if (valid_uuid($selectedId)) {
    $statement = $pdo->prepare(
        'SELECT call_id, device_name, status, started_at, ended_at FROM calls WHERE call_id = ?'
    );
    $statement->execute([$selectedId]);
    $selectedCall = $statement->fetch() ?: null;
    if ($selectedCall !== null) {
        $statement = $pdo->prepare(
            'SELECT speaker, transcript_text, occurred_at FROM transcripts '
            . 'WHERE call_id = ? ORDER BY occurred_at ASC, id ASC'
        );
        $statement->execute([$selectedId]);
        $messages = $statement->fetchAll();
    }
}

function h(string $value): string
{
    return htmlspecialchars($value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

function local_time(?string $value, DateTimeZone $timezone, bool $seconds = true): string
{
    if ($value === null || $value === '') {
        return '—';
    }
    $date = new DateTimeImmutable($value, new DateTimeZone('UTC'));
    return $date->setTimezone($timezone)->format($seconds ? 'Y/m/d H:i:s' : 'Y/m/d H:i');
}

function status_label(string $status): string
{
    $labels = [
        'active' => '通話中',
        'completed' => '完了',
        'cancelled' => '開始中止',
        'error' => 'エラー',
    ];
    return $labels[$status] ?? $status;
}
?>
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="3">
  <title>会話記録ダッシュボード</title>
  <style>
    :root { color-scheme: light; --ink:#231f1a; --muted:#746d64; --paper:#f6f1e7; --panel:#fffdf8; --line:#ddd3c4; --accent:#812f2f; }
    * { box-sizing: border-box; }
    body { margin:0; color:var(--ink); background:var(--paper); font-family:-apple-system,BlinkMacSystemFont,"Noto Sans JP","Yu Gothic",sans-serif; }
    header { display:flex; align-items:end; justify-content:space-between; gap:1rem; padding:1.25rem 1.5rem; border-bottom:1px solid var(--line); background:var(--panel); }
    h1 { margin:0; font-size:1.35rem; }
    header p { margin:.25rem 0 0; color:var(--muted); font-size:.85rem; }
    .live { color:#247044; font-weight:700; }
    main { display:grid; grid-template-columns:minmax(280px, 360px) minmax(0, 1fr); min-height:calc(100vh - 82px); }
    nav { border-right:1px solid var(--line); padding:1rem; overflow:auto; }
    nav h2, article h2 { margin:.2rem 0 1rem; font-size:1rem; }
    .call { display:block; padding:.85rem; margin-bottom:.6rem; color:inherit; text-decoration:none; background:var(--panel); border:1px solid var(--line); border-radius:10px; }
    .call:hover, .call.selected { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent) inset; }
    .call-top { display:flex; justify-content:space-between; gap:.5rem; font-size:.9rem; }
    .call-meta { margin-top:.35rem; color:var(--muted); font-size:.78rem; }
    .badge { white-space:nowrap; padding:.12rem .45rem; border-radius:999px; background:#eee7db; font-size:.72rem; }
    .badge.active { color:#165c35; background:#dff3e8; }
    .badge.error { color:#8a2525; background:#f8dddd; }
    article { padding:1.25rem clamp(1rem, 3vw, 2.5rem); overflow:auto; }
    .summary { display:flex; flex-wrap:wrap; gap:.75rem 1.5rem; padding:1rem; margin-bottom:1.25rem; background:var(--panel); border:1px solid var(--line); border-radius:10px; font-size:.85rem; }
    .summary span { color:var(--muted); }
    .messages { max-width:900px; }
    .message { width:fit-content; max-width:82%; margin:.7rem 0; padding:.75rem .9rem; border-radius:12px; background:#e6ded1; white-space:pre-wrap; overflow-wrap:anywhere; }
    .message.assistant { margin-right:auto; border-bottom-left-radius:3px; }
    .message.user { margin-left:auto; background:#f0dede; border-bottom-right-radius:3px; }
    .speaker { margin-bottom:.25rem; color:var(--muted); font-size:.72rem; }
    .time { margin-top:.35rem; color:var(--muted); font-size:.7rem; text-align:right; }
    .empty { padding:2rem; color:var(--muted); text-align:center; border:1px dashed var(--line); border-radius:10px; }
    @media (max-width:760px) { main { grid-template-columns:1fr; } nav { max-height:38vh; border-right:0; border-bottom:1px solid var(--line); } .message { max-width:94%; } }
  </style>
</head>
<body>
<header>
  <div><h1>会話記録ダッシュボード</h1><p>3秒ごとに自動更新します</p></div>
  <?php if (($selectedCall['status'] ?? '') === 'active'): ?><div class="live">● 通話中</div><?php endif; ?>
</header>
<main>
  <nav aria-label="通話一覧">
    <h2>最近の通話（最大100件）</h2>
    <?php if ($calls === []): ?><div class="empty">記録はまだありません</div><?php endif; ?>
    <?php foreach ($calls as $call): ?>
      <a class="call <?= $call['call_id'] === $selectedId ? 'selected' : '' ?>" href="?call=<?= h($call['call_id']) ?>">
        <div class="call-top"><strong><?= h(local_time($call['started_at'], $timezone, false)) ?></strong><span class="badge <?= h($call['status']) ?>"><?= h(status_label($call['status'])) ?></span></div>
        <div class="call-meta"><?= h($call['device_name']) ?>・<?= (int)$call['message_count'] ?>件</div>
      </a>
    <?php endforeach; ?>
  </nav>
  <article>
    <?php if ($selectedCall === null): ?>
      <div class="empty">左の一覧から通話を選択してください</div>
    <?php else: ?>
      <h2><?= h(local_time($selectedCall['started_at'], $timezone)) ?> の通話</h2>
      <div class="summary">
        <div><span>端末</span><br><?= h($selectedCall['device_name']) ?></div>
        <div><span>状態</span><br><?= h(status_label($selectedCall['status'])) ?></div>
        <div><span>開始</span><br><?= h(local_time($selectedCall['started_at'], $timezone)) ?></div>
        <div><span>終了</span><br><?= h(local_time($selectedCall['ended_at'], $timezone)) ?></div>
      </div>
      <section class="messages" aria-label="文字起こし">
        <?php if ($messages === []): ?><div class="empty">文字起こしを待っています…</div><?php endif; ?>
        <?php foreach ($messages as $message): ?>
          <div class="message <?= h($message['speaker']) ?>">
            <div class="speaker"><?= $message['speaker'] === 'assistant' ? 'AI' : '参加者' ?></div>
            <?= nl2br(h($message['transcript_text'])) ?>
            <div class="time"><?= h(local_time($message['occurred_at'], $timezone)) ?></div>
          </div>
        <?php endforeach; ?>
      </section>
    <?php endif; ?>
  </article>
</main>
</body>
</html>

