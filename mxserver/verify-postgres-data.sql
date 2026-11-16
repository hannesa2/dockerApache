SET TIME ZONE 'Europe/Berlin';
SET datestyle = 'ISO, DMY';
SET bytea_output = 'hex';
CREATE FUNCTION pg_temp.table_checks()
RETURNS TABLE(table_name text, row_count bigint, checksum numeric)
LANGUAGE plpgsql AS $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT n.nspname, c.relname
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'm')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
        ORDER BY n.nspname, c.relname
    LOOP
        RETURN QUERY EXECUTE format(
            'SELECT %L::text, count(*), coalesce(sum((''x'' || substr(md5(row_to_json(t)::text), 1, 16))::bit(64)::bigint::numeric), 0) FROM ONLY %I.%I t',
            format('%I.%I', r.nspname, r.relname), r.nspname, r.relname);
    END LOOP;
END
$$;
SELECT * FROM pg_temp.table_checks() ORDER BY table_name;
