<?php
declare(strict_types=1);

return [
    'database' => [
        'host' => 'mysql.example.ne.jp',
        'port' => 3306,
        'name' => 'your_database_name',
        'user' => 'your_database_user',
        'password' => 'your_database_password',
        'charset' => 'utf8mb4',
    ],

    // Raspberry PiのTRANSCRIPT_API_TOKENと同じ、十分に長いランダム文字列。
    'api_token' => 'replace-with-a-long-random-token',

    // ダッシュボードのBasic認証。password_hash()で作った値を設定する。
    'dashboard_user' => 'admin',
    'dashboard_password_hash' => '$2y$10$replace.with.password_hash.output',

    // ダッシュボードでの日時表示。
    'display_timezone' => 'Asia/Tokyo',
];

