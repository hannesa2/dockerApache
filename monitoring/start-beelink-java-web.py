#!/usr/bin/env python3
"""Check copied Apache/PHP, GlassFish and WildFly in the isolated staging network."""

import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import xml.etree.ElementTree as ET


SPEC = importlib.util.spec_from_file_location(
    "beelink_apps", Path(__file__).with_name("start-beelink-apps.py"))
apps = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apps)
WORK = apps.RUNTIME / "java-web"
COMPOSE = ["docker", "compose", "-p", "beelink-java-web", "-f", str(WORK / "compose.json")]
POOL = "post-gre-sql_MXServer_postgresPool_local"
JAVA_HOME = "/opt/java/openjdk"
ASADMIN = "/opt/glassfish-4.0/glassfish/bin/asadmin"
JDBC_PROBE = r"""
import java.io.File;
import java.sql.*;
import java.util.Properties;
import javax.xml.parsers.DocumentBuilderFactory;
import org.w3c.dom.*;

public class BeelinkJdbcProbe {
    public static void main(String[] args) throws Exception {
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
        Document document = factory.newDocumentBuilder().parse(new File(
            "/opt/glassfish-4.0/glassfish/domains/domain1/config/domain.xml"));
        NodeList pools = document.getElementsByTagName("jdbc-connection-pool");
        Properties credentials = new Properties();
        String url = null;
        int matches = 0;
        for (int i = 0; i < pools.getLength(); i++) {
            Element pool = (Element) pools.item(i);
            if (!pool.getAttribute("name").equals("post-gre-sql_MXServer_postgresPool_local")) {
                continue;
            }
            matches++;
            NodeList properties = pool.getElementsByTagName("property");
            for (int j = 0; j < properties.getLength(); j++) {
                Element property = (Element) properties.item(j);
                String name = property.getAttribute("name").toLowerCase(java.util.Locale.ROOT);
                String value = property.getAttribute("value");
                if (name.equals("user") || name.equals("password")) credentials.setProperty(name, value);
                if (name.equals("url")) url = value;
            }
        }
        if (matches != 1 || !"jdbc:postgresql://beelink-staging-mxserver-1:5432/MXServer".equals(url)) {
            throw new IllegalStateException("Expected isolated MXServer JDBC configuration");
        }
        Class.forName("org.postgresql.Driver");
        DriverManager.setLoginTimeout(10);
        try (Connection connection = DriverManager.getConnection(url, credentials);
             Statement statement = connection.createStatement();
             ResultSet result = statement.executeQuery(
                 "SELECT current_database(), (SELECT count(*) FROM public.tracks)")) {
            if (!result.next() || !"MXServer".equals(result.getString(1)) || result.getLong(2) <= 0) {
                throw new IllegalStateException("Expected nonempty restored MXServer tracks");
            }
            System.out.println("GlassFish Java/driver/credentials: isolated MXServer JDBC query OK");
        }
    }
}
"""
WORDPRESS_PATCH = r"""
$path = $argv[1];
$text = file_get_contents($path);
$pattern = '/(define\s*\(\s*[\'"]DB_HOST[\'"]\s*,\s*)[\'"][^\'"]*[\'"]/';
$text = preg_replace($pattern, '$1\'beelink-staging-apache-mysql-1\'', $text, -1, $count);
if ($count !== 1) {
    fwrite(STDERR, "Expected exactly one WordPress DB_HOST definition.\n");
    exit(1);
}
$cron = '/define\s*\(\s*[\'"]DISABLE_WP_CRON[\'"]\s*,\s*(true|false)\s*\)/i';
if (preg_match($cron, $text)) {
    $text = preg_replace($cron, "define('DISABLE_WP_CRON', true)", $text);
} else {
    $text = preg_replace('/<\?php/', "<?php\ndefine('DISABLE_WP_CRON', true);", $text, 1, $count);
    if ($count !== 1) {
        fwrite(STDERR, "Expected PHP opening tag in WordPress config.\n");
        exit(1);
    }
}
if (file_put_contents($path, $text) === false) {
    fwrite(STDERR, "Could not write isolated WordPress configuration.\n");
    exit(1);
}
"""


