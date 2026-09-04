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
        self.assertIn("trevorswanson/libvirt-balloon-keeper/main", plugin_url)
        self.assertIn("/issues", support)
        self.assertIn("trevorswanson/libvirt-balloon-keeper", project)
        self.assertTrue(overview.strip())
        self.assertIn("loopback HTTP API on port 8765", overview)
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

    def test_plugin_installer_is_thin_verified_lifecycle_wrapper(self):
        text = (UNRAID / "libvirt-balloon-keeper.plg").read_text()
        self.assertIn("releases/download/&version;/libvirt-balloon-keeper.tar.gz", text)
        self.assertIn("<SHA256>d9c3e7bad17538508d490788b9dd8d9bca892aa948f789f8027bbcd24122e2f7</SHA256>", text)
        self.assertIn("<URL>", text)
        self.assertNotIn("releases/latest", text)
        self.assertNotIn("curl --fail", text)
        self.assertIn("2026.09.02", text)
        self.assertIn("bash /tmp/libvirt-balloon-keeper-release/unraid/lifecycle.sh install", text)
        self.assertIn("/boot/config/plugins/libvirt-balloon-keeper/lifecycle.sh uninstall", text)
        self.assertNotIn("127.0.0.1", text)
        self.assertNotRegex(text, r"(password|token|secret)\\s*=", re.IGNORECASE)

    def test_release_workflow_is_tag_driven_and_publishes_stable_assets(self):
        text = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn("tags:", text)
        self.assertIn("'*'", text)
        self.assertIn("actions/checkout@v4", text)
        self.assertIn("gh release create", text)
        self.assertIn("--verify-tag", text)
        self.assertNotIn("--verify-tag \"$TAG\"", text)

    def test_test_workflow_builds_and_uploads_package(self):
        text = (ROOT / ".github/workflows/test.yml").read_text()
        self.assertIn("package:", text)
        self.assertIn("cmp", text)
        self.assertIn("sha256sum -c", text)
        self.assertIn("actions/upload-artifact@v4", text)


if __name__ == "__main__":
    unittest.main()
