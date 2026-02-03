import hashlib
import json
import os
import requests
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from datetime import datetime

class ModMetadata:
    id: str
    _version: str
    name: str
    author: str
    url: str
    digest: str

    branch: str
    tag: list[str]
    depends_on: list[str]

    @staticmethod
    def from_about_xml(elem: str | ET.Element | Path):
        """
        parse ModMetadata from xml string or Element
        """
        if isinstance(elem, Path):
            elem = elem.read_text()

        if isinstance(elem, str):
            elem = ET.fromstring(elem)

        mm = ModMetadata()

        def get_field(name) -> str:
            el = elem.findtext(name)
            if el is None:
                raise ValueError(f"Missing  required <{name}> in About.xml")
            return el.strip()

        mm.id = get_field("ModID")
        mm.version = get_field("Version")
        mm.name = get_field("Name")
        mm.author = get_field("Author")

        mm.read_data(elem)

        return mm

    @staticmethod
    def from_modrepo(el: ET.Element):
        mm = ModMetadata()

        mm.id = el.attrib["ModID"]
        mm.version = el.attrib["Version"]
        mm.name = el.attrib["Name"]
        mm.author = el.attrib["Author"]
        mm.url = el.attrib["Url"]
        mm.digest = el.attrib["Digest"]
        mm.read_data(el)
        return mm

    @property
    def version(self):
        return self._version

    @version.setter
    def version(self, version: str):
        self._version = version
        self.version_parsed = parse_version(version)

    def read_data(self, elem: ET.Element):
        tags_el = elem.find("Tags")
        self.tag = []
        if tags_el:
            tags = [((t.text or "").strip()) for t in elem.findall("Tag")]
            self.tag = [t for t in tags if t]

        depends = [
            ((d.attrib.get("ModID") or d.attrib.get("WorkshopHandle") or "").strip())
            for d in elem.findall("DependsOn")
        ]
        self.depends_on = [d for d in depends if d]

        # If multiple <Branch> entries exist, prefer the last one; default to empty string
        branches = [b.text or "" for b in elem.findall("Branch")]
        branch = branches[-1] if branches else ""
        self.branch = branch.strip()

    def to_xml(self):
        elem = ET.Element("ModMetadata")
        ET.SubElement(elem, "ModID").text = self.id
        ET.SubElement(elem, "Version").text = self.version
        ET.SubElement(elem, "Branch").text = self.branch or ""
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
    return h.hexdigest()


def read_about_xml_from_zip(zip_path: Path) -> str | None:
    """
    Returns the content of About/About.xml if present, otherwise None.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        # Normalize to forward slashes as zip uses them regardless of OS
        candidates = [n for n in zf.namelist() if n.endswith("About/About.xml")]
        if not candidates:
            return None
        # Prefer exact path if present
        name = "About/About.xml" if "About/About.xml" in candidates else candidates[0]
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


def read_existing_modrepo():
    path = Path("modrepo.xml")

    modrepo_date = datetime.fromisoformat("1970-01-01T00:00:00Z")

    if not path.exists():
        return modrepo_date, []

    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except Exception:
        return modrepo_date, []

    if root.tag != "ModRepo":
        mr = root.find("ModRepo")
        if mr is None:
            return modrepo_date, []
        root = mr

    modrepo_date = datetime.fromisoformat(
        subprocess.check_output(
            ["git", "log", "-1", "--format=%cI", "--", str(path)],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    )

    return modrepo_date, [
        ModMetadata.from_modrepo(mv) for mv in root.findall("ModVersion")
    ]


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


def main():
    # This runs during a GitHub Action workflow. Ensure GH CLI auth via:
    #   env: GH_TOKEN: ${{ github.token }}

    print("ModRepo Builder")
    last_build_time, old_releases = read_existing_modrepo()
    
    print(f"Loaded {len(old_releases)} old releases")
    
    old_releases_by_tag = {mr.tag: mr for mr in old_releases if getattr(mr, "tag", None)}

    releases = get_release_data()
    
    print(f"Loaded {len(releases)} new releases")
    
    print(releases)

    entries: list[ModMetadata] = []

    for rel in releases:
        tag = rel.get("tag_name")
        assets = rel.get("assets") or []
        print("assets", assets)
        has_zip = any(((a.get("name") or "").lower().endswith(".zip")) for a in assets)
        print("has_zip", has_zip)
        
        # if datetime.fromisoformat(rel["updated_at"].strip()) < last_build_time:
        #     old_release = old_releases_by_tag.get(tag)
        #     if old_release is not None:
        #         print(f"Using old release {tag}")
        #         entries.append(old_release)
        #         continue
        #     # else:
        #     #     print(f"Skipping old release file {tag}")
        
        if not has_zip:
            print(f"Skipping release {tag} as it has no zip file")
            continue
            
        print("checking new release:", tag)

        assets = rel.get("assets") or []
        tmp = Path("_downloads")
        for i, asset in enumerate(assets):
            name = asset.get("name", "")
            url = asset.get("browser_download_url", "")
            if not name.lower().endswith(".zip"):
                continue
            if not url:
                continue
                
            print("\tchecking asset", name)

            # Download asset to temp dir using requests
            outdir = tmp / f"asset_{i}"
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
                continue

            mm = ModMetadata.from_about_xml(about_xml)
            mm.digest = sha256(zip_path)
            mm.url = url
            print(f"\tfound mod id={mm.id}, name={mm.name}, version={mm.version}, branch={mm.branch}")
            entries.append(mm)

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

        b = ET.SubElement(mv, "Branch")
        b.set("Value", mm.branch or "")

    ET.indent(modrepo, space="  ", level=0)
    xml_str = ET.tostring(modrepo, encoding="unicode")

    # Add XML header for readability/compatibility if consumers expect it
    xml_out = '<?xml version="1.0" encoding="utf-8"?>\n' + xml_str + "\n"
    Path("modrepo.xml").write_text(xml_out, encoding="utf-8")


if __name__ == "__main__":
    main()