def patch_glassfish(path):
    tree = ET.parse(path)
    pools = [element for element in tree.iter("jdbc-connection-pool")
             if element.get("name") == POOL]
    if len(pools) != 1:
        raise RuntimeError("Expected the production MXServer JDBC pool")
    properties = {element.get("name").lower(): element
                  for element in pools[0].findall("property")}
    for key in ("servername", "url", "portnumber", "databasename"):
        if key not in properties:
            raise RuntimeError("Missing MXServer JDBC property: " + key)
    if properties["databasename"].get("value") != "MXServer":
        raise RuntimeError("Unexpected MXServer database name")
    properties["servername"].set("value", "beelink-staging-mxserver-1")
    properties["portnumber"].set("value", "5432")
    properties["url"].set("value", "jdbc:postgresql://beelink-staging-mxserver-1:5432/MXServer")
    temporary = path.with_suffix(".tmp")
    tree.write(temporary, encoding="utf-8", xml_declaration=True)
    shutil.copystat(path, temporary)
    temporary.replace(path)


def compose_config(apache_image, apache_environment, java_images):
    return {
        "name": "beelink-java-web",
        "services": {
            "glassfish": {
                "image": java_images[8], "restart": "no", "networks": ["isolated"],
                "environment": {"JAVA_HOME": JAVA_HOME, "AS_JAVA": JAVA_HOME, "TZ": "Europe/Berlin"},
                "entrypoint": [ASADMIN],
                "command": ["start-domain", "--verbose=true", "domain1"],
                "volumes": [
                    apps.mount(WORK / "glassfish", "/opt/glassfish-4.0"),
                    apps.mount(apps.ROOTFS / "srv/postgres/data/mxinfo",
                               "/srv/postgres/data/mxinfo", True),
                ],
            },
            "wildfly": {
                "image": java_images[11], "restart": "no", "networks": ["isolated"],
                "environment": {"JAVA_HOME": JAVA_HOME, "TZ": "Europe/Berlin"},
                "entrypoint": ["/opt/wildfly/bin/standalone.sh"],
                "command": ["-c", "standalone.xml", "-b", "0.0.0.0"],
                "volumes": [
                    apps.mount(WORK / "wildfly", "/opt/wildfly"),
                ],
            },
            "apache": {
                "image": apache_image, "restart": "no", "networks": ["isolated"],
                "environment": dict(apache_environment, MYSQL_HOST="beelink-staging-apache-mysql-1"),
                "volumes": [
                    apps.mount(WORK / "www", "/var/www/html"),
                    apps.mount(WORK / "vhosts", "/etc/apache2/sites-enabled", True),
                    apps.mount(apps.ROOTFS / "srv/www.docker/certs", "/etc/apache2/certs", True),
                    apps.mount(apps.ROOTFS / "srv/www.docker/php-config",
                               "/usr/local/etc/php/conf.d/custom", True),
                    apps.mount(WORK / "apache-logs", "/var/log/apache2"),
                ],
            },
        },
        "networks": {"isolated": {"external": True, "name": "beelink-staging_isolated"}},
    }


def upgrade_launcher(existing, desired):
    previous = json.loads(json.dumps(desired))
    previous["services"]["glassfish"]["entrypoint"] = ["/opt/glassfish-4.0/bin/asadmin"]
    if existing != previous:
        raise RuntimeError("Existing Java/web configuration differs; inspect before retrying")
    for service in desired["services"]:
        ids = apps.capture(*COMPOSE, "ps", "--all", "--quiet", service).splitlines()
        for container in ids:
            if apps.capture("docker", "inspect", "--format", "{{.State.Running}}", container) != "false":
                raise RuntimeError("Stop Java/web test containers before changing the launcher")
    print("Using standard GlassFish launcher with AS_JAVA; existing test data retained.", flush=True)


def patch_launcher(path):
    original = path.read_text()
    override = "JAVA=/opt/jdk1.8.0_201/bin/java"
    if original.splitlines().count(override) != 1:
        raise RuntimeError("Unexpected GlassFish Java override; inspect the copied launcher")
    updated = "\n".join(line for line in original.splitlines()
                        if line != override) + "\n"
    path.write_text(updated)


