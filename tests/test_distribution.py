import pathlib
import re
import unittest
import xml.etree.ElementTree as ET


ROOT = pathlib.Path(__file__).resolve().parents[1]
UNRAID = ROOT / "unraid"


class DistributionMetadataTests(unittest.TestCase):
    def test_community_application_metadata_is_well_formed_and_points_to_new_repo(self):
        # These are repository-controlled static metadata files, not user input.
        tree = ET.parse(ROOT / "plugins/libvirt-balloon-keeper.xml")
        root = tree.getroot()
        plugin_url = root.findtext("PluginURL") or ""
        support = root.findtext("Support") or ""
        project = root.findtext("Project") or ""
        overview = root.findtext("Overview") or ""
        icon = root.findtext("Icon") or ""
        self.assertEqual(root.tag, "Plugin")
        self.assertEqual(root.findtext("Name"), "Libvirt Balloon Keeper")
        self.assertEqual(plugin_url, "https://raw.githubusercontent.com/trevorswanson/libvirt-balloon-keeper/main/unraid/libvirt-balloon-keeper.plg")
        self.assertEqual(root.findtext("ReadMe"), "https://raw.githubusercontent.com/trevorswanson/libvirt-balloon-keeper/main/README.md")
        self.assertEqual(icon, "https://raw.githubusercontent.com/trevorswanson/libvirt-balloon-keeper/main/unraid/libvirt-balloon-keeper.png")
        self.assertIn("/issues", support)
        self.assertIn("trevorswanson/libvirt-balloon-keeper", project)
        self.assertTrue(overview.strip())
        self.assertIn("mode-restricted Unix socket", overview)
        self.assertIn("once-per-minute cron check", overview)
        self.assertIn("persistent appdata directory", overview)
        self.assertTrue(icon.endswith("libvirt-balloon-keeper.png"))

    def test_community_application_profile_is_well_formed(self):
        root = ET.parse(ROOT / "ca_profile.xml").getroot()
        profile = root.findtext("Profile") or ""
        webpage = root.findtext("WebPage") or ""
        forum = root.findtext("Forum") or ""
        self.assertEqual(root.tag, "CommunityApplications")
        self.assertTrue(profile.strip())
        self.assertIn("trevorswanson/libvirt-balloon-keeper", webpage)
        self.assertIn("/issues", forum)

    def test_plugin_catalog_matches_plugin_manifest_url(self):
        catalog = ET.parse(ROOT / "plugins/libvirt-balloon-keeper.xml").getroot()
        manifest = ET.parse(UNRAID / "libvirt-balloon-keeper.plg").getroot()
        self.assertEqual(catalog.findtext("PluginURL"), manifest.attrib["pluginURL"])
        self.assertEqual(catalog.findtext("PluginAuthor"), manifest.attrib["author"])

    def test_plugin_manifest_uses_only_manager_read_file_actions(self):
        manifest = UNRAID / "libvirt-balloon-keeper.plg"
        text = manifest.read_text()
        # The manifest is repository-controlled static XML, never user input.
        # Its local DOCTYPE entities are required by Unraid's PLG format.
        root = ET.parse(manifest).getroot()
        files = list(root.findall("FILE"))
        self.assertEqual(len(files), 4)
        self.assertEqual(root.findall("INSTALL"), [])
        self.assertEqual(root.findall("REMOVE"), [])

        self.assertEqual(files[0].attrib["Run"], "/bin/bash")
        self.assertIsNotNone(files[0].find("INLINE"))
        self.assertEqual(files[1].attrib["Name"], "/boot/config/plugins/libvirt-balloon-keeper/libvirt-balloon-keeper.tar.gz")
        self.assertEqual(
            files[1].findtext("URL"),
            "https://github.com/trevorswanson/libvirt-balloon-keeper/releases/download/2026.09.03/libvirt-balloon-keeper.tar.gz",
        )
        self.assertEqual(
            files[1].findtext("SHA256"),
            "0b32ab45239d09f079c07d8a39eb1c41ed034d0f935bcecdcb0a6c7e391e1b7b",
        )
        self.assertEqual(files[2].attrib["Run"], "/bin/bash")
        self.assertNotIn("Method", files[2].attrib)
        self.assertIn("lifecycle.sh", files[2].findtext("INLINE") or "")
        self.assertEqual(files[3].attrib, {"Run": "/bin/bash", "Method": "remove"})
        self.assertIn("lifecycle.sh", files[3].findtext("INLINE") or "")

        self.assertNotIn("/tmp/libvirt-balloon-keeper.tar.gz", text)
        self.assertNotIn("releases/latest", text)
        self.assertNotIn("curl --fail", text)
        self.assertNotIn("127.0.0.1", text)
        self.assertNotRegex(text, r"(password|token|secret)\\s*=", re.IGNORECASE)

    def test_plugin_manifest_uses_documented_file_action_attributes(self):
        root = ET.parse(UNRAID / "libvirt-balloon-keeper.plg").getroot()
        for element in root.findall("FILE"):
            self.assertTrue(set(element.attrib) <= {"Name", "Run", "Method"})
            if "Run" in element.attrib:
                self.assertEqual(element.attrib["Run"], "/bin/bash")
                self.assertIsNotNone(element.find("INLINE"))

    def test_release_workflow_is_tag_driven_and_publishes_stable_assets(self):
        text = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn("tags:", text)
        self.assertIn("'*'", text)
        self.assertIn("actions/checkout@v4", text)
        self.assertIn("gh release create", text)
        self.assertIn("--verify-tag", text)
        self.assertIn('EXPECTED="$(sed -n', text)
        self.assertIn('test "$ACTUAL" = "$EXPECTED"', text)
        self.assertNotIn("--verify-tag \"$TAG\"", text)

    def test_test_workflow_builds_and_uploads_package(self):
        text = (ROOT / ".github/workflows/test.yml").read_text()
        self.assertIn("package:", text)
        self.assertIn("cmp", text)
        self.assertIn("sha256sum -c", text)
        self.assertIn('test "$ACTUAL" = "$EXPECTED"', text)
        self.assertIn("actions/upload-artifact@v4", text)

    def test_package_builder_uses_deterministic_gzip(self):
        text = (UNRAID / "build-package.sh").read_text()
        self.assertIn("gzip.GzipFile", text)
        self.assertIn("mtime=0", text)
        self.assertIn("tarfile.USTAR_FORMAT", text)


if __name__ == "__main__":
    unittest.main()
