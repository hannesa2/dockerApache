#!/usr/bin/env python3
"""Suggest GitLab and Runner versions without changing the installation."""

import argparse
import json
import re
import subprocess
import sys
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


STOPS_URL = "https://gitlab.com/gitlab-org/gitlab/-/raw/master/config/upgrade_path.yml"
HUB = "https://hub.docker.com/v2/repositories/gitlab"
ARCHES = {"amd64", "arm64"}


def version(value):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise ValueError("Invalid version: " + value)
    return tuple(map(int, value.split(".")))


def fetch(url):
    request = Request(url, headers={"User-Agent": "beelink-gitlab-version-suggestion/1"})
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def required_stops(text):
    stops = []
    major = None
    for line in text.splitlines():
        if match := re.fullmatch(r"- major: ([0-9]+)", line):
            if major is not None:
                raise ValueError("Malformed GitLab upgrade stops")
            major = int(match.group(1))
        elif match := re.fullmatch(r"  minor: ([0-9]+)", line):
            if major is None:
                raise ValueError("Malformed GitLab upgrade stops")
            stops.append((major, int(match.group(1))))
            major = None
        elif line.strip() and not line.startswith(("#", "---", "  comments:")):
            raise ValueError("Unsupported GitLab upgrade stops format")
    if major is not None or not stops or stops != sorted(set(stops)):
        raise ValueError("Incomplete or unordered GitLab upgrade stops")
    return stops


def available_images(repository, prefix, pattern):
    url = f"{HUB}/{repository}/tags?page_size=100&name={quote(prefix)}"
    found = set()
    pages = 0
    while url:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "hub.docker.com":
            raise ValueError("Unexpected Docker Hub pagination URL")
        pages += 1
        if pages > 100:
            raise ValueError("Docker Hub pagination exceeds safety limit")
        data = json.loads(fetch(url))
        for tag in data["results"]:
            match = re.fullmatch(pattern, tag["name"])
            if not match or tag.get("tag_status") != "active":
                continue
            arches = {entry.get("architecture") for entry in tag["images"]
                      if entry.get("os") == "linux" and entry.get("status") == "active"}
            parsed_version = version(match.group(1))
            if ARCHES <= arches:
                found.add(parsed_version)
        url = data["next"]
    if not found:
        raise ValueError("No matching multi-architecture images for " + repository)
    return sorted(found)


def installed():
    def docker(*args):
        result = subprocess.run(["docker", *args], text=True, capture_output=True,
                                timeout=30, check=True)
        return result.stdout.strip()
    gitlab = version(docker("exec", "beelink-apps-gitlab-1", "cat",
                            "/opt/gitlab/embedded/service/gitlab-rails/VERSION"))
    runner_image = docker("inspect", "--format", "{{.Config.Image}}",
                          "beelink-docker-runner-runner-1")
    match = re.fullmatch(r"gitlab/gitlab-runner:v([0-9]+\.[0-9]+\.[0-9]+)", runner_image)
    if not match:
        raise ValueError("Running Runner container does not use a pinned official image")
    return gitlab, version(match.group(1))


def gitlab_targets(current, gitlab_images, stops):
    if current < (17, 5, 0):
        raise ValueError("Versions before 17.5 require manual upgrade planning")
    if not any(stop[0] == current[0] for stop in stops):
        raise ValueError("Official stop data does not cover the installed major version")
    candidates = [v for v in gitlab_images if v[0] == current[0]]
    if not candidates or max(candidates) < current:
        raise ValueError("Installed GitLab is newer than the available image data")
    target = max(candidates)
    if target == current:
        return []
    minors = sorted({stop for stop in stops
                     if current[:2] <= stop < target[:2]})
    targets = []
    for minor in minors:
        matches = [v for v in candidates if v[:2] == minor]
        if not matches:
            raise ValueError("No multi-architecture GitLab image for required stop "
                             + ".".join(map(str, minor)))
        stop = max(matches)
        if stop > current:
            targets.append(stop)
    targets.append(target)
    return targets


def suggest_runners(runner, targets, runner_images):
    runners = []
    previous = runner
    for stop in targets:
        matches = [v for v in runner_images if v[:2] == stop[:2] and v >= previous]
        if not matches:
            raise ValueError("No matching Runner image without downgrade for "
                             + ".".join(map(str, stop)))
        previous = max(matches)
        runners.append(previous)
    return runners


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gitlab", help="Override installed GitLab version for planning")
    parser.add_argument("--runner", help="Override installed Runner version for planning")
    args = parser.parse_args()
    if bool(args.gitlab) != bool(args.runner):
        parser.error("--gitlab and --runner must be supplied together")
    current, runner = ((version(args.gitlab), version(args.runner))
                       if args.gitlab else installed())
    stops = required_stops(fetch(STOPS_URL))
    gitlab_images = available_images("gitlab-ce", f"{current[0]}.",
                                     r"([0-9]+\.[0-9]+\.[0-9]+)-ce\.0")
    targets = gitlab_targets(current, gitlab_images, stops)
    runner_images = set()
    for major, minor in {v[:2] for v in targets}:
        runner_images.update(available_images("gitlab-runner", f"v{major}.{minor}.",
                                               r"v([0-9]+\.[0-9]+\.[0-9]+)"))
    runners = suggest_runners(runner, targets, runner_images)
    fmt = lambda value: ".".join(map(str, value))
    print(f"Installed GitLab: {fmt(current)}; Runner: {fmt(runner)}")
    print("Scope: latest available multi-architecture images within the installed GitLab major version.")
    if not targets:
        print("GitLab is already at the latest available version in this major release.")
        return
    print("--versions " + " ".join(map(fmt, targets)))
    print("--runner-versions " + " ".join(map(fmt, runners)))
    print("Suggestion only: check GitLab's upgrade path and version-specific notes manually,")
    print("including conditional stops and migrations, before using --upgrade-path-reviewed.")
    print("Upgrade path: https://docs.gitlab.com/update/upgrade_paths/")
    print("Stop data: " + STOPS_URL)
    print("Images: https://hub.docker.com/r/gitlab/gitlab-ce/tags and "
          "https://hub.docker.com/r/gitlab/gitlab-runner/tags")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        sys.exit(1)