def prepare(apache_image):
    apache_image = apps.capture("docker", "image", "inspect", apache_image,
                                "--format", "{{.Id}}")
    inventory = json.loads((apps.SEED / "metadata/docker-inspect.json").read_text())
    source = next(container for container in inventory if container["Name"] == "/apache-apache-1")
    environment = dict(value.split("=", 1) for value in source["Config"]["Env"])
    environment = {key: value for key, value in environment.items()
                   if key.startswith(("MYSQL_", "APACHE_SERVER_NAME"))}
    java_images = {version: apps.capture(
        "docker", "image", "inspect", f"eclipse-temurin:{version}-jdk-jammy",
        "--format", "{{.Id}}") for version in (8, 11)}
    for image in (apache_image, *java_images.values()):
        apps.run("docker", "image", "inspect", image, stdout=subprocess.DEVNULL)
    config = compose_config(apache_image, environment, java_images)
    config_path = WORK / "compose.json"
    if config_path.exists():
        if not (WORK / "prepared").is_file():
            raise RuntimeError("Existing Java/web preparation is incomplete; inspect before retrying")
        existing = json.loads(config_path.read_text())
        if existing != config:
            upgrade_launcher(existing, config)
            patch_launcher(WORK / "glassfish/glassfish/bin/asadmin")
            apps.write_json(config_path, config)
        return
    if any((WORK / name).exists() for name in ("glassfish", "wildfly", "www", "vhosts")):
        raise RuntimeError("Unmanaged Java/web directories exist; inspect before retrying")
    for original, name in (
            ("opt/glassfish-4.0", "glassfish"),
            ("opt/wildfly-16.0.0.Final", "wildfly"),
            ("srv/www.docker/www", "www"), ("srv/www.docker/vhosts", "vhosts")):
        apps.run("rsync", "-aHAX", "--numeric-ids",
                 str(apps.ROOTFS / original) + "/", str(WORK / name) + "/",
                 stdout=subprocess.DEVNULL)
    (WORK / "apache-logs").mkdir()
    patch_launcher(WORK / "glassfish/glassfish/bin/asadmin")
    patch_glassfish(WORK / "glassfish/glassfish/domains/domain1/config/domain.xml")
    # WordPress constants and plugins can ignore MYSQL_HOST. Patch the actual
    # copied site configuration, never the seed or a production database.
    configs = sorted((WORK / "www").rglob("wp-config.php"))
    if not configs:
        raise RuntimeError("Expected copied WordPress configurations")
    for path in configs:
        apps.run("docker", "run", "--rm", "-i", "--network", "none", "--entrypoint", "php",
                 "--mount", f"type=bind,source={path.parent},target=/site",
                 apache_image, "-r", WORDPRESS_PATCH, "--", "/site/wp-config.php")
    apps.write_json(config_path, config)
    (WORK / "prepared").touch()


def verify():
    retired = (WORK / "copy-sites-retired").is_file()
    apps.run(*COMPOSE, "up", "-d", *(("glassfish", "wildfly") if retired else ()))
    # Reuse the same fail-fast readiness checks with this separate project.
    apps.COMPOSE = COMPOSE
    apps.wait_http("glassfish", ["curl", "--fail", "--silent", "--max-time", "10",
                                "http://127.0.0.1:8081/MXServer/"])
    verify_jdbc()
    apps.wait_http("wildfly", ["curl", "--fail", "--silent", "--max-time", "10",
                              "http://127.0.0.1:8080/"])
    deployments = json.loads(apps.capture(
        *COMPOSE, "exec", "-T", "wildfly", "/opt/wildfly/bin/jboss-cli.sh",
        "--connect", "--output-json",
        "--command=:read-children-resources(child-type=deployment,include-runtime=true)"))
    if deployments.get("outcome") != "success":
        raise RuntimeError("WildFly deployment query failed")
    for name in ("camera-rs.war", "jboss-kitchensink.war"):
        deployment = deployments.get("result", {}).get(name, {})
        if deployment.get("status") != "OK" or not deployment.get("enabled"):
            raise RuntimeError("WildFly deployment is not running: " + name)
    if not retired:
        verify_copy_sites()
    apps.write_json(WORK / "java-web-report.json", {
        "glassfish": "MXServer HTTP and direct JDBC query using configured credentials checked; "
                     "legacy admin TLS/pool ping not checked",
        "wildfly": "HTTP and both deployment runtime states checked",
        "apache": "Copy sites retired; original WordPress uses its separate Docker project" if retired
                  else "Configuration, both HTTP vhosts and WordPress database queries checked",
        "production_ready": False,
    })
    print("Isolated Java/web checks complete. TLS/proxies and final sync are separate checks.")


