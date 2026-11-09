<?php
// Run in the production container as root, with the web container offline for apply.
$apply = in_array('--apply', $argv, true);
$root = '/mnt/data/files';
$changes = [];
$directories = 0;
$collisions = 0;
$walk = function ($directory) use (&$walk, &$changes, &$directories, &$collisions) {
    $entries = scandir($directory);
    if ($entries === false) {
        throw new RuntimeException('Cannot read directory: ' . bin2hex($directory));
    }
    foreach ($entries as $name) {
        if ($name === '.' || $name === '..') {
            continue;
        }
        $source = $directory . '/' . $name;
        if (is_link($source)) {
            continue;
        }
        if (is_dir($source)) {
            $walk($source);
        }
        if (preg_match('//u', $name) === 1) {
            continue;
        }
        if (!str_contains($source, '/files_trashbin/')) {
            throw new RuntimeException('Invalid filename outside trash; requires separate metadata review');
        }
        $newName = iconv('ISO-8859-1', 'UTF-8', $name);
        if ($newName === false) {
            throw new RuntimeException('Cannot convert filename');
        }
        $target = $directory . '/' . $newName;
        if (file_exists($target) || is_link($target)) {
            $extension = pathinfo($newName, PATHINFO_EXTENSION);
            $newName = 'latin1-conflict-' . hash('sha256', $name);
            if (preg_match('/^[a-zA-Z0-9]{1,10}$/', $extension)) {
                $newName .= '.' . $extension;
            }
            $target = $directory . '/' . $newName;
            if (file_exists($target) || is_link($target)) {
                throw new RuntimeException('Conflict preservation target already exists');
            }
            ++$collisions;
        }
        $changes[] = [$source, $target];
    }
    if (++$directories % 1000 === 0) {
        echo "Scanned $directories directories; invalid names: " . count($changes) . "\n";
    }
};
$walk($root);
echo 'Invalid UTF-8 filenames requiring Latin-1 conversion: ' . count($changes) . "\n";
echo "Collisions preserved with unique latin1-conflict names: $collisions\n";
if (!$apply || !$changes) {
    exit(0);
}
$journal = '/mnt/data/config/filename-normalization-' . date('Ymd_His') . '.jsonl';
$stream = fopen($journal, 'x');
if ($stream === false || !chmod($journal, 0600)) {
    throw new RuntimeException('Cannot create restricted filename journal');
}
foreach ($changes as [$source, $target]) {
    $line = json_encode(['source_base64' => base64_encode($source),
        'target_base64' => base64_encode($target)]) . "\n";
    if (fwrite($stream, $line) !== strlen($line) || !fflush($stream)) {
        throw new RuntimeException('Cannot persist filename journal');
    }
    if (!rename($source, $target)) {
        throw new RuntimeException('Filename rename failed; inspect journal');
    }
}
fclose($stream);
echo "Normalization complete; contents unchanged. Journal: $journal\n";
