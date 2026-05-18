<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Apache + MySQL – it works!</title>
    <style>
        body { font-family: sans-serif; max-width: 600px; margin: 80px auto; padding: 0 20px; color: #333; }
        h1   { color: #2c7be5; }
        code { background: #f4f4f4; padding: 2px 6px; border-radius: 3px; }
        .ok  { color: green; }
    </style>
</head>
<body>
    <h1>🚀 Apache + MySQL – it works!</h1>
    <p>Your Docker stack is running. Place your application files in this directory.</p>
    <hr>
    <h2>PHP info</h2>
    <p>
        <?php
        echo '<span class="ok">✔ PHP ' . phpversion() . ' is active</span><br>';
        $host   = getenv('MYSQL_HOST')     ?: 'db';
        $db     = getenv('MYSQL_DATABASE') ?: 'webapp';
        $user   = getenv('MYSQL_USER')     ?: 'webapp';
        $pass   = getenv('MYSQL_PASSWORD') ?: '';
        $dsn    = "mysql:host=$host;dbname=$db";
        try {
            new PDO($dsn, $user, $pass);
            echo '<span class="ok">✔ MySQL connection OK</span>';
        } catch (PDOException $e) {
            echo '<span style="color:red">✘ MySQL: ' . htmlspecialchars($e->getMessage()) . '</span>';
        }
    ?>
    </p>
    <hr>
    <p><small>Remove or replace this file before going to production.</small></p>
</body>
</html>

