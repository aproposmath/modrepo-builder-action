import hashlib
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests


@dataclass
class ModMetadata:
    id: str
    version: str
    name: str
    author: str
    url: str
    digest: str

    branch: list[str]
    tag: list[str]
    depends_on: list[str]

    @staticmethod
    def from_about_xml(elem: str | ET.Element | Path, url: str, digest: str):
        """
        parse ModMetadata from xml string or Element
        """
        if isinstance(elem, Path):
            elem = elem.read_text()

        if isinstance(elem, str):
            elem = ET.fromstring(elem)

        def get_field(name) -> str:
            el = elem.findtext(name)
            if el is None:
                raise ValueError(f"Missing required <{name}> in About.xml")
            return el.strip()

        data = ModMetadata.read_data(elem)
        return ModMetadata(
            id=get_field("ModID"),
            version=get_field("Version"),
            name=get_field("Name"),
            author=get_field("Author"),
            tag=data["tag"],
            branch=data["branch"],
            depends_on=data["depends_on"],
            url=url,
            digest=digest,
        )

    @property
    def version_parsed(self):
        return parse_version(self.version)

    @staticmethod
    def read_data(elem: ET.Element) -> dict:
        tags_el = elem.find("Tags")
        tag = []
        if tags_el is not None:
            tags = [((t.text or "").strip()) for t in tags_el.findall("Tag")]
            tag = [t for t in tags if t]

        depends = [
            ((d.attrib.get("ModID") or d.attrib.get("WorkshopHandle") or "").strip())
            for d in elem.findall("DependsOn")
        ]
        depends_on = [d for d in depends if d]
        branch = list(set([b.text or "" for b in elem.findall("Branch")]))

        return {"tag": tag, "depends_on": depends_on, "branch": branch}

    def to_xml(self):
        elem = ET.Element("ModMetadata")
        ET.SubElement(elem, "ModID").text = self.id
        ET.SubElement(elem, "Version").text = self.version
        for branch in self.branch:
            ET.SubElement(elem, "Branch").text = branch
        return ET.tostring(elem, encoding="unicode")