def verify_copy_sites():
    apps.run(*COMPOSE, "exec", "-T", "apache", "apache2ctl", "configtest")
    for domain, subdir in (("devcopy.mxtracks.info", "dev.mxtracks"),
                           ("wwwcopy.mxtracks.info", "mxdocs")):
        apps.wait_http("apache", ["curl", "--fail", "--silent", "--max-time", "15",
                                 "-H", "Host: " + domain, "http://127.0.0.1/"])
        configurations = sorted((WORK / "www" / subdir).rglob("wp-config.php"))
        if len(configurations) != 1:
            raise RuntimeError("Expected one WordPress installation for " + domain)
        relative = configurations[0].parent.relative_to(WORK / "www")
        apps.run(*COMPOSE, "exec", "-T", "apache", "wp", "--allow-root",
                 "--path=/var/www/html/" + str(relative), "--skip-plugins", "--skip-themes",
                 "eval", 'global $wpdb; if ((string) $wpdb->get_var("SELECT 1") !== "1") '
                 '{ WP_CLI::error("WordPress database connection failed"); }',
                 stdout=subprocess.DEVNULL)


def verify_jdbc():
    # Avoid the legacy admin TLS endpoint without weakening Java security.
    # The probe uses the application's Java, driver, URL and credentials.
    command = r"""
set -eu
work=$(mktemp -d /tmp/beelink-jdbc.XXXXXXXX)
trap 'rm -f "$work/BeelinkJdbcProbe.java" "$work/BeelinkJdbcProbe.class"; rmdir "$work"' EXIT
cat > "$work/BeelinkJdbcProbe.java"
"$JAVA_HOME/bin/javac" "$work/BeelinkJdbcProbe.java"
"$JAVA_HOME/bin/java" -cp "$work:/opt/glassfish-4.0/glassfish/domains/domain1/lib/*" BeelinkJdbcProbe
"""
    apps.run(*COMPOSE, "exec", "-T", "glassfish", "/bin/sh", "-c", command,
             input=JDBC_PROBE, text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-isolated", action="store_true", required=True)
    parser.add_argument("--apache-image", required=True, help="Local ID of the exact source Apache image")
    args = parser.parse_args()
    os.umask(0o077)
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    os.listdir("/daten")
    if apps.capture("findmnt", "-rn", "-M", "/daten", "-t", "ext4", "-o", "UUID") != \
            "c0c42461-ae4c-4edb-a993-bba94a05ed6c":
        raise RuntimeError("Expected encrypted T7 is not mounted")
    if (apps.RUNTIME.is_symlink() or apps.RUNTIME.stat().st_uid != 0
            or apps.RUNTIME.stat().st_mode & 0o077
            or not (apps.APPS / "applications-checked").is_file()):
        raise RuntimeError("Complete the private ownCloud/GitLab staging first")
    if shutil.disk_usage(apps.RUNTIME).free < 50 * 1024**3:
        raise RuntimeError("At least 50 GiB free space is required")
    network = json.loads(apps.capture(
        "docker", "network", "inspect", "beelink-staging_isolated"))[0]
    if not network["Internal"]:
        raise RuntimeError("Staging network must remain isolated")
    if WORK.is_symlink():
        raise RuntimeError("Java/web root must not be a symlink")
    WORK.mkdir(mode=0o700, exist_ok=True)
    with (WORK / "start.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        prepare(args.apache_image)
        try:
            verify()
        except BaseException:
            print("ERROR: Java/web staging failed; stopping this project's test services.", flush=True)
            apps.run(*COMPOSE, "stop")
            raise


if __name__ == "__main__":
    main()
