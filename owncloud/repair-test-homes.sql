\set ON_ERROR_STOP on
BEGIN;
LOCK TABLE oc_accounts, oc_storages IN EXCLUSIVE MODE;

DO $$
BEGIN
    IF current_database() NOT IN ('owncloud_stage', 'owncloud') THEN
        RAISE EXCEPTION 'Expected ownCloud production/test or mirror database';
    END IF;
    IF EXISTS (
        SELECT 1 FROM oc_accounts
        WHERE home NOT IN (
            '/daten/owncloud/data/' || user_id,
            '/mnt/data/files/' || user_id
        )
    ) THEN
        RAISE EXCEPTION 'Unexpected user home path; inspect before relocating';
    END IF;
END $$;

-- Home storage IDs (home::<user>) and file/share references stay unchanged.
UPDATE oc_accounts
SET home = '/mnt/data/files/' || user_id
WHERE home = '/daten/owncloud/data/' || user_id;

UPDATE oc_storages
SET id = 'local::/mnt/data/files/' ||
    substring(id FROM length('local::/daten/owncloud/data/') + 1)
WHERE id IN (
    SELECT 'local::/daten/owncloud/data/' || user_id || '/uploads/'
    FROM oc_accounts
);

COMMIT;