def github(args: list[str]) -> object:
    out = subprocess.check_output(
        ["gh", *args],
        stderr=subprocess.PIPE,
        text=True,
    ).strip()
    return json.loads(out) if out else []


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def read_about_xml_from_zip(zip_path: Path) -> str | None:
    """
    Returns the content of About/About.xml if present, otherwise None.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        name = "About/About.xml"
        if name not in zf.namelist():
            return None
        with zf.open(name, "r") as fp:
            return fp.read().decode("utf-8", errors="replace")


def parse_version(s):
    s = s.strip()
    if not s:
        return [[("", 0, "")]]

    if s[0] in "vV":
        s = s[1:]

    def _cmp_key_str(x: str) -> str:
        # OrdinalIgnoreCase equivalent for our purposes: case-insensitive compare
        return (x or "").casefold()

    def _parse_part(part: str) -> tuple[str, int, str]:
        part = part or ""
        if not part:
            return ("", 0, "")

        # If starts with digit: prefix empty, parse leading digits as number, remainder as suffix
        if part[0].isdigit():
            m = re.match(r"^(\d+)(.*)$", part)
            if m:
                num_s, suffix = m.group(1), m.group(2)
                num = int(num_s) if num_s else 0
                return ("", num, _cmp_key_str(suffix))
            return ("", 0, "")

        # Otherwise: parse trailing digits as number (default 0 if none), rest is prefix
        m = re.match(r"^(.*?)(\d+)?$", part)
        if m:
            prefix, num_s = m.group(1), m.group(2)
            num = int(num_s) if num_s else 0
            return (_cmp_key_str(prefix), num, "")
        return (_cmp_key_str(part), 0, "")

    sections: list[list[tuple[str, int, str]]] = []
    for section in s.split("."):
        parts = [_parse_part(p) for p in section.split("-")]
        sections.append(parts)

    return sections


def get_release_data() -> list:
    owner, repo = os.environ["GITHUB_REPOSITORY"].strip().split("/", 1)
    return (
        github(
            [
                "api",
                f"/repos/{owner}/{repo}/releases",
                "--paginate",
            ]
        )
        or []
    )


def handle_asset(asset: dict, cache: dict, all_zip_digests: set) -> ModMetadata | None:
    name = asset.get("name", "")
    url = asset.get("browser_download_url", "")
    if not name.lower().endswith(".zip"):
        return
    if not url:
        return

    print("\tchecking asset", name)
    digest = asset.get("digest")

    if not digest:
        return

    all_zip_digests.add(digest)
    if digest in cache:
        metadata = cache[digest]
        if not metadata:
            return

        mm = ModMetadata(**metadata)
        print(
            f"\tfound mod in cache: id={mm.id}, name={mm.name}, version={mm.version}, branch={mm.branch}"
        )
        return mm

    # Download asset to temp dir using requests
    outdir = Path("_downloads") / f"asset_{digest.replace(":", "_")}"
    outdir.mkdir(parents=True, exist_ok=True)

    zip_path = outdir / name
    with requests.get(url, stream=True, timeout=10) as r:
        r.raise_for_status()
        with zip_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

    about_xml = read_about_xml_from_zip(zip_path)
    if not about_xml:
        cache[digest] = False
        return

    mm = ModMetadata.from_about_xml(about_xml, url, digest)
    mm.digest = sha256(zip_path)
    mm.url = url
    print(
        f"\tfound mod id={mm.id}, name={mm.name}, version={mm.version}, branch={mm.branch}"
    )
    cache[digest] = dict(mm.__dict__)


def main():
    # This runs during a GitHub Action workflow. Ensure GH CLI auth via:
    #   env: GH_TOKEN: ${{ github.token }}

    cache_file = Path("modrepo_cache.json")
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}

    print("Cache entries:", len(cache))

    releases = get_release_data()

    print(f"Loaded {len(releases)} new releases")

    entries: list[ModMetadata] = []

    all_zip_digests = set()

    for rel in releases:
        tag = rel.get("tag_name")
        assets = rel.get("assets") or []
        has_zip = any(((a.get("name") or "").lower().endswith(".zip")) for a in assets)

        if not has_zip:
            print(f"Skipping release {tag} as it has no zip file")
            continue

        print("handling release:", tag)

        assets = rel.get("assets") or []
        for asset in assets:
            try:
                mm = handle_asset(asset, cache, all_zip_digests)
                if mm:
                    entries.append(mm)
            except Exception as e:
                print(f"Error handling asset {asset.get('name')} in release {tag}: {e}")

    # write modrepo.xml
    modrepo = ET.Element("ModRepo")

    entries.sort(key=lambda t: (t.id, t.version_parsed, t.branch))

    for mm in entries:
        mv = ET.SubElement(modrepo, "ModVersion")
        mv.set("ModID", mm.id)
        mv.set("Version", mm.version)
        mv.set("Name", mm.name)
        mv.set("Author", mm.author)
        mv.set("Url", mm.url)
        mv.set("Digest", mm.digest)

        for branch in mm.branch:
            b = ET.SubElement(mv, "Branch")
            b.set("Value", branch)

    ET.indent(modrepo, space="  ", level=0)
    xml_str = ET.tostring(modrepo, encoding="unicode")

    # Add XML header for readability/compatibility if consumers expect it
    xml_out = '<?xml version="1.0" encoding="utf-8"?>\n' + xml_str + "\n"
    Path("modrepo.xml").write_text(xml_out, encoding="utf-8")

    # remove cache entries for zip files no longer present
    removed_digests = set(cache.keys()) - all_zip_digests
    for digest in removed_digests:
        del cache[digest]
    cache_file.write_text(json.dumps(cache, indent=4, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
