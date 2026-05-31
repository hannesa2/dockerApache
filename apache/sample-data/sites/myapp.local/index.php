<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>My App</title>
    <style>
        body { font-family: sans-serif; max-width: 700px; margin: 80px auto; padding: 0 20px; color: #333; }
        h1   { color: #e25c2c; }
        nav a { margin-right: 12px; color: #2c7be5; text-decoration: none; }
        nav a:hover { text-decoration: underline; }
        .card { background: #f9f9f9; border: 1px solid #ddd; border-radius: 6px; padding: 16px; margin-top: 20px; }
        .ok   { color: green; }
        .err  { color: red; }
    </style>
</head>
<body>
    <h1>🌐 myapp.local</h1>
    <nav>
        <a href="/">Home</a>
        <a href="/about">About</a>
        <a href="/phpinfo.php">PHP Info</a>
    </nav>

    <div class="card">
        <h2>Environment</h2>
        <?php
            echo '<b>PHP:</b> ' . phpversion() . '<br>';
            echo '<b>Server:</b> ' . ($_SERVER['SERVER_SOFTWARE'] ?? 'n/a') . '<br>';
            echo '<b>Host:</b> ' . htmlspecialchars($_SERVER['HTTP_HOST'] ?? 'n/a') . '<br>';

            $host = getenv('MYSQL_HOST')     ?: 'db';
            $db   = getenv('MYSQL_DATABASE') ?: 'webapp';
            $user = getenv('MYSQL_USER')     ?: 'webapp';
            $pass = getenv('MYSQL_PASSWORD') ?: '';
            try {
                new PDO("mysql:host=$host;dbname=$db", $user, $pass);
                echo '<br><span class="ok">✔ MySQL connection OK</span>';
            } catch (PDOException $e) {
                echo '<br><span class="err">✘ MySQL: ' . htmlspecialchars($e->getMessage()) . '</span>';
            }
        ?>
    </div>

    <div class="card">
        <h2>Request</h2>
        <b>URI:</b> <?= htmlspecialchars($_SERVER['REQUEST_URI']) ?><br>
        <b>Method:</b> <?= $_SERVER['REQUEST_METHOD'] ?>
    </div>
</body>
</html>

