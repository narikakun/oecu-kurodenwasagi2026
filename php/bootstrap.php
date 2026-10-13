<?php
declare(strict_types=1);

function app_config(): array
{
    static $config = null;
    if ($config !== null) {
        return $config;
    }

    $path = __DIR__ . '/config.php';
    if (!is_file($path)) {
        throw new RuntimeException('config.phpがありません。config.example.phpをコピーして設定してください。');
    }
    $loaded = require $path;
    if (!is_array($loaded)) {
        throw new RuntimeException('config.phpの形式が正しくありません。');
    }
    $config = $loaded;
    return $config;
}

function db(): PDO
{
    static $pdo = null;
    if ($pdo instanceof PDO) {
        return $pdo;
    }

    $database = app_config()['database'] ?? [];
    $host = (string)($database['host'] ?? '');
    $port = (int)($database['port'] ?? 3306);
    $name = (string)($database['name'] ?? '');
    $charset = (string)($database['charset'] ?? 'utf8mb4');
    $dsn = "mysql:host={$host};port={$port};dbname={$name};charset={$charset}";
    $pdo = new PDO(
        $dsn,
        (string)($database['user'] ?? ''),
        (string)($database['password'] ?? ''),
        [
            PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            PDO::ATTR_EMULATE_PREPARES => false,
        ]
    );
    return $pdo;
}

function request_authorization(): string
{
    if (isset($_SERVER['HTTP_AUTHORIZATION'])) {
        return trim((string)$_SERVER['HTTP_AUTHORIZATION']);
    }
    if (function_exists('getallheaders')) {
        $headers = getallheaders();
        foreach ($headers as $name => $value) {
            if (strcasecmp((string)$name, 'Authorization') === 0) {
                return trim((string)$value);
            }
        }
    }
    return '';
}

function json_response(int $status, array $body): void
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo json_encode($body, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

function require_dashboard_auth(): void
{
    $config = app_config();
    $username = $_SERVER['PHP_AUTH_USER'] ?? null;
    $password = $_SERVER['PHP_AUTH_PW'] ?? null;

    if ($username === null || $password === null) {
        $authorization = request_authorization();
        if (stripos($authorization, 'Basic ') === 0) {
            $decoded = base64_decode(substr($authorization, 6), true);
            if ($decoded !== false && strpos($decoded, ':') !== false) {
                [$username, $password] = explode(':', $decoded, 2);
            }
        }
    }

    $validUser = is_string($username)
        && hash_equals((string)($config['dashboard_user'] ?? ''), $username);
    $validPassword = is_string($password)
        && password_verify($password, (string)($config['dashboard_password_hash'] ?? ''));
    if (!$validUser || !$validPassword) {
        header('WWW-Authenticate: Basic realm="Conversation Dashboard", charset="UTF-8"');
        header('Cache-Control: no-store');
        http_response_code(401);
        echo '認証が必要です。';
        exit;
    }
}

function valid_uuid(string $value): bool
{
    return preg_match(
        '/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i',
        $value
    ) === 1;
}

function mysql_datetime(string $value): string
{
    $date = new DateTimeImmutable($value);
    return $date->setTimezone(new DateTimeZone('UTC'))->format('Y-m-d H:i:s.v');
}

