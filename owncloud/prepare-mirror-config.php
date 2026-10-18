<?php

$sourceConfig = '/run/owncloud-source-config/config.php';
$runtimeConfigDir = '/var/www/owncloud/config';
$databaseName = getenv('OWNCLOUD_DB_NAME');
$databaseUser = getenv('OWNCLOUD_DB_USER');
$databasePassword = getenv('OWNCLOUD_DB_PASSWORD');
$publicHost = getenv('OWNCLOUD_PUBLIC_HOST');

if (!is_file($sourceConfig)) {
    fwrite(STDERR, "Source ownCloud config is missing: {$sourceConfig}\n");
    exit(1);
}

if ($databaseName === false || $databaseUser === false || $databasePassword === false || $publicHost === false) {
    fwrite(STDERR, "Required ownCloud mirror environment settings are missing.\n");
    exit(1);
}

$CONFIG = [];
include $sourceConfig;

if (!is_array($CONFIG) || empty($CONFIG['installed']) || ($CONFIG['dbtype'] ?? '') !== 'pgsql') {
    fwrite(STDERR, "Source ownCloud config is not an installed PostgreSQL instance.\n");
    exit(1);
}

if (!is_dir($runtimeConfigDir) && !mkdir($runtimeConfigDir, 0750, true) && !is_dir($runtimeConfigDir)) {
    fwrite(STDERR, "Could not create runtime ownCloud config directory.\n");
    exit(1);
}

$sourceConfigDir = dirname($sourceConfig);
foreach (new DirectoryIterator($runtimeConfigDir) as $entry) {
    if ($entry->isDot() || $entry->getFilename() === 'config.php') {
        continue;
    }

    $path = $entry->getPathname();
    if ($entry->isDir()) {
        $iterator = new RecursiveIteratorIterator(
            new RecursiveDirectoryIterator($path, FilesystemIterator::SKIP_DOTS),
            RecursiveIteratorIterator::CHILD_FIRST
        );
        foreach ($iterator as $child) {
            $child->isDir() ? rmdir($child->getPathname()) : unlink($child->getPathname());
        }
        rmdir($path);
    } else {
        unlink($path);
    }
}

$iterator = new RecursiveIteratorIterator(
    new RecursiveDirectoryIterator($sourceConfigDir, FilesystemIterator::SKIP_DOTS)
);
foreach ($iterator as $entry) {
    if (!$entry->isFile() || in_array($entry->getFilename(), ['config.php', '.owncloud-mirror-managed'], true)) {
        continue;
    }

    $relativePath = substr($entry->getPathname(), strlen($sourceConfigDir) + 1);
    $targetPath = $runtimeConfigDir . '/' . $relativePath;
    $targetDir = dirname($targetPath);
    if (!is_dir($targetDir) && !mkdir($targetDir, 0750, true) && !is_dir($targetDir)) {
        fwrite(STDERR, "Could not create runtime config subdirectory.\n");
        exit(1);
    }
    if (!copy($entry->getPathname(), $targetPath)) {
        fwrite(STDERR, "Could not copy a source ownCloud config file.\n");
        exit(1);
    }
}

$CONFIG['dbhost'] = 'owncloud-db';
$CONFIG['dbname'] = $databaseName;
$CONFIG['dbuser'] = $databaseUser;
$CONFIG['dbpassword'] = $databasePassword;
$CONFIG['datadirectory'] = '/mnt/data';
$CONFIG['trusted_domains'] = ['www16.mxtracks.info'];
$CONFIG['overwrite.cli.url'] = 'https://' . $publicHost . '/owncloud';
$CONFIG['overwritehost'] = $publicHost;
$CONFIG['overwriteprotocol'] = 'https';
$CONFIG['overwritewebroot'] = '/owncloud';
$CONFIG['htaccess.RewriteBase'] = '/owncloud';
$CONFIG['maintenance'] = false;

$tmpConfig = $runtimeConfigDir . '/config.php.tmp';
$contents = "<?php\n\$CONFIG = " . var_export($CONFIG, true) . ";\n";
if (file_put_contents($tmpConfig, $contents, LOCK_EX) === false) {
    fwrite(STDERR, "Could not write the mirror ownCloud config.\n");
    exit(1);
}

chmod($tmpConfig, 0600);
if (!rename($tmpConfig, $runtimeConfigDir . '/config.php')) {
    unlink($tmpConfig);
    fwrite(STDERR, "Could not atomically install the mirror ownCloud config.\n");
    exit(1);
}
